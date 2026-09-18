"""M14 — the closed-loop virtual world (RESEARCH.md §5).

The property that matters here is the one the baseline simulation did not have:
what the firmware does to the fan changes what the sensor says next. Without it,
no cooling behaviour is verifiable and every "the morph cooled the device" claim
is decoration.
"""

import ast
from pathlib import Path

import pytest

import config
from server.sim import scenarios, world as world_module
from server.sim.scenarios import SCENARIOS, VERIFICATION_SCENARIOS
from server.sim.world import SensorFault, ThermalWorld, WorldConfig


def settle(world: ThermalWorld, ticks: int) -> float:
    for _ in range(ticks):
        world.step()
    return world.device_temp_c


# --- the closed loop ---------------------------------------------------------


def test_the_fan_affects_simulated_temperature():
    """The core requirement. Same world, same seed, same ticks — the only
    difference is the fan, and it has to show up in the temperature."""
    hot = WorldConfig(heat_generation_c_per_s=2.0)
    without_fan = ThermalWorld(hot)
    with_fan = ThermalWorld(hot)
    with_fan.set_actuator("fan", "on")

    assert settle(with_fan, 60) < settle(without_fan, 60) - 20.0


def test_cooling_pulls_the_device_back_below_the_threshold():
    """Not just "lower" — a healthy fan has to actually resolve the situation,
    or the activation-latency property would be satisfiable by a controller that
    achieves nothing."""
    world = ThermalWorld(WorldConfig(heat_generation_c_per_s=2.0))
    settle(world, 60)
    assert world.device_temp_c > 80.0
    world.set_actuator("fan", "on")
    assert settle(world, 120) < 60.0


def test_turning_the_fan_off_lets_the_device_heat_again():
    world = ThermalWorld(WorldConfig(heat_generation_c_per_s=2.0))
    world.set_actuator("fan", "on")
    cooled = settle(world, 100)
    world.set_actuator("fan", "off")
    assert settle(world, 60) > cooled + 10.0


def test_an_ineffective_fan_does_not_save_the_device():
    """Correct control cannot compensate for hardware that does not work, and
    the simulation has to be able to say so."""
    world = ThermalWorld(
        WorldConfig(heat_generation_c_per_s=2.0, fan_effectiveness=0.02)
    )
    world.set_actuator("fan", "on")
    assert settle(world, 200) > config.CRITICAL_TEMP_C


# --- reproducibility ---------------------------------------------------------


def test_same_seed_produces_an_identical_trace():
    def trace(seed: int) -> list[tuple[float, float]]:
        world = ThermalWorld(WorldConfig(seed=seed, sensor_noise_c=2.0))
        return [
            (snapshot.device_temp_c, snapshot.sensor_temp_c)
            for snapshot in (world.step() for _ in range(30))
        ]

    assert trace(11) == trace(11)
    assert trace(11) != trace(12)


def test_a_reading_is_pure_within_a_tick():
    """A controller that samples twice in one step must see the same number, or
    rule order would silently change behaviour."""
    world = ThermalWorld(WorldConfig(sensor_noise_c=2.0))
    world.step()
    assert world.read_temperature_c() == world.read_temperature_c()
    assert world.metrics() == world.metrics()


def test_the_world_advances_only_when_stepped():
    """Ticks, never sleeps: a run's outcome must not depend on how fast the
    machine executing it is."""
    tree = ast.parse(Path(world_module.__file__).read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "sleep" not in called
    assert "time" not in {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }

    world = ThermalWorld(WorldConfig())
    before = world.device_temp_c
    assert world.tick == 0 and world.device_temp_c == before


# --- faults ------------------------------------------------------------------


def test_a_stuck_high_sensor_lies_upward_from_its_start_tick():
    world = ThermalWorld(
        WorldConfig(sensor_fault=SensorFault.STUCK_HIGH, sensor_fault_from_tick=5)
    )
    readings = [world.step().sensor_temp_c for _ in range(10)]
    assert readings[0] < 90.0
    assert readings[-1] == pytest.approx(99.0)
    # The truth is unchanged — the trace records both, so a fault scenario can
    # be told apart from a control failure.
    assert world.device_temp_c < 90.0


def test_a_stuck_low_sensor_hides_a_real_overheat():
    world = ThermalWorld(
        WorldConfig(
            heat_generation_c_per_s=2.0,
            sensor_fault=SensorFault.STUCK_LOW,
            sensor_fault_from_tick=5,
        )
    )
    settle(world, 80)
    assert world.device_temp_c > config.CRITICAL_TEMP_C
    assert world.read_temperature_c() == pytest.approx(40.0)


def test_a_spike_is_applied_once_at_its_tick():
    world = ThermalWorld(
        WorldConfig(heat_generation_c_per_s=0.8, spike_at_tick=10, spike_c=40.0)
    )
    before = [world.step().device_temp_c for _ in range(9)][-1]
    after = world.step().device_temp_c
    assert after - before > 35.0


def test_load_schedule_changes_heat_generation():
    world = ThermalWorld(
        WorldConfig(heat_generation_c_per_s=2.0, load=0.0, load_schedule=((10, 1.0),))
    )
    idle = settle(world, 9)
    assert world.device_temp_c < idle + 1.0
    assert settle(world, 30) > idle + 10.0


# --- actuator ownership ------------------------------------------------------


def test_only_declared_actuators_can_be_moved():
    world = ThermalWorld(WorldConfig())
    with pytest.raises(KeyError):
        world.set_actuator("laser", "on")


def test_transitions_are_logged_for_the_verifier():
    world = ThermalWorld(WorldConfig())
    world.set_actuator("fan", "on")
    world.step()
    world.set_actuator("fan", "on")  # not a transition
    world.set_actuator("fan", "off")
    assert world.actuator_transitions == 2
    assert [entry[2] for entry in world.transition_log] == ["on", "off"]


# --- the scenario set --------------------------------------------------------


def test_every_required_scenario_exists():
    required = {
        "normal",
        "gradual_overheat",
        "sudden_spike",
        "noisy_threshold",
        "sensor_stuck_high",
        "sensor_stuck_low",
        "ineffective_fan",
        "firmware_crash",
        "network_loss_after_deploy",
        "server_failure_after_deploy",
        "repeated_duplicate_triggers",
    }
    assert required == set(SCENARIOS)
    assert set(VERIFICATION_SCENARIOS) <= set(SCENARIOS)


def test_scenarios_reach_the_conditions_they_describe():
    """A scenario that does not actually get hot cannot test cooling, and one
    that never settles cannot test recovery. Checked against the *uncontrolled*
    trajectory, which is the scenario's own physics."""
    peaks = {}
    for name, scenario in SCENARIOS.items():
        world = ThermalWorld(scenario.world)
        peak = world.device_temp_c
        crossed = False
        for _ in range(scenario.ticks):
            world.step()
            peak = max(peak, world.device_temp_c)
            crossed = crossed or world.read_temperature_c() >= 80.0
        peaks[name] = peak
        assert crossed == scenario.expects_activation or name == "sensor_stuck_high", name

    assert peaks["normal"] < 80.0
    assert peaks["gradual_overheat"] > config.CRITICAL_TEMP_C
    assert peaks["ineffective_fan"] > config.CRITICAL_TEMP_C


def test_a_scenario_is_reproducible_from_its_seed():
    scenario = scenarios.get("noisy_threshold")
    def trace(seed):
        world = ThermalWorld(scenario.with_seed(seed).world)
        return [world.step().sensor_temp_c for _ in range(20)]

    assert trace(5) == trace(5)
    assert trace(5) != trace(6)


def test_unwinnable_scenarios_are_marked_as_such():
    """The verifier must not hold a controller to a bound it has no means of
    meeting; that starts with the scenario saying so."""
    assert scenarios.get("ineffective_fan").cooling_is_sufficient is False
    assert scenarios.get("sensor_stuck_low").cooling_is_sufficient is False
    assert scenarios.get("gradual_overheat").cooling_is_sufficient is True
