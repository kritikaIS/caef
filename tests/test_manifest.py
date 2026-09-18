"""M12 — Behavior Manifest, canonical serialization, capability registry, validator.

These are the tests behind the claim that the model cannot smuggle behaviour
past the contract: what it sends is parsed into a closed shape, and everything
that shape does not already exclude is checked deterministically against this
device's hardware schema and this registry version (RESEARCH.md §2/§3).
"""

import json
from pathlib import Path

import pytest

import config
from server.manifest import registry as registry_module
from server.manifest.canonical import canonical_json, content_hash
from server.manifest.models import BehaviorManifest, Operator
from server.manifest.registry import UnsupportedRegistryVersion, load_registry
from server.manifest.validator import CHECK_NAMES, validate
from server.schemas import load_hardware_schema
from tests.fixtures import manifests

FIXTURE_REGISTRIES = Path(__file__).parent / "fixtures" / "registries"


@pytest.fixture
def schema():
    return load_hardware_schema("pi_node_alpha")


@pytest.fixture
def registry():
    return load_registry("1.0.0")


@pytest.fixture
def fixture_registry(monkeypatch):
    """Load one of the deliberately-broken registries under tests/fixtures."""

    def _load(version: str):
        monkeypatch.setattr(config, "CAPABILITY_REGISTRY_DIR", FIXTURE_REGISTRIES)
        load_registry.cache_clear()
        return load_registry(version)

    yield _load
    load_registry.cache_clear()


# --- parsing: the shape itself is the first gate -----------------------------


def test_unknown_manifest_fields_are_rejected():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        manifests.cooling(privileged_mode=True)


def test_unknown_nested_field_is_rejected():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        manifests.cooling(
            activation_condition={
                "metric": "temperature_c",
                "operator": ">=",
                "value": 80.0,
                "then_run": "import os; os.system('id')",
            }
        )


@pytest.mark.parametrize(
    "condition",
    [
        # A metric name is an identifier, matched against a pattern before it is
        # ever used as a lookup key.
        {"metric": "__import__('os').system('id')", "operator": ">=", "value": 1.0},
        {"metric": "temperature_c; DROP TABLE devices", "operator": ">=", "value": 1.0},
        # The operator comes from a closed enum, so it cannot carry a clause.
        {"metric": "temperature_c", "operator": "or 1==1", "value": 1.0},
        {"metric": "temperature_c", "operator": ">= 0 or True", "value": 1.0},
        # Strict mode: a string is not a number, so a value cannot carry code.
        {"metric": "temperature_c", "operator": ">=", "value": "80; import os"},
        {"metric": "temperature_c", "operator": ">=", "value": "eval('1')"},
        # Nor a callable, nor a nested structure pretending to be one.
        {"metric": "temperature_c", "operator": ">=", "value": {"__class__": "float"}},
    ],
)
def test_arbitrary_code_cannot_be_placed_in_a_condition(condition):
    with pytest.raises(ValueError):
        manifests.cooling(activation_condition=condition)


def test_conditions_are_evaluated_numerically_not_executed():
    """The evaluator is a `match` over an enum — there is no parser to exploit."""
    manifest = manifests.cooling()
    assert manifest.activation_condition.evaluate({"temperature_c": 81.0}) is True
    assert manifest.activation_condition.evaluate({"temperature_c": 79.9}) is False
    # A metric the device is not reporting is False, never an exception: the
    # supervisor has to keep running when a sensor drops out.
    assert manifest.activation_condition.evaluate({}) is False


def test_every_operator_is_covered_by_the_evaluator():
    from server.manifest.models import Condition

    for operator in Operator:
        condition = Condition(metric="temperature_c", operator=operator, value=10.0)
        assert isinstance(condition.evaluate({"temperature_c": 10.0}), bool)


def test_rationale_is_bounded_and_plain_text():
    with pytest.raises(ValueError, match="rationale exceeds"):
        manifests.cooling(rationale="x" * (config.MANIFEST_MAX_RATIONALE_CHARS + 1))
    with pytest.raises(ValueError, match="control characters"):
        manifests.cooling(rationale="fine\x00then not")


def test_a_validated_manifest_is_immutable():
    """The hash taken at validation must still describe the object at compile
    time, so the model is frozen rather than merely conventionally unmodified."""
    manifest = manifests.cooling()
    with pytest.raises(ValueError):
        manifest.maximum_duration_seconds = 999999


# --- canonical serialization -------------------------------------------------


def test_identical_manifests_hash_identically_regardless_of_encoding():
    manifest = manifests.cooling()
    shuffled = dict(reversed(list(manifests.COOLING.items())))
    pretty = json.loads(json.dumps(shuffled, indent=4))
    assert BehaviorManifest.model_validate(pretty).manifest_hash == manifest.manifest_hash


def test_any_field_change_changes_the_hash():
    baseline = manifests.cooling().manifest_hash
    assert manifests.cooling(maximum_duration_seconds=301).manifest_hash != baseline
    assert (
        manifests.cooling(
            activation_condition={"metric": "temperature_c", "operator": ">=", "value": 80.1}
        ).manifest_hash
        != baseline
    )


def test_canonical_json_is_stable_and_rejects_non_finite_floats():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert content_hash({"a": -0.0}) == content_hash({"a": 0.0})
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"a": float("nan")})


# --- capability registry -----------------------------------------------------


def test_registry_is_the_closed_set_of_prototype_capabilities(registry):
    assert set(registry.capabilities) == {
        "read_temperature",
        "read_distance",
        "fan_on",
        "fan_off",
        "emit_heartbeat",
        "emit_critical_event",
        "enter_safe_idle",
        # Beyond the seven the specification names: a monitoring controller
        # needs a way to raise the situation that starts an adaptation loop, and
        # emit_critical_event is the wrong trigger_type for that — it would
        # route a heat event into the auto-patching loop (RESEARCH.md §3).
        "emit_context_event",
    }


def test_every_capability_declares_its_full_contract(registry):
    for name, capability in registry.capabilities.items():
        assert capability.description, name
        assert capability.input_type and capability.output_type, name
        assert capability.resource_cost.cpu_ms_per_step >= 0, name
        assert capability.safe_fallback in registry.capabilities, name
        assert capability.template in registry_module.TEMPLATES, name
        if capability.kind == "actuator":
            assert capability.actuator_limits is not None, name
            assert capability.permitted_pins, name
        if capability.kind == "sensor":
            assert capability.metric in registry.metrics, name


def test_registry_carries_the_hash_of_its_own_bytes(registry):
    payload = json.loads(Path("registry/capability_registry_v1.json").read_text())
    assert registry.content_hash == content_hash(payload)


def test_unknown_registry_version_is_refused():
    with pytest.raises(UnsupportedRegistryVersion):
        load_registry("42.0.0")
    assert registry_module.is_supported_version("1.0.0") is True
    assert registry_module.is_supported_version("42.0.0") is False


def test_registry_module_offers_no_writer():
    """Read-only by construction (SAFETY_PROTOCOL.md §1 layer 1): the Agent
    cannot rewrite the registry because no function exists that writes it."""
    source = Path(registry_module.__file__).read_text()
    assert "def save" not in source and "def write" not in source
    assert '"w"' not in source and "'w'" not in source
    assert "write_text" not in source and "write_bytes" not in source


def test_no_agent_facing_module_writes_the_registry_or_schema():
    """Same guarantee, checked across the packages the model's output flows
    through rather than in one file."""
    for path in [*Path("server/agent").rglob("*.py"), *Path("server/manifest").rglob("*.py")]:
        source = path.read_text()
        for forbidden in ("write_text", "write_bytes", "os.remove", "shutil.copy"):
            assert forbidden not in source, f"{path} must not write pipeline inputs"


# --- validation --------------------------------------------------------------


def test_a_well_formed_cooling_manifest_validates(registry, schema):
    result = validate(manifests.cooling(), registry, schema)
    assert result.status == "pass", result.errors
    assert set(result.checks) == set(CHECK_NAMES)
    assert result.capability_registry_hash == registry.content_hash


def test_unknown_capability_is_rejected(registry, schema):
    manifest = manifests.cooling(
        requested_capabilities=[*manifests.COOLING["requested_capabilities"], "open_shell"]
    )
    result = validate(manifest, registry, schema)
    assert result.status == "fail"
    assert result.checks["known_capabilities"] == "fail"
    assert "open_shell" in result.reason


def test_actuator_on_an_incompatible_pin_is_rejected(fixture_registry, schema):
    """The registry claims the fan lives on GPIO_17, which the schema says is the
    DHT11. An actuator may not target hardware it is not compatible with."""
    registry = fixture_registry("9.0.0")
    manifest = manifests.cooling(capability_registry_version="9.0.0")
    result = validate(manifest, registry, schema)
    assert result.status == "fail"
    assert result.checks["capability_preconditions"] == "fail"
    assert "GPIO_17 is wired to DHT11" in result.reason


def test_capability_on_a_forbidden_pin_is_rejected(fixture_registry, schema):
    registry = fixture_registry("8.0.0")
    manifest = manifests.cooling(capability_registry_version="8.0.0")
    result = validate(manifest, registry, schema)
    assert result.status == "fail"
    assert result.checks["pin_permissions"] == "fail"
    assert "forbidden pin GPIO_0" in result.reason


def test_manifest_for_another_device_is_rejected(registry, schema):
    result = validate(manifests.cooling(device_id="pi_node_beta"), registry, schema)
    assert result.status == "fail"
    assert result.checks["device_identity"] == "fail"


def test_registry_version_mismatch_is_rejected(registry, schema):
    result = validate(manifests.cooling(capability_registry_version="2.0.0"), registry, schema)
    assert result.status == "fail"
    assert result.checks["registry_version"] == "fail"


def test_unbounded_lease_is_rejected(registry, schema):
    result = validate(
        manifests.cooling(maximum_duration_seconds=config.MAX_LEASE_SECONDS + 1),
        registry,
        schema,
    )
    assert result.status == "fail"
    assert result.checks["lease_bounds"] == "fail"


def test_overlapping_activation_and_recovery_are_rejected(registry, schema):
    result = validate(
        manifests.cooling(
            recovery_condition={"metric": "temperature_c", "operator": ">", "value": 60.0}
        ),
        registry,
        schema,
    )
    assert result.status == "fail"
    assert result.checks["condition_consistency"] == "fail"


def test_condition_on_an_unsensed_metric_is_rejected(registry, schema):
    result = validate(
        manifests.cooling(
            activation_condition={"metric": "distance_cm", "operator": ">=", "value": 5.0}
        ),
        registry,
        schema,
    )
    assert result.status == "fail"
    assert result.checks["sensor_binding"] == "fail"


def test_actuator_capability_without_a_declared_output_is_rejected(registry, schema):
    result = validate(manifests.cooling(actuator_outputs=[]), registry, schema)
    assert result.status == "fail"
    assert result.checks["actuator_binding"] == "fail"


def test_unreachable_fallback_is_rejected(registry, schema):
    """`hold_cooling` needs `fan_on`; a manifest that drops it cannot fall back."""
    result = validate(
        manifests.cooling(
            fallback_behavior="hold_cooling",
            requested_capabilities=["read_temperature", "emit_heartbeat"],
            actuator_outputs=[],
        ),
        registry,
        schema,
    )
    assert result.status == "fail"
    assert result.checks["fallback_reachable"] == "fail"


def test_budget_below_the_registry_cost_is_rejected(registry, schema):
    result = validate(
        manifests.cooling(
            resource_budget={
                "max_cpu_ms_per_step": 0.1,
                "max_memory_kb": 1,
                "max_actuator_transitions_per_minute": 12,
            }
        ),
        registry,
        schema,
    )
    assert result.status == "fail"
    assert result.checks["resource_budget"] == "fail"


def test_baseline_monitor_manifest_validates(registry, schema):
    result = validate(manifests.monitor(), registry, schema)
    assert result.status == "pass", result.errors
