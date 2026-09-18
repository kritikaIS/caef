"""Named, seeded scenarios (RESEARCH.md §5).

Each scenario is a frozen record: the physical conditions, how long to run, and
any fault injected outside the world itself. Two runs of the same scenario at
the same seed produce the same trace, which is what makes a counterexample worth
keeping and a metric worth comparing across arms.

Two kinds of fault live here, and they are deliberately separate:

  - *world faults* (`WorldConfig`) — noise, a stuck sensor, a fan that spins but
    does not cool. These are conditions the firmware must cope with.
  - *harness faults* (`ScenarioFaults`) — a controller that dies, a server that
    goes away, a supervisor that restarts, a trigger delivered twice. These are
    conditions the *system around* the firmware must cope with, and no
    controller can be written to prevent them.

`cooling_is_sufficient` records whether cooling can physically hold the device
under the critical limit in this scenario. Where it cannot — an ineffective fan,
a sensor stuck low — the verifier does not hold the controller to a temperature
bound it has no means of meeting, and says so in the report rather than
recording a pass it did not earn.
"""

import math
from dataclasses import dataclass, field, replace

import config
from server.sim.world import SensorFault, WorldConfig


@dataclass(frozen=True)
class ScenarioFaults:
    """Failures injected around the controller rather than inside the world."""

    # The controller raises at this tick, as a runtime fault would.
    controller_crash_tick: int | None = None
    # From this tick the device cannot reach the server, and vice versa.
    server_offline_from_tick: int | None = None
    # The device supervisor process restarts here; persisted state must survive.
    supervisor_restart_at_tick: int | None = None
    # How many times the same trigger is delivered to the pipeline.
    duplicate_trigger_count: int = 1


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    world: WorldConfig
    ticks: int
    faults: ScenarioFaults = field(default_factory=ScenarioFaults)
    # Can cooling physically hold the device below CRITICAL_TEMP_C here?
    cooling_is_sufficient: bool = True
    # Is the activation threshold expected to be reached at all? A scenario that
    # never gets hot cannot demonstrate an activation latency, and the verifier
    # must not read "never activated" as "activated instantly".
    expects_activation: bool = True

    def with_seed(self, seed: int) -> "Scenario":
        return replace(self, world=replace(self.world, seed=seed))

    def for_verification(self, lease_seconds: int | None = None) -> "Scenario":
        """This scenario as the pre-deployment verifier should run it.

        Two adjustments, both because verification is judging an artifact that
        will be installed *into* a situation rather than one that waits for it:

        - **Hot start.** A scenario that expects activation begins at
          `VERIFY_SITUATION_START_TEMP_C` instead of warming up from cold, so
          the threshold is crossed in the first few ticks. Warming up from 45C
          wastes half the run and — with a short lease — can expire the morph
          before its own scenario ever gets hot, which would fail the artifact
          for the verifier's scheduling rather than for its behaviour.
        - **Tick budget.** The run covers the lease plus a tail, so what is
          measured is the leased period and the supervisor's handling of its
          end, not a hundred ticks of aftermath.

        Sensor-fault scenarios keep their cold start: the whole point of a
        stuck-high sensor is that the device is *not* hot.
        """
        world = self.world
        if self.expects_activation and world.sensor_fault is SensorFault.NONE:
            world = replace(
                world,
                initial_device_c=max(
                    world.initial_device_c, config.VERIFY_SITUATION_START_TEMP_C
                ),
            )
        ticks = self.ticks
        if lease_seconds:
            lease_ticks = math.ceil(lease_seconds / world.tick_seconds)
            ticks = min(ticks, lease_ticks + config.VERIFY_TAIL_TICKS)
        return replace(self, world=world, ticks=max(ticks, 1))


DEFAULT_TICKS = 120

# Heat generation rates, named so the scenarios below read as intent rather than
# as magic numbers. Equilibrium with no fan is ambient + rate / k_ambient.
IDLE_HEAT = 0.8  # settles around 65C — below the 80C activation threshold
THRESHOLD_HEAT = 1.1  # settles right at 80C — the worst case for a noisy sensor
OVERHEAT_HEAT = 2.0  # runs away to ~124C unless something cools it


def _world(**overrides) -> WorldConfig:
    return WorldConfig(seed=config.SIM_DEFAULT_SEED, **overrides)


SCENARIOS: dict[str, Scenario] = {
    "normal": Scenario(
        name="normal",
        description="Steady idle load. The device never reaches the activation threshold.",
        world=_world(heat_generation_c_per_s=IDLE_HEAT, sensor_noise_c=0.3),
        ticks=DEFAULT_TICKS,
        expects_activation=False,
    ),
    "gradual_overheat": Scenario(
        name="gradual_overheat",
        description="Sustained load drives the device past the threshold over ~25 ticks.",
        world=_world(heat_generation_c_per_s=OVERHEAT_HEAT, sensor_noise_c=0.3),
        ticks=DEFAULT_TICKS,
    ),
    "sudden_spike": Scenario(
        name="sudden_spike",
        description=(
            "A warm device takes a 15C step at tick 8. Tests reaction speed to a "
            "transient rather than endurance: the step is survivable, but only if "
            "cooling is already commanded or arrives immediately."
        ),
        world=_world(
            heat_generation_c_per_s=IDLE_HEAT,
            # Explicitly warm rather than warming up, so the scenario behaves the
            # same whether it is run raw or through `for_verification`.
            initial_device_c=78.0,
            sensor_noise_c=0.3,
            spike_at_tick=8,
            spike_c=15.0,
        ),
        ticks=DEFAULT_TICKS,
    ),
    "noisy_threshold": Scenario(
        name="noisy_threshold",
        description=(
            "The device settles exactly on the 80C threshold with a noisy sensor: "
            "every tick is a coin flip about whether cooling is called for."
        ),
        # Started at the threshold rather than warmed up to it: the interesting
        # ticks are the ones where noise decides the answer, and there is no
        # reason to spend a hundred of them getting there.
        world=_world(
            heat_generation_c_per_s=THRESHOLD_HEAT,
            initial_device_c=79.5,
            sensor_noise_c=2.5,
        ),
        ticks=DEFAULT_TICKS,
    ),
    "sensor_stuck_high": Scenario(
        name="sensor_stuck_high",
        description=(
            "From tick 10 the sensor reports 99C while the device is actually idle. "
            "Cooling a cool device wastes power; it does not endanger it."
        ),
        world=_world(
            heat_generation_c_per_s=IDLE_HEAT,
            sensor_fault=SensorFault.STUCK_HIGH,
            sensor_fault_from_tick=10,
        ),
        ticks=DEFAULT_TICKS,
    ),
    "sensor_stuck_low": Scenario(
        name="sensor_stuck_low",
        description=(
            "From tick 10 the sensor reports 40C while the device really overheats. "
            "Nothing downstream of that sensor can know — the controller and the "
            "supervisor read the same lie. Recorded as physically unwinnable."
        ),
        world=_world(
            heat_generation_c_per_s=OVERHEAT_HEAT,
            sensor_fault=SensorFault.STUCK_LOW,
            sensor_fault_from_tick=10,
        ),
        ticks=DEFAULT_TICKS,
        cooling_is_sufficient=False,
        expects_activation=False,
    ),
    "ineffective_fan": Scenario(
        name="ineffective_fan",
        description=(
            "The fan runs but removes almost no heat. Correct control cannot "
            "compensate for hardware that does not work."
        ),
        world=_world(
            heat_generation_c_per_s=OVERHEAT_HEAT,
            fan_effectiveness=0.02,
            sensor_noise_c=0.3,
        ),
        ticks=DEFAULT_TICKS,
        cooling_is_sufficient=False,
    ),
    "firmware_crash": Scenario(
        name="firmware_crash",
        description=(
            "The controller faults at tick 12 while the device is overheating. "
            "The supervisor's safe state must keep cooling — stopping the "
            "firmware is not by itself safe."
        ),
        world=_world(heat_generation_c_per_s=OVERHEAT_HEAT, sensor_noise_c=0.3),
        ticks=DEFAULT_TICKS,
        faults=ScenarioFaults(controller_crash_tick=12),
    ),
    "network_loss_after_deploy": Scenario(
        name="network_loss_after_deploy",
        description=(
            "The telemetry link drops at tick 5, just after a morph installs. "
            "The lease must still expire locally."
        ),
        world=_world(heat_generation_c_per_s=OVERHEAT_HEAT, sensor_noise_c=0.3),
        ticks=DEFAULT_TICKS,
        faults=ScenarioFaults(server_offline_from_tick=5),
    ),
    "server_failure_after_deploy": Scenario(
        name="server_failure_after_deploy",
        description=(
            "The server goes away at tick 5 and the device supervisor restarts at "
            "tick 20. Persisted slot and lease state must survive both."
        ),
        world=_world(heat_generation_c_per_s=OVERHEAT_HEAT, sensor_noise_c=0.3),
        ticks=DEFAULT_TICKS,
        faults=ScenarioFaults(server_offline_from_tick=5, supervisor_restart_at_tick=20),
    ),
    "repeated_duplicate_triggers": Scenario(
        name="repeated_duplicate_triggers",
        description=(
            "The same heat trigger is delivered three times. One morph must "
            "install, not three."
        ),
        world=_world(heat_generation_c_per_s=OVERHEAT_HEAT, sensor_noise_c=0.3),
        ticks=DEFAULT_TICKS,
        faults=ScenarioFaults(duplicate_trigger_count=3),
    ),
}

# Scenarios the pre-deployment verifier runs. The remaining three are pipeline
# faults — a server that vanishes, a duplicate delivery — which are properties of
# the system around the controller and are exercised by the device/e2e tests.
VERIFICATION_SCENARIOS = tuple(config.VERIFY_REQUIRED_SCENARIOS)


def get(name: str) -> Scenario:
    scenario = SCENARIOS.get(name)
    if scenario is None:
        raise KeyError(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}")
    return scenario


def names() -> list[str]:
    return list(SCENARIOS)
