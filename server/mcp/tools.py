"""MCP-shaped tool surface for the contract-constrained pipeline (RESEARCH.md §13).

An interoperability layer, and **not** a safety mechanism. Everything here is a
thin wrapper over the same deterministic components the local pipeline uses, and
every safety decision stays where it was: in the validator, the compiler, the
verifier, the signer and the device. Exposing them over a protocol changes who
can call them, not what they enforce.

Two rules shape the surface:

  - **Read-only inputs stay read-only.** `get_hardware_schema` and
    `get_capability_registry` return data. There is no setter for either, here
    or anywhere else (SAFETY_PROTOCOL.md §1 layer 1).
  - **Deployment is not a tool.** A model can propose, validate and simulate.
    It cannot deploy, sign, or ask a device to install anything. `deploy` exists
    only as an explicit refusal, so calling it produces an answer rather than a
    confusing "unknown tool".

This module has **no MCP dependency**. It is plain functions over JSON-ish
dicts, so it is testable and usable with nothing installed; `server/mcp/server.py`
binds it to a real MCP server only if the optional package is present.
"""

import uuid
from typing import Any, Callable

import config
from server.compiler.compiler import compile_manifest
from server.manifest.models import BehaviorManifest
from server.manifest.registry import UnsupportedRegistryVersion, load_registry
from server.manifest.validator import validate
from server.schemas import load_hardware_schema
from server.sim import scenarios as scenario_registry
from server.verify.verifier import verify

# Proposals and reports submitted through this surface, by id. In-process and
# non-durable on purpose: this is an interoperability shim, not a store of
# record — the ledger is (DATA_SCHEMAS.md §19).
_PROPOSALS: dict[str, BehaviorManifest] = {}
_REPORTS: dict[str, dict] = {}


class ToolError(ValueError):
    """A tool was called with something it cannot use. Returned, never raised
    across the protocol boundary."""


def _ok(**payload) -> dict[str, Any]:
    return {"ok": True, **payload}


def _error(message: str, **payload) -> dict[str, Any]:
    return {"ok": False, "error": message, **payload}


# --- read-only inputs --------------------------------------------------------


def get_hardware_schema(device_id: str = "") -> dict[str, Any]:
    """The device's physical reality. Read-only: there is no counterpart setter."""
    schema = load_hardware_schema(device_id or config.DEVICE_ID)
    return _ok(schema=schema.model_dump(mode="json"))


def get_capability_registry(version: str = "") -> dict[str, Any]:
    """The closed set of behaviours the compiler will build, and its content hash."""
    try:
        registry = load_registry(version or config.CAPABILITY_REGISTRY_VERSION)
    except UnsupportedRegistryVersion as exc:
        return _error(str(exc))
    payload = registry.model_dump(mode="json")
    return _ok(registry=payload, content_hash=registry.content_hash)


# --- proposal ----------------------------------------------------------------


def _parse(manifest: dict[str, Any]) -> BehaviorManifest:
    if not isinstance(manifest, dict):
        raise ToolError("manifest must be a JSON object")
    return BehaviorManifest.model_validate(manifest)


def propose_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Submit a manifest. Parses and validates it; deploys nothing.

    A proposal id comes back so the caller can simulate it without resending —
    but a proposal id confers nothing. It is a handle, not an authorisation.
    """
    try:
        parsed = _parse(manifest)
    except (ToolError, ValueError) as exc:
        return _error(f"not a valid Behavior Manifest: {exc}")

    registry_result = _registry_for(parsed)
    if isinstance(registry_result, dict):
        return registry_result
    schema = load_hardware_schema(parsed.device_id)
    verdict = validate(parsed, registry_result, schema)

    proposal_id = f"proposal-{uuid.uuid4().hex[:12]}"
    _PROPOSALS[proposal_id] = parsed
    return _ok(
        proposal_id=proposal_id,
        manifest_hash=parsed.manifest_hash,
        validation=verdict.model_dump(mode="json"),
        deployed=False,
        note="Accepted for review only. Deployment is not available through this surface.",
    )


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate without submitting. Same deterministic checks as the pipeline."""
    try:
        parsed = _parse(manifest)
    except (ToolError, ValueError) as exc:
        return _error(f"not a valid Behavior Manifest: {exc}")
    registry_result = _registry_for(parsed)
    if isinstance(registry_result, dict):
        return registry_result
    verdict = validate(parsed, registry_result, load_hardware_schema(parsed.device_id))
    return _ok(validation=verdict.model_dump(mode="json"))


# --- simulation --------------------------------------------------------------


def simulate_candidate(
    proposal_id: str = "",
    manifest: dict[str, Any] | None = None,
    scenarios: list[str] | None = None,
    seeds: list[int] | None = None,
) -> dict[str, Any]:
    """Compile and run a manifest in the closed-loop world. Deploys nothing.

    Accepts either a previously submitted `proposal_id` or an inline manifest,
    so a caller can iterate without accumulating proposals.
    """
    parsed = _PROPOSALS.get(proposal_id)
    if parsed is None:
        if manifest is None:
            return _error(f"unknown proposal_id {proposal_id!r} and no manifest supplied")
        try:
            parsed = _parse(manifest)
        except (ToolError, ValueError) as exc:
            return _error(f"not a valid Behavior Manifest: {exc}")

    registry_result = _registry_for(parsed)
    if isinstance(registry_result, dict):
        return registry_result
    schema = load_hardware_schema(parsed.device_id)

    verdict = validate(parsed, registry_result, schema)
    if verdict.status == "fail":
        return _error("manifest is invalid", validation=verdict.model_dump(mode="json"))

    compilation = compile_manifest(parsed, registry_result, schema)
    if compilation.status == "fail":
        return _error(
            "manifest could not be compiled",
            compiler_report=compilation.report.model_dump(mode="json"),
        )

    unknown = [name for name in (scenarios or []) if name not in scenario_registry.SCENARIOS]
    if unknown:
        return _error(f"unknown scenarios: {unknown}")

    suite = verify(
        compilation.program,
        schema,
        registry_result,
        scenario_names=scenarios or None,
        seeds=seeds or None,
    )
    report_id = f"report-{uuid.uuid4().hex[:12]}"
    _REPORTS[report_id] = suite.model_dump(mode="json")
    return _ok(
        report_id=report_id,
        artifact_hash=compilation.program.artifact_hash,
        status=suite.status,
        summary=suite.summary(),
        compiler_report=compilation.report.model_dump(mode="json"),
        deployed=False,
    )


def retrieve_verification_report(report_id: str) -> dict[str, Any]:
    report = _REPORTS.get(report_id)
    if report is None:
        return _error(f"unknown report_id {report_id!r}")
    return _ok(report=report)


# --- the refusal -------------------------------------------------------------


def deploy(proposal_id: str = "", report_id: str = "", device_id: str = "") -> dict[str, Any]:
    """Deliberately not a capability of this surface.

    Present so that calling it returns a reason rather than "unknown tool", and
    so the boundary is documented in the place someone would look for it. The
    arguments exist only so a caller that supplies the obvious ones gets the
    refusal rather than an argument error.
    """
    return _error(
        "deployment is not available through the MCP surface. A signed package is "
        "issued only by the deterministic pipeline, after validation, compilation "
        "and closed-loop verification, and is verified again on the device. MCP is "
        "an interoperability layer, not an authorisation one."
    )


TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_hardware_schema": get_hardware_schema,
    "get_capability_registry": get_capability_registry,
    "propose_manifest": propose_manifest,
    "validate_manifest": validate_manifest,
    "simulate_candidate": simulate_candidate,
    "retrieve_verification_report": retrieve_verification_report,
    "deploy": deploy,
}

DESCRIPTIONS: dict[str, str] = {
    "get_hardware_schema": "Read a device's hardware schema. Read-only.",
    "get_capability_registry": (
        "Read the capability registry: the closed set of behaviours the compiler "
        "will build. Read-only."
    ),
    "propose_manifest": (
        "Submit a Behavior Manifest for review. Returns a proposal id and the "
        "deterministic validation result. Deploys nothing."
    ),
    "validate_manifest": "Validate a Behavior Manifest without submitting it.",
    "simulate_candidate": (
        "Compile a manifest and run it in the closed-loop virtual world across "
        "seeded scenarios. Returns a verification report id. Deploys nothing."
    ),
    "retrieve_verification_report": "Fetch a verification report by id.",
    "deploy": "Not available. Deployment is behind the deterministic pipeline.",
}


def call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch a tool call. Never raises across the boundary."""
    tool = TOOLS.get(name)
    if tool is None:
        return _error(f"unknown tool {name!r}; available: {sorted(TOOLS)}")
    try:
        return tool(**(arguments or {}))
    except TypeError as exc:
        return _error(f"bad arguments for {name}: {exc}")
    except Exception as exc:  # a tool must not take the protocol down with it
        return _error(f"{name} failed: {type(exc).__name__}: {exc}")


def reset() -> None:
    """Drop submitted proposals and reports. For tests and long-lived processes."""
    _PROPOSALS.clear()
    _REPORTS.clear()


def _registry_for(manifest: BehaviorManifest):
    try:
        return load_registry(manifest.capability_registry_version)
    except UnsupportedRegistryVersion as exc:
        return _error(str(exc))
