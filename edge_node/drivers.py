"""Sensor Driver Library — the "Docs on comp." RAG corpus (DATA_SCHEMAS.md §8).

Pre-vetted driver snippets the Agent should prefer over writing its own. Each
class takes its pin from the hardware schema; nothing here hardcodes a pin, so
generated code must still go through check_hardware_schema to obtain one.

v0.1 simulates the hardware. On a real Pi these bodies swap for RPi.GPIO /
Adafruit_DHT calls; the interface stays put so generated firmware is unaffected.
"""

import math
import random
import time

import config


class DHT11:
    """Digital 1-wire temperature/humidity sensor. Schema: GPIO_17."""

    def __init__(self, pin: int) -> None:
        self.pin = pin
        self._t0 = time.time()

    def read_temp_c(self) -> float:
        elapsed = time.time() - self._t0
        if config.SCENARIO == "heat":
            # Ramps past HEAT_THRESHOLD_C within a few ticks so a demo doesn't
            # wait on real thermal mass.
            base = 70.0 + elapsed * 4.0
        else:
            base = 45.0 + 3.0 * math.sin(elapsed / 10.0)
        # Real DHT11s are noisy and read a few degrees off; the offset is the
        # per-device calibration knob, not a simulation artifact.
        return round(min(base, 120.0) + random.uniform(-0.4, 0.4) + config.SENSOR_TEMP_OFFSET_C, 1)


class RelayFan:
    """Active-HIGH relay-driven fan. Schema: GPIO_27, dormant until a morph."""

    def __init__(self, pin: int, active_level: str = "HIGH") -> None:
        self.pin = pin
        self.active_level = active_level
        self.state = False

    def on(self) -> None:
        self.state = True
        print(f"[fan] GPIO_{self.pin} -> {self.active_level} (fan ON)", flush=True)

    def off(self) -> None:
        self.state = False
        print(f"[fan] GPIO_{self.pin} -> idle (fan OFF)", flush=True)


class LidarX2:
    """UART range finder. Schema: GPIO_22. The nonessential driver a heat morph
    is expected to strip to free CPU (PRD Scenario A step 3)."""

    def __init__(self, pin: int) -> None:
        self.pin = pin
        self.buffer: list[float] = []

    def read_distance_cm(self) -> float:
        # Deliberately CPU-wasteful: gives "strip the Lidar driver" a measurable
        # effect in the sandbox's resource numbers.
        for _ in range(20_000):
            math.sqrt(random.random())
        reading = round(random.uniform(20.0, 400.0), 1)
        self.buffer.append(reading)
        if len(self.buffer) > 10:
            self.buffer.pop(0)
        return reading
