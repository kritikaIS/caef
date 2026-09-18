"""M23 — the optional MCP tool surface (RESEARCH.md §13).

MCP is an interoperability layer, and the point of these tests is that it is
*only* that: the same deterministic gates apply behind it, the read-only inputs
stay read-only, and deployment is not reachable through it at all.

Nothing here imports `mcp`. The tools are plain functions, which is why the
whole surface is testable with no optional dependency installed.
"""

import logging

import pytest

from server.mcp import tools
from tests.fixtures import manifests


@pytest.fixture(autouse=True)
def clean():
    logging.getLogger("caef.supervisor").setLevel(logging.CRITICAL)
    tools.reset()
    yield
    tools.reset()
    logging.getLogger("caef.supervisor").setLevel(logging.NOTSET)


# --- read-only inputs --------------------------------------------------------


def test_the_schema_and_registry_are_readable():
    schema = tools.call("get_hardware_schema", {"device_id": "pi_node_alpha"})
    assert schema["ok"] and schema["schema"]["device_id"] == "pi_node_alpha"

    registry = tools.call("get_capability_registry")
    assert registry["ok"]
    assert "fan_on" in registry["registry"]["capabilities"]
    assert len(registry["content_hash"]) == 64


def test_there_is_no_setter_for_either():
    """Read-only by construction: the surface offers no way to write them
    (SAFETY_PROTOCOL.md §1 layer 1)."""
    assert not [name for name in tools.TOOLS if name.startswith(("set_", "write_", "update_"))]
    for name in ("get_hardware_schema", "get_capability_registry"):
        assert name in tools.TOOLS


def test_an_unsupported_registry_version_is_an_error_not_a_fallback():
    result = tools.call("get_capability_registry", {"version": "42.0.0"})
    assert result["ok"] is False


# --- the same gates, behind the protocol ------------------------------------


def test_a_sound_manifest_validates_and_simulates():
    proposal = tools.call("propose_manifest", {"manifest": manifests.COOLING})
    assert proposal["ok"] and proposal["validation"]["status"] == "pass"

    simulation = tools.call(
        "simulate_candidate",
        {"proposal_id": proposal["proposal_id"], "scenarios": ["gradual_overheat"], "seeds": [1]},
    )
    assert simulation["ok"] and simulation["status"] == "pass"

    report = tools.call(
        "retrieve_verification_report", {"report_id": simulation["report_id"]}
    )
    assert report["ok"] and report["report"]["status"] == "pass"


def test_an_invalid_manifest_is_rejected_by_the_same_validator():
    bad = {**manifests.COOLING, "requested_capabilities": ["gpio_write_raw"]}
    result = tools.call("validate_manifest", {"manifest": bad})
    assert result["ok"]
    assert result["validation"]["status"] == "fail"
    assert "gpio_write_raw" in "; ".join(result["validation"]["errors"])


def test_an_unknown_field_is_rejected_at_the_boundary():
    result = tools.call("propose_manifest", {"manifest": {**manifests.COOLING, "sudo": True}})
    assert result["ok"] is False
    assert "not a valid Behavior Manifest" in result["error"]


def test_simulation_refuses_a_manifest_that_will_not_compile():
    incoherent = {
        **manifests.COOLING,
        "requested_capabilities": [
            "read_temperature", "read_distance", "fan_on", "fan_off", "emit_heartbeat",
        ],
        "sensor_inputs": ["temperature_c", "distance_cm"],
        "fallback_behavior": "restore_previous_firmware",
    }
    result = tools.call("simulate_candidate", {"manifest": incoherent})
    assert result["ok"] is False
    assert "could not be compiled" in result["error"]


def test_a_failing_candidate_reports_failure_rather_than_passing_quietly():
    useless = {
        **manifests.COOLING,
        "activation_condition": {"metric": "temperature_c", "operator": ">=", "value": 200.0},
    }
    result = tools.call(
        "simulate_candidate", {"manifest": useless, "scenarios": ["gradual_overheat"], "seeds": [1]}
    )
    assert result["ok"] and result["status"] == "fail"


# --- what the surface will not do -------------------------------------------


def test_deployment_is_not_available_through_mcp():
    """A model can propose, validate and simulate. It cannot deploy."""
    result = tools.call("deploy", {"proposal_id": "anything"})
    assert result["ok"] is False
    assert "not available through the MCP surface" in result["error"]


def test_nothing_on_the_surface_deploys_signs_or_installs():
    forbidden = ("sign", "install", "push", "ota", "rollback", "activate")
    for name in tools.TOOLS:
        if name == "deploy":
            continue  # present only to refuse
        assert not any(word in name for word in forbidden), name


def test_a_successful_simulation_still_deploys_nothing():
    proposal = tools.call("propose_manifest", {"manifest": manifests.COOLING})
    assert proposal["deployed"] is False
    simulation = tools.call(
        "simulate_candidate", {"proposal_id": proposal["proposal_id"], "seeds": [1]}
    )
    assert simulation["deployed"] is False


def test_a_proposal_id_confers_nothing():
    """It is a handle, not an authorisation."""
    proposal = tools.call("propose_manifest", {"manifest": manifests.COOLING})
    assert tools.call("deploy", {"proposal_id": proposal["proposal_id"]})["ok"] is False


# --- robustness --------------------------------------------------------------


def test_an_unknown_tool_is_an_error_not_an_exception():
    result = tools.call("rm_rf_slash")
    assert result["ok"] is False and "unknown tool" in result["error"]


def test_bad_arguments_are_an_error_not_an_exception():
    assert tools.call("get_hardware_schema", {"nope": 1})["ok"] is False
    assert tools.call("simulate_candidate", {"proposal_id": "missing"})["ok"] is False
    assert tools.call("retrieve_verification_report", {"report_id": "missing"})["ok"] is False


def test_a_manifest_that_is_not_an_object_is_an_error():
    for payload in ("not a manifest", 42, ["a", "list"]):
        assert tools.call("propose_manifest", {"manifest": payload})["ok"] is False


# --- optionality -------------------------------------------------------------


def test_mcp_is_not_a_dependency():
    """Nothing in CAEF requires the package, and the tools work without it."""
    from pathlib import Path

    requirements = Path("requirements.txt").read_text().lower()
    assert "\nmcp" not in requirements and not requirements.startswith("mcp")
    assert tools.call("get_capability_registry")["ok"] is True


def test_the_binding_builds_or_degrades_cleanly():
    """Either the optional package is installed and a server is built over
    exactly the tool surface, or it is absent and the adapter says so without
    failing. `main()` is not called here: with the package present it would
    block serving."""
    from server.mcp import server

    built = server.build_server()
    if built is None:
        assert server.main() == 0
        return
    assert hasattr(built, "run")
