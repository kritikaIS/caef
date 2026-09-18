"""M21 — the demo runs, end to end, with no API key (REQUIRED DEMO).

`demo/safe_demo.py` is the artifact the README points at, so it is under test
like anything else: if the pipeline stops proposing, validating, compiling,
verifying, signing, installing, activating, expiring or reverting, this fails.

The demo is also the strongest single integration test in the repository — it
exercises every component in this milestone series against the real
implementations, and the only thing it stubs is the model.
"""

import json
import logging

import pytest

import config
from demo import safe_demo


@pytest.fixture(autouse=True)
def quiet():
    for name in ("caef.supervisor", "caef.device", "caef.ota", "caef.slots",
                 "caef.ledger", "caef.pipeline"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    yield
    for name in ("caef.supervisor", "caef.device", "caef.ota", "caef.slots",
                 "caef.ledger", "caef.pipeline"):
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture
def window(monkeypatch):
    """Restore the reversion window the demo compresses."""
    original = config.REVERSION_WINDOW_SECONDS
    yield
    config.REVERSION_WINDOW_SECONDS = original


@pytest.fixture
def trace(tmp_path, window, monkeypatch):
    """Run the demo once and hand back its machine-readable trace."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    output = tmp_path / "demo_trace.json"
    assert safe_demo.main(["--seed", "1", "--lease", "25", "--out", str(output)]) == 0
    return json.loads(output.read_text())


def labels(trace) -> list[str]:
    return [moment["label"] for moment in trace["moments"]]


# --- the thirteen steps ------------------------------------------------------


def test_the_demo_runs_without_an_api_key(trace):
    """The whole point of the stub agent: no key, no network, no Docker."""
    assert trace["moments"], "the demo produced no timeline"
    assert len(trace["moments"]) == 13


def test_every_required_step_appears_in_order(trace):
    expected = [
        "baseline firmware running",
        "virtual world overheating",
        "HIGH_HEAT_DETECTED raised",
        "stub agent proposed a cooling manifest",
        "manifest validated",
        "compiled deterministically",
        "verified in the closed loop",
        "signed package installed into the inactive slot",
        "probation passed, candidate activated",
        "cooling is working",
        "server disconnected",
        "local lease expired",
        "previous firmware restored automatically",
    ]
    assert labels(trace) == expected
    assert [moment["step"] for moment in trace["moments"]] == list(range(1, 14))


def test_the_ticks_are_monotonic(trace):
    ticks = [moment["tick"] for moment in trace["moments"]]
    assert ticks == sorted(ticks)


# --- what the steps actually did --------------------------------------------


def test_the_baseline_cannot_cool_and_the_morph_can(trace):
    assert trace["manifest"]["actuator_outputs"] == ["fan"]
    assert "fan_on" in trace["manifest"]["requested_capabilities"]
    # Before the morph installs, no fan has ever run.
    install_tick = next(
        moment["tick"] for moment in trace["moments"]
        if moment["label"].startswith("signed package")
    )
    early = [row for row in trace["ticks"] if row["tick"] <= install_tick]
    assert all(row["fan_state"] == "off" for row in early)


def test_verification_passed_every_scenario_before_signing(trace):
    assert trace["verification"]["status"] == "pass"
    assert len(trace["verification"]["scenarios"]) >= 8
    assert trace["package"] is not None
    assert len(trace["package"]["signature"]) == 64


def test_the_fan_actually_cooled_the_simulated_device(trace):
    install_tick = next(
        moment["tick"] for moment in trace["moments"]
        if moment["label"].startswith("signed package")
    )
    at_install = next(
        row["device_temp_c"] for row in trace["ticks"] if row["tick"] == install_tick
    )
    after = [row for row in trace["ticks"] if row["tick"] > install_tick]
    assert any(row["fan_state"] == "on" for row in after)
    assert min(row["device_temp_c"] for row in after) < at_install - 10


def test_the_lease_expired_after_the_server_was_disconnected(trace):
    """The claim the demo exists to demonstrate: the reversion cannot have come
    from the server, because the server was already gone."""
    offline_at = trace["server_offline_at_tick"]
    expiry = next(
        moment["tick"] for moment in trace["moments"] if moment["label"] == "local lease expired"
    )
    assert offline_at is not None
    assert expiry > offline_at

    expired_rows = [row for row in trace["ticks"] if "lease_expired" in row["events"]]
    assert len(expired_rows) == 1
    assert expired_rows[0]["tick"] == expiry


def test_the_device_ended_on_the_baseline_artifact(trace):
    outcome = trace["outcome"]
    assert outcome["running_slot"] == "A"
    assert outcome["running_manifest"] == "baseline-monitor"
    assert outcome["last_known_good_slot"] in ("A", "B")
    assert outcome["ledger_state"] == "reverted"


def test_the_ledger_records_the_whole_journey(trace):
    states = [transition["to"] for transition in trace["ledger_transitions"]]
    assert states == [
        "proposed",
        "manifest_validated",
        "compiled",
        "simulation_verified",
        "signed",
        "delivery_attempted",
        "accepted_by_device",
        "active_on_device",
        "reverted",
    ]


def test_the_device_never_exceeded_the_critical_limit(trace):
    assert trace["outcome"]["peak_device_temp_c"] < config.CRITICAL_TEMP_C


def test_the_run_is_reproducible(tmp_path, window, monkeypatch):
    """Same seed, same trace — the property every measurement in the paper
    depends on."""
    monkeypatch.setattr(config, "LLM_API_KEY", "")

    def run(name):
        output = tmp_path / name
        assert safe_demo.main(["--seed", "3", "--lease", "25", "--out", str(output)]) == 0
        payload = json.loads(output.read_text())
        # Deployment ids are uuids and manifest ids embed the event tick; the
        # physical trace is what has to match.
        return [
            (row["tick"], row["device_temp_c"], row["fan_state"], row["events"])
            for row in payload["ticks"]
        ]

    assert run("first.json") == run("second.json")
