"""Deterministic manifest validator — the first gate in manifest_compiler mode.

`RESEARCH.md §2/§3`. Nothing here calls a model, touches the network or reads
the clock: the same (manifest, registry, schema, config) always yields the same
verdict, which is what lets a rejection be treated as evidence rather than as an
opinion. It is the manifest-mode counterpart of Guard Rail, and it reports in
the same shape — a per-check pass/fail map plus a joined human-readable reason
(DATA_SCHEMAS.md §12, modelled on §5).

Parsing already rejected the shapes that cannot be checked semantically —
unknown fields, non-numeric comparison values, operators outside the enum. What
is left for this module is everything that depends on *this device* and *this
registry*: that the capabilities exist, that their pins are real and permitted,
that the hardware type matches, that the metrics a rule needs are actually
sensed, that the lease is finite and bounded, that activation and recovery
cannot both hold at once, and that the declared fallback is reachable.
"""

from typing import Literal

from pydantic import BaseModel, Field

import config
from server.manifest.models import (
    BehaviorManifest,
    Condition,
    FallbackKind,
    trigger_terms,
)
from server.manifest.registry import CapabilityRegistry
from server.schemas import HardwareSchema

# Which registry capability each fallback kind needs in order to be reachable.
# `restore_previous_firmware` needs none: it is a supervisor/slot action, not a
# behaviour the controller performs (RESEARCH.md §10).
FALLBACK_REQUIREMENTS: dict[FallbackKind, str | None] = {
    FallbackKind.ENTER_SAFE_IDLE: "enter_safe_idle",
    FallbackKind.HOLD_COOLING: "fan_on",
    FallbackKind.RESTORE_PREVIOUS_FIRMWARE: None,
}

CHECK_NAMES = (
    "manifest_version",
    "device_identity",
    "registry_version",
    "known_capabilities",
    "sensor_binding",
    "actuator_binding",
    "capability_preconditions",
    "pin_permissions",
    "lease_bounds",
    "control_period",
    "condition_consistency",
    "fallback_reachable",
    "resource_budget",
)


class ManifestValidationResult(BaseModel):
    """DATA_SCHEMAS.md §12."""

    manifest_id: str
    manifest_hash: str
    status: Literal["pass", "fail"]
    checks: dict[str, Literal["pass", "fail"]]
    errors: list[str] = Field(default_factory=list)
    capability_registry_version: str
    capability_registry_hash: str

    @property
    def reason(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None


# --- individual checks -------------------------------------------------------


def _check_manifest_version(manifest: BehaviorManifest) -> list[str]:
    if manifest.manifest_version != config.MANIFEST_VERSION:
        return [
            f"manifest_version {manifest.manifest_version!r} is not the supported "
            f"{config.MANIFEST_VERSION!r}"
        ]
    return []


def _check_device_identity(manifest: BehaviorManifest, schema: HardwareSchema) -> list[str]:
    """A manifest is compiled for exactly one device.

    Mismatched ids are the same class of error the signed package rejects on the
    device (RESEARCH.md §9); catching it here means it never gets that far.
    """
    if manifest.device_id != schema.device_id:
        return [
            f"manifest targets {manifest.device_id!r} but the hardware schema is for "
            f"{schema.device_id!r}"
        ]
    return []


def _check_registry_version(
    manifest: BehaviorManifest, registry: CapabilityRegistry
) -> list[str]:
    if manifest.capability_registry_version != registry.capability_registry_version:
        return [
            f"manifest declares registry {manifest.capability_registry_version!r}, "
            f"loaded registry is {registry.capability_registry_version!r}"
        ]
    return []


def _check_known_capabilities(
    manifest: BehaviorManifest, registry: CapabilityRegistry
) -> list[str]:
    """The whole point of the registry: an unregistered capability is not a
    capability the compiler can build, so it is rejected rather than improvised."""
    return [
        f"unknown capability {name!r} (the Agent may select registered capabilities, "
        "not define new ones)"
        for name in manifest.requested_capabilities
        if registry.get(name) is None
    ]


def _check_sensor_binding(
    manifest: BehaviorManifest, registry: CapabilityRegistry
) -> list[str]:
    """Every metric a condition reads must be produced by a requested sensor and
    declared in `sensor_inputs`."""
    errors: list[str] = []
    produced = {
        capability.metric
        for name in manifest.requested_capabilities
        if (capability := registry.get(name)) and capability.metric
    }

    for metric in sorted(manifest.sensor_inputs):
        if metric not in registry.metrics:
            errors.append(f"sensor input {metric!r} is not a registry metric")
        elif metric not in produced:
            errors.append(
                f"sensor input {metric!r} is declared but no requested capability produces it"
            )

    for metric in sorted(manifest.metrics_used()):
        if metric not in manifest.sensor_inputs:
            errors.append(
                f"condition reads {metric!r}, which is not in sensor_inputs — a rule "
                "cannot depend on a metric the manifest never asked to sense"
            )
    return errors


def _check_actuator_binding(
    manifest: BehaviorManifest, registry: CapabilityRegistry
) -> list[str]:
    errors: list[str] = []
    driven = {
        capability.actuator
        for name in manifest.requested_capabilities
        if (capability := registry.get(name)) and capability.actuator
    }
    for actuator in sorted(manifest.actuator_outputs):
        if actuator not in registry.actuators:
            errors.append(f"actuator output {actuator!r} is not in the registry")
        elif actuator not in driven:
            errors.append(
                f"actuator output {actuator!r} is declared but no requested capability drives it"
            )
    for actuator in sorted(filter(None, driven)):
        if actuator not in manifest.actuator_outputs:
            errors.append(
                f"capability drives actuator {actuator!r}, which is not declared in "
                "actuator_outputs"
            )
    return errors


def _check_preconditions(
    manifest: BehaviorManifest, registry: CapabilityRegistry, schema: HardwareSchema
) -> list[str]:
    """Run each capability's declared safety preconditions. Every name in a
    registry entry maps to an implemented check here; there is no decorative
    precondition."""
    errors: list[str] = []
    for name in manifest.requested_capabilities:
        capability = registry.get(name)
        if capability is None:
            continue  # already reported by _check_known_capabilities
        for precondition in capability.safety_preconditions:
            match precondition:
                case "pin_present_in_hardware_schema":
                    for pin in capability.permitted_pins:
                        if schema.pin(pin) is None:
                            errors.append(
                                f"{name}: GPIO_{pin} is not present in the hardware schema"
                            )
                case "pin_not_forbidden":
                    for pin in capability.permitted_pins:
                        if schema.is_forbidden(pin):
                            errors.append(f"{name}: GPIO_{pin} is forbidden on this device")
                case "hardware_type_matches":
                    for pin in capability.permitted_pins:
                        entry = schema.pin(pin)
                        if entry and entry.connected_device not in capability.compatible_hardware:
                            errors.append(
                                f"{name}: GPIO_{pin} is wired to {entry.connected_device}, "
                                f"not one of {capability.compatible_hardware}"
                            )
                case "actuator_declared_in_manifest":
                    if capability.actuator and capability.actuator not in manifest.actuator_outputs:
                        errors.append(
                            f"{name}: drives {capability.actuator!r}, which the manifest "
                            "does not declare in actuator_outputs"
                        )
                case "metrics_available":
                    for metric in capability.requires_metrics:
                        if metric not in manifest.sensor_inputs:
                            errors.append(
                                f"{name}: needs {metric!r} but the manifest does not sense it"
                            )
    return errors


def _check_pin_permissions(
    manifest: BehaviorManifest, registry: CapabilityRegistry, schema: HardwareSchema
) -> list[str]:
    """A second, capability-independent sweep of every pin the compiled artifact
    could ever touch.

    Deliberately redundant with the preconditions above: the registry decides
    which checks a capability declares, and a registry entry that forgot to
    declare `pin_not_forbidden` must still not be able to reach a forbidden pin
    (SAFETY_PROTOCOL.md §1 — defense in depth, not a single gate).
    """
    errors: list[str] = []
    for name in manifest.requested_capabilities:
        capability = registry.get(name)
        if capability is None:
            continue
        for pin in capability.permitted_pins:
            if schema.is_forbidden(pin):
                errors.append(f"{name}: forbidden pin GPIO_{pin}")
            elif schema.pin(pin) is None:
                errors.append(f"{name}: GPIO_{pin} is not in the hardware schema")
    return errors


def _check_lease_bounds(manifest: BehaviorManifest) -> list[str]:
    """A temporary morph must expire, and must expire soon enough to matter."""
    if manifest.maximum_duration_seconds > config.MAX_LEASE_SECONDS:
        return [
            f"maximum_duration_seconds {manifest.maximum_duration_seconds} exceeds the "
            f"configured ceiling {config.MAX_LEASE_SECONDS}"
        ]
    return []


def _check_control_period(manifest: BehaviorManifest) -> list[str]:
    errors: list[str] = []
    period = manifest.control_period_seconds
    if not (config.MIN_CONTROL_PERIOD_SECONDS <= period <= config.MAX_CONTROL_PERIOD_SECONDS):
        errors.append(
            f"control_period_seconds {period} is outside "
            f"[{config.MIN_CONTROL_PERIOD_SECONDS}, {config.MAX_CONTROL_PERIOD_SECONDS}]"
        )
    if period > manifest.maximum_duration_seconds:
        errors.append("control_period_seconds exceeds the whole lease: the controller would "
                      "never complete a step before expiry")
    return errors


def _satisfiable_together(left: Condition, right: Condition) -> bool:
    """Can both comparisons hold for some reading of the same metric?

    Only decided for two comparisons on the *same* metric; different metrics are
    independent and are treated as satisfiable. Probing a small set of candidate
    values around both thresholds is exact for this operator set: any interval
    the two conditions share is either open around a threshold or contains one
    of the probed points.
    """
    if left.metric != right.metric:
        return True
    epsilon = 1e-9
    probes = {
        left.value,
        right.value,
        left.value + epsilon,
        left.value - epsilon,
        right.value + epsilon,
        right.value - epsilon,
        min(left.value, right.value) - 1.0,
        max(left.value, right.value) + 1.0,
        (left.value + right.value) / 2.0,
    }
    return any(
        left.evaluate({left.metric: probe}) and right.evaluate({right.metric: probe})
        for probe in probes
    )


def _check_condition_consistency(manifest: BehaviorManifest) -> list[str]:
    """Activation and recovery must be disjoint.

    If both can hold at once the compiled rules are ambiguous — the fan would be
    commanded on and off in the same step — and no amount of runtime arbitration
    makes that a behaviour anyone specified. Reject it at the contract instead.
    """
    errors: list[str] = []
    activation = trigger_terms(manifest.activation_condition)
    recovery = trigger_terms(manifest.recovery_condition)

    for left in activation:
        for right in recovery:
            if left.metric == right.metric and _satisfiable_together(left, right):
                errors.append(
                    f"activation ({left.describe()}) and recovery ({right.describe()}) can "
                    "hold simultaneously; the two must be disjoint"
                )
    return errors


def _check_fallback_reachable(
    manifest: BehaviorManifest, registry: CapabilityRegistry
) -> list[str]:
    """The declared fallback must be something this manifest can actually do."""
    required = FALLBACK_REQUIREMENTS[manifest.fallback_behavior]
    if required is None:
        return []
    if required not in manifest.requested_capabilities:
        return [
            f"fallback_behavior {manifest.fallback_behavior.value!r} needs capability "
            f"{required!r}, which the manifest does not request"
        ]
    capability = registry.get(required)
    if capability is not None and not capability.is_fallback_safe:
        return [
            f"capability {required!r} is not marked safe to fall back to in the registry"
        ]
    return []


def _check_resource_budget(
    manifest: BehaviorManifest, registry: CapabilityRegistry
) -> list[str]:
    """The declared budget must cover what the requested capabilities cost."""
    errors: list[str] = []
    cpu = sum(
        capability.resource_cost.cpu_ms_per_step
        for name in manifest.requested_capabilities
        if (capability := registry.get(name))
    )
    memory = sum(
        capability.resource_cost.memory_kb
        for name in manifest.requested_capabilities
        if (capability := registry.get(name))
    )
    if cpu > manifest.resource_budget.max_cpu_ms_per_step:
        errors.append(
            f"requested capabilities cost {cpu}ms/step, above the declared budget "
            f"{manifest.resource_budget.max_cpu_ms_per_step}ms"
        )
    if memory > manifest.resource_budget.max_memory_kb:
        errors.append(
            f"requested capabilities need {memory}KB, above the declared budget "
            f"{manifest.resource_budget.max_memory_kb}KB"
        )

    limit = config.VERIFY_MAX_ACTUATOR_TRANSITIONS_PER_MIN
    if manifest.resource_budget.max_actuator_transitions_per_minute > limit:
        errors.append(
            f"max_actuator_transitions_per_minute "
            f"{manifest.resource_budget.max_actuator_transitions_per_minute} exceeds the "
            f"configured ceiling {limit}"
        )
    for name in manifest.requested_capabilities:
        capability = registry.get(name)
        limits = capability.actuator_limits if capability else None
        if limits and manifest.resource_budget.max_actuator_transitions_per_minute > (
            limits.max_transitions_per_minute
        ):
            errors.append(
                f"{name}: budget allows more transitions/min than the actuator's registry "
                f"limit of {limits.max_transitions_per_minute}"
            )
    return errors


# --- entry point -------------------------------------------------------------


def validate(
    manifest: BehaviorManifest,
    registry: CapabilityRegistry,
    schema: HardwareSchema,
) -> ManifestValidationResult:
    """Run every check. Pure: no DB, no network, no model call, no clock."""
    failures: dict[str, list[str]] = {
        "manifest_version": _check_manifest_version(manifest),
        "device_identity": _check_device_identity(manifest, schema),
        "registry_version": _check_registry_version(manifest, registry),
        "known_capabilities": _check_known_capabilities(manifest, registry),
        "sensor_binding": _check_sensor_binding(manifest, registry),
        "actuator_binding": _check_actuator_binding(manifest, registry),
        "capability_preconditions": _check_preconditions(manifest, registry, schema),
        "pin_permissions": _check_pin_permissions(manifest, registry, schema),
        "lease_bounds": _check_lease_bounds(manifest),
        "control_period": _check_control_period(manifest),
        "condition_consistency": _check_condition_consistency(manifest),
        "fallback_reachable": _check_fallback_reachable(manifest, registry),
        "resource_budget": _check_resource_budget(manifest, registry),
    }
    assert set(failures) == set(CHECK_NAMES), "check table drifted from CHECK_NAMES"

    errors = [error for reasons in failures.values() for error in reasons]
    return ManifestValidationResult(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        status="fail" if errors else "pass",
        checks={name: ("fail" if reasons else "pass") for name, reasons in failures.items()},
        errors=errors,
        capability_registry_version=registry.capability_registry_version,
        capability_registry_hash=registry.content_hash,
    )
