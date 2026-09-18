"""Capability Registry — the closed set of behaviours the compiler may build.

`DATA_SCHEMAS.md §11`, RESEARCH.md §3. The hardware schema and this registry
answer different questions and are versioned separately:

    hardware schema      what is physically wired to this device
    capability registry  which behaviours may be constructed from it

The Agent may *select* a registered capability. It cannot define one, extend
one, or change a limit on one, because this module offers no writer: there is no
function here that opens the file for writing, and nothing else in the pipeline
touches the path. That is the same construction the v0.1 hardware-schema tool
uses (SAFETY_PROTOCOL.md §1 layer 1) — a missing code path, not a prompt.

Every loaded registry carries the SHA-256 of its own bytes. The compiler report
and the signed firmware package both record it, so an artifact is traceable to
the exact registry content that produced it — and a registry edited between
compile and install is detectable rather than assumed away.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import config
from server.manifest.canonical import content_hash

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class UnsupportedRegistryVersion(ValueError):
    """The requested registry version is not one this build can serve.

    Raised rather than falling back to whatever version happens to be on disk:
    compiling a manifest against a different registry than it declared would
    silently change what its capabilities mean.
    """


class ResourceCost(BaseModel):
    model_config = _FROZEN

    cpu_ms_per_step: float = Field(ge=0)
    memory_kb: int = Field(ge=0)


class ActuatorLimits(BaseModel):
    """The physical envelope an actuator capability may not leave.

    Enforced by the verifier over a whole run and by the supervisor per intent,
    so a controller cannot chatter a relay or exceed the schema's current budget
    (RESEARCH.md §6 property 4, §7 rule 1).
    """

    model_config = _FROZEN

    actuator: str
    state: str
    current_ma: float = Field(ge=0)
    max_transitions_per_minute: int = Field(gt=0)
    min_hold_seconds: float = Field(ge=0)


# Preconditions are a closed set of checks the validator actually implements, so
# a registry entry cannot reference a check that does not exist.
PRECONDITIONS = (
    "pin_present_in_hardware_schema",
    "pin_not_forbidden",
    "hardware_type_matches",
    "actuator_declared_in_manifest",
    "metrics_available",
)

TEMPLATES = ("sensor_read", "actuator_set_state", "telemetry_emit", "safe_idle")


class Capability(BaseModel):
    model_config = _FROZEN

    kind: Literal["sensor", "actuator", "telemetry", "safety"]
    description: str
    compatible_hardware: list[str] = Field(default_factory=list)
    permitted_pins: list[int] = Field(default_factory=list)
    input_type: str
    output_type: str
    metric: str | None = None
    actuator: str | None = None
    resource_cost: ResourceCost
    actuator_limits: ActuatorLimits | None = None
    template: Literal[TEMPLATES]  # type: ignore[valid-type]
    template_params: dict[str, str] = Field(default_factory=dict)
    safety_preconditions: list[Literal[PRECONDITIONS]] = Field(  # type: ignore[valid-type]
        default_factory=list
    )
    requires_metrics: list[str] = Field(default_factory=list)
    safe_fallback: str
    is_fallback_safe: bool = False


class MetricSpec(BaseModel):
    model_config = _FROZEN

    unit: str
    produced_by: str


class ActuatorSpec(BaseModel):
    model_config = _FROZEN

    connected_device: str
    states: list[str]
    default_state: str
    safe_state_when_hot: str


class CapabilityRegistry(BaseModel):
    model_config = _FROZEN

    capability_registry_version: str
    description: str
    metrics: dict[str, MetricSpec]
    actuators: dict[str, ActuatorSpec]
    capabilities: dict[str, Capability]
    # Not part of the file: the hash of the bytes it was loaded from.
    content_hash: str

    def get(self, name: str) -> Capability | None:
        return self.capabilities.get(name)

    def require(self, name: str) -> Capability:
        capability = self.capabilities.get(name)
        if capability is None:
            raise KeyError(f"unknown capability {name!r}")
        return capability

    def metric_producers(self) -> dict[str, str]:
        return {
            capability.metric: name
            for name, capability in self.capabilities.items()
            if capability.metric
        }


def registry_path(version: str) -> Path:
    """One file per major version: `capability_registry_v<major>.json`."""
    major = version.split(".", 1)[0]
    return Path(config.CAPABILITY_REGISTRY_DIR) / f"capability_registry_v{major}.json"


@lru_cache(maxsize=4)
def load_registry(version: str | None = None) -> CapabilityRegistry:
    """Read a registry version. Read-only: opened for reading, returned frozen.

    Cached because it is read on every validation, compilation and intent check;
    the cache key is the version string, so a test pointing
    `CAPABILITY_REGISTRY_DIR` elsewhere must clear it (`load_registry.cache_clear`).
    """
    version = version or config.CAPABILITY_REGISTRY_VERSION
    path = registry_path(version)
    if not path.exists():
        raise UnsupportedRegistryVersion(f"no capability registry for version {version!r}")

    raw = path.read_bytes()
    payload = json.loads(raw)
    declared = payload.get("capability_registry_version")
    if declared != version:
        raise UnsupportedRegistryVersion(
            f"{path.name} declares version {declared!r}, not the requested {version!r}"
        )

    return CapabilityRegistry.model_validate(
        {**payload, "content_hash": content_hash(payload)}
    )


def is_supported_version(version: str) -> bool:
    """Whether a package's declared registry version can be served at all.

    Used by the device before it trusts a package (RESEARCH.md §9).
    """
    try:
        load_registry(version)
    except (UnsupportedRegistryVersion, ValueError):
        return False
    return True
