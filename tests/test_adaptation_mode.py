"""M11 / RESEARCH.md §1 — the adaptation-mode switch.

Two pipelines coexist so they can be compared:

    source_generation  the preserved v0.1 baseline (LLM writes Python)
    manifest_compiler  contract-constrained (LLM writes a manifest)

The point of these tests is that introducing the switch changed nothing about
the baseline: the default is still `source_generation`, and every component the
baseline is made of still imports and behaves identically under either setting.
"""

import importlib

import pytest

import config


@pytest.fixture
def reloaded_config(monkeypatch):
    """Reload config under a patched environment, then restore it.

    config is read at import time by design (TDD.md §6), so the only honest way
    to test its parsing is to re-run it.
    """

    def _reload(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config)

    yield _reload
    monkeypatch.undo()
    importlib.reload(config)


def test_blank_mode_falls_back_to_the_baseline(reloaded_config):
    """A blank `.env` line is "not configured", not a fatal misconfiguration.

    Recommending manifest_compiler is not the same as silently switching an
    operator onto it, so the fallback is the preserved baseline.
    """
    reloaded = reloaded_config(ADAPTATION_MODE="")
    assert reloaded.ADAPTATION_MODE == reloaded.SOURCE_GENERATION


def test_unset_mode_defaults_to_source_generation(reloaded_config, monkeypatch):
    monkeypatch.delenv("ADAPTATION_MODE", raising=False)
    reloaded = importlib.reload(config)
    assert reloaded.ADAPTATION_MODE == reloaded.SOURCE_GENERATION


def test_manifest_compiler_mode_is_selectable(reloaded_config):
    reloaded = reloaded_config(ADAPTATION_MODE="manifest_compiler")
    assert reloaded.ADAPTATION_MODE == reloaded.MANIFEST_COMPILER


def test_unknown_mode_fails_at_import(reloaded_config):
    """Fail loudly rather than falling back to a pipeline nobody selected."""
    with pytest.raises(ValueError, match="ADAPTATION_MODE"):
        reloaded_config(ADAPTATION_MODE="yolo")


def test_safety_limits_are_config_driven_not_inlined():
    """CLAUDE.md §4: every safety-relevant number lives in config."""
    for name in (
        "MAX_LEASE_SECONDS",
        "EMERGENCY_TEMP_C",
        "CRITICAL_TEMP_C",
        "PROBATION_HEALTHY_TICKS",
        "LOCAL_FAILURE_LIMIT",
        "VERIFY_ACTIVATION_LATENCY_TICKS",
        "SIM_DEFAULT_SEED",
    ):
        assert hasattr(config, name), f"{name} must be a config knob"

    assert config.MAX_LEASE_SECONDS > 0
    # The verifier's damage limit must sit above the supervisor's intervention
    # point, or the supervisor would only ever act after the damage it exists to
    # prevent (SAFETY_PROTOCOL.md §1 layer 6 in spirit).
    assert config.EMERGENCY_TEMP_C < config.CRITICAL_TEMP_C


def test_baseline_components_import_under_either_mode(reloaded_config):
    """The baseline is not conditional on the flag: switching modes must not
    make Guard Rail, the Sandbox or the rollback path unimportable."""
    for mode in ("source_generation", "manifest_compiler"):
        reloaded_config(ADAPTATION_MODE=mode)
        for module in (
            "server.guardrail.guardrail",
            "server.sandbox.sandbox_runner",
            "server.deploy.rollback",
            "server.deploy.deployer",
            "server.orchestrator",
        ):
            assert importlib.import_module(module) is not None
