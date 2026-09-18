"""Controller program — the compiled artifact, and the intents it emits.

`DATA_SCHEMAS.md §13/§14`. The artifact a manifest compiles into is **canonical
JSON data**, not source code: an ordered list of sensor bindings and rules, each
rule a typed condition and a fixed list of intent specifications drawn from
registry templates.

That choice is the load-bearing one in this design. Because the artifact is
data interpreted by a hand-written runtime that ships with the device, "firmware
cannot modify the supervisor, the watchdog, the schema or the rollback path" is
true by construction — there is no instruction in this language that expresses
it, and nothing model-authored is ever passed to `exec`, `eval`, `compile` or an
import (RESEARCH.md §4/§7).

A controller returns `ActuatorIntent`s. It never touches a driver; the
supervisor decides what actually happens (RESEARCH.md §7).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from server.manifest.canonical import content_hash
from server.manifest.models import Trigger

_FROZEN = ConfigDict(extra="forbid", frozen=True)

IntentKind = Literal["actuator", "telemetry", "safety"]


class IntentSpec(BaseModel):
    """A compile-time description of one intent a rule emits.

    Every field is filled in from the capability's registry entry — the compiler
    copies limits rather than restating them, so a limit can only be changed by
    editing the registry.
    """

    model_config = _FROZEN

    capability: str
    kind: IntentKind
    actuator: str | None = None
    state: str | None = None
    pin: int | None = None
    event: str | None = None
    trigger_type: str | None = None
    current_ma: float | None = None
    min_hold_seconds: float = 0.0
    max_transitions_per_minute: int | None = None


class SensorBinding(BaseModel):
    model_config = _FROZEN

    capability: str
    metric: str
    pin: int


class Rule(BaseModel):
    """`when condition then emit intents`. `condition: None` means every step."""

    model_config = _FROZEN

    rule_id: str
    description: str
    condition: Trigger | None = None
    emit: list[IntentSpec]


class ProgramBudget(BaseModel):
    model_config = _FROZEN

    max_cpu_ms_per_step: float
    max_memory_kb: int
    max_actuator_transitions_per_minute: int
    # Summed registry cost of everything this program evaluates in one step.
    # Compile-time constant, so the runtime's budget check is a comparison, not
    # a measurement — a wall-clock measurement would make the run
    # non-reproducible, which is the property the whole simulation rests on.
    estimated_cpu_ms_per_step: float
    estimated_memory_kb: int


class ControllerProgram(BaseModel):
    """The deployable artifact. Hashed canonically; the hash is what gets signed."""

    model_config = _FROZEN

    program_version: Literal["1.0"]
    pattern: str
    manifest_id: str
    manifest_hash: str
    event_id: str
    device_id: str
    source_firmware_hash: str
    capability_registry_version: str
    capability_registry_hash: str
    control_period_seconds: float
    maximum_duration_seconds: int
    # The supervisor's emergency threshold as it stood at compile time. Recorded
    # so an artifact can be audited against the policy it was built under; the
    # supervisor still enforces its own live value, never this copy.
    emergency_temp_c: float
    sensors: list[SensorBinding]
    rules: list[Rule]
    fallback: list[IntentSpec]
    fallback_behavior: str
    resource_budget: ProgramBudget
    # Minimum steps between two changes of the same actuator, derived from the
    # registry's `min_hold_seconds` and the control period. This is what stops
    # noise around a threshold becoming relay chatter (RESEARCH.md §6 property 7).
    min_hold_ticks: int

    @property
    def artifact_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def capabilities_used(self) -> list[str]:
        names = {binding.capability for binding in self.sensors}
        names |= {spec.capability for rule in self.rules for spec in rule.emit}
        names |= {spec.capability for spec in self.fallback}
        return sorted(names)

    def pins_used(self) -> list[int]:
        pins = {binding.pin for binding in self.sensors}
        pins |= {
            spec.pin
            for rule in self.rules
            for spec in rule.emit
            if spec.pin is not None
        }
        pins |= {spec.pin for spec in self.fallback if spec.pin is not None}
        return sorted(pins)


class ActuatorIntent(BaseModel):
    """What a controller returns and the supervisor consumes.

    One model for all three kinds so there is exactly one channel between
    replaceable firmware and the immutable layer; `kind` says whether it is an
    actuator command, a telemetry emission or a request to hand control back.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    manifest_id: str
    capability: str
    kind: IntentKind
    tick: int
    reason: str
    actuator: str | None = None
    state: str | None = None
    pin: int | None = None
    event: str | None = None
    trigger_type: str | None = None
    current_ma: float | None = None
    value: float | None = None

    def describe(self) -> str:
        if self.kind == "actuator":
            return f"{self.actuator}={self.state}"
        if self.kind == "telemetry":
            return f"telemetry:{self.event}"
        return f"safety:{self.capability}"


class IntentDecision(BaseModel):
    """The supervisor's per-intent verdict (DATA_SCHEMAS.md §14).

    Recorded for every intent, accepted or not: a rejection is the evidence that
    the supervisor did its job, and the verifier reads these to decide whether a
    controller ever tried something outside its contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: ActuatorIntent
    accepted: bool
    reason: str
