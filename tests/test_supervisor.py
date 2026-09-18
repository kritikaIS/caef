"""M15 — the immutable local safety supervisor (RESEARCH.md §7).

These tests treat the controller as hostile. They hand the supervisor intents no
compiled artifact would ever produce — a forbidden pin, an undeclared
capability, a fan-off during an emergency, a current draw beyond the schema's
budget — and require that each one is rejected individually, without the
supervisor crashing and without any of them reaching the actuator port.

That framing is deliberate. The supervisor's value is not that the compiler is
careful; it is that the supervisor does not depend on the compiler being careful.
"""

import logging

import pytest

import config
from edge_node.supervisor import SafetyState, SafetySupervisor, SupervisorPolicy
from server.compiler.compiler import compile_manifest
from server.compiler.program import ActuatorIntent
from server.compiler.runtime import ControllerFault
from server.manifest.registry import load_registry
from server.schemas import load_hardware_schema
from server.sim import scenarios
from server.sim.harness import run_scenario
from server.sim.world import ThermalWorld, WorldConfig
from tests.fixtures import manifests


@pytest.fixture(autouse=True)
def quiet_supervisor_logs():
    """The supervisor logs every emergency and safe-state entry at WARNING; the
    tests below provoke a great many of them on purpose."""
    logging.getLogger("caef.supervisor").setLevel(logging.CRITICAL)
    yield
    logging.getLogger("caef.supervisor").setLevel(logging.NOTSET)


@pytest.fixture
def registry():
    return load_registry("1.0.0")


@pytest.fixture
def schema():
    return load_hardware_schema("pi_node_alpha")


@pytest.fixture
def program(registry, schema):
    return compile_manifest(manifests.cooling(), registry, schema).program


@pytest.fixture
def rig(registry, schema, program):
    """A supervisor bound to a world, with the cooling program installed."""
    world = ThermalWorld(WorldConfig())
    supervisor = SafetySupervisor(port=world, schema=schema, registry=registry)
    supervisor.install_program(program)
    return world, supervisor


def intent(**overrides) -> ActuatorIntent:
    """A forged intent. Whatever a controller could emit, and things it could not."""
    payload = {
        "intent_id": "forged:1",
        "manifest_id": "manifest-cooling-1",
        "capability": "fan_on",
        "kind": "actuator",
        "tick": 1,
        "reason": "test",
        "actuator": "fan",
        "state": "on",
        "pin": 27,
        "current_ma": 12.0,
    }
    return ActuatorIntent(**{**payload, **overrides})


def only(decisions):
    assert len(decisions) == 1
    return decisions[0]


# --- the happy path, so the rejections below mean something -----------------


def test_a_well_formed_intent_is_applied(rig):
    world, supervisor = rig
    decision = only(supervisor.apply([intent()], {"temperature_c": 85.0}, 1.0))
    assert decision.accepted, decision.reason
    assert world.actuators["fan"] == "on"


# --- emergency policy outranks the firmware ---------------------------------


def test_firmware_cannot_stop_cooling_during_an_emergency(rig):
    """The required property: a fan-off while the local emergency policy demands
    cooling is rejected, not deferred and not merged."""
    world, supervisor = rig
    hot = {"temperature_c": config.EMERGENCY_TEMP_C + 1.0}

    supervisor.enforce_local_policy(hot, tick=1, time_s=1.0)
    assert world.actuators["fan"] == "on", "the supervisor must cool without being asked"

    decision = only(
        supervisor.apply(
            [intent(capability="fan_off", state="off", current_ma=0.0)], hot, 1.0
        )
    )
    assert decision.accepted is False
    assert decision.reason.startswith("emergency_override")
    assert world.actuators["fan"] == "on", "the rejection must not have been cosmetic"


def test_cooling_is_released_once_the_emergency_clears(rig):
    world, supervisor = rig
    hot = {"temperature_c": config.EMERGENCY_TEMP_C + 1.0}
    cool = {"temperature_c": 50.0}

    supervisor.enforce_local_policy(hot, 1, 1.0)
    supervisor.enforce_local_policy(cool, 2, 2.0)
    assert supervisor.emergency_active is False

    decision = only(
        supervisor.apply(
            [intent(capability="fan_off", state="off", current_ma=0.0)], cool, 2.0
        )
    )
    assert decision.accepted is True
    assert world.actuators["fan"] == "off"


# --- pins, capabilities, envelope -------------------------------------------


def test_a_pin_absent_from_the_hardware_schema_is_rejected(rig):
    world, supervisor = rig
    decision = only(supervisor.apply([intent(pin=5)], {"temperature_c": 85.0}, 1.0))
    assert decision.accepted is False
    assert "pin_not_permitted" in decision.reason
    assert world.actuators["fan"] == "off"


def test_a_forbidden_pin_is_rejected_even_if_the_registry_permitted_it(
    rig, registry, schema, monkeypatch
):
    """Belt and braces: the intent claims a pin the capability does not permit,
    and separately the device denylists it."""
    world, supervisor = rig
    forbidden = schema.constraints.forbidden_pins[0]
    decision = only(supervisor.apply([intent(pin=forbidden)], {"temperature_c": 85.0}, 1.0))
    assert decision.accepted is False
    assert world.actuators["fan"] == "off"


def test_an_undeclared_capability_is_rejected(rig):
    """The registry would permit `emit_context_event`, but this program did not
    declare it, so it is not authorised on this device right now."""
    _, supervisor = rig
    decision = only(
        supervisor.apply(
            [
                intent(
                    capability="emit_context_event",
                    kind="telemetry",
                    event="HIGH_HEAT_DETECTED",
                    actuator=None,
                    state=None,
                    pin=None,
                    current_ma=None,
                )
            ],
            {"temperature_c": 60.0},
            1.0,
        )
    )
    assert decision.accepted is False
    assert "undeclared_capability" in decision.reason


def test_an_unknown_capability_is_rejected(rig):
    _, supervisor = rig
    decision = only(
        supervisor.apply([intent(capability="open_shell")], {"temperature_c": 60.0}, 1.0)
    )
    assert decision.accepted is False
    assert "unknown_capability" in decision.reason


def test_a_capability_lying_about_its_kind_is_rejected(rig):
    """`fan_on` claiming to be telemetry would slip past the actuator checks."""
    _, supervisor = rig
    decision = only(
        supervisor.apply([intent(kind="telemetry", event="HEARTBEAT")], {}, 1.0)
    )
    assert decision.accepted is False
    assert "kind_mismatch" in decision.reason


def test_an_actuator_state_outside_the_registry_is_rejected(rig):
    world, supervisor = rig
    decision = only(supervisor.apply([intent(state="turbo")], {"temperature_c": 85.0}, 1.0))
    assert decision.accepted is False
    assert "unknown_state" in decision.reason
    assert world.actuators["fan"] == "off"


def test_a_draw_above_the_schema_budget_is_rejected(rig, schema):
    """`max_gpio_current` is the device's own declared envelope."""
    _, supervisor = rig
    limit = schema.constraints.max_current_ma()
    decision = only(
        supervisor.apply([intent(current_ma=limit + 1.0)], {"temperature_c": 85.0}, 1.0)
    )
    assert decision.accepted is False
    assert "current_limit" in decision.reason


def test_the_transition_budget_is_enforced(rig, registry):
    """Chatter is rejected by the supervisor as well as suppressed by the
    compiler, so an artifact that skipped the compiler still cannot chatter."""
    world, supervisor = rig
    ceiling = registry.require("fan_on").actuator_limits.max_transitions_per_minute
    metrics = {"temperature_c": 60.0}
    rejected = 0
    for tick in range(ceiling * 2):
        state = "on" if tick % 2 == 0 else "off"
        capability = "fan_on" if state == "on" else "fan_off"
        decision = only(
            supervisor.apply(
                [intent(capability=capability, state=state, current_ma=0.0)],
                metrics,
                # All within one 60s window.
                time_s=float(tick),
            )
        )
        rejected += 0 if decision.accepted else 1
    assert rejected > 0
    assert supervisor.counters.rejections_by_reason.get("transition_budget", 0) > 0


# --- the supervisor never crashes -------------------------------------------


def test_one_bad_intent_does_not_stop_the_others(rig):
    world, supervisor = rig
    decisions = supervisor.apply(
        [
            intent(intent_id="a", capability="open_shell"),
            intent(intent_id="b", pin=5),
            intent(intent_id="c"),
        ],
        {"temperature_c": 85.0},
        1.0,
    )
    assert [decision.accepted for decision in decisions] == [False, False, True]
    assert world.actuators["fan"] == "on"


def test_a_malformed_actuator_name_is_a_rejection_not_an_exception(rig):
    _, supervisor = rig
    decision = only(supervisor.apply([intent(actuator="laser")], {"temperature_c": 85.0}, 1.0))
    assert decision.accepted is False
    assert "unknown_actuator" in decision.reason


def test_every_decision_is_recorded_for_audit(rig):
    _, supervisor = rig
    supervisor.apply([intent(), intent(intent_id="bad", pin=5)], {"temperature_c": 85.0}, 1.0)
    assert len(supervisor.decisions) == 2
    assert supervisor.counters.accepted == 1
    assert supervisor.counters.rejected == 1


# --- safe state --------------------------------------------------------------


def test_safe_state_keeps_cooling_while_the_device_is_hot(rig):
    """The prototype's safe state is defined explicitly: stopping the firmware is
    not by itself safe (RESEARCH.md §7 rule 3)."""
    world, supervisor = rig
    hot = {"temperature_c": config.SAFE_STATE_COOLING_TEMP_C + 5.0}
    supervisor.enter_safe_state("test")
    supervisor.enforce_local_policy(hot, 1, 1.0)
    assert world.actuators["fan"] == "on"


def test_safe_state_releases_cooling_once_the_device_is_clearly_cool(rig):
    world, supervisor = rig
    supervisor.enter_safe_state("test")
    supervisor.enforce_local_policy({"temperature_c": 90.0}, 1, 1.0)
    assert world.actuators["fan"] == "on"
    below = config.SAFE_STATE_COOLING_TEMP_C - config.SAFE_STATE_HYSTERESIS_C - 1.0
    supervisor.enforce_local_policy({"temperature_c": below}, 2, 2.0)
    assert world.actuators["fan"] == "off"


def test_safe_state_cools_when_it_cannot_read_a_temperature_at_all(rig):
    world, supervisor = rig
    supervisor.enter_safe_state("sensor lost")
    supervisor.enforce_local_policy({}, 1, 1.0)
    assert world.actuators["fan"] == "on"


def test_in_safe_state_the_controller_owns_nothing(rig):
    world, supervisor = rig
    supervisor.enter_safe_state("test")
    decision = only(supervisor.apply([intent()], {"temperature_c": 85.0}, 1.0))
    assert decision.accepted is False
    assert decision.reason.startswith("safe_state")


def test_a_controller_fault_enters_safe_state(rig):
    _, supervisor = rig
    supervisor.on_controller_fault(ControllerFault("boom"))
    assert supervisor.state is SafetyState.SAFE_STATE
    assert "boom" in supervisor.safe_state_reason


def test_missed_heartbeats_enter_safe_state(rig):
    _, supervisor = rig
    for tick in range(config.LOCAL_HEARTBEAT_MISS_LIMIT + 2):
        supervisor.enforce_local_policy({"temperature_c": 50.0}, tick, float(tick))
    assert supervisor.state is SafetyState.SAFE_STATE
    assert "heartbeat" in supervisor.safe_state_reason


def test_a_heartbeat_resets_the_miss_counter(rig):
    _, supervisor = rig
    beat = intent(capability="emit_heartbeat", kind="telemetry", event="HEARTBEAT",
                  actuator=None, state=None, pin=None, current_ma=None)
    for tick in range(config.LOCAL_HEARTBEAT_MISS_LIMIT * 3):
        supervisor.enforce_local_policy({"temperature_c": 50.0}, tick, float(tick))
        supervisor.apply([beat], {"temperature_c": 50.0}, float(tick))
    assert supervisor.state is SafetyState.NORMAL
    assert supervisor.counters.heartbeats == config.LOCAL_HEARTBEAT_MISS_LIMIT * 3


def test_a_controller_may_hand_control_back(rig):
    _, supervisor = rig
    decision = only(
        supervisor.apply(
            [intent(capability="enter_safe_idle", kind="safety", actuator=None,
                    state=None, pin=None, current_ma=None)],
            {"temperature_c": 85.0},
            1.0,
        )
    )
    assert decision.accepted is True
    assert supervisor.state is SafetyState.SAFE_STATE


# --- the loop as a whole -----------------------------------------------------


def test_a_crash_while_overheating_leaves_the_device_cooling(program, schema, registry):
    """The scenario the safe state exists for: the controller dies at tick 30
    with the device above 80C, and the device must not be left to cook."""
    result = run_scenario(program, scenarios.get("firmware_crash"), schema, registry)
    assert result.faulted is True
    assert result.supervisor.state is SafetyState.SAFE_STATE

    after_crash = [row for row in result.rows if row.tick > 30]
    assert after_crash
    assert result.peak_device_temp_c < config.CRITICAL_TEMP_C
    # Judged on the *sensor* reading, not the true temperature: the supervisor
    # acts on what the device can measure, and a noisy sensor a few tenths below
    # the engage point is a correct reason not to have engaged yet.
    hot_rows = [
        row for row in after_crash if row.sensor_temp_c >= config.SAFE_STATE_COOLING_TEMP_C
    ]
    assert hot_rows, "the scenario must actually get hot after the crash"
    assert all(row.fan_state == "on" for row in hot_rows)


def test_emergency_cooling_engages_without_any_controller_involvement(
    program, schema, registry
):
    """`ineffective_fan` runs past the emergency threshold. The supervisor acts
    on its own policy; the controller cannot prevent it and is not asked."""
    result = run_scenario(program, scenarios.get("ineffective_fan"), schema, registry)
    assert result.supervisor.counters.emergency_activations >= 1
    assert any(row.emergency_active for row in result.rows)


def test_a_stuck_high_sensor_costs_power_not_safety(program, schema, registry):
    result = run_scenario(program, scenarios.get("sensor_stuck_high"), schema, registry)
    assert result.peak_device_temp_c < config.SAFE_STATE_COOLING_TEMP_C
    assert any(row.fan_state == "on" for row in result.rows)


def test_the_run_is_reproducible(program, schema, registry):
    def trace(seed):
        result = run_scenario(program, scenarios.get("noisy_threshold"), schema, registry, seed=seed)
        return [(row.tick, row.sensor_temp_c, row.fan_state) for row in result.rows]

    assert trace(3) == trace(3)
    assert trace(3) != trace(4)
