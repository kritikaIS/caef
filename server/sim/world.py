"""ThermalWorld — the closed-loop virtual environment (RESEARCH.md §5).

This replaces the baseline's open-loop sensor simulation in manifest mode. The
difference is the whole point: in `edge_node/drivers.py` a reading is a function
of elapsed wall time and `random`, so turning the fan on cannot change what the
next reading says and no cooling behaviour is verifiable. Here the fan is a term
in the temperature update, so "the controller cooled the device" is a claim the
simulation can actually settle.

Two rules make a run reproducible:

  - **Ticks, never sleeps.** The world advances only when `step()` is called, so
    a run's outcome does not depend on how fast the machine executing it is.
  - **One seeded generator.** Every stochastic quantity — sensor noise, the
    Lidar's reading — is drawn from `random.Random(seed)` in a fixed order
    inside `step()`, so a reading is pure with respect to when it is read.

Thermal model: first-order lumped capacitance.

    T[t+1] = T[t] + dt * ( heat_rate * load
                         - k_ambient      * (T[t] - ambient)
                         - k_fan * eff * fan * (T[t] - ambient) )

`fan` is 0 or 1 and `k_fan > 0`, so fan state provably influences future
readings. This is an approximation chosen to be legible and monotone, not a
calibrated thermal model of any real board — no physical hardware was involved
in fitting it, and none of the constants should be read as characterising one.
"""

import random
from dataclasses import dataclass, field, replace
from enum import StrEnum

import config


class SensorFault(StrEnum):
    NONE = "none"
    STUCK_HIGH = "stuck_high"
    STUCK_LOW = "stuck_low"


@dataclass(frozen=True)
class WorldConfig:
    """Every physical constant of one simulated device, in one frozen record.

    Frozen and copied with `replace()` rather than mutated, so a scenario cannot
    be altered halfway through a run and a seed always identifies exactly one
    trajectory.
    """

    seed: int = config.SIM_DEFAULT_SEED
    tick_seconds: float = config.SIM_TICK_SECONDS
    ambient_c: float = 25.0
    initial_device_c: float = 45.0
    # Degrees per second the device would gain at full load with no losses.
    heat_generation_c_per_s: float = 0.8
    # CPU/resource load, 0..1, scaling heat generation.
    load: float = 1.0
    # Passive loss coefficient to ambient, per second.
    k_ambient: float = 0.02
    # Additional loss coefficient while the fan runs, per second.
    k_fan: float = 0.15
    # How much of `k_fan` this particular fan actually delivers. 1.0 is a
    # healthy fan; a small value is a fan that spins but does not cool.
    fan_effectiveness: float = 1.0
    # Standard deviation of the temperature sensor's noise, in degrees.
    sensor_noise_c: float = 0.0
    sensor_fault: SensorFault = SensorFault.NONE
    sensor_fault_from_tick: int = 0
    stuck_high_c: float = 99.0
    stuck_low_c: float = 40.0
    # A one-off thermal event: at this tick, add `spike_c` to the device.
    spike_at_tick: int | None = None
    spike_c: float = 0.0
    # Load steps as (tick, load) pairs, applied in order.
    load_schedule: tuple[tuple[int, float], ...] = field(default_factory=tuple)


@dataclass
class WorldSnapshot:
    """One row of the trace. Both the true state and what the sensor claimed,
    so a fault scenario can be told apart from a control failure."""

    tick: int
    time_s: float
    device_temp_c: float
    sensor_temp_c: float
    ambient_c: float
    fan_state: str
    load: float
    sensor_fault: str


class ThermalWorld:
    """The simulated device. Only the supervisor may move an actuator."""

    def __init__(self, world_config: WorldConfig | None = None) -> None:
        self.config = world_config or WorldConfig()
        self._rng = random.Random(self.config.seed)
        self.tick = 0
        self.time_s = 0.0
        self.device_temp_c = self.config.initial_device_c
        self.load = self.config.load
        self.actuators: dict[str, str] = {"fan": "off"}
        self._noise_c = 0.0
        self._distance_cm = 0.0
        self._draw_stochastics()
        # Bookkeeping the verifier reads back: who moved what, and when.
        self.actuator_transitions = 0
        self.transition_log: list[tuple[int, str, str]] = []

    # --- actuators (supervisor-only) -----------------------------------------

    def set_actuator(self, name: str, state: str) -> None:
        """Apply an actuator change.

        Called by the safety supervisor and by nothing else. A compiled
        controller has no reference to a world object and no capability that
        reaches one — it returns intents (RESEARCH.md §7).
        """
        if name not in self.actuators:
            raise KeyError(f"no actuator {name!r} in this world")
        if self.actuators[name] != state:
            self.actuators[name] = state
            self.actuator_transitions += 1
            self.transition_log.append((self.tick, name, state))

    @property
    def fan_on(self) -> bool:
        return self.actuators.get("fan") == "on"

    # --- time ----------------------------------------------------------------

    def step(self) -> WorldSnapshot:
        """Advance one tick and return the new snapshot.

        Order matters and is fixed: apply scheduled events, integrate the
        thermal model, then draw this tick's stochastic quantities. Reads are
        pure afterwards, so a controller that samples a metric twice in one step
        sees the same number both times.
        """
        self.tick += 1
        self.time_s = round(self.tick * self.config.tick_seconds, 6)

        for at_tick, load in self.config.load_schedule:
            if self.tick == at_tick:
                self.load = load

        if self.config.spike_at_tick is not None and self.tick == self.config.spike_at_tick:
            self.device_temp_c += self.config.spike_c

        delta_to_ambient = self.device_temp_c - self.config.ambient_c
        cooling = self.config.k_fan * self.config.fan_effectiveness if self.fan_on else 0.0
        change = self.config.tick_seconds * (
            self.config.heat_generation_c_per_s * self.load
            - self.config.k_ambient * delta_to_ambient
            - cooling * delta_to_ambient
        )
        self.device_temp_c = round(self.device_temp_c + change, 6)
        self._draw_stochastics()
        return self.snapshot()

    def _draw_stochastics(self) -> None:
        """Draw every random quantity for this tick, in a fixed order."""
        self._noise_c = (
            self._rng.gauss(0.0, self.config.sensor_noise_c)
            if self.config.sensor_noise_c
            else 0.0
        )
        self._distance_cm = round(self._rng.uniform(20.0, 400.0), 2)

    # --- sensors -------------------------------------------------------------

    def read_temperature_c(self) -> float:
        """What the temperature sensor *claims*, which is not the truth under a
        fault. The controller only ever sees this."""
        fault = self.config.sensor_fault
        if fault is not SensorFault.NONE and self.tick >= self.config.sensor_fault_from_tick:
            if fault is SensorFault.STUCK_HIGH:
                return round(self.config.stuck_high_c, 2)
            if fault is SensorFault.STUCK_LOW:
                return round(self.config.stuck_low_c, 2)
        return round(self.device_temp_c + self._noise_c, 2)

    def read_distance_cm(self) -> float:
        return self._distance_cm

    def metrics(self) -> dict[str, float]:
        """The observation handed to a controller each control step."""
        return {
            "temperature_c": self.read_temperature_c(),
            "distance_cm": self.read_distance_cm(),
        }

    def snapshot(self) -> WorldSnapshot:
        return WorldSnapshot(
            tick=self.tick,
            time_s=self.time_s,
            device_temp_c=round(self.device_temp_c, 3),
            sensor_temp_c=self.read_temperature_c(),
            ambient_c=self.config.ambient_c,
            fan_state=self.actuators.get("fan", "off"),
            load=self.load,
            sensor_fault=self.config.sensor_fault.value,
        )


def with_seed(world_config: WorldConfig, seed: int) -> WorldConfig:
    return replace(world_config, seed=seed)
