"""M13 — the deterministic compiler and the controller runtime (RESEARCH.md §4).

What these tests are for: an artifact must be a pure function of its inputs, it
must contain nothing the registry did not author, and the thing that executes it
must be incapable of executing anything else.
"""

from pathlib import Path

import pytest

import config
from server.compiler import compiler, runtime
from server.compiler.compiler import compile_manifest, select_pattern
from server.compiler.program import ControllerProgram
from server.compiler.runtime import (
    BudgetExceeded,
    CompiledController,
    ControllerContext,
    ControllerFault,
    Observation,
)
from server.manifest.registry import load_registry
from server.schemas import load_hardware_schema
from tests.fixtures import manifests

FULL_COOLING = [
    "read_temperature",
    "fan_on",
    "fan_off",
    "emit_heartbeat",
    "emit_context_event",
    "emit_critical_event",
    "enter_safe_idle",
]


@pytest.fixture
def registry():
    return load_registry("1.0.0")


@pytest.fixture
def schema():
    return load_hardware_schema("pi_node_alpha")


@pytest.fixture
def cooling_program(registry, schema) -> ControllerProgram:
    result = compile_manifest(manifests.cooling(), registry, schema)
    assert result.status == "pass", result.report.errors
    return result.program


def drive(program: ControllerProgram, readings: list[float]) -> tuple[CompiledController, list]:
    """Run a controller over a fixed reading series. No world, no supervisor —
    just the interpreter, so these tests isolate its behaviour."""
    controller = CompiledController(program)
    controller.initialize(
        ControllerContext(device_id=program.device_id, started_at_s=0.0, actuator_states={"fan": "off"})
    )
    fan = "off"
    emitted = []
    for tick, reading in enumerate(readings, start=1):
        intents = controller.step(
            Observation(
                tick=tick,
                time_s=float(tick),
                metrics={"temperature_c": reading},
                actuator_states={"fan": fan},
            )
        )
        for intent in intents:
            if intent.kind == "actuator" and intent.actuator == "fan":
                fan = intent.state
        emitted.append(intents)
    return controller, emitted


# --- determinism -------------------------------------------------------------


def test_identical_manifests_compile_to_identical_artifacts(registry, schema):
    first = compile_manifest(manifests.cooling(), registry, schema)
    second = compile_manifest(manifests.cooling(), registry, schema)
    assert first.program.artifact_hash == second.program.artifact_hash
    from server.manifest.canonical import canonical_bytes

    assert canonical_bytes(first.program.model_dump(mode="json")) == canonical_bytes(
        second.program.model_dump(mode="json")
    )


def test_capability_order_does_not_change_the_compiled_behaviour(registry, schema):
    """A model listing its capabilities in a different order asked for the same
    thing and must get the same controller.

    The two artifacts are not byte-identical — they embed `manifest_hash`, and
    the two manifests genuinely differ — but nothing about what the controller
    *does* may depend on the order the model happened to type.
    """
    forward = compile_manifest(manifests.cooling(requested_capabilities=FULL_COOLING), registry, schema)
    reverse = compile_manifest(
        manifests.cooling(requested_capabilities=list(reversed(FULL_COOLING))), registry, schema
    )
    ignore = {"manifest_hash", "manifest_id"}
    forward_payload = forward.program.model_dump(mode="json")
    reverse_payload = reverse.program.model_dump(mode="json")
    assert {k: v for k, v in forward_payload.items() if k not in ignore} == {
        k: v for k, v in reverse_payload.items() if k not in ignore
    }


def test_a_different_threshold_changes_the_artifact(registry, schema):
    baseline = compile_manifest(manifests.cooling(), registry, schema).program.artifact_hash
    changed = compile_manifest(
        manifests.cooling(
            activation_condition={"metric": "temperature_c", "operator": ">=", "value": 78.0}
        ),
        registry,
        schema,
    ).program.artifact_hash
    assert changed != baseline


def test_artifact_records_its_provenance(cooling_program, registry):
    assert cooling_program.manifest_id == manifests.COOLING["manifest_id"]
    assert cooling_program.event_id == manifests.COOLING["event_id"]
    assert cooling_program.device_id == manifests.COOLING["device_id"]
    assert cooling_program.source_firmware_hash == manifests.COOLING["current_firmware_hash"]
    assert cooling_program.capability_registry_hash == registry.content_hash
    assert cooling_program.manifest_hash == manifests.cooling().manifest_hash


# --- template-only, no model text ------------------------------------------


def test_artifact_is_data_not_code(cooling_program):
    """The deployable artifact is JSON. Nothing in it is a Python fragment, so
    there is nothing to execute even if something wanted to."""
    payload = cooling_program.model_dump(mode="json")
    import json

    text = json.dumps(payload)
    for marker in ("import ", "exec(", "eval(", "lambda", "__", "os.system", "subprocess"):
        assert marker not in text, f"artifact contains {marker!r}"


DYNAMIC_EXECUTION = {"exec", "eval", "compile", "__import__"}


def module_calls(path: Path) -> tuple[set[str], set[str], set[str]]:
    """(bare calls, attribute calls, imported top-level modules) for a module.

    An AST walk rather than a substring scan: prose in a docstring that mentions
    `exec` is not a call to it, and a test that cannot tell the difference is
    not evidence of anything. Bare and dotted calls are kept apart because
    `re.compile` is not `compile`.
    """
    import ast

    tree = ast.parse(path.read_text())
    bare: set[str] = set()
    attrs: set[str] = set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                bare.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                attrs.add(node.func.attr)
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return bare, attrs, imports


def test_manifest_mode_never_executes_generated_content():
    """The claim the whole design rests on, checked against the code rather
    than asserted in a comment."""
    for path in [
        *Path("server/compiler").rglob("*.py"),
        *Path("server/manifest").rglob("*.py"),
    ]:
        bare, _, imports = module_calls(path)
        assert not (bare & DYNAMIC_EXECUTION), f"{path} performs dynamic execution"
        assert "importlib" not in imports, f"{path} imports dynamically"


def test_limits_are_copied_from_the_registry_not_the_manifest(cooling_program, registry):
    fan_on = registry.require("fan_on").actuator_limits
    spec = next(
        spec
        for rule in cooling_program.rules
        for spec in rule.emit
        if spec.capability == "fan_on"
    )
    assert spec.pin == registry.require("fan_on").permitted_pins[0]
    assert spec.current_ma == fan_on.current_ma
    assert spec.max_transitions_per_minute == fan_on.max_transitions_per_minute


def test_min_hold_comes_from_the_registry_not_the_proposal(registry, schema):
    """A manifest cannot shorten the actuator's hold: it is the registry's
    property, expressed in the manifest's own control period."""
    slow = compile_manifest(manifests.cooling(control_period_seconds=1.0), registry, schema)
    fast = compile_manifest(manifests.cooling(control_period_seconds=0.5), registry, schema)
    hold = registry.require("fan_on").actuator_limits.min_hold_seconds
    assert slow.program.min_hold_ticks == int(hold / 1.0)
    assert fast.program.min_hold_ticks == int(hold / 0.5)


# --- refusal rather than improvisation ---------------------------------------


def test_unsupported_capability_combination_is_refused(registry, schema):
    result = compile_manifest(
        manifests.cooling(
            requested_capabilities=["read_distance", "fan_on", "fan_off", "emit_heartbeat"],
            sensor_inputs=["distance_cm"],
        ),
        registry,
        schema,
    )
    assert result.status == "fail"
    assert result.program is None
    assert "no compilation pattern covers" in result.report.reason


def test_a_pattern_that_would_silently_drop_a_capability_is_refused():
    """`monitor_only_v1` has no rule that drives a fan. Asking for one and being
    handed a monitoring controller is exactly the silent improvisation this
    refuses — so the request must match a pattern in both directions."""
    with pytest.raises(compiler.CompilationRejected):
        select_pattern({"read_temperature", "emit_heartbeat", "fan_on"})


def test_unknown_capability_is_refused_by_the_compiler_too(registry, schema):
    """Redundant with the validator on purpose: the experiment harness compiles
    directly, so the compiler cannot rely on having been called after it."""
    result = compile_manifest(
        manifests.cooling(requested_capabilities=[*FULL_COOLING, "open_shell"]),
        registry,
        schema,
    )
    assert result.status == "fail"
    assert "open_shell" in result.report.reason


def test_registry_version_mismatch_is_refused(registry, schema):
    result = compile_manifest(
        manifests.cooling(capability_registry_version="2.0.0"), registry, schema
    )
    assert result.status == "fail"


def test_forbidden_pin_cannot_reach_an_artifact(monkeypatch, schema):
    """A registry that puts the fan on a forbidden pin still cannot produce a
    compiled artifact."""
    monkeypatch.setattr(
        config, "CAPABILITY_REGISTRY_DIR", Path("tests/fixtures/registries")
    )
    load_registry.cache_clear()
    try:
        registry = load_registry("8.0.0")
        result = compile_manifest(
            manifests.cooling(capability_registry_version="8.0.0"), registry, schema
        )
        assert result.status == "fail"
        assert "GPIO_0 is forbidden" in result.report.reason
    finally:
        load_registry.cache_clear()


# --- the runtime -------------------------------------------------------------


def test_controller_commands_cooling_when_the_activation_condition_holds(cooling_program):
    _, emitted = drive(cooling_program, [60.0, 79.9, 81.0])
    assert not _fan_intents(emitted[0]) and not _fan_intents(emitted[1])
    assert _fan_intents(emitted[2])[0].state == "on"
    assert _fan_intents(emitted[2])[0].pin == 27


def test_a_holding_condition_emits_one_intent_not_one_per_tick(cooling_program):
    controller, emitted = drive(cooling_program, [81.0] * 20)
    assert sum(len(_fan_intents(step)) for step in emitted) == 1
    assert controller.actuator_transitions == 1


def test_minimum_hold_suppresses_chatter(cooling_program):
    """Readings oscillating across both thresholds must not become relay
    chatter: the registry's hold is enforced per transition."""
    controller, _ = drive(cooling_program, [81.0, 60.0] * 10)
    assert controller.actuator_transitions <= 1 + 20 // cooling_program.min_hold_ticks


def test_controller_yields_when_its_lease_elapses(cooling_program):
    controller = CompiledController(cooling_program)
    controller.initialize(ControllerContext(device_id="pi_node_alpha", started_at_s=0.0))
    beyond = cooling_program.maximum_duration_seconds + 1
    intents = controller.step(
        Observation(tick=1, time_s=float(beyond), metrics={"temperature_c": 90.0})
    )
    assert controller.yielded is True
    assert [intent.capability for intent in intents] == ["enter_safe_idle"]
    # And stays yielded rather than resuming control.
    assert controller.step(
        Observation(tick=2, time_s=float(beyond + 1), metrics={"temperature_c": 90.0})
    ) == []


def test_shutdown_emits_the_declared_fallback(cooling_program):
    controller = CompiledController(cooling_program)
    controller.initialize(ControllerContext(device_id="pi_node_alpha", started_at_s=0.0))
    intents = controller.shutdown("operator reverted")
    assert [intent.capability for intent in intents] == ["enter_safe_idle"]
    assert "operator reverted" in intents[0].reason


def test_step_before_initialize_is_a_fault(cooling_program):
    with pytest.raises(ControllerFault):
        CompiledController(cooling_program).step(
            Observation(tick=1, time_s=1.0, metrics={"temperature_c": 90.0})
        )


def test_budget_backstop_fires_if_validation_was_skipped(cooling_program):
    """The runtime re-checks the compile-time cost against the compile-time
    budget, so an artifact that never met the validator still cannot run over."""
    starved = cooling_program.model_copy(
        update={
            "resource_budget": cooling_program.resource_budget.model_copy(
                update={"max_cpu_ms_per_step": 0.01}
            )
        }
    )
    controller = CompiledController(starved)
    controller.initialize(ControllerContext(device_id="pi_node_alpha", started_at_s=0.0))
    with pytest.raises(BudgetExceeded):
        controller.step(Observation(tick=1, time_s=1.0, metrics={"temperature_c": 90.0}))


def test_a_missing_metric_is_not_an_exception(cooling_program):
    """A sensor dropout must not crash the controller — the supervisor decides
    what a dropout means, and it can only do that if the controller survives it."""
    controller = CompiledController(cooling_program)
    controller.initialize(ControllerContext(device_id="pi_node_alpha", started_at_s=0.0))
    assert controller.step(Observation(tick=1, time_s=1.0, metrics={})) == [
        intent for intent in controller.step(Observation(tick=1, time_s=1.0, metrics={}))
    ]


def test_runtime_holds_no_driver_or_io_handles():
    """The interpreter has no way to reach hardware, the network or the disk —
    everything it can do, it does by returning an intent."""
    bare, attrs, imports = module_calls(Path(runtime.__file__))
    reachable = bare | attrs | imports
    for forbidden in ("socket", "open", "subprocess", "urllib", "requests", "edge_node"):
        assert forbidden not in reachable, f"the runtime must not reach {forbidden!r}"


def _fan_intents(intents):
    return [intent for intent in intents if intent.kind == "actuator" and intent.actuator == "fan"]
