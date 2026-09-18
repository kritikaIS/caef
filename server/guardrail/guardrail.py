"""Guard Rail — deterministic pre-execution static gate (SAFETY_PROTOCOL.md §2).

Runs after Agent generation, before the code executes anywhere including the
Sandbox. No LLM call inside, no I/O, no "trusted patch" fast path: same input
always yields the same verdict (NFR-1, CLAUDE.md §4).

The five checks are exactly SAFETY_PROTOCOL.md §2.1–§2.5, one function each, in
that order. Any single failure rejects; the structured `reason` is fed back to
the Agent identically to a Sandbox failure and counts toward the same retry
budget (§4).

Analysis is AST-based rather than textual, so a pin mentioned only in a `#`
comment is not treated as usage (§2.1 requires cross-checking actual usage).
String literals and docstrings *are* AST nodes and do count — deliberately
conservative: a spurious rejection is cheap, a missed forbidden-pin write is not.
"""

import ast
import re
from dataclasses import dataclass, field

import config
from server.schemas import (
    TOOL_CHECK_HARDWARE_SCHEMA,
    TOOL_RESULT_SAFE_PREFIX,
    AgentOutput,
    GuardRailChecks,
    GuardRailResult,
    HardwareSchema,
    load_hardware_schema,
)

# `GPIO_<n>` appearing as a string literal or identifier.
GPIO_LITERAL = re.compile(r"\bGPIO_(\d+)\b")
# Keyword arguments whose integer value is a pin number.
PIN_KWARGS = ("pin", "pin_number", "gpio")
# Keyword arguments declaring a current draw, for the §2.5 sanity check.
CURRENT_KWARGS = ("current_ma", "draw_ma", "current")
_MILLIAMPS = re.compile(r"([\d.]+)\s*mA", re.IGNORECASE)

# Sensor Driver Library class -> the `connected_device` it is valid for. Used by
# the schema conformance check: instantiating RelayFan on the DHT11's pin is a
# schema violation even though both pins are individually permitted.
DRIVER_DEVICES = {
    "DHT11": "DHT11",
    "RelayFan": "Relay_Fan",
    "LidarX2": "Lidar_X2",
}
# Module/symbol markers implying a bus protocol, checked against the schema's
# declared `protocol` for the pins the code targets.
PROTOCOL_MARKERS = {
    "I2C": ("smbus", "SMBus", "i2c", "I2C"),
    "SPI": ("spidev", "SpiDev", "SPI"),
    "UART": ("serial", "Serial", "UART"),
}
# Calls that turn the firmware into a listening server rather than the defined
# telemetry client (SAFETY_PROTOCOL.md §2.4).
SOCKET_SERVER_CALLS = ("bind", "listen", "accept")
# Calls that make a `while True` bounded enough not to be a busy spin.
YIELDING_CALLS = ("sleep", "wait", "join", "select", "recv", "accept", "read")


@dataclass
class _Scan:
    """Everything the five checks need, collected in one AST pass."""

    pins: set[int] = field(default_factory=set)
    driver_pins: list[tuple[str, int]] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    socket_server_calls: list[str] = field(default_factory=list)
    busy_loops: int = 0
    current_declarations: list[tuple[int | None, float]] = field(default_factory=list)


def _dotted(node: ast.AST) -> str | None:
    """Flatten `os.path.join` into "os.path.join"; None if not a name chain."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scan = _Scan()

    # --- pins ----------------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.scan.pins.update(int(n) for n in GPIO_LITERAL.findall(node.value))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.scan.symbols.add(node.id)
        self.scan.pins.update(int(n) for n in GPIO_LITERAL.findall(node.id))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.scan.symbols.add(node.attr)
        self.scan.pins.update(int(n) for n in GPIO_LITERAL.findall(node.attr))
        self.generic_visit(node)

    # --- imports / calls -----------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.scan.imports.add(alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.scan.imports.add(node.module.split(".")[0])
        for alias in node.names:
            self.scan.symbols.add(alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        if name:
            self.scan.calls.add(name)
            if name.rsplit(".", 1)[-1] in SOCKET_SERVER_CALLS:
                self.scan.socket_server_calls.append(name)

        driver = name.rsplit(".", 1)[-1] if name else None
        pin_here: int | None = None

        for keyword in node.keywords:
            if keyword.arg in PIN_KWARGS and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, int):
                    pin_here = keyword.value.value
                    self.scan.pins.add(pin_here)
            elif keyword.arg in CURRENT_KWARGS and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, (int, float)):
                    self.scan.current_declarations.append((None, float(keyword.value.value)))

        if driver in DRIVER_DEVICES:
            # A driver's first positional argument is its pin, by the Sensor
            # Driver Library's own signature.
            for arg in node.args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                    pin_here = arg.value
                    self.scan.pins.add(pin_here)
            if pin_here is not None:
                self.scan.driver_pins.append((driver, pin_here))

        if pin_here is not None:
            # Attribute the most recent bare current declaration to this pin.
            for index, (attributed, value) in enumerate(self.scan.current_declarations):
                if attributed is None:
                    self.scan.current_declarations[index] = (pin_here, value)

        self.generic_visit(node)

    # --- loops ---------------------------------------------------------------

    def visit_While(self, node: ast.While) -> None:
        unbounded = isinstance(node.test, ast.Constant) and node.test.value is True
        if unbounded and not self._yields(node.body):
            self.scan.busy_loops += 1
        self.generic_visit(node)

    @staticmethod
    def _yields(body: list[ast.stmt]) -> bool:
        """A loop body that sleeps, yields, breaks or returns is not a busy spin."""
        for statement in body:
            for node in ast.walk(statement):
                if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Break, ast.Return)):
                    return True
                if isinstance(node, ast.Call):
                    name = _dotted(node.func)
                    if name and name.rsplit(".", 1)[-1] in YIELDING_CALLS:
                        return True
        return False


def _validated_pins(output: AgentOutput) -> set[int]:
    """Pins with a successful `check_hardware_schema` call in this patch's trace."""
    validated: set[int] = set()
    for call in output.tool_calls:
        if call.tool != TOOL_CHECK_HARDWARE_SCHEMA:
            continue
        if not call.result.startswith(TOOL_RESULT_SAFE_PREFIX):
            continue
        pin = call.args.get("pin_number")
        if isinstance(pin, int):
            validated.add(pin)
    return validated


def _max_current_ma(schema: HardwareSchema) -> float | None:
    match = _MILLIAMPS.search(schema.constraints.max_gpio_current)
    return float(match.group(1)) if match else None


# --- the five checks ---------------------------------------------------------


def _check_forbidden_pins(scan: _Scan, schema: HardwareSchema) -> list[str]:
    """§2.1 — both the constraints denylist and per-pin `status: forbidden`."""
    return [
        f"forbidden pin GPIO_{pin} referenced in generated code"
        for pin in sorted(scan.pins)
        if schema.is_forbidden(pin)
    ]


def _check_tool_call_provenance(scan: _Scan, output: AgentOutput) -> list[str]:
    """§2.2 / NFR-3 — no pin literal without a logged successful tool call."""
    validated = _validated_pins(output)
    reasons = [
        f"GPIO_{pin} used without a successful {TOOL_CHECK_HARDWARE_SCHEMA}({pin}) tool call"
        for pin in sorted(scan.pins - validated)
    ]
    # The Agent's own declaration must match what the code actually does; an
    # under-declared pin would otherwise slip past a trace-only reviewer.
    undeclared = scan.pins - set(output.pins_referenced)
    if undeclared:
        reasons.append(
            "pins_referenced omits pins used in code: "
            + ", ".join(f"GPIO_{pin}" for pin in sorted(undeclared))
        )
    return reasons


def _check_schema_conformance(scan: _Scan, schema: HardwareSchema) -> list[str]:
    """§2.3 — the code must not assume a device/protocol the schema contradicts."""
    reasons: list[str] = []
    for pin in sorted(scan.pins):
        if schema.pin(pin) is None:
            reasons.append(f"GPIO_{pin} is not present in the device hardware schema")

    for driver, pin in scan.driver_pins:
        entry = schema.pin(pin)
        if entry is None:
            continue  # already reported above
        expected = DRIVER_DEVICES[driver]
        if entry.connected_device != expected:
            reasons.append(
                f"{driver} driver targets GPIO_{pin}, which the schema declares as "
                f"{entry.connected_device}, not {expected}"
            )

    declared_protocols = {
        schema.pin(pin).protocol for pin in scan.pins if schema.pin(pin) is not None
    }
    for protocol, markers in PROTOCOL_MARKERS.items():
        used = scan.imports | scan.symbols
        if not used.intersection(markers):
            continue
        if not any(p and protocol.lower() in p.lower() for p in declared_protocols):
            reasons.append(
                f"code uses {protocol} but no targeted pin is schema-declared as {protocol}"
            )
    return reasons


def _check_static_safety(scan: _Scan) -> list[str]:
    """§2.4 — config-driven denylist, plus busy-loop and socket-server shapes."""
    reasons = [
        f"disallowed import: {name}"
        for name in sorted(scan.imports.intersection(config.DENYLIST_IMPORTS))
    ]
    denied_calls = set(config.DENYLIST_CALLS)
    for call in sorted(scan.calls):
        if call in denied_calls or ("." not in call and call in denied_calls):
            reasons.append(f"disallowed call: {call}()")
    for call in sorted(set(scan.socket_server_calls)):
        reasons.append(f"raw socket server call outside the telemetry client: {call}()")
    if scan.busy_loops:
        reasons.append(
            f"{scan.busy_loops} unbounded `while True` loop(s) with no sleep/yield/break"
        )
    return reasons


def _check_current_draw(scan: _Scan, schema: HardwareSchema) -> list[str]:
    """§2.5 — best-effort only.

    Catches an *explicitly declared* current that exceeds `max_gpio_current`.
    It cannot infer real draw from driver semantics, and is documented as no
    substitute for physical hardware protection.
    """
    limit = _max_current_ma(schema)
    if limit is None:
        return []
    return [
        f"declared current {value}mA on "
        + (f"GPIO_{pin}" if pin is not None else "an actuator")
        + f" exceeds max_gpio_current {limit}mA"
        for pin, value in scan.current_declarations
        if value > limit
    ]


def check(output: AgentOutput, schema: HardwareSchema | None = None) -> GuardRailResult:
    """Run all five checks. Pure: no DB, no network, no model call."""
    schema = schema or load_hardware_schema(output.device_id)

    try:
        tree = ast.parse(output.code)
    except SyntaxError as exc:
        # Unparseable code can't be analysed, so it can't be cleared. Reported
        # under schema conformance: it does not conform to being valid firmware.
        return GuardRailResult(
            patch_id=output.patch_id,
            status="fail",
            checks=GuardRailChecks(
                forbidden_pin_check="pass",
                tool_call_provenance="pass",
                schema_conformance="fail",
                static_safety_denylist="pass",
                current_draw_sanity="pass",
            ),
            reason=f"syntax_error: {exc}",
        )

    collector = _Collector()
    collector.visit(tree)
    scan = collector.scan

    failures = {
        "forbidden_pin_check": _check_forbidden_pins(scan, schema),
        "tool_call_provenance": _check_tool_call_provenance(scan, output),
        "schema_conformance": _check_schema_conformance(scan, schema),
        "static_safety_denylist": _check_static_safety(scan),
        "current_draw_sanity": _check_current_draw(scan, schema),
    }

    checks = GuardRailChecks(
        **{name: ("fail" if reasons else "pass") for name, reasons in failures.items()}
    )
    all_reasons = [reason for reasons in failures.values() for reason in reasons]
    return GuardRailResult(
        patch_id=output.patch_id,
        status="fail" if all_reasons else "pass",
        checks=checks,
        reason="; ".join(all_reasons) if all_reasons else None,
    )
