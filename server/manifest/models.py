"""Behavior Manifest — the declarative contract the model is allowed to propose.

`DATA_SCHEMAS.md §10`. A manifest is **data, never code**: it names registered
capabilities and states conditions as typed structures. There is no field in
which an expression, an import, a shell command or a Python fragment can be
placed and later evaluated — conditions are `{metric, operator, value}` triples
compared numerically by a hand-written evaluator, and metric names must match a
bare-identifier pattern before any of it is looked at (RESEARCH.md §2).

Three model settings carry the weight:

  - `extra="forbid"` — an unknown field is a rejection, not a field the model
    smuggled past a permissive parser.
  - `strict=True`   — `"80.0"` is not a float and `"true"` is not a bool. A
    string where a number belongs is exactly the shape an injection attempt
    takes, so it must not be silently coerced into something valid.
  - `frozen=True`   — a validated manifest cannot be edited afterwards; the
    hash taken at validation stays the hash that gets compiled and signed.
"""

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import config
from server.manifest.canonical import content_hash
from server.schemas import TriggerType

# Identifier shapes. Everything the model may name — a metric, a capability, an
# actuator — must match one of these before it is looked up anywhere, so a
# lookup can never be handed an arbitrary string.
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
HEX_HASH = re.compile(r"^[0-9a-f]{8,64}$")
# manifest_id / event_id: UUIDs or the short deterministic ids the stub agent
# mints. Constrained so an id cannot carry a path or a separator.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)

Identifier = Annotated[str, Field(pattern=IDENTIFIER.pattern)]


def _enum(kind):
    """An enum field that accepts its own string value.

    Strict mode otherwise demands an already-constructed enum member, which no
    JSON payload can supply. Relaxing it here keeps the set closed — anything
    outside the enum is still a rejection — while letting `">="` mean `Operator.GE`
    on the wire.
    """
    return Annotated[kind, Field(strict=False)]


class Operator(StrEnum):
    """The complete set of comparisons a manifest can express.

    Deliberately closed: an operator is selected from this enum, never parsed
    from a string, so `"or 1==1"` is a validation error rather than a clause.
    """

    GE = ">="
    LE = "<="
    GT = ">"
    LT = "<"
    EQ = "=="
    NE = "!="


class FallbackKind(StrEnum):
    """What the device does when the contract ends or the controller faults."""

    ENTER_SAFE_IDLE = "enter_safe_idle"
    HOLD_COOLING = "hold_cooling"
    RESTORE_PREVIOUS_FIRMWARE = "restore_previous_firmware"


class Condition(BaseModel):
    """A single numeric comparison against one named metric."""

    model_config = _STRICT

    metric: Identifier
    operator: _enum(Operator)
    value: float

    def evaluate(self, metrics: dict[str, float]) -> bool:
        """Deterministic evaluation. A missing metric is False, never an error:
        the supervisor must keep running when a sensor drops out."""
        reading = metrics.get(self.metric)
        if reading is None:
            return False
        match self.operator:
            case Operator.GE:
                return reading >= self.value
            case Operator.LE:
                return reading <= self.value
            case Operator.GT:
                return reading > self.value
            case Operator.LT:
                return reading < self.value
            case Operator.EQ:
                return reading == self.value
            case Operator.NE:
                return reading != self.value
        raise AssertionError(f"unhandled operator {self.operator}")  # pragma: no cover

    def describe(self) -> str:
        return f"{self.metric} {self.operator.value} {self.value}"


class ConditionGroup(BaseModel):
    """A one-level conjunction or disjunction of `Condition`s.

    Exactly one of `all_of`/`any_of` is set and nesting is not permitted: the
    validator has to decide whether activation and recovery can hold at the same
    time, and that stays decidable only while the shape is this flat.
    """

    model_config = _STRICT

    all_of: list[Condition] | None = None
    any_of: list[Condition] | None = None

    @model_validator(mode="after")
    def _exactly_one_branch(self) -> "ConditionGroup":
        chosen = [branch for branch in (self.all_of, self.any_of) if branch is not None]
        if len(chosen) != 1:
            raise ValueError("set exactly one of all_of / any_of")
        terms = chosen[0]
        if not terms:
            raise ValueError("condition group must contain at least one condition")
        if len(terms) > config.MANIFEST_MAX_CONDITION_TERMS:
            raise ValueError(
                f"condition group exceeds {config.MANIFEST_MAX_CONDITION_TERMS} terms"
            )
        return self

    @property
    def terms(self) -> list[Condition]:
        return list(self.all_of or self.any_of or [])

    def evaluate(self, metrics: dict[str, float]) -> bool:
        if self.all_of is not None:
            return all(term.evaluate(metrics) for term in self.all_of)
        return any(term.evaluate(metrics) for term in (self.any_of or []))

    def describe(self) -> str:
        joiner = " AND " if self.all_of is not None else " OR "
        return "(" + joiner.join(term.describe() for term in self.terms) + ")"


Trigger = Condition | ConditionGroup


def trigger_terms(trigger: Trigger) -> list[Condition]:
    return [trigger] if isinstance(trigger, Condition) else trigger.terms


def trigger_metrics(trigger: Trigger) -> set[str]:
    return {term.metric for term in trigger_terms(trigger)}


class ResourceBudget(BaseModel):
    """What one control step is allowed to cost.

    Enforced twice: the validator checks the manifest's budget against the
    summed registry cost of the capabilities it requests, and the runtime aborts
    a step that exceeds it (RESEARCH.md §6, property 10).
    """

    model_config = _STRICT

    max_cpu_ms_per_step: float = Field(gt=0, le=1000)
    max_memory_kb: int = Field(gt=0, le=65536)
    max_actuator_transitions_per_minute: int = Field(gt=0, le=600)


class BehaviorManifest(BaseModel):
    """The complete proposal. Nothing outside these fields survives parsing."""

    model_config = _STRICT

    manifest_version: Literal["1.0"]
    manifest_id: Annotated[str, Field(pattern=SAFE_ID.pattern)]
    device_id: Annotated[str, Field(pattern=SAFE_ID.pattern)]
    event_id: Annotated[str, Field(pattern=SAFE_ID.pattern)]
    trigger_type: _enum(TriggerType)
    trigger_event: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]
    current_firmware_hash: Annotated[str, Field(pattern=HEX_HASH.pattern)]
    capability_registry_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    requested_capabilities: list[Identifier] = Field(min_length=1)
    sensor_inputs: list[Identifier] = Field(default_factory=list)
    actuator_outputs: list[Identifier] = Field(default_factory=list)
    activation_condition: Trigger
    recovery_condition: Trigger
    maximum_duration_seconds: int = Field(gt=0)
    control_period_seconds: float = Field(gt=0)
    resource_budget: ResourceBudget
    fallback_behavior: _enum(FallbackKind)
    rationale: str = Field(min_length=1)

    @field_validator("requested_capabilities", "sensor_inputs", "actuator_outputs")
    @classmethod
    def _no_duplicates(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("duplicate entries are not permitted")
        if len(values) > config.MANIFEST_MAX_CAPABILITIES:
            raise ValueError(f"at most {config.MANIFEST_MAX_CAPABILITIES} entries")
        return values

    @field_validator("rationale")
    @classmethod
    def _plain_text_rationale(cls, value: str) -> str:
        """The rationale is stored and displayed, never executed — but it is the
        one free-text field, so it is length-capped and stripped of control
        characters rather than trusted."""
        if len(value) > config.MANIFEST_MAX_RATIONALE_CHARS:
            raise ValueError(
                f"rationale exceeds {config.MANIFEST_MAX_RATIONALE_CHARS} characters"
            )
        if any(ord(character) < 32 and character not in "\n\t" for character in value):
            raise ValueError("rationale contains control characters")
        return value

    @property
    def manifest_hash(self) -> str:
        """Canonical hash of the manifest itself. Stable across serializations
        and carried into the compiled artifact and the signed package."""
        return content_hash(self.model_dump(mode="json"))

    def metrics_used(self) -> set[str]:
        return trigger_metrics(self.activation_condition) | trigger_metrics(
            self.recovery_condition
        )
