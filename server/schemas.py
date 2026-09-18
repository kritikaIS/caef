"""Pydantic mirrors of DATA_SCHEMAS.md — the wire contract between components.

Field names here are copied verbatim from that document (DATA_SCHEMAS.md §9,
naming consistency rule). Renaming a field here without updating the doc in the
same commit is a build failure per CLAUDE.md §4.
"""

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

import config


# The only sanctioned way the Agent learns whether a pin is safe (TDD.md §2.4).
# Guard Rail matches tool-call traces against this exact name, so it lives here
# rather than in the Agent package that Guard Rail must not depend on.
TOOL_CHECK_HARDWARE_SCHEMA = "check_hardware_schema"
# Prefix marking a tool call that resolved successfully (DATA_SCHEMAS.md §4
# example: "SAFE: Connected to Relay_Fan"). Anything else is not provenance.
TOOL_RESULT_SAFE_PREFIX = "SAFE"


class TriggerType(StrEnum):
    CONTEXT_TRIGGER = "CONTEXT_TRIGGER"
    CRITICAL_FAILURE = "CRITICAL_FAILURE"


class PinStatus(StrEnum):
    AVAILABLE = "available"
    DORMANT = "dormant"
    ACTIVE = "active"
    FORBIDDEN = "forbidden"


class RecordType(StrEnum):
    MORPH_DEPLOY = "morph_deploy"
    PATCH_DEPLOY = "patch_deploy"
    REVERSION = "reversion"
    ROLLBACK = "rollback"


class RecordStatus(StrEnum):
    DEPLOYED = "deployed"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class AdaptationMode(StrEnum):
    """Which pipeline produced a deployment (RESEARCH.md §1).

    Recorded on the ledger so both arms can be compared from the same table
    rather than from two divergent ones.
    """

    SOURCE_GENERATION = "source_generation"
    MANIFEST_COMPILER = "manifest_compiler"


class DeploymentState(StrEnum):
    """Every distinguishable stage of a deployment (DATA_SCHEMAS.md §19).

    The v0.1 ledger had `deployed | superseded | failed`, and `deployer.deploy`
    wrote `deployed` even when the OTA push found nobody home. That conflates
    four different things — we tried to send it, the device took it, the device
    is running it, and it worked — and only the last is what an operator reading
    "deployed" believes. These states keep them apart (RESEARCH.md §11).
    """

    PROPOSED = "proposed"
    MANIFEST_VALIDATED = "manifest_validated"
    COMPILED = "compiled"
    SIMULATION_VERIFIED = "simulation_verified"
    SIGNED = "signed"
    DELIVERY_ATTEMPTED = "delivery_attempted"
    ACCEPTED_BY_DEVICE = "accepted_by_device"
    ACTIVE_ON_DEVICE = "active_on_device"
    REJECTED = "rejected"
    REVERTED = "reverted"
    ROLLED_BACK = "rolled_back"


# --- §1 Device Hardware Schema ----------------------------------------------


class PinEntry(BaseModel):
    connected_device: str
    type: str
    status: PinStatus
    protocol: str | None = None
    active_level: str | None = None


class SchemaConstraints(BaseModel):
    max_gpio_current: str
    forbidden_pins: list[int] = Field(default_factory=list)

    def max_current_ma(self) -> float | None:
        """`max_gpio_current` as a number, or None if it is not expressed in mA.

        Added for the local safety supervisor, which checks a declared actuator
        draw against it per intent (RESEARCH.md §7). Guard Rail keeps its own
        parser: the baseline is the experimental control and is not edited to
        share code with the mode being compared against it.
        """
        match = re.search(r"([\d.]+)\s*mA", self.max_gpio_current, re.IGNORECASE)
        return float(match.group(1)) if match else None


class HardwareSchema(BaseModel):
    """Read-only to the Agent — enforced at the tool layer, not by convention."""

    device_id: str
    mcu_type: str
    constraints: SchemaConstraints
    pinout: dict[str, PinEntry]

    def pin(self, pin_number: int) -> PinEntry | None:
        return self.pinout.get(f"GPIO_{pin_number}")

    def is_forbidden(self, pin_number: int) -> bool:
        """All three denylists are authoritative (DATA_SCHEMAS.md §1 field notes
        plus config.EXTRA_FORBIDDEN_PINS as a pipeline-wide extension)."""
        entry = self.pin(pin_number)
        return (
            pin_number in self.constraints.forbidden_pins
            or pin_number in config.EXTRA_FORBIDDEN_PINS
            or (entry is not None and entry.status is PinStatus.FORBIDDEN)
        )


def load_hardware_schema(device_id: str) -> HardwareSchema:
    """Read the device's hardware schema. Read-only by construction: nothing in
    the pipeline ever writes back through this path (SAFETY_PROTOCOL.md §1)."""
    path = config.HARDWARE_SCHEMA_DIR / f"schema_{device_id}.json"
    if not path.exists():  # v0.1 ships one example device schema
        path = config.HARDWARE_SCHEMA_DIR / "schema_dev01.json"
    return HardwareSchema.model_validate(json.loads(path.read_text()))


# --- §2 Telemetry Payload ----------------------------------------------------


class TelemetryPayload(BaseModel):
    id: str
    timestamp: int  # device-authoritative (ARCHITECTURE.md §4.2)
    trigger_type: TriggerType
    event: str
    data: dict[str, Any] = Field(default_factory=dict)
    current_state_hash: str


# --- §3 Agent Task -----------------------------------------------------------


class AgentTask(BaseModel):
    task_id: str
    event_id: str
    device_id: str
    trigger_type: TriggerType
    event: str
    raw_payload: dict[str, Any]
    retry_count: int = 0


# --- §3a Event Notification --------------------------------------------------


class EventNotification(BaseModel):
    """The `Event` half of the Distributor fan-out (ARCHITECTURE.md §3): the
    same occurrence as the Task, addressed to observers rather than the Agent.
    Carries `event_id` so the Frontend can join it to the History Table."""

    event_id: str
    device_id: str
    trigger_type: TriggerType
    event: str
    timestamp: int  # device-authoritative
    current_state_hash: str
    data: dict[str, Any] = Field(default_factory=dict)


# --- §4 Agent Output ---------------------------------------------------------


class ToolCallRecord(BaseModel):
    tool: str
    args: dict[str, Any]
    result: str


class AgentOutput(BaseModel):
    patch_id: str
    event_id: str
    device_id: str
    plan: str
    target_file: str
    code: str
    pins_referenced: list[int] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


# --- §5 Guard Rail Result ----------------------------------------------------


class GuardRailChecks(BaseModel):
    forbidden_pin_check: Literal["pass", "fail"]
    tool_call_provenance: Literal["pass", "fail"]
    schema_conformance: Literal["pass", "fail"]
    static_safety_denylist: Literal["pass", "fail"]
    current_draw_sanity: Literal["pass", "fail"]


class GuardRailResult(BaseModel):
    patch_id: str
    status: Literal["pass", "fail"]
    checks: GuardRailChecks
    reason: str | None = None


# --- §6 Sandbox Result -------------------------------------------------------


class SandboxResult(BaseModel):
    patch_id: str
    status: Literal["pass", "fail"]
    runtime_seconds: float
    exit_code: int
    logs: str = ""
    results: str | None = None
    delta_firmware: str | None = None


# --- §6a OTA Push ------------------------------------------------------------


class OTAPush(BaseModel):
    device_id: str
    fw_hash: str
    target_file: str
    code: str
    patch_id: str | None = None
    record_type: RecordType


class OTAAck(BaseModel):
    device_id: str
    status: Literal["accepted", "rejected"]
    fw_hash: str
    reason: str | None = None


# --- §6b Poll / Reconciliation ----------------------------------------------


class PollResponse(BaseModel):
    poll_id: str
    device_id: str
    assigned_fw_hash: str | None
    in_sync: bool


def fw_hash(code: str) -> str:
    """Canonical firmware hash. One implementation, used by device, deploy and
    History Table alike, so `current_state_hash` and `fw_hash` are comparable."""
    return hashlib.sha256(code.encode()).hexdigest()[:16]
