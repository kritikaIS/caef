"""Faulty firmware fixture for PRD Scenario B (Auto-Patching).

Off-by-one: the loop runs to len(buffer) inclusive, so buffer[10] on a 10-element
buffer raises IndexError. Deployed via OTA push like any other firmware, so the
crash path is exercised exactly as a real regression would arrive.
"""

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import config  # noqa: E402
from edge_node import telemetry  # noqa: E402
from edge_node.drivers import DHT11  # noqa: E402
from server.schemas import TriggerType  # noqa: E402


def log(msg: str) -> None:
    print(f"[firmware] {msg}", flush=True)


def sensor_loop() -> None:
    temp_sensor = DHT11(pin=17)
    buffer = [0.0] * 10
    log("faulty firmware up (scenario B)")

    while True:
        buffer.append(temp_sensor.read_temp_c())
        buffer.pop(0)
        # BUG: range is off by one — i reaches len(buffer), indexing past the end.
        for i in range(len(buffer) + 1):
            log(f"sample[{i}]={buffer[i]}")
        time.sleep(config.SENSOR_TICK_SECONDS)


def main() -> None:
    try:
        sensor_loop()
    except KeyboardInterrupt:
        log("stopped")
    except Exception:
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
