"""Deterministic property verifier (RESEARCH.md §6).

Runs a compiled controller through the seeded scenarios and decides a fixed list
of properties over the resulting traces. No model is involved, no wall clock is
read, and nothing is sampled — the same artifact, scenarios and seeds always
produce the same report.

Three rules keep the report honest:

  - **A property that cannot be met is skipped, not passed.** In a scenario
    where cooling is physically insufficient, the temperature bound is recorded
    as `skipped` with its reason. A pass the controller did not earn would make
    the whole report worthless.
  - **A failure carries its counterexample.** The report includes the trace rows
    around the failing tick, so a rejection can be understood without re-running.
  - **The Docker sandbox is not evidence of any of this.** It shows a candidate
    did not crash in a window. Behavioural claims come from here (SAFETY_PROTOCOL
    §3 remains true about what the sandbox is; it is simply not this).

A failed verification never reaches the signer — that gate is in the pipeline,
and it is tested there.
"""

from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, Field

import config
from edge_node.supervisor import SafetySupervisor, SafetyState
from server.compiler.program import ControllerProgram
from server.compiler.runtime import CompiledController, ControllerContext
from server.manifest.models import Condition, Operator, trigger_terms
from server.manifest.registry import CapabilityRegistry
from server.schemas import HardwareSchema
from server.sim import scenarios as scenario_registry
from server.sim.harness import RunResult, run_scenario
from server.sim.scenarios import Scenario
from server.sim.world import ThermalWorld
from edge_node.supervisor import EMERGENCY_METRIC

# How many trace rows either side of a failure the counterexample carries.
COUNTEREXAMPLE_MARGIN = 5


class PropertyResult(BaseModel):
    name: str
    description: str
    status: Literal["pass", "fail", "skipped"]
    detail: str = ""
    counterexample_tick: int | None = None


class ResourceUse(BaseModel):
    steps: int
    cpu_ms_total: float
    cpu_ms_per_step: float
    memory_kb: int
    artifact_bytes: int


class SupervisorSummary(BaseModel):
    accepted: int
    rejected: int
    emergency_activations: int
    safe_state_entries: int
    rejections_by_reason: dict[str, int] = Field(default_factory=dict)


class VerificationReport(BaseModel):
    """DATA_SCHEMAS.md §16 — one scenario, one seed, one verdict."""

    scenario: str
    seed: int
    manifest_id: str
    artifact_hash: str
    status: Literal["pass", "fail"]
    properties: list[PropertyResult]
    counterexample: list[dict] = Field(default_factory=list)
    peak_device_temp_c: float
    peak_sensor_temp_c: float
    activation_latency_ticks: int | None = None
    recovery_time_ticks: int | None = None
    actuator_transitions: int = 0
    transitions_per_minute: float = 0.0
    time_above_critical_ticks: int = 0
    resource_use: ResourceUse
    supervisor: SupervisorSummary
    controller_faulted: bool = False
    fault_reason: str | None = None

    @property
    def failures(self) -> list[PropertyResult]:
        return [prop for prop in self.properties if prop.status == "fail"]


class VerificationSuite(BaseModel):
    manifest_id: str
    artifact_hash: str
    status: Literal["pass", "fail"]
    seeds: list[int]
    scenarios: list[str]
    reports: list[VerificationReport]

    @property
    def failed_reports(self) -> list[VerificationReport]:
        return [report for report in self.reports if report.status == "fail"]

    def summary(self) -> str:
        failed = self.failed_reports
        if not failed:
            return f"{len(self.reports)} runs passed"
        first = failed[0]
        names = ", ".join(prop.name for prop in first.failures)
        return (
            f"{len(failed)}/{len(self.reports)} runs failed; first: "
            f"{first.scenario} seed={first.seed} ({names})"
        )


# --- reading the contract back out of the artifact ---------------------------


def _threshold(program: ControllerProgram, rule_id: str, operators: set[Operator]) -> float | None:
    """The temperature threshold a compiled rule fires on.

    Read off the artifact rather than the manifest: the artifact is what runs,
    and a verifier that checked the proposal instead of the product would be
    verifying the wrong thing.
    """
    for rule in program.rules:
        if rule.rule_id != rule_id or rule.condition is None:
            continue
        for term in trigger_terms(rule.condition):
            if term.metric == EMERGENCY_METRIC and term.operator in operators:
                return term.value
    return None


def activation_threshold(program: ControllerProgram) -> float | None:
    return _threshold(program, "r_activate", {Operator.GE, Operator.GT})


def recovery_threshold(program: ControllerProgram) -> float | None:
    return _threshold(program, "r_recover", {Operator.LE, Operator.LT})


def _fan_on_ticks(result: RunResult) -> list[int]:
    return [
        decision.intent.tick
        for decision in result.decisions
        if decision.accepted
        and decision.intent.kind == "actuator"
        and decision.intent.state == "on"
    ]


# --- the properties ----------------------------------------------------------

@dataclass(frozen=True)
class Ctx:
    """Everything a property is decided against.

    `expect_cooling` is the caller's claim about what this artifact is *for*.
    Inferred from the artifact by default — a program with no cooling rule
    cannot be held to a cooling latency — but the pipeline sets it explicitly
    when the manifest was a response to a heat event, so "the model answered an
    overheating device with a monitoring controller" is a verification failure
    rather than a vacuous pass.
    """

    program: ControllerProgram
    scenario: Scenario
    result: RunResult
    schema: HardwareSchema
    registry: CapabilityRegistry
    expect_cooling: bool


Property = Callable[[Ctx], PropertyResult]


def _prop(name: str, description: str, ok: bool, detail: str = "", tick: int | None = None):
    return PropertyResult(
        name=name,
        description=description,
        status="pass" if ok else "fail",
        detail=detail,
        counterexample_tick=None if ok else tick,
    )


def _skip(name: str, description: str, why: str) -> PropertyResult:
    return PropertyResult(name=name, description=description, status="skipped", detail=why)


def prop_cooling_latency(ctx: Ctx) -> PropertyResult:
    """Did the firmware cool the device, soon enough, and before the supervisor
    had to step in on its own?

    Three clauses, because any one of them alone is satisfiable by a controller
    that achieves nothing:

      - it commanded cooling at all in a scenario that overheats;
      - it did so within the configured latency of *its own* declared threshold;
      - it did so before the local emergency policy had to intervene. A morph
        whose thresholds are set so high that the supervisor gets there first is
        not adapting; it is being rescued.
    """
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    name = "cooling_latency"
    description = (
        f"cooling is commanded within {config.VERIFY_ACTIVATION_LATENCY_TICKS} ticks of "
        "the activation threshold, and before the supervisor intervenes"
    )
    threshold = activation_threshold(program)
    if threshold is None:
        if ctx.expect_cooling:
            return _prop(
                name, description, False,
                "this artifact declares no cooling rule, but it was compiled in response "
                "to a situation that requires one",
            )
        return _skip(name, description, "this controller has no cooling rule")
    if not scenario.expects_activation:
        return _skip(name, description, "this scenario never reaches the threshold")

    commanded = _fan_on_ticks(result)
    emergency_tick = result.first_tick_where(lambda row: row.emergency_active)

    if not commanded:
        return _prop(
            name, description, False,
            f"the controller never commanded cooling in a scenario that overheats "
            f"(its threshold is {threshold}C; peak reading was "
            f"{result.peak_sensor_temp_c}C)",
            emergency_tick or scenario.ticks,
        )

    if emergency_tick is not None and commanded[0] > emergency_tick:
        return _prop(
            name, description, False,
            f"the supervisor's emergency policy engaged at tick {emergency_tick}, before "
            f"the firmware commanded cooling at tick {commanded[0]}",
            emergency_tick,
        )

    crossed = result.first_tick_where(lambda row: row.sensor_temp_c >= threshold)
    if crossed is None:
        return _prop(
            name, description, False,
            f"the controller's own threshold of {threshold}C was never reached in a "
            f"scenario that overheats (peak reading {result.peak_sensor_temp_c}C)",
            commanded[0],
        )

    after = [tick for tick in commanded if tick >= crossed]
    if not after:
        return _prop(name, description, False,
                     f"cooling was commanded at {commanded[0]} but the threshold was not "
                     f"read until {crossed}", crossed)
    latency = after[0] - crossed
    return _prop(
        name,
        description,
        latency <= config.VERIFY_ACTIVATION_LATENCY_TICKS,
        f"threshold {threshold}C read at tick {crossed}, cooling commanded at "
        f"{after[0]} (latency {latency} ticks)",
        after[0],
    )


def prop_declared_capabilities_only(ctx: Ctx) -> PropertyResult:
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "declared_capabilities_only"
    description = "no intent uses a capability the artifact did not declare"
    declared = set(program.capabilities_used())
    for decision in result.decisions:
        if decision.intent.capability not in declared:
            return _prop(
                name,
                description,
                False,
                f"intent {decision.intent.intent_id} used undeclared "
                f"{decision.intent.capability!r}",
                decision.intent.tick,
            )
    return _prop(name, description, True, f"all intents within {sorted(declared)}")


def prop_pins_within_schema(ctx: Ctx) -> PropertyResult:
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "pins_within_schema"
    description = "no accepted intent touches a pin outside the hardware schema"
    for decision in result.decisions:
        pin = decision.intent.pin
        if pin is None or not decision.accepted:
            continue
        if schema.pin(pin) is None:
            return _prop(name, description, False, f"GPIO_{pin} is not in the schema",
                         decision.intent.tick)
        if schema.is_forbidden(pin):
            return _prop(name, description, False, f"GPIO_{pin} is forbidden",
                         decision.intent.tick)
        permitted = registry.require(decision.intent.capability).permitted_pins
        if pin not in permitted:
            return _prop(
                name, description, False,
                f"GPIO_{pin} is outside {decision.intent.capability}'s permitted {permitted}",
                decision.intent.tick,
            )
    return _prop(name, description, True, f"pins used: {program.pins_used()}")


def prop_actuator_envelope(ctx: Ctx) -> PropertyResult:
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "actuator_envelope"
    description = "accepted actuator intents stay inside the registry and schema envelope"
    limit = schema.constraints.max_current_ma()
    for decision in result.decisions:
        intent = decision.intent
        if not decision.accepted or intent.kind != "actuator":
            continue
        if limit is not None and (intent.current_ma or 0.0) > limit:
            return _prop(name, description, False,
                         f"{intent.current_ma}mA exceeds {limit}mA", intent.tick)
        spec = registry.actuators.get(intent.actuator or "")
        if spec is None or intent.state not in spec.states:
            return _prop(name, description, False,
                         f"{intent.actuator}={intent.state} is not a registered state",
                         intent.tick)
    return _prop(name, description, True, f"max declared draw within {limit}mA")


def prop_supervisor_integrity(ctx: Ctx) -> PropertyResult:
    """The controller cannot disable the supervisor.

    This holds *by construction*: the artifact is data, the intent language has
    exactly three kinds, and none of them addresses the supervisor. The check
    below is a regression test for that construction, not an experiment whose
    outcome is in doubt — if a future capability kind appeared that could touch
    the supervisor, this is what would catch it.
    """
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "supervisor_integrity"
    description = "no intent kind can address the supervisor, the registry or the schema"
    allowed_kinds = {"actuator", "telemetry", "safety"}
    for rule in program.rules:
        for spec in rule.emit:
            if spec.kind not in allowed_kinds:
                return _prop(name, description, False, f"intent kind {spec.kind!r} is not one "
                             f"of {sorted(allowed_kinds)}")
    for decision in result.decisions:
        if decision.intent.kind not in allowed_kinds:
            return _prop(name, description, False,
                         f"intent {decision.intent.intent_id} claims kind "
                         f"{decision.intent.kind!r}", decision.intent.tick)
    return _prop(name, description, True, "artifact is data; no instruction addresses the supervisor")


def prop_critical_temperature(ctx: Ctx) -> PropertyResult:
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "critical_temperature_bound"
    description = f"device temperature stays below {config.CRITICAL_TEMP_C}C"
    if not scenario.cooling_is_sufficient:
        return _skip(
            name,
            description,
            f"cooling cannot physically hold this scenario below the limit "
            f"({scenario.description})",
        )
    peak = result.peak_device_temp_c
    tick = result.first_tick_where(lambda row: row.device_temp_c >= config.CRITICAL_TEMP_C)
    return _prop(name, description, peak < config.CRITICAL_TEMP_C, f"peak {peak}C", tick)


def prop_oscillation_bound(ctx: Ctx) -> PropertyResult:
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "oscillation_bound"
    ceiling = config.VERIFY_MAX_ACTUATOR_TRANSITIONS_PER_MIN
    description = f"actuator changes stay under {ceiling} per simulated minute"
    minutes = max((result.rows[-1].time_s if result.rows else 0.0) / 60.0, 1e-9)
    rate = result.actuator_transitions / minutes
    return _prop(name, description, rate <= ceiling,
                 f"{result.actuator_transitions} transitions over {minutes:.2f} min "
                 f"({rate:.1f}/min)")


def prop_finite_lease(ctx: Ctx) -> PropertyResult:
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "finite_lease"
    description = f"the firmware lease is finite and at most {config.MAX_LEASE_SECONDS}s"
    lease = program.maximum_duration_seconds
    return _prop(name, description, 0 < lease <= config.MAX_LEASE_SECONDS, f"lease {lease}s")


def prop_fallback_reachable(ctx: Ctx) -> PropertyResult:
    """Actually reach the fallback, rather than trusting that it is declared.

    A fresh world and a fresh supervisor, so this measures the fallback and not
    whatever state the scenario happened to end in.
    """
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "fallback_reachable"
    description = "the declared fallback behaviour executes and is accepted"
    if not program.fallback:
        ok = program.fallback_behavior == "restore_previous_firmware"
        return _prop(
            name, description, ok,
            "restoring the previous slot is the supervisor's action, not the controller's"
            if ok else f"fallback {program.fallback_behavior!r} emits nothing",
        )

    world = ThermalWorld(scenario.world)
    supervisor = SafetySupervisor(port=world, schema=schema, registry=registry)
    supervisor.install_program(program)
    controller = CompiledController(program)
    controller.initialize(
        ControllerContext(device_id=program.device_id, started_at_s=0.0,
                          actuator_states=dict(world.actuators))
    )
    decisions = supervisor.apply(
        controller.shutdown("verification probe"), world.metrics(), world.time_s
    )
    rejected = [decision for decision in decisions if not decision.accepted]
    return _prop(name, description, not rejected,
                 "; ".join(decision.reason for decision in rejected) or "fallback accepted")


def prop_control_budget(ctx: Ctx) -> PropertyResult:
    program, scenario, result = ctx.program, ctx.scenario, ctx.result
    schema, registry = ctx.schema, ctx.registry
    name = "control_budget"
    description = "the controller terminates or yields within its declared control budget"
    budget = program.resource_budget
    if budget.estimated_cpu_ms_per_step > budget.max_cpu_ms_per_step:
        return _prop(name, description, False,
                     f"{budget.estimated_cpu_ms_per_step}ms/step exceeds "
                     f"{budget.max_cpu_ms_per_step}ms")
    # A run longer than the lease must have yielded — unless the controller
    # stopped being stepped at all, which is what a fault means. Losing control
    # to the supervisor is a stronger form of yielding than yielding, and
    # demanding both would fail every crash scenario for the wrong reason.
    run_seconds = result.rows[-1].time_s if result.rows else 0.0
    stopped_early = result.faulted or result.supervisor.state is not SafetyState.NORMAL
    if (
        run_seconds > program.maximum_duration_seconds
        and not result.controller.yielded
        and not stopped_early
    ):
        return _prop(name, description, False,
                     f"ran {run_seconds}s past a {program.maximum_duration_seconds}s lease "
                     "without yielding")
    return _prop(name, description, True,
                 f"{budget.estimated_cpu_ms_per_step}ms/step within "
                 f"{budget.max_cpu_ms_per_step}ms over {result.controller.steps_taken} steps")


PROPERTIES: tuple[Property, ...] = (
    prop_cooling_latency,
    prop_declared_capabilities_only,
    prop_pins_within_schema,
    prop_actuator_envelope,
    prop_supervisor_integrity,
    prop_critical_temperature,
    prop_oscillation_bound,
    prop_finite_lease,
    prop_fallback_reachable,
    prop_control_budget,
)


# --- running -----------------------------------------------------------------


def _metrics(program: ControllerProgram, result: RunResult) -> dict:
    activation = activation_threshold(program)
    recovery = recovery_threshold(program)

    latency = None
    recovery_time = None
    if activation is not None:
        crossed = result.first_tick_where(lambda row: row.sensor_temp_c >= activation)
        commanded = [tick for tick in _fan_on_ticks(result) if crossed is not None and tick >= crossed]
        if crossed is not None and commanded:
            latency = commanded[0] - crossed
            if recovery is not None:
                recovered = result.first_tick_where(
                    lambda row: row.tick > commanded[0] and row.sensor_temp_c < recovery
                )
                if recovered is not None:
                    recovery_time = recovered - commanded[0]

    minutes = max((result.rows[-1].time_s if result.rows else 0.0) / 60.0, 1e-9)
    above_critical = sum(
        1 for row in result.rows if row.device_temp_c >= config.CRITICAL_TEMP_C
    )
    return {
        "activation_latency_ticks": latency,
        "recovery_time_ticks": recovery_time,
        "transitions_per_minute": round(result.actuator_transitions / minutes, 3),
        "time_above_critical_ticks": above_critical,
    }


def _counterexample(result: RunResult, tick: int | None) -> list[dict]:
    if tick is None:
        return []
    low = max(0, tick - COUNTEREXAMPLE_MARGIN - 1)
    high = tick + COUNTEREXAMPLE_MARGIN
    return [row.as_dict() for row in result.rows[low:high]]


def verify_scenario(
    program: ControllerProgram,
    scenario: Scenario,
    schema: HardwareSchema,
    registry: CapabilityRegistry,
    seed: int | None = None,
    expect_cooling: bool | None = None,
) -> VerificationReport:
    # Judge the artifact under the conditions it will actually be installed
    # into, and over the period its lease actually covers.
    scenario = scenario.for_verification(program.maximum_duration_seconds)
    result = run_scenario(program, scenario, schema, registry, seed=seed)
    context = Ctx(
        program=program,
        scenario=scenario,
        result=result,
        schema=schema,
        registry=registry,
        expect_cooling=(
            activation_threshold(program) is not None
            if expect_cooling is None
            else expect_cooling
        ),
    )
    properties = [prop(context) for prop in PROPERTIES]
    failures = [prop for prop in properties if prop.status == "fail"]
    first_failure_tick = next(
        (prop.counterexample_tick for prop in failures if prop.counterexample_tick is not None),
        None,
    )
    from server.manifest.canonical import canonical_bytes

    return VerificationReport(
        scenario=scenario.name,
        seed=result.seed,
        manifest_id=program.manifest_id,
        artifact_hash=program.artifact_hash,
        status="fail" if failures else "pass",
        properties=properties,
        counterexample=_counterexample(result, first_failure_tick),
        peak_device_temp_c=round(result.peak_device_temp_c, 3),
        peak_sensor_temp_c=round(result.peak_sensor_temp_c, 3),
        actuator_transitions=result.actuator_transitions,
        resource_use=ResourceUse(
            steps=result.controller.steps_taken,
            cpu_ms_total=round(result.controller.cpu_ms_used, 3),
            cpu_ms_per_step=program.resource_budget.estimated_cpu_ms_per_step,
            memory_kb=program.resource_budget.estimated_memory_kb,
            artifact_bytes=len(canonical_bytes(program.model_dump(mode="json"))),
        ),
        supervisor=SupervisorSummary(
            accepted=result.supervisor.counters.accepted,
            rejected=result.supervisor.counters.rejected,
            emergency_activations=result.supervisor.counters.emergency_activations,
            safe_state_entries=result.supervisor.counters.safe_state_entries,
            rejections_by_reason=dict(result.supervisor.counters.rejections_by_reason),
        ),
        controller_faulted=result.faulted,
        fault_reason=result.fault_reason,
        **_metrics(program, result),
    )


def verify(
    program: ControllerProgram,
    schema: HardwareSchema,
    registry: CapabilityRegistry,
    scenario_names: list[str] | None = None,
    seeds: list[int] | None = None,
    expect_cooling: bool | None = None,
) -> VerificationSuite:
    """Verify an artifact across every required scenario and seed.

    Fails if *any* run fails: a controller that is safe on four seeds out of five
    is not a controller anyone should install.
    """
    names = list(scenario_names or scenario_registry.VERIFICATION_SCENARIOS)
    seed_list = list(seeds or [config.SIM_DEFAULT_SEED])
    reports = [
        verify_scenario(
            program,
            scenario_registry.get(name),
            schema,
            registry,
            seed=seed,
            expect_cooling=expect_cooling,
        )
        for name in names
        for seed in seed_list
    ]
    return VerificationSuite(
        manifest_id=program.manifest_id,
        artifact_hash=program.artifact_hash,
        status="fail" if any(report.status == "fail" for report in reports) else "pass",
        seeds=seed_list,
        scenarios=names,
        reports=reports,
    )
