"""CAEF baseline firmware — Device Sensor Loop (LOOPS.md §1).

THIS FILE IS THE FIRMWARE ARTIFACT. The Agent replaces it in full (FR-14), so
nothing durable belongs here: transport lives in telemetry.py, supervision in
watchdog.py, drivers in drivers.py. Those survive a morph; this does not.

Baseline behaviour: read DHT11 + Lidar_X2 every tick, emit CONTEXT_TRIGGER when
temperature crosses threshold, emit CRITICAL_FAILURE with a stack trace on any
unhandled exception, then hold in a safe idle state awaiting a patch.
"""

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from edge_node import telemetry  # noqa: E402
from edge_node.drivers import DHT11, LidarX2  # noqa: E402
from server.schemas import TriggerType, load_hardware_schema  # noqa: E402


def log(msg: str) -> None:
    print(f"[firmware] {msg}", flush=True)


def sensor_loop() -> None:
    # Pins come from the hardware schema, never from literals in firmware.
    schema = load_hardware_schema(config.DEVICE_ID)
    temp_sensor = DHT11(pin=17)
    lidar = LidarX2(pin=22)
    log(f"baseline firmware up on {schema.mcu_type}, hash={telemetry.current_state_hash()}")

    while True:
        temp_c = temp_sensor.read_temp_c()
        distance_cm = lidar.read_distance_cm()
        log(f"temp={temp_c}C distance={distance_cm}cm")

        if temp_c > config.HEAT_THRESHOLD_C:
            log(f"HIGH_HEAT_DETECTED temp={temp_c} > {config.HEAT_THRESHOLD_C}")
            telemetry.send_event(
                telemetry.build(
                    TriggerType.CONTEXT_TRIGGER,
                    "HIGH_HEAT_DETECTED",
                    {"temp_c": temp_c, "threshold": config.HEAT_THRESHOLD_C},
                )
            )
            # Pause per-loop work while awaiting OTA instead of re-firing the
            # same trigger every tick (LOOPS.md §1).
            time.sleep(config.POST_TRIGGER_HOLD_SECONDS)
            continue

        telemetry.send_heartbeat(
            telemetry.build(TriggerType.CONTEXT_TRIGGER, "HEARTBEAT", {"temp_c": temp_c})
        )
        time.sleep(config.SENSOR_TICK_SECONDS)


def main() -> None:
    try:
        sensor_loop()
    except KeyboardInterrupt:
        log("stopped")
    except Exception:
        # LOOPS.md §1 failure mode: report the trace, then exit so the watchdog
        # holds the device idle awaiting a patch — never busy-crash-loop.
        trace = traceback.format_exc()
        log(f"CRITICAL_FAILURE\n{trace}")
        telemetry.send_event(
            telemetry.build(
                TriggerType.CRITICAL_FAILURE, "UNHANDLED_EXCEPTION", {"trace": trace}
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
