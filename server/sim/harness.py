"""Simulation harness — one tick loop, shared by the verifier and the demo.

RESEARCH.md §5/§6. The order inside a tick is the design, not an implementation
detail, so it lives in one place and everything drives it:

    1. the world advances                      (physics, seeded)
    2. sensors are read                        (what the firmware can know)
    3. the supervisor's local policy runs      (emergency cooling, safe state)
    4. the controller is stepped               (only if it still has control)
    5. the supervisor considers its intents    (accept or reject, one by one)

Step 3 before step 4 is what makes "emergency policy outranks adaptive
firmware" a property of the loop rather than a race between two writers.

The harness records a row per tick containing both the true device state and
what the sensor claimed, plus every intent and every decision. That row is the
counterexample when a property fails, so it has to carry enough to explain the
failure without re-running anything.
"""

from dataclasses import asdict, dataclass, field

import config
from edge_node.supervisor import SafetyState, SafetySupervisor, SupervisorPolicy
from server.compiler.program import ActuatorIntent, ControllerProgram, IntentDecision
from server.compiler.runtime import (
    CompiledController,
    ControllerContext,
    ControllerFault,
    Observation,
)
from server.manifest.registry import CapabilityRegistry
from server.schemas import HardwareSchema
from server.sim.scenarios import Scenario
from server.sim.world import ThermalWorld


class InjectedControllerFault(ControllerFault):
    """A controller runtime failure, injected at a scenario's chosen tick.

    Honest about what it models: a compiled controller is an interpreted data
    structure and does not spontaneously crash, so a crash has to be injected to
    test what the supervisor does about one. The scenario is testing the
    supervisor's response, not the controller's fragility.
    """


class CrashingController:
    """Wraps a controller and faults at a given tick."""

    def __init__(self, inner: CompiledController, crash_tick: int) -> None:
        self.inner = inner
        self.crash_tick = crash_tick
        self.program = inner.program

    def initialize(self, context: ControllerContext) -> None:
        self.inner.initialize(context)

    def step(self, observation: Observation) -> list[ActuatorIntent]:
        if observation.tick >= self.crash_tick:
            raise InjectedControllerFault(
                f"injected controller fault at tick {observation.tick}"
            )
        return self.inner.step(observation)

    def shutdown(self, reason: str) -> list[ActuatorIntent]:
        return self.inner.shutdown(reason)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@dataclass
class TraceRow:
    tick: int
    time_s: float
    device_temp_c: float
    sensor_temp_c: float
    fan_state: str
    supervisor_state: str
    emergency_active: bool
    intents: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunResult:
    """Everything a property needs to be decided, from one scenario run."""

    scenario: str
    seed: int
    rows: list[TraceRow]
    world: ThermalWorld
    supervisor: SafetySupervisor
    controller: CompiledController
    decisions: list[IntentDecision]
    faulted: bool = False
    fault_reason: str | None = None

    @property
    def peak_device_temp_c(self) -> float:
        return max((row.device_temp_c for row in self.rows), default=0.0)

    @property
    def peak_sensor_temp_c(self) -> float:
        return max((row.sensor_temp_c for row in self.rows), default=0.0)

    @property
    def actuator_transitions(self) -> int:
        return self.world.actuator_transitions

    def first_tick_where(self, predicate) -> int | None:
        for row in self.rows:
            if predicate(row):
                return row.tick
        return None

    def rejections(self) -> list[IntentDecision]:
        return [decision for decision in self.decisions if not decision.accepted]


def run_scenario(
    program: ControllerProgram,
    scenario: Scenario,
    schema: HardwareSchema,
    registry: CapabilityRegistry,
    seed: int | None = None,
    policy: SupervisorPolicy | None = None,
    telemetry_sink=None,
) -> RunResult:
    """Drive one compiled controller through one seeded scenario.

    Deterministic end to end: the world's randomness comes from the seed, the
    controller is an interpreter over data, and the supervisor is a table of
    comparisons. Two runs with the same arguments produce the same trace.
    """
    scenario = scenario if seed is None else scenario.with_seed(seed)
    world = ThermalWorld(scenario.world)
    supervisor = SafetySupervisor(
        port=world,
        schema=schema,
        registry=registry,
        policy=policy,
        telemetry_sink=telemetry_sink,
    )
    supervisor.install_program(program)

    inner = CompiledController(program)
    controller: CompiledController = inner
    if scenario.faults.controller_crash_tick is not None:
        controller = CrashingController(inner, scenario.faults.controller_crash_tick)

    controller.initialize(
        ControllerContext(
            device_id=program.device_id,
            started_at_s=0.0,
            actuator_states=dict(world.actuators),
        )
    )

    rows: list[TraceRow] = []
    faulted = False
    fault_reason: str | None = None

    for _ in range(scenario.ticks):
        snapshot = world.step()
        metrics = world.metrics()
        supervisor.enforce_local_policy(metrics, snapshot.tick, snapshot.time_s)

        intents: list[ActuatorIntent] = []
        note: str | None = None
        if supervisor.state is SafetyState.NORMAL:
            try:
                intents = controller.step(
                    Observation(
                        tick=snapshot.tick,
                        time_s=snapshot.time_s,
                        metrics=metrics,
                        actuator_states=dict(world.actuators),
                    )
                )
            except ControllerFault as exc:
                faulted = True
                fault_reason = str(exc)
                note = f"controller fault: {exc}"
                supervisor.on_controller_fault(exc)
                # The supervisor's safe state applies from this tick, not the
                # next one: a fault while overheating must not cost a tick of
                # cooling (RESEARCH.md §7 rule 3).
                supervisor.enforce_local_policy(metrics, snapshot.tick, snapshot.time_s)

        decisions = supervisor.apply(intents, metrics, snapshot.time_s)
        after = world.snapshot()
        rows.append(
            TraceRow(
                tick=snapshot.tick,
                time_s=snapshot.time_s,
                device_temp_c=snapshot.device_temp_c,
                sensor_temp_c=snapshot.sensor_temp_c,
                fan_state=after.fan_state,
                supervisor_state=supervisor.state.value,
                emergency_active=supervisor.emergency_active,
                intents=[intent.describe() for intent in intents],
                accepted=[
                    decision.intent.describe() for decision in decisions if decision.accepted
                ],
                rejected=[
                    f"{decision.intent.describe()} ({decision.reason})"
                    for decision in decisions
                    if not decision.accepted
                ],
                note=note,
            )
        )

    return RunResult(
        scenario=scenario.name,
        seed=scenario.world.seed,
        rows=rows,
        world=world,
        supervisor=supervisor,
        controller=inner,
        decisions=list(supervisor.decisions),
        faulted=faulted,
        fault_reason=fault_reason,
    )
