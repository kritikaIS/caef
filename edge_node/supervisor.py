"""Immutable local safety supervisor — the device's actuator owner.

RESEARCH.md §7. This module is **outside** the replaceable firmware. In
`manifest_compiler` mode the firmware artifact is data (a controller program),
so there is no mechanism by which it could reach this code even in principle:
nothing here is imported by an artifact, because an artifact imports nothing.

What that buys, stated precisely, because the claim is easy to overstate:

  - Every actuator change on the device goes through `apply()` or through the
    supervisor's own emergency policy. A controller returns intents and has no
    reference to the actuator port at all.
  - The local emergency policy runs *before* controller intents each tick and
    outranks them. While cooling is required, an intent that would stop it is
    rejected — not deferred, not merged.
  - A controller that faults, misses its heartbeats or exceeds its control
    budget loses control to a deterministic safe state. For the thermal
    prototype that state keeps cooling on while the device is hot, because
    stopping the firmware is not by itself safe.
  - No model, no server and no network is reachable from this path.

What it does not buy: this is a simulation. There is no privilege boundary, no
MMU and no secure element here — a process that can import this module can also
call the actuator port directly. The guarantee is about what *compiled firmware*
can express, not about an attacker who already runs code on the device
(RESEARCH.md §14).
"""

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import config
from server.compiler.program import ActuatorIntent, ControllerProgram, IntentDecision
from server.manifest.registry import CapabilityRegistry
from server.schemas import HardwareSchema

log = logging.getLogger("caef.supervisor")

# The metric the emergency policy is written against. The prototype's policy is
# thermal; a second policy would be a second named metric here, not a new
# meaning for this one.
EMERGENCY_METRIC = "temperature_c"


class SafetyState(StrEnum):
    NORMAL = "normal"
    SAFE_STATE = "safe_state"


class ActuatorPort(Protocol):
    """The only interface the supervisor has to physical reality.

    `ThermalWorld` implements it in simulation; a real device would implement it
    over GPIO. The supervisor is written against this so the same enforcement
    code covers both.
    """

    actuators: dict[str, str]

    def set_actuator(self, name: str, state: str) -> None: ...


@dataclass(frozen=True)
class SupervisorPolicy:
    """Local safety thresholds. Config-sourced; never inlined at a call site."""

    emergency_temp_c: float = config.EMERGENCY_TEMP_C
    safe_state_cooling_temp_c: float = config.SAFE_STATE_COOLING_TEMP_C
    safe_state_hysteresis_c: float = config.SAFE_STATE_HYSTERESIS_C
    heartbeat_miss_limit: int = config.LOCAL_HEARTBEAT_MISS_LIMIT
    transition_window_seconds: float = 60.0


@dataclass
class SupervisorCounters:
    """What the verifier and the experiment harness read back."""

    accepted: int = 0
    rejected: int = 0
    emergency_activations: int = 0
    safe_state_entries: int = 0
    heartbeats: int = 0
    missed_heartbeats: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)


class SafetySupervisor:
    """Validates every intent against the registry, the schema and safety state."""

    def __init__(
        self,
        port: ActuatorPort,
        schema: HardwareSchema,
        registry: CapabilityRegistry,
        policy: SupervisorPolicy | None = None,
        telemetry_sink=None,
    ) -> None:
        self.port = port
        self.schema = schema
        self.registry = registry
        self.policy = policy or SupervisorPolicy()
        # Where accepted telemetry intents go. `None` means "nowhere", which is
        # the correct behaviour when the server is unreachable: telemetry is not
        # on the safety path and losing it must not change local behaviour.
        self.telemetry_sink = telemetry_sink

        self.state = SafetyState.NORMAL
        self.safe_state_reason: str | None = None
        self.emergency_active = False
        self.counters = SupervisorCounters()
        self.decisions: list[IntentDecision] = []
        # Capabilities the installed program is allowed to use. Empty means no
        # program is installed, and every controller intent is undeclared.
        self._allowed_capabilities: set[str] = set()
        self._transitions: list[tuple[float, str]] = []
        self._ticks_since_heartbeat = 0

    # --- installation --------------------------------------------------------

    def install_program(self, program: ControllerProgram) -> None:
        """Bind the supervisor to what this artifact declared it would use.

        The registry says what a capability *may* do; the installed program says
        which capabilities were actually authorised for this device right now.
        An intent outside that set is rejected as undeclared even if the registry
        would otherwise permit it (RESEARCH.md §6 property 2).
        """
        self._allowed_capabilities = set(program.capabilities_used())
        self._ticks_since_heartbeat = 0

    def uninstall_program(self) -> None:
        self._allowed_capabilities = set()

    def manage_unowned_actuators(self, metrics: dict[str, float], time_s: float) -> list[str]:
        """Release actuators no installed capability owns. Release only — never engage.

        Firmware comes and goes; the fan does not. After a revert the outgoing
        morph may have left it running while the incoming baseline declares no
        actuator at all, and an actuator energised by firmware that no longer
        exists is a stuck relay.

        The rule is deliberately one-directional. Engaging cooling here as well
        would make the supervisor a thermostat, which would keep the device off
        the activation threshold and stop the adaptation loop ever starting —
        the supervisor would be quietly doing the firmware's job and the
        experiment would be measuring the wrong thing. Engagement stays where it
        belongs: the emergency policy and the safe state.

        Nor does it switch cooling *off* on a hot device. Above the safe-state
        engage point the actuator is simply left alone: not the supervisor's to
        start, not the supervisor's to stop.
        """
        released: list[str] = []
        if self.emergency_active or self.state is SafetyState.SAFE_STATE:
            return released  # both already decided; neither is ours to override

        reading = metrics.get(EMERGENCY_METRIC)
        if reading is None or reading >= self.policy.safe_state_cooling_temp_c:
            return released

        driven = {
            capability.actuator
            for name in self._allowed_capabilities
            if (capability := self.registry.get(name)) and capability.actuator
        }
        for name, spec in self.registry.actuators.items():
            if name in driven or name not in self.port.actuators:
                continue
            if self.port.actuators[name] != spec.default_state:
                log.info(
                    "releasing unowned %s to %s at %.2fC", name, spec.default_state, reading
                )
                self._set(name, spec.default_state, time_s)
                released.append(name)
        return released

    def _thermal_target(self, name: str, spec, reading: float | None) -> str:
        """What the local thermal policy wants an actuator in, with hysteresis.

        Shared with the safe state so there is one rule, not two that can drift:
        engage at `safe_state_cooling_temp_c`, release only once the device is
        `safe_state_hysteresis_c` below it, hold in between.
        """
        if reading is None:
            # No reading: cool. Cooling a cool device costs a watt; not cooling a
            # hot one costs the device.
            return spec.safe_state_when_hot
        engage = self.policy.safe_state_cooling_temp_c
        release = engage - self.policy.safe_state_hysteresis_c
        if reading >= engage:
            return spec.safe_state_when_hot
        if reading <= release:
            return spec.default_state
        current = self.port.actuators.get(name)
        return current if current in spec.states else spec.default_state

    # --- per-tick local policy ----------------------------------------------

    def enforce_local_policy(self, metrics: dict[str, float], tick: int, time_s: float) -> None:
        """Run before any controller intent, every tick.

        Ordering is the guarantee: by the time a controller's `fan_off` is
        examined, the emergency policy has already decided whether cooling is
        required, so the rejection is not a race.
        """
        reading = metrics.get(EMERGENCY_METRIC)
        if reading is not None and reading >= self.policy.emergency_temp_c:
            if not self.emergency_active:
                self.emergency_active = True
                self.counters.emergency_activations += 1
                log.warning(
                    "emergency policy engaged at %.2fC (>= %.2fC): forcing cooling",
                    reading,
                    self.policy.emergency_temp_c,
                )
            self._force("fan", "on", time_s)
        elif self.emergency_active and reading is not None:
            self.emergency_active = False
            log.info("emergency policy released at %.2fC", reading)

        if self.state is SafetyState.SAFE_STATE:
            self._apply_safe_state(metrics, time_s)
        else:
            self.manage_unowned_actuators(metrics, time_s)

        if self._allowed_capabilities:
            self._check_heartbeat(tick)

    def _check_heartbeat(self, tick: int) -> None:
        """A controller that stops beating has stopped controlling."""
        if "emit_heartbeat" not in self._allowed_capabilities:
            return  # nothing to miss
        self._ticks_since_heartbeat += 1
        if self._ticks_since_heartbeat > self.policy.heartbeat_miss_limit:
            self.counters.missed_heartbeats += 1
            self.enter_safe_state(
                f"controller missed {self._ticks_since_heartbeat} heartbeats by tick {tick}"
            )

    # --- intents -------------------------------------------------------------

    def apply(
        self, intents: list[ActuatorIntent], metrics: dict[str, float], time_s: float
    ) -> list[IntentDecision]:
        """Validate and apply each intent independently.

        Never raises. One malformed intent is rejected and the rest are still
        considered — a supervisor that dies on bad input is not a supervisor
        (RESEARCH.md §7).
        """
        decisions: list[IntentDecision] = []
        for intent in intents:
            try:
                accepted, reason = self._consider(intent, metrics, time_s)
            except Exception as exc:  # a supervisor must not be crashable by input
                log.exception("intent %s raised during validation", intent.intent_id)
                accepted, reason = False, f"validation_error: {exc}"
            decision = IntentDecision(intent=intent, accepted=accepted, reason=reason)
            decisions.append(decision)
            self.decisions.append(decision)
            if accepted:
                self.counters.accepted += 1
            else:
                self.counters.rejected += 1
                key = reason.split(":", 1)[0]
                self.counters.rejections_by_reason[key] = (
                    self.counters.rejections_by_reason.get(key, 0) + 1
                )
        return decisions

    def _consider(
        self, intent: ActuatorIntent, metrics: dict[str, float], time_s: float
    ) -> tuple[bool, str]:
        if self.state is SafetyState.SAFE_STATE:
            return False, f"safe_state: {self.safe_state_reason}"

        capability = self.registry.get(intent.capability)
        if capability is None:
            return False, f"unknown_capability: {intent.capability}"
        if intent.capability not in self._allowed_capabilities:
            return False, f"undeclared_capability: {intent.capability}"
        if capability.kind != intent.kind:
            return False, (
                f"kind_mismatch: registry says {intent.capability} is "
                f"{capability.kind}, intent claims {intent.kind}"
            )

        match intent.kind:
            case "telemetry":
                return self._accept_telemetry(intent)
            case "safety":
                self.enter_safe_state(f"controller requested {intent.capability}")
                return True, "safe_state_requested"
            case "actuator":
                return self._consider_actuator(intent, capability, metrics, time_s)
        return False, f"unsupported_intent_kind: {intent.kind}"

    def _accept_telemetry(self, intent: ActuatorIntent) -> tuple[bool, str]:
        if intent.event == "HEARTBEAT":
            self._ticks_since_heartbeat = 0
            self.counters.heartbeats += 1
        if self.telemetry_sink is not None:
            try:
                self.telemetry_sink(intent)
            except Exception:
                # Telemetry is observability, not safety. An unreachable server
                # must never change what the device does locally (NFR-6).
                log.exception("telemetry sink failed for %s", intent.intent_id)
        return True, "telemetry_emitted"

    def _consider_actuator(
        self, intent: ActuatorIntent, capability, metrics: dict[str, float], time_s: float
    ) -> tuple[bool, str]:
        actuator = intent.actuator
        spec = self.registry.actuators.get(actuator or "")
        if spec is None:
            return False, f"unknown_actuator: {actuator}"
        if intent.state not in spec.states:
            return False, f"unknown_state: {intent.state} not in {spec.states}"

        if intent.pin is None or intent.pin not in capability.permitted_pins:
            return False, (
                f"pin_not_permitted: {intent.capability} may use "
                f"{capability.permitted_pins}, intent targets {intent.pin}"
            )
        entry = self.schema.pin(intent.pin)
        if entry is None:
            return False, f"pin_absent_from_schema: GPIO_{intent.pin}"
        if self.schema.is_forbidden(intent.pin):
            return False, f"forbidden_pin: GPIO_{intent.pin}"
        if entry.connected_device != spec.connected_device:
            return False, (
                f"hardware_mismatch: GPIO_{intent.pin} is {entry.connected_device}, "
                f"actuator {actuator} is {spec.connected_device}"
            )

        limit = self.schema.constraints.max_current_ma()
        if limit is not None and (intent.current_ma or 0.0) > limit:
            return False, (
                f"current_limit: {intent.current_ma}mA exceeds max_gpio_current {limit}mA"
            )

        # The local emergency policy outranks the firmware, unconditionally.
        if (
            self.emergency_active
            and actuator == "fan"
            and intent.state != spec.safe_state_when_hot
        ):
            reading = metrics.get(EMERGENCY_METRIC)
            return False, (
                f"emergency_override: cooling is required at {reading}C; refusing "
                f"{actuator}={intent.state}"
            )

        if not self._transition_budget_allows(actuator, intent.state, time_s, capability):
            return False, (
                f"transition_budget: {actuator} has changed state too often within "
                f"{self.policy.transition_window_seconds}s"
            )

        self._set(actuator, intent.state, time_s)
        return True, "applied"

    def _transition_budget_allows(
        self, actuator: str, state: str, time_s: float, capability
    ) -> bool:
        """Rate-limit actuator changes to the registry's declared ceiling."""
        if self.port.actuators.get(actuator) == state:
            return True  # a no-op is not a transition
        limits = capability.actuator_limits
        ceiling = (
            limits.max_transitions_per_minute
            if limits
            else config.VERIFY_MAX_ACTUATOR_TRANSITIONS_PER_MIN
        )
        window_start = time_s - self.policy.transition_window_seconds
        recent = [entry for entry in self._transitions if entry[0] >= window_start]
        return len([entry for entry in recent if entry[1] == actuator]) < ceiling

    # --- safe state ----------------------------------------------------------

    def enter_safe_state(self, reason: str) -> None:
        """Take control back, deterministically.

        Idempotent: repeated faults do not re-enter, so the first cause is the
        one recorded. The controller is not consulted, and the server is not
        contacted — safe state must work with neither available.
        """
        if self.state is SafetyState.SAFE_STATE:
            return
        self.state = SafetyState.SAFE_STATE
        self.safe_state_reason = reason
        self.counters.safe_state_entries += 1
        log.warning("entering safe state: %s", reason)

    def on_controller_fault(self, exc: BaseException) -> None:
        self.enter_safe_state(f"controller fault: {type(exc).__name__}: {exc}")

    def leave_safe_state(self) -> None:
        """Called by the device layer once a known-good artifact is running."""
        self.state = SafetyState.NORMAL
        self.safe_state_reason = None
        self._ticks_since_heartbeat = 0

    def _apply_safe_state(self, metrics: dict[str, float], time_s: float) -> None:
        """The thermal prototype's safe state: cooling stays on while hot.

        Explicitly not "stop the firmware and leave the actuators wherever they
        were" — a device that overheats after its firmware dies is not in a safe
        state (RESEARCH.md §7 rule 3). The threshold rule is `_thermal_target`,
        shared with unowned-actuator management so the two cannot drift apart.
        """
        reading = metrics.get(EMERGENCY_METRIC)
        for name, spec in self.registry.actuators.items():
            if name in self.port.actuators:
                self._force(name, self._thermal_target(name, spec, reading), time_s)

    # --- actuator port -------------------------------------------------------

    def _force(self, actuator: str, state: str, time_s: float) -> None:
        """The supervisor's own actuator write. Bypasses intent validation
        because it is not acting on anyone's behalf but its own policy."""
        if self.port.actuators.get(actuator) != state:
            self._set(actuator, state, time_s)

    def _set(self, actuator: str, state: str, time_s: float) -> None:
        if self.port.actuators.get(actuator) == state:
            return
        self.port.set_actuator(actuator, state)
        self._transitions.append((time_s, actuator))
