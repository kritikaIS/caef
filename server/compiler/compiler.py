"""Deterministic compiler — validated manifest in, controller artifact out.

RESEARCH.md §4. Three properties define this module:

  - **Deterministic.** The same (manifest, registry version, policy) triple
    always produces byte-identical output. No clock, no uuid, no dict iteration
    over an unordered set, no model call.
  - **Template-only.** Every emitted fragment comes from a registry capability's
    prevalidated template. Model-authored text never enters the artifact; the
    manifest reaches this module already parsed into typed fields.
  - **Refuses rather than improvises.** A capability combination that matches no
    compilation pattern is rejected. Inventing a plausible controller for an
    unrecognised request is exactly the failure mode the manifest exists to
    remove.

A pattern is the mapping from "what was requested" to "which rules get built".
Adding one is a reviewed code change, which is the point.
"""

import math
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, Field

import config
from server.compiler import templates
from server.compiler.program import (
    ControllerProgram,
    IntentSpec,
    ProgramBudget,
    Rule,
    SensorBinding,
)
from server.manifest.models import BehaviorManifest, Condition, FallbackKind, Operator
from server.manifest.registry import CapabilityRegistry
from server.schemas import HardwareSchema

PROGRAM_VERSION = "1.0"
# The metric the thermal patterns are written against. Named once here rather
# than inline at four call sites.
THERMAL_METRIC = "temperature_c"


class CompilationRejected(ValueError):
    """No pattern covers this request, or a template could not build it."""


class CompilerReport(BaseModel):
    """DATA_SCHEMAS.md §13a — what the compiler did, or why it refused."""

    status: Literal["pass", "fail"]
    manifest_id: str
    manifest_hash: str
    pattern: str | None = None
    artifact_hash: str | None = None
    capability_registry_version: str
    capability_registry_hash: str
    capabilities_used: list[str] = Field(default_factory=list)
    pins_used: list[int] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # A deterministic text listing of the compiled rules, for the audit trail
    # and the operator dashboard. It is a *rendering*, never executed and never
    # deployed — the artifact is the JSON program.
    rendering: str = ""

    @property
    def reason(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None


@dataclass(frozen=True)
class CompilationResult:
    status: Literal["pass", "fail"]
    program: ControllerProgram | None
    report: CompilerReport


# --- compilation patterns ----------------------------------------------------


@dataclass(frozen=True)
class Pattern:
    """A recognised shape of request and the rules it compiles to."""

    pattern_id: str
    required: frozenset[str]
    optional: frozenset[str]
    build: Callable[["BuildContext"], list[Rule]]


@dataclass(frozen=True)
class BuildContext:
    manifest: BehaviorManifest
    registry: CapabilityRegistry
    intents: dict[str, IntentSpec]  # capability name -> its emitted intent

    def has(self, capability: str) -> bool:
        return capability in self.manifest.requested_capabilities

    def intent(self, capability: str) -> IntentSpec:
        return self.intents[capability]


def _critical_rule(context: BuildContext) -> list[Rule]:
    """Report a reading at or above the supervisor's emergency threshold.

    Telemetry only. Reporting is never the local response to overheating — the
    supervisor's own policy is, and it does not wait for anyone to answer
    (RESEARCH.md §7 rule 1).
    """
    if not context.has("emit_critical_event"):
        return []
    if THERMAL_METRIC not in context.manifest.sensor_inputs:
        return []
    return [
        Rule(
            rule_id="r_critical",
            description="report a reading at or above the emergency threshold",
            condition=Condition(
                metric=THERMAL_METRIC, operator=Operator.GE, value=config.EMERGENCY_TEMP_C
            ),
            emit=[context.intent("emit_critical_event")],
        )
    ]


def _context_rule(context: BuildContext) -> list[Rule]:
    """Raise the manifest's own trigger event while its situation holds."""
    if not context.has("emit_context_event"):
        return []
    return [
        Rule(
            rule_id="r_context",
            description=f"raise {context.manifest.trigger_event} while the situation holds",
            condition=context.manifest.activation_condition,
            emit=[context.intent("emit_context_event")],
        )
    ]


def _heartbeat_rule(context: BuildContext) -> list[Rule]:
    if not context.has("emit_heartbeat"):
        return []
    return [
        Rule(
            rule_id="r_heartbeat",
            description="liveness beat on every control step",
            condition=None,
            emit=[context.intent("emit_heartbeat")],
        )
    ]


def _thermal_cooling(context: BuildContext) -> list[Rule]:
    """Activation turns cooling on, recovery turns it off.

    The validator has already proved the two conditions are disjoint, so no
    step can match both and the ordering below is a presentation detail rather
    than a tie-break.
    """
    return [
        Rule(
            rule_id="r_activate",
            description="cool while the activation condition holds",
            condition=context.manifest.activation_condition,
            emit=[context.intent("fan_on")],
        ),
        Rule(
            rule_id="r_recover",
            description="stop cooling once the recovery condition holds",
            condition=context.manifest.recovery_condition,
            emit=[context.intent("fan_off")],
        ),
        *_critical_rule(context),
        *_context_rule(context),
        *_heartbeat_rule(context),
    ]


def _monitor_only(context: BuildContext) -> list[Rule]:
    return [
        *_context_rule(context),
        *_critical_rule(context),
        *_heartbeat_rule(context),
    ]


def _safe_idle(context: BuildContext) -> list[Rule]:
    return [
        Rule(
            rule_id="r_idle",
            description="hand control back to the supervisor's safe state every step",
            condition=None,
            emit=[context.intent("enter_safe_idle")],
        ),
        *_heartbeat_rule(context),
    ]


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        pattern_id="thermal_cooling_v1",
        required=frozenset({"read_temperature", "fan_on", "fan_off"}),
        optional=frozenset(
            {"emit_heartbeat", "emit_context_event", "emit_critical_event", "enter_safe_idle"}
        ),
        build=_thermal_cooling,
    ),
    Pattern(
        pattern_id="monitor_only_v1",
        required=frozenset({"read_temperature", "emit_heartbeat"}),
        optional=frozenset(
            {"emit_context_event", "emit_critical_event", "read_distance", "enter_safe_idle"}
        ),
        build=_monitor_only,
    ),
    Pattern(
        pattern_id="safe_idle_v1",
        required=frozenset({"enter_safe_idle"}),
        optional=frozenset({"emit_heartbeat", "emit_critical_event", "read_temperature"}),
        build=_safe_idle,
    ),
)


def select_pattern(requested: set[str]) -> Pattern:
    """First pattern whose requirements are met and which covers everything asked.

    Both directions matter. `required ⊆ requested` stops a pattern being built
    without the capabilities its rules reference; `requested ⊆ required|optional`
    stops a capability being silently ignored — a manifest that asked for
    something the pattern has no rule for did not get what it asked for, and
    quietly dropping it is exactly the improvisation this module refuses.
    """
    for pattern in PATTERNS:
        if pattern.required <= requested and requested <= (pattern.required | pattern.optional):
            return pattern
    raise CompilationRejected(
        "no compilation pattern covers capabilities "
        f"{sorted(requested)}; known patterns: "
        + ", ".join(
            f"{pattern.pattern_id}(requires {sorted(pattern.required)})" for pattern in PATTERNS
        )
    )


# --- fallback ----------------------------------------------------------------


def _fallback_intents(context: BuildContext) -> list[IntentSpec]:
    """What `shutdown(reason)` emits.

    `restore_previous_firmware` emits nothing: restoring a slot is the
    supervisor's action, not a behaviour the controller performs. The program
    still records the choice so the device knows which one it is (RESEARCH.md §10).
    """
    match context.manifest.fallback_behavior:
        case FallbackKind.ENTER_SAFE_IDLE:
            return [context.intent("enter_safe_idle")]
        case FallbackKind.HOLD_COOLING:
            return [context.intent("fan_on")]
        case FallbackKind.RESTORE_PREVIOUS_FIRMWARE:
            return []
    raise CompilationRejected(  # pragma: no cover - enum is exhaustive
        f"unhandled fallback {context.manifest.fallback_behavior}"
    )


# --- rendering ---------------------------------------------------------------


def render(program: ControllerProgram) -> str:
    """A deterministic text listing of the artifact, for audit and the dashboard.

    Not code and not executed anywhere — the deployable artifact is the JSON
    program. This exists so a reviewer can read what was built without parsing
    JSON by eye.
    """
    lines = [
        f"# controller {program.manifest_id} (pattern {program.pattern})",
        f"# device={program.device_id} registry={program.capability_registry_version} "
        f"period={program.control_period_seconds}s lease={program.maximum_duration_seconds}s",
        "sensors:",
        *(
            f"  {binding.metric} <- {binding.capability}(GPIO_{binding.pin})"
            for binding in program.sensors
        ),
        "rules:",
    ]
    for rule in program.rules:
        condition = "always" if rule.condition is None else rule.condition.describe()
        emitted = ", ".join(
            f"{spec.actuator}={spec.state}" if spec.kind == "actuator" else
            f"emit {spec.event}" if spec.kind == "telemetry" else
            f"{spec.capability}"
            for spec in rule.emit
        )
        lines.append(f"  [{rule.rule_id}] when {condition} -> {emitted}")
    fallback = ", ".join(spec.capability for spec in program.fallback) or "(supervisor action)"
    lines.append(f"fallback ({program.fallback_behavior}): {fallback}")
    lines.append(f"min_hold_ticks: {program.min_hold_ticks}")
    return "\n".join(lines)


# --- entry point -------------------------------------------------------------


def compile_manifest(
    manifest: BehaviorManifest,
    registry: CapabilityRegistry,
    schema: HardwareSchema,
) -> CompilationResult:
    """Compile a validated manifest. Never raises: a refusal is a `fail` report.

    The caller has already run the validator; the checks repeated here (unknown
    capability, registry version) are the ones whose absence would let this
    module build something incoherent, and they are cheap.
    """

    def _fail(*errors: str) -> CompilationResult:
        report = CompilerReport(
            status="fail",
            manifest_id=manifest.manifest_id,
            manifest_hash=manifest.manifest_hash,
            capability_registry_version=registry.capability_registry_version,
            capability_registry_hash=registry.content_hash,
            errors=list(errors),
        )
        return CompilationResult(status="fail", program=None, report=report)

    if manifest.capability_registry_version != registry.capability_registry_version:
        return _fail(
            f"manifest declares registry {manifest.capability_registry_version!r}, "
            f"compiler loaded {registry.capability_registry_version!r}"
        )

    unknown = [name for name in manifest.requested_capabilities if registry.get(name) is None]
    if unknown:
        return _fail(f"unknown capabilities: {', '.join(sorted(unknown))}")

    context = TemplateContextFactory(manifest, registry)
    try:
        sensors, intents = context.build_fragments()
        pattern = select_pattern(set(manifest.requested_capabilities))
        build_context = BuildContext(manifest=manifest, registry=registry, intents=intents)
        rules = pattern.build(build_context)
        fallback = _fallback_intents(build_context)
    except (CompilationRejected, templates.TemplateError, KeyError) as exc:
        return _fail(str(exc).strip("'"))

    if not rules:
        return _fail(f"pattern {pattern.pattern_id} produced no rules for this manifest")

    # Anything the compiler is about to reference must be wired to a real,
    # permitted pin. The validator checked this already; repeating it here means
    # a compiler invoked directly — as the experiment harness does — cannot
    # produce an artifact that reaches a forbidden pin.
    for pin in _referenced_pins(sensors, rules, fallback):
        # Forbidden first: a denylisted pin is often absent from the pinout too,
        # and "not in the schema" would be the less useful of the two truths.
        if schema.is_forbidden(pin):
            return _fail(f"GPIO_{pin} is forbidden on this device")
        if schema.pin(pin) is None:
            return _fail(f"GPIO_{pin} is not present in the hardware schema")

    program = ControllerProgram(
        program_version=PROGRAM_VERSION,
        pattern=pattern.pattern_id,
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        event_id=manifest.event_id,
        device_id=manifest.device_id,
        source_firmware_hash=manifest.current_firmware_hash,
        capability_registry_version=registry.capability_registry_version,
        capability_registry_hash=registry.content_hash,
        control_period_seconds=manifest.control_period_seconds,
        maximum_duration_seconds=manifest.maximum_duration_seconds,
        emergency_temp_c=config.EMERGENCY_TEMP_C,
        sensors=sensors,
        rules=rules,
        fallback=fallback,
        fallback_behavior=manifest.fallback_behavior.value,
        resource_budget=_budget(manifest, registry),
        min_hold_ticks=_min_hold_ticks(manifest, registry),
    )
    report = CompilerReport(
        status="pass",
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        pattern=pattern.pattern_id,
        artifact_hash=program.artifact_hash,
        capability_registry_version=registry.capability_registry_version,
        capability_registry_hash=registry.content_hash,
        capabilities_used=program.capabilities_used(),
        pins_used=program.pins_used(),
        rules=[rule.rule_id for rule in program.rules],
        rendering=render(program),
    )
    return CompilationResult(status="pass", program=program, report=report)


class TemplateContextFactory:
    """Turns each requested capability into its template fragment, once."""

    def __init__(self, manifest: BehaviorManifest, registry: CapabilityRegistry) -> None:
        self.manifest = manifest
        self.registry = registry

    def build_fragments(self) -> tuple[list[SensorBinding], dict[str, IntentSpec]]:
        context = templates.TemplateContext(manifest=self.manifest)
        sensors: list[SensorBinding] = []
        intents: dict[str, IntentSpec] = {}
        # Sorted, not manifest order: the artifact must not depend on the order
        # the model happened to list its capabilities in.
        for name in sorted(self.manifest.requested_capabilities):
            capability = self.registry.require(name)
            fragment = templates.build(name, capability, context)
            if isinstance(fragment, SensorBinding):
                sensors.append(fragment)
            else:
                intents[name] = fragment
        return sensors, intents


def _referenced_pins(
    sensors: list[SensorBinding], rules: list[Rule], fallback: list[IntentSpec]
) -> list[int]:
    pins = {binding.pin for binding in sensors}
    pins |= {spec.pin for rule in rules for spec in rule.emit if spec.pin is not None}
    pins |= {spec.pin for spec in fallback if spec.pin is not None}
    return sorted(pins)


def _budget(manifest: BehaviorManifest, registry: CapabilityRegistry) -> ProgramBudget:
    cpu = sum(
        registry.require(name).resource_cost.cpu_ms_per_step
        for name in manifest.requested_capabilities
    )
    memory = sum(
        registry.require(name).resource_cost.memory_kb
        for name in manifest.requested_capabilities
    )
    return ProgramBudget(
        max_cpu_ms_per_step=manifest.resource_budget.max_cpu_ms_per_step,
        max_memory_kb=manifest.resource_budget.max_memory_kb,
        max_actuator_transitions_per_minute=(
            manifest.resource_budget.max_actuator_transitions_per_minute
        ),
        estimated_cpu_ms_per_step=round(cpu, 6),
        estimated_memory_kb=memory,
    )


def _min_hold_ticks(manifest: BehaviorManifest, registry: CapabilityRegistry) -> int:
    """Steps an actuator must hold its state before it may change again.

    Taken from the strictest registry limit among the requested capabilities, so
    a manifest cannot shorten it — the hold is the registry's property, not the
    proposal's.
    """
    holds = [
        registry.require(name).actuator_limits.min_hold_seconds
        for name in manifest.requested_capabilities
        if registry.require(name).actuator_limits is not None
    ]
    if not holds:
        return 0
    return max(1, math.ceil(max(holds) / manifest.control_period_seconds))
