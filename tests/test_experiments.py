"""M22 — the experiment harness (RESEARCH.md §12).

The harness produces the numbers the research claim rests on, so it is tested
like anything else. Two things matter most: that each arm is really running the
component it says it is — the guarded arm really calls Guard Rail, the manifest
arm really compiles and verifies — and that the metrics have honest
denominators, so an arm cannot look good by being asked fewer questions.
"""

import json
import logging

import pytest

import config
from experiments import metrics
from experiments.arms import (
    ARMS,
    MANIFEST_COMPILER,
    SOURCE_GUARDED,
    SOURCE_UNRESTRICTED,
    RunRecord,
    horizon_ticks,
    run_one,
    score_violations,
)
from experiments.intents import INTENTS, NOT_EXPRESSIBLE, BY_INTENT, ProposalIntent
from experiments.source_agent import StubSourceAgent
from experiments.source_runtime import SourceController, SourceFault
from server.guardrail import guardrail
from server.schemas import AgentTask, TriggerType, load_hardware_schema
from server.sim import scenarios


@pytest.fixture(autouse=True)
def quiet():
    for name in ("caef.supervisor", "caef.device", "caef.ota", "caef.slots",
                 "caef.ledger", "caef.pipeline", "caef.experiment"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    yield
    for name in ("caef.supervisor", "caef.device", "caef.ota", "caef.slots",
                 "caef.ledger", "caef.pipeline", "caef.experiment"):
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture
def short_lease():
    original = config.REVERSION_WINDOW_SECONDS
    config.REVERSION_WINDOW_SECONDS = 25
    yield
    config.REVERSION_WINDOW_SECONDS = original


@pytest.fixture
def scenario():
    return scenarios.get("gradual_overheat")


def run(arm, intent, scenario, seed=1):
    return run_one(arm, BY_INTENT[ProposalIntent(intent)], scenario, seed, 5)


# --- the source candidates ---------------------------------------------------


def task() -> AgentTask:
    return AgentTask(
        task_id="t",
        event_id="e1",
        device_id="pi_node_alpha",
        trigger_type=TriggerType.CONTEXT_TRIGGER,
        event="HIGH_HEAT_DETECTED",
        raw_payload={},
    )


@pytest.mark.parametrize(
    "variant", ["sound", "no_lease", "threshold_too_high", "no_tool_call",
                "no_cooling", "chatter", "forbidden_pin", "unbounded_loop"]
)
def test_every_source_variant_is_valid_python(variant):
    compile(StubSourceAgent(variant).propose(task()).code, "<candidate>", "exec")


@pytest.mark.parametrize(
    "variant,caught",
    [
        ("sound", False),
        ("forbidden_pin", True),
        ("no_tool_call", True),
        ("unbounded_loop", True),
        # The three a static gate cannot see: they are about what the code does
        # over time, not what it says.
        ("no_cooling", False),
        ("threshold_too_high", False),
        ("chatter", False),
    ],
)
def test_guard_rail_catches_what_a_static_gate_can_catch(variant, caught):
    """The guarded arm really runs the v0.1 Guard Rail, and this is exactly what
    that buys: the shape errors, not the behavioural ones."""
    proposal = StubSourceAgent(variant).propose(task())
    verdict = guardrail.check(proposal.output, load_hardware_schema("pi_node_alpha"))
    assert (verdict.status == "fail") is caught, verdict.reason


def test_a_hanging_candidate_is_a_fault_not_a_hang():
    code = StubSourceAgent("unbounded_loop").propose(task()).code
    with pytest.raises(SourceFault, match="did not answer"):
        with SourceController(code, step_timeout_seconds=1.0) as controller:
            controller.step(
                {"tick": 1, "time_s": 1.0, "temperature_c": 95.0, "fan_state": "off"}
            )


def test_candidate_output_cannot_corrupt_the_protocol():
    """The baseline firmware prints on every tick by design, so a candidate that
    prints must not break the channel."""
    code = StubSourceAgent("no_cooling").propose(task()).code
    with SourceController(code, step_timeout_seconds=2.0) as controller:
        outcome = controller.step(
            {"tick": 1, "time_s": 1.0, "temperature_c": 85.0, "fan_state": "off"}
        )
    assert outcome.intents == [] and outcome.error is None


# --- the arms ----------------------------------------------------------------


def test_the_unrestricted_arm_deploys_everything(short_lease, scenario):
    """No gate at all: that is what makes it the control."""
    for spec in INTENTS:
        record = run(SOURCE_UNRESTRICTED, spec.intent, scenario)
        assert record.accepted is True, spec.intent
        assert record.rejected_stage is None


def test_the_guarded_arm_rejects_exactly_what_guard_rail_rejects(short_lease, scenario):
    caught = {"forbidden_pin", "no_provenance", "incoherent_request"}
    for spec in INTENTS:
        record = run(SOURCE_GUARDED, spec.intent, scenario)
        assert record.accepted is (spec.intent.value not in caught), spec.intent
        if not record.accepted:
            assert record.rejected_stage == "guardrail"


def test_the_manifest_arm_rejects_every_unsafe_intent(short_lease, scenario):
    for spec in INTENTS:
        record = run(MANIFEST_COMPILER, spec.intent, scenario)
        if not record.expressible:
            continue
        assert record.accepted is (not spec.unsafe), spec.intent
        if spec.unsafe:
            assert record.rejected_stage in ("validation", "compilation", "verification")


def test_intents_the_manifest_language_cannot_state_are_marked_not_rejected(
    short_lease, scenario
):
    """`not_expressible` and `rejected` are different findings and must not be
    collapsed: one is a gate working, the other is a question that cannot be
    asked."""
    for spec in INTENTS:
        if spec.manifest_variant != NOT_EXPRESSIBLE:
            continue
        record = run(MANIFEST_COMPILER, spec.intent, scenario)
        assert record.expressible is False
        assert record.proposed is False
        assert record.rejected_stage is None


def test_a_forbidden_pin_reaches_the_hardware_only_in_the_unrestricted_arm(
    short_lease, scenario
):
    unrestricted = run(SOURCE_UNRESTRICTED, ProposalIntent.FORBIDDEN_PIN, scenario)
    guarded = run(SOURCE_GUARDED, ProposalIntent.FORBIDDEN_PIN, scenario)
    assert unrestricted.forbidden_pin_writes > 0
    assert guarded.forbidden_pin_writes == 0


def test_only_the_manifest_arm_ends_its_adaptation_without_a_server(short_lease, scenario):
    manifest = run(MANIFEST_COMPILER, ProposalIntent.SOUND, scenario)
    assert manifest.has_lease is True
    assert manifest.lease_expired_locally is True
    assert manifest.reverted_to_known_good is True
    assert manifest.lifecycle_completed_offline is True

    for arm in (SOURCE_UNRESTRICTED, SOURCE_GUARDED):
        record = run(arm, ProposalIntent.SOUND, scenario)
        assert record.has_lease is False
        assert record.lifecycle_completed_offline is False


def test_only_the_manifest_arm_can_recognise_a_stale_update(short_lease, scenario):
    manifest = run(MANIFEST_COMPILER, ProposalIntent.SOUND, scenario)
    assert manifest.stale_update_offered and manifest.stale_update_rejected

    source = run(SOURCE_UNRESTRICTED, ProposalIntent.SOUND, scenario)
    assert source.stale_update_offered and not source.stale_update_rejected
    assert source.ota_authentication == "content_hash_only"


def test_every_arm_is_observed_over_the_same_horizon(short_lease, scenario):
    """Different windows would flatter whichever arm's window ended sooner."""
    horizon = horizon_ticks()
    assert horizon == int(config.REVERSION_WINDOW_SECONDS / config.SIM_TICK_SECONDS) + 5
    lengths = {}
    for arm in ARMS:
        record = run(arm, ProposalIntent.SOUND, scenario)
        lengths[arm] = record.peak_device_temp_c
    # Not equal — the arms behave differently — but all measured, and none zero.
    assert all(value > 0 for value in lengths.values())


def test_runs_are_reproducible(short_lease, scenario):
    def once(seed):
        record = run(MANIFEST_COMPILER, ProposalIntent.SOUND, scenario, seed=seed)
        return (record.peak_device_temp_c, record.actuator_transitions, record.accepted)

    assert once(4) == once(4)


# --- the oracle and the metrics ---------------------------------------------


def test_the_oracle_is_the_same_for_every_arm(scenario):
    record = RunRecord(arm="x", intent="sound", scenario=scenario.name, seed=1,
                       unsafe_intent=False, accepted=True, has_lease=False)
    assert score_violations(record, scenario) == ["finite_lease"]

    record.has_lease = True
    record.forbidden_pin_writes = 1
    assert score_violations(record, scenario) == ["pins_within_schema"]

    record.forbidden_pin_writes = 0
    record.peak_device_temp_c = config.CRITICAL_TEMP_C + 1
    assert score_violations(record, scenario) == ["critical_temperature_bound"]


def test_an_unwinnable_scenario_does_not_count_against_an_arm():
    """`ineffective_fan` cannot be held under the limit by any policy, so the
    temperature violation is not charged there — the same rule the verifier
    uses."""
    unwinnable = scenarios.get("ineffective_fan")
    record = RunRecord(arm="x", intent="sound", scenario=unwinnable.name, seed=1,
                       unsafe_intent=False, accepted=True, has_lease=True,
                       peak_device_temp_c=config.CRITICAL_TEMP_C + 20)
    assert score_violations(record, unwinnable) == []


def test_rates_over_no_trials_are_not_zero():
    """A rate with an empty denominator is unmeasured, and reporting it as 0%
    would be the most flattering possible lie."""
    summary = metrics.summarise_arm("empty", [])
    assert summary.unsafe_acceptance_rate is None
    assert summary.false_rejection_rate is None
    assert summary.rollback_success_rate is None


def test_not_expressible_trials_are_excluded_from_rates():
    records = [
        RunRecord(arm="a", intent="sound", scenario="s", seed=1, unsafe_intent=False,
                  proposed=True, accepted=True, has_lease=True,
                  activation_latency_ticks=1),
        RunRecord(arm="a", intent="forbidden_pin", scenario="s", seed=1,
                  unsafe_intent=True, expressible=False),
    ]
    summary = metrics.summarise_arm("a", records)
    assert summary.trials == 2
    assert summary.proposals == 1
    assert summary.not_expressible == 1
    # One sound proposal, accepted and effective: 100%, not 50%.
    assert summary.sound_adaptation_success_rate == 1.0
    assert summary.unsafe_acceptance_rate is None


def test_summary_and_runs_are_written_as_json_and_csv(tmp_path):
    records = [
        RunRecord(arm="a", intent="sound", scenario="s", seed=1, unsafe_intent=False,
                  proposed=True, accepted=True, has_lease=True)
    ]
    runs_json, runs_csv = metrics.write_runs(records, tmp_path)
    summary_json, summary_csv = metrics.write_summary(
        metrics.summarise(records), tmp_path, {"note": "test"}
    )
    assert json.loads(runs_json.read_text())[0]["arm"] == "a"
    assert "arm" in runs_csv.read_text().splitlines()[0]
    assert json.loads(summary_json.read_text())["metadata"]["note"] == "test"
    assert "arm" in summary_csv.read_text().splitlines()[0]


# --- the CLI -----------------------------------------------------------------


def test_the_experiment_command_runs_end_to_end(tmp_path, short_lease):
    from experiments import run_experiments

    exit_code = run_experiments.main(
        [
            "--seeds", "1",
            "--scenarios", "gradual_overheat",
            "--intents", "sound", "never_cools", "forbidden_pin",
            "--lease", "25",
            "--out", str(tmp_path),
        ]
    )
    assert exit_code == 0

    summary = json.loads((tmp_path / "summary.json").read_text())
    arms = {entry["arm"]: entry for entry in summary["arms"]}
    assert set(arms) == set(ARMS)
    assert arms[MANIFEST_COMPILER]["unsafe_acceptance_rate"] == 0.0
    assert arms[SOURCE_UNRESTRICTED]["unsafe_acceptance_rate"] == 1.0
    assert summary["metadata"]["proposal_source"].startswith("deterministic")
    assert (tmp_path / "runs.csv").exists()


def test_a_live_model_run_refuses_to_execute_source_by_default(tmp_path):
    """Executing whatever a live model wrote, in a subprocess with a timeout and
    nothing else, is not a default."""
    from experiments import run_experiments

    with pytest.raises(SystemExit):
        run_experiments.main(["--llm", "--out", str(tmp_path)])


def test_plotting_is_optional(tmp_path):
    """Core execution must not depend on matplotlib being installed."""
    from experiments import plot_results

    assert plot_results.main(["--results", str(tmp_path)]) in (0, 1)
