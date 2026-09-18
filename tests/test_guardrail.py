"""M4 check: every SAFETY_PROTOCOL.md §2 rule, against hand-written good/bad
firmware. No LLM anywhere in this file (TDD.md §5) — Guard Rail is deterministic
by construction and its tests must prove that."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.guardrail import guardrail  # noqa: E402
from server.schemas import (  # noqa: E402
    TOOL_CHECK_HARDWARE_SCHEMA,
    AgentOutput,
    ToolCallRecord,
    load_hardware_schema,
)

SCHEMA = load_hardware_schema("dev01")

# The morph the Agent is expected to produce for PRD Scenario A: enable the
# dormant fan on GPIO_27, drop the Lidar driver to free CPU.
GOOD_MORPH = """
import time

import config
from edge_node import telemetry
from edge_node.drivers import DHT11, RelayFan
from server.schemas import TriggerType


def sensor_loop():
    temp_sensor = DHT11(pin=17)
    fan = RelayFan(pin=27)
    fan.on()
    while True:
        temp_c = temp_sensor.read_temp_c()
        telemetry.send_heartbeat(
            telemetry.build(TriggerType.CONTEXT_TRIGGER, "HEARTBEAT", {"temp_c": temp_c})
        )
        time.sleep(config.SENSOR_TICK_SECONDS)
"""

GOOD_TRACE = [
    ToolCallRecord(
        tool=TOOL_CHECK_HARDWARE_SCHEMA,
        args={"pin_number": 17},
        result="SAFE: Connected to DHT11",
    ),
    ToolCallRecord(
        tool=TOOL_CHECK_HARDWARE_SCHEMA,
        args={"pin_number": 27},
        result="SAFE: Connected to Relay_Fan",
    ),
]


def patch(code: str, pins=(17, 27), trace=None, patch_id="p1") -> AgentOutput:
    return AgentOutput(
        patch_id=patch_id,
        event_id="e1",
        device_id="pi_node_alpha",
        plan="Enable Relay_Fan on GPIO_27; drop Lidar_X2 to free CPU.",
        target_file="main.py",
        code=code,
        pins_referenced=list(pins),
        tool_calls=list(GOOD_TRACE if trace is None else trace),
    )


def test_valid_morph_passes_every_check():
    result = guardrail.check(patch(GOOD_MORPH), SCHEMA)
    assert result.status == "pass", result.reason
    assert result.reason is None
    assert set(result.checks.model_dump().values()) == {"pass"}


def test_forbidden_pin_in_code_is_rejected():
    """§2.1 — forbidden_pins [0, 1] must never appear in generated code."""
    code = GOOD_MORPH.replace("RelayFan(pin=27)", "RelayFan(pin=1)")
    trace = GOOD_TRACE + [
        # Even with a tool call in the trace, a forbidden pin stays forbidden:
        # provenance does not launder a denylisted pin.
        ToolCallRecord(
            tool=TOOL_CHECK_HARDWARE_SCHEMA,
            args={"pin_number": 1},
            result="SAFE: Connected to UART_TX",
        )
    ]
    result = guardrail.check(patch(code, pins=(17, 1), trace=trace), SCHEMA)
    assert result.status == "fail"
    assert result.checks.forbidden_pin_check == "fail"
    assert "GPIO_1" in result.reason


def test_extra_forbidden_pins_from_config_are_enforced(monkeypatch):
    """Pipeline-wide denylist extension, not just the per-device schema."""
    monkeypatch.setattr(config, "EXTRA_FORBIDDEN_PINS", [27])
    result = guardrail.check(patch(GOOD_MORPH), SCHEMA)
    assert result.status == "fail"
    assert result.checks.forbidden_pin_check == "fail"
    assert "GPIO_27" in result.reason


def test_pin_without_tool_call_is_rejected():
    """§2.2 / NFR-3 — the core patent claim, checked mechanically."""
    result = guardrail.check(patch(GOOD_MORPH, trace=GOOD_TRACE[:1]), SCHEMA)
    assert result.status == "fail"
    assert result.checks.tool_call_provenance == "fail"
    assert f"{TOOL_CHECK_HARDWARE_SCHEMA}(27)" in result.reason


def test_unsuccessful_tool_call_is_not_provenance():
    """A tool call that did not resolve SAFE proves nothing."""
    trace = GOOD_TRACE[:1] + [
        ToolCallRecord(
            tool=TOOL_CHECK_HARDWARE_SCHEMA,
            args={"pin_number": 27},
            result="ERROR: pin not found",
        )
    ]
    result = guardrail.check(patch(GOOD_MORPH, trace=trace), SCHEMA)
    assert result.status == "fail"
    assert result.checks.tool_call_provenance == "fail"


def test_pin_used_in_code_but_omitted_from_pins_referenced_is_rejected():
    """The declared trace must match what the code actually does."""
    result = guardrail.check(patch(GOOD_MORPH, pins=(17,)), SCHEMA)
    assert result.status == "fail"
    assert result.checks.tool_call_provenance == "fail"
    assert "pins_referenced omits" in result.reason


def test_pin_mentioned_only_in_a_comment_is_not_usage():
    """§2.1 says cross-check actual usage, not bare text mentions."""
    code = GOOD_MORPH + "\n# GPIO_0 is the console UART — deliberately untouched.\n"
    result = guardrail.check(patch(code), SCHEMA)
    assert result.status == "pass", result.reason


def test_pin_absent_from_schema_is_rejected():
    """§2.3 — the Agent cannot invent a pin that isn't physically there."""
    code = GOOD_MORPH.replace("RelayFan(pin=27)", "RelayFan(pin=99)")
    trace = GOOD_TRACE + [
        ToolCallRecord(
            tool=TOOL_CHECK_HARDWARE_SCHEMA,
            args={"pin_number": 99},
            result="SAFE: hallucinated",
        )
    ]
    result = guardrail.check(patch(code, pins=(17, 99), trace=trace), SCHEMA)
    assert result.status == "fail"
    assert result.checks.schema_conformance == "fail"
    assert "not present in the device hardware schema" in result.reason


def test_driver_on_the_wrong_pin_is_rejected():
    """§2.3 — RelayFan on the DHT11's pin, both individually permitted."""
    code = GOOD_MORPH.replace("RelayFan(pin=27)", "RelayFan(pin=17)")
    result = guardrail.check(patch(code, pins=(17,), trace=GOOD_TRACE[:1]), SCHEMA)
    assert result.status == "fail"
    assert result.checks.schema_conformance == "fail"
    assert "schema declares as DHT11" in result.reason


def test_wrong_protocol_for_the_targeted_pin_is_rejected():
    """§2.3's literal example: I2C setup written to a digital_1wire pin."""
    code = "import smbus\nfrom edge_node.drivers import DHT11\n\nbus = smbus.SMBus(1)\ns = DHT11(pin=17)\n"
    result = guardrail.check(patch(code, pins=(17,), trace=GOOD_TRACE[:1]), SCHEMA)
    assert result.status == "fail"
    assert result.checks.schema_conformance == "fail"
    assert "I2C" in result.reason


def test_uart_is_allowed_on_the_pin_that_declares_it():
    """GPIO_22/Lidar_X2 is schema-declared UART, so serial use conforms."""
    code = "import serial\nfrom edge_node.drivers import LidarX2\n\nlidar = LidarX2(pin=22)\n"
    trace = [
        ToolCallRecord(
            tool=TOOL_CHECK_HARDWARE_SCHEMA,
            args={"pin_number": 22},
            result="SAFE: Connected to Lidar_X2",
        )
    ]
    result = guardrail.check(patch(code, pins=(22,), trace=trace), SCHEMA)
    assert result.status == "pass", result.reason


@pytest.mark.parametrize(
    "snippet,fragment",
    [
        ("import subprocess\nsubprocess.run(['ls'])\n", "disallowed import: subprocess"),
        ("import ctypes\n", "disallowed import: ctypes"),
        ("eval('1+1')\n", "disallowed call: eval()"),
        ("exec('x = 1')\n", "disallowed call: exec()"),
        ("import os\nos.system('rm -rf /')\n", "disallowed call: os.system()"),
    ],
)
def test_static_safety_denylist(snippet, fragment):
    """§2.4 — the config-driven denylist, one case per starting-set entry."""
    result = guardrail.check(patch(snippet, pins=()), SCHEMA)
    assert result.status == "fail"
    assert result.checks.static_safety_denylist == "fail"
    assert fragment in result.reason


def test_denylist_is_config_driven_not_hardcoded(monkeypatch):
    """CLAUDE.md §4: safety constants live in config, so extending config
    must change the verdict with no code edit."""
    code = "import telnetlib\n"
    assert guardrail.check(patch(code, pins=()), SCHEMA).status == "pass"
    monkeypatch.setattr(config, "DENYLIST_IMPORTS", [*config.DENYLIST_IMPORTS, "telnetlib"])
    assert guardrail.check(patch(code, pins=()), SCHEMA).status == "fail"


def test_busy_loop_without_sleep_is_rejected():
    """§2.4 — unbounded `while True` with no yield/sleep."""
    code = "x = 0\nwhile True:\n    x += 1\n"
    result = guardrail.check(patch(code, pins=()), SCHEMA)
    assert result.status == "fail"
    assert result.checks.static_safety_denylist == "fail"
    assert "while True" in result.reason


def test_while_true_with_sleep_is_allowed():
    """The baseline firmware's own shape must not be rejected."""
    code = "import time\nwhile True:\n    time.sleep(1)\n"
    assert guardrail.check(patch(code, pins=()), SCHEMA).status == "pass"


def test_raw_socket_server_is_rejected():
    """§2.4 — firmware is a telemetry client, not a listening server."""
    code = "import socket\ns = socket.socket()\ns.bind(('0.0.0.0', 9999))\ns.listen(1)\n"
    result = guardrail.check(patch(code, pins=()), SCHEMA)
    assert result.status == "fail"
    assert result.checks.static_safety_denylist == "fail"
    assert "socket server" in result.reason


def test_declared_current_over_the_schema_limit_is_rejected():
    """§2.5 — best-effort: only an explicitly declared draw is catchable."""
    code = "from edge_node.drivers import RelayFan\n\nfan = RelayFan(pin=27, current_ma=250)\n"
    result = guardrail.check(patch(code, pins=(27,), trace=GOOD_TRACE[1:]), SCHEMA)
    assert result.status == "fail"
    assert result.checks.current_draw_sanity == "fail"
    assert "16.0mA" in result.reason


def test_declared_current_within_the_limit_passes():
    code = "from edge_node.drivers import RelayFan\n\nfan = RelayFan(pin=27, current_ma=8)\n"
    assert guardrail.check(patch(code, pins=(27,), trace=GOOD_TRACE[1:]), SCHEMA).status == "pass"


def test_unparseable_code_is_rejected_not_crashed():
    result = guardrail.check(patch("def broken(:\n", pins=()), SCHEMA)
    assert result.status == "fail"
    assert "syntax_error" in result.reason


def test_baseline_firmware_passes_its_own_guard_rail():
    """Regression floor: the firmware we ship must survive our own gate."""
    code = (ROOT / "edge_node" / "main.py").read_text()
    trace = GOOD_TRACE[:1] + [
        ToolCallRecord(
            tool=TOOL_CHECK_HARDWARE_SCHEMA,
            args={"pin_number": 22},
            result="SAFE: Connected to Lidar_X2",
        )
    ]
    result = guardrail.check(patch(code, pins=(17, 22), trace=trace), SCHEMA)
    assert result.status == "pass", result.reason


def test_verdict_is_deterministic():
    """NFR-1: same input, same verdict, every time — no model in the loop."""
    verdicts = {guardrail.check(patch(GOOD_MORPH), SCHEMA).model_dump_json() for _ in range(5)}
    assert len(verdicts) == 1


def test_multiple_failures_are_all_reported():
    """One rejection is enough, but the Agent gets every reason at once so a
    retry doesn't burn budget fixing them one at a time."""
    code = "import subprocess\nfrom edge_node.drivers import RelayFan\n\nfan = RelayFan(pin=0)\n"
    result = guardrail.check(patch(code, pins=(0,), trace=[]), SCHEMA)
    assert result.status == "fail"
    assert result.checks.forbidden_pin_check == "fail"
    assert result.checks.tool_call_provenance == "fail"
    assert result.checks.static_safety_denylist == "fail"
