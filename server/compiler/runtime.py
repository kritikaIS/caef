"""Controller runtime — the immutable interpreter for a compiled artifact.

RESEARCH.md §4. This module ships with the device and never changes when
firmware does. It reads a `ControllerProgram` (data) and answers `step()` with
intents; it holds no reference to a driver, a socket or the filesystem, so the
worst a malformed program can do is ask for something the supervisor then
rejects.

`exec`, `eval`, `compile` and `__import__` appear nowhere in the manifest-mode
path. A test asserts that, because "generated firmware is not executed" is the
claim the whole design rests on and a comment is not evidence.

The interface is the one the simulator, the verifier and the virtual device all
drive, so what was verified is what runs:

    initialize(context)
    step(observation) -> list[ActuatorIntent]
    shutdown(reason)  -> list[ActuatorIntent]
"""

from dataclasses import dataclass, field

from server.compiler.program import ActuatorIntent, ControllerProgram, IntentSpec
from server.manifest.models import Trigger


class ControllerFault(RuntimeError):
    """The controller cannot continue. The supervisor's cue to enter safe state."""


class BudgetExceeded(ControllerFault):
    """A step would cost more than the manifest's declared budget."""


@dataclass(frozen=True)
class Observation:
    """What the supervisor hands the controller each control step.

    Sensor *readings*, not sensor objects: the controller never touches a
    driver, so a faulty sensor is data to it, not an exception.
    """

    tick: int
    time_s: float
    metrics: dict[str, float]
    actuator_states: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ControllerContext:
    device_id: str
    started_at_s: float
    actuator_states: dict[str, str] = field(default_factory=dict)


class CompiledController:
    """Executes a `ControllerProgram`. Deterministic given its observations."""

    def __init__(self, program: ControllerProgram) -> None:
        self.program = program
        self.context: ControllerContext | None = None
        self.steps_taken = 0
        self.cpu_ms_used = 0.0
        self.actuator_transitions = 0
        self.yielded = False
        self._commanded: dict[str, str] = {}
        self._last_transition_tick: dict[str, int] = {}
        self._emitted_ids: set[str] = set()

    # --- lifecycle -----------------------------------------------------------

    def initialize(self, context: ControllerContext) -> None:
        self.context = context
        self.steps_taken = 0
        self.cpu_ms_used = 0.0
        self.actuator_transitions = 0
        self.yielded = False
        # Seed from the actuator states the supervisor reports, so the first
        # step does not re-command a state the device is already in.
        self._commanded = dict(context.actuator_states)
        self._last_transition_tick = {}

    def shutdown(self, reason: str) -> list[ActuatorIntent]:
        """Emit the manifest's fallback behaviour and stop.

        Called on lease expiry, on reversion and by the supervisor when it takes
        control back. `restore_previous_firmware` compiles to an empty fallback
        because restoring a slot is the supervisor's job, not the controller's.
        """
        self.yielded = True
        tick = self.steps_taken
        return [
            self._intent(spec, tick, "fallback", f"shutdown: {reason}", index)
            for index, spec in enumerate(self.program.fallback)
        ]

    # --- the control step ----------------------------------------------------

    def step(self, observation: Observation) -> list[ActuatorIntent]:
        if self.context is None:
            raise ControllerFault("step() before initialize()")

        cost = self.program.resource_budget.estimated_cpu_ms_per_step
        if cost > self.program.resource_budget.max_cpu_ms_per_step:
            # Compile-time constant vs compile-time budget: this can only fire
            # if an artifact reached the runtime without passing the validator,
            # which is exactly when a backstop is worth having.
            raise BudgetExceeded(
                f"step costs {cost}ms, budget is "
                f"{self.program.resource_budget.max_cpu_ms_per_step}ms"
            )

        self.steps_taken += 1
        self.cpu_ms_used += cost

        elapsed = observation.time_s - self.context.started_at_s
        if elapsed >= self.program.maximum_duration_seconds:
            # The contract is over. The controller yields rather than running on
            # — the device also expires the lease independently, so this is the
            # cooperative half of a guarantee that does not depend on it
            # (RESEARCH.md §8).
            if self.yielded:
                return []
            return self.shutdown("lease elapsed")

        intents: list[ActuatorIntent] = []
        for rule in self.program.rules:
            if not _holds(rule.condition, observation.metrics):
                continue
            for index, spec in enumerate(rule.emit):
                intent = self._maybe_emit(spec, observation, rule.rule_id, index)
                if intent is not None:
                    intents.append(intent)
        return intents

    # --- emission ------------------------------------------------------------

    def _maybe_emit(
        self, spec: IntentSpec, observation: Observation, rule_id: str, index: int
    ) -> ActuatorIntent | None:
        """Level-triggered with a hold, for actuators; unconditional otherwise.

        Two filters, both deterministic and both derived from the registry:

        - *no-op suppression*: commanding the state the actuator is already in
          is not a transition and is not emitted, so a rule that holds for a
          hundred ticks produces one intent, not a hundred;
        - *minimum hold*: an actuator may not change state again within
          `min_hold_ticks`. This is what keeps sensor noise around a threshold
          from becoming relay chatter (RESEARCH.md §6 property 7).
        """
        if spec.kind != "actuator" or spec.actuator is None:
            return self._intent(spec, observation.tick, rule_id, f"rule {rule_id}", index)

        current = self._commanded.get(
            spec.actuator, observation.actuator_states.get(spec.actuator)
        )
        if current == spec.state:
            return None

        last = self._last_transition_tick.get(spec.actuator)
        if last is not None and observation.tick - last < self.program.min_hold_ticks:
            return None

        self._commanded[spec.actuator] = spec.state or ""
        self._last_transition_tick[spec.actuator] = observation.tick
        self.actuator_transitions += 1
        return self._intent(spec, observation.tick, rule_id, f"rule {rule_id}", index)

    def _intent(
        self, spec: IntentSpec, tick: int, tag: str, reason: str, index: int
    ) -> ActuatorIntent:
        """`intent_id` is derived, never random: two identical runs produce
        identical ids, which is what lets a counterexample trace be diffed."""
        return ActuatorIntent(
            intent_id=f"{self.program.manifest_id}:{tick}:{tag}:{index}",
            manifest_id=self.program.manifest_id,
            capability=spec.capability,
            kind=spec.kind,
            tick=tick,
            reason=reason,
            actuator=spec.actuator,
            state=spec.state,
            pin=spec.pin,
            event=spec.event,
            trigger_type=spec.trigger_type,
            current_ma=spec.current_ma,
        )


def _holds(condition: Trigger | None, metrics: dict[str, float]) -> bool:
    """`None` means every step. Evaluation is the manifest model's own method —
    a numeric comparison, never a parsed expression."""
    return True if condition is None else condition.evaluate(metrics)


def build_controller(program: ControllerProgram) -> CompiledController:
    return CompiledController(program)
