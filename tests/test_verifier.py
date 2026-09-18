"""M16 — deterministic behavioural verification (RESEARCH.md §6).

The verifier is what makes a safety claim in this project checkable rather than
asserted. These tests hold it to three things: it passes a controller that
actually works, it fails ones that do not and says why with a trace, and it
never records a pass a controller did not earn.
"""

import logging

import pytest

import config
from server.compiler.compiler import compile_manifest
from server.manifest.registry import load_registry
from server.schemas import load_hardware_schema
from server.sim import scenarios
from server.verify import verifier
from server.verify.verifier import PROPERTIES, verify, verify_scenario
from tests.fixtures import manifests


@pytest.fixture(autouse=True)
def quiet():
    logging.getLogger("caef.supervisor").setLevel(logging.CRITICAL)
    yield
    logging.getLogger("caef.supervisor").setLevel(logging.NOTSET)


@pytest.fixture
def registry():
    return load_registry("1.0.0")


@pytest.fixture
def schema():
    return load_hardware_schema("pi_node_alpha")


def build(registry, schema, **overrides):
    result = compile_manifest(manifests.cooling(**overrides), registry, schema)
    assert result.status == "pass", result.report.errors
    return result.program


# --- a controller that works -------------------------------------------------


def test_a_correct_cooling_controller_passes_every_scenario(registry, schema):
    suite = verify(build(registry, schema), schema, registry, seeds=[1, 2])
    assert suite.status == "pass", suite.summary()
    assert len(suite.reports) == len(scenarios.VERIFICATION_SCENARIOS) * 2


def test_the_report_carries_the_measurements_the_paper_needs(registry, schema):
    report = verify_scenario(
        build(registry, schema), scenarios.get("gradual_overheat"), schema, registry, seed=1
    )
    assert report.status == "pass"
    assert report.scenario == "gradual_overheat" and report.seed == 1
    assert report.peak_device_temp_c > 0
    assert report.activation_latency_ticks is not None
    assert report.recovery_time_ticks is not None
    assert report.actuator_transitions >= 1
    assert report.resource_use.steps > 0
    assert report.resource_use.artifact_bytes > 0
    assert report.supervisor.accepted > 0
    assert {prop.name for prop in report.properties} == {
        prop(  # every registered property must appear in every report
            verifier.Ctx(
                program=build(registry, schema),
                scenario=scenarios.get("normal"),
                result=_empty_result(registry, schema),
                schema=schema,
                registry=registry,
                expect_cooling=False,
            )
        ).name
        for prop in PROPERTIES
    }


def _empty_result(registry, schema):
    from server.sim.harness import run_scenario

    tiny = scenarios.get("normal")
    return run_scenario(build(registry, schema), tiny, schema, registry)


def test_verification_is_reproducible(registry, schema):
    program = build(registry, schema)
    first = verify(program, schema, registry, seeds=[7])
    second = verify(program, schema, registry, seeds=[7])
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# --- controllers that do not work -------------------------------------------


def test_a_threshold_no_device_will_reach_fails_with_a_counterexample(registry, schema):
    """A perfectly valid manifest that compiles cleanly and does nothing useful.

    This is the case static validation cannot catch and behavioural verification
    exists for: 200C is a legal number, disjoint from the recovery condition,
    within every limit — and the device cooks while the controller waits.
    """
    program = build(
        registry,
        schema,
        activation_condition={"metric": "temperature_c", "operator": ">=", "value": 200.0},
    )
    suite = verify(program, schema, registry, seeds=[1])
    assert suite.status == "fail"

    report = suite.failed_reports[0]
    failed = {prop.name for prop in report.failures}
    assert "cooling_latency" in failed
    assert report.counterexample, "a failure must carry the trace that produced it"
    assert all("tick" in row and "device_temp_c" in row for row in report.counterexample)
    assert any(row["fan_state"] == "off" for row in report.counterexample)


def test_a_controller_the_supervisor_has_to_rescue_fails(registry, schema):
    """Being rescued is not adapting.

    Exercised as a unit rather than by tuning a scenario: with the emergency
    policy working, a lazy controller nearly always lands in the "never
    commanded cooling" branch first, and the window where the supervisor beats
    it by a tick is too narrow to pin a regression test to. So the trace is
    synthesised — the supervisor engages at tick 10 and the firmware only reacts
    at tick 20 — and the property is asked to decide it.
    """
    program = build(registry, schema)
    rows = [
        _row(tick, temp=90.0 + tick * 0.1, fan="off", emergency=tick >= 10)
        for tick in range(1, 31)
    ]
    ctx = verifier.Ctx(
        program=program,
        scenario=scenarios.get("gradual_overheat"),
        result=_synthetic_result(rows, fan_on_tick=20),
        schema=schema,
        registry=registry,
        expect_cooling=True,
    )
    verdict = verifier.prop_cooling_latency(ctx)
    assert verdict.status == "fail"
    assert "emergency policy engaged at tick 10" in verdict.detail
    assert verdict.counterexample_tick == 10


def _row(tick: int, temp: float, fan: str, emergency: bool):
    from server.sim.harness import TraceRow

    return TraceRow(
        tick=tick,
        time_s=float(tick),
        device_temp_c=temp,
        sensor_temp_c=temp,
        fan_state=fan,
        supervisor_state="normal",
        emergency_active=emergency,
    )


def _synthetic_result(rows, fan_on_tick: int):
    """A RunResult carrying just the trace and decisions a property reads."""
    from server.compiler.program import ActuatorIntent, IntentDecision
    from server.sim.harness import RunResult

    intent = ActuatorIntent(
        intent_id="synthetic:1",
        manifest_id="manifest-cooling-1",
        capability="fan_on",
        kind="actuator",
        tick=fan_on_tick,
        reason="rule r_activate",
        actuator="fan",
        state="on",
        pin=27,
        current_ma=12.0,
    )
    return RunResult(
        scenario="synthetic",
        seed=0,
        rows=rows,
        world=None,
        supervisor=None,
        controller=None,
        decisions=[IntentDecision(intent=intent, accepted=True, reason="applied")],
    )


def test_a_monitoring_artifact_offered_as_a_heat_response_fails(registry, schema):
    """The same artifact is fine as a baseline and wrong as an adaptation, so
    the caller states which it is."""
    monitor = compile_manifest(
        manifests.monitor(
            requested_capabilities=["read_temperature", "emit_heartbeat", "emit_context_event"]
        ),
        registry,
        schema,
    ).program
    assert verify(monitor, schema, registry, seeds=[1]).status == "pass"
    assert verify(monitor, schema, registry, seeds=[1], expect_cooling=True).status == "fail"


def test_an_over_long_lease_fails_verification(registry, schema):
    """The validator would have caught this; the verifier catches it again, so
    an artifact that reached the signer another way still cannot pass."""
    program = build(registry, schema)
    forged = program.model_copy(
        update={"maximum_duration_seconds": config.MAX_LEASE_SECONDS + 1}
    )
    report = verify_scenario(forged, scenarios.get("normal"), schema, registry, seed=1)
    assert report.status == "fail"
    assert "finite_lease" in {prop.name for prop in report.failures}


def test_an_unreachable_fallback_fails(registry, schema):
    program = build(registry, schema)
    forged = program.model_copy(update={"fallback": [], "fallback_behavior": "enter_safe_idle"})
    report = verify_scenario(forged, scenarios.get("normal"), schema, registry, seed=1)
    assert report.status == "fail"
    assert "fallback_reachable" in {prop.name for prop in report.failures}


# --- honesty -----------------------------------------------------------------


def test_a_physically_unwinnable_scenario_is_skipped_not_passed(registry, schema):
    """`ineffective_fan` cannot be held under the critical limit by any control
    policy. Recording that as a pass would make the whole report worthless."""
    report = verify_scenario(
        build(registry, schema), scenarios.get("ineffective_fan"), schema, registry, seed=1
    )
    bound = next(
        prop for prop in report.properties if prop.name == "critical_temperature_bound"
    )
    assert bound.status == "skipped"
    assert "cannot physically" in bound.detail
    assert report.peak_device_temp_c > config.CRITICAL_TEMP_C
    # The run as a whole still passes: the controller did everything it could.
    assert report.status == "pass"


def test_a_scenario_that_never_heats_skips_the_latency_property(registry, schema):
    report = verify_scenario(
        build(registry, schema), scenarios.get("normal"), schema, registry, seed=1
    )
    latency = next(prop for prop in report.properties if prop.name == "cooling_latency")
    assert latency.status == "skipped"
    assert report.activation_latency_ticks is None


def test_a_crash_scenario_still_verifies_the_supervisors_response(registry, schema):
    report = verify_scenario(
        build(registry, schema), scenarios.get("firmware_crash"), schema, registry, seed=1
    )
    assert report.controller_faulted is True
    assert report.supervisor.safe_state_entries >= 1
    assert report.status == "pass", [prop.detail for prop in report.failures]
    assert report.peak_device_temp_c < config.CRITICAL_TEMP_C


def test_every_property_reports_one_of_three_statuses(registry, schema):
    suite = verify(build(registry, schema), schema, registry, seeds=[1])
    for report in suite.reports:
        for prop in report.properties:
            assert prop.status in ("pass", "fail", "skipped")
            assert prop.description
            if prop.status == "skipped":
                assert prop.detail, f"{prop.name} skipped without saying why"


def test_the_verifier_calls_no_model_and_reads_no_clock():
    """Determinism is the whole basis of the report, so the two things that
    would break it are checked in the source."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(verifier.__file__).read_text())
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "time" not in imports and "datetime" not in imports and "random" not in imports
    assert not any(name.startswith("langchain") for name in imports)
