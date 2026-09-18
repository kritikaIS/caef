"""Manifest fixtures shared by the contract-constrained tests.

One valid cooling manifest and one baseline monitoring manifest, both built by
`dict` merge so a test can express exactly the one field it is breaking.
"""

from server.manifest.models import BehaviorManifest

COOLING: dict = {
    "manifest_version": "1.0",
    "manifest_id": "manifest-cooling-1",
    "device_id": "pi_node_alpha",
    "event_id": "event-heat-1",
    "trigger_type": "CONTEXT_TRIGGER",
    "trigger_event": "HIGH_HEAT_DETECTED",
    "current_firmware_hash": "a1b2c3d4e5f60718",
    "capability_registry_version": "1.0.0",
    "requested_capabilities": [
        "read_temperature",
        "fan_on",
        "fan_off",
        "emit_heartbeat",
        "emit_critical_event",
        "enter_safe_idle",
    ],
    "sensor_inputs": ["temperature_c"],
    "actuator_outputs": ["fan"],
    "activation_condition": {"metric": "temperature_c", "operator": ">=", "value": 80.0},
    "recovery_condition": {"metric": "temperature_c", "operator": "<", "value": 70.0},
    "maximum_duration_seconds": 300,
    "control_period_seconds": 1.0,
    "resource_budget": {
        "max_cpu_ms_per_step": 5.0,
        "max_memory_kb": 256,
        "max_actuator_transitions_per_minute": 12,
    },
    "fallback_behavior": "enter_safe_idle",
    "rationale": "Device is over the heat threshold; hold cooling until it recovers.",
}

MONITOR: dict = {
    **COOLING,
    "manifest_id": "manifest-monitor-1",
    "trigger_event": "BASELINE_MONITOR",
    "requested_capabilities": ["read_temperature", "emit_heartbeat", "emit_critical_event"],
    "actuator_outputs": [],
    "fallback_behavior": "restore_previous_firmware",
    "rationale": "Baseline: sample temperature, beat, report critical readings.",
}


def cooling(**overrides) -> BehaviorManifest:
    return BehaviorManifest.model_validate({**COOLING, **overrides})


def monitor(**overrides) -> BehaviorManifest:
    return BehaviorManifest.model_validate({**MONITOR, **overrides})
