# DATA_SCHEMAS.md
## CAEF Pipeline — Canonical Data Structures

All components must conform to these shapes. If an implementation needs a
field not listed here, extend this document first, then the code — schemas
are the contract, not an afterthought.

---

## 1. Device Hardware Schema (`schema_dev01.json`)

**Read-only to the Agent.** Represents physical reality; never modified by
generated code or by the Agent, only by an operator/provisioning step.

```json
{
  "device_id": "pi_node_alpha",
  "mcu_type": "RaspberryPi_4B",
  "constraints": {
    "max_gpio_current": "16mA",
    "forbidden_pins": [0, 1]
  },
  "pinout": {
    "GPIO_17": {
      "connected_device": "DHT11",
      "type": "sensor",
      "protocol": "digital_1wire",
      "status": "available"
    },
    "GPIO_27": {
      "connected_device": "Relay_Fan",
      "type": "actuator",
      "active_level": "HIGH",
      "status": "dormant"
    },
    "GPIO_22": {
      "connected_device": "Lidar_X2",
      "type": "sensor",
      "protocol": "UART",
      "status": "dormant"
    }
  }
}
```

**Field notes:**
- `status`: one of `available`, `dormant`, `active`, `forbidden`. `dormant` = physically connected but not in the currently-running code. `forbidden` pins must never appear in generated code (Guard Rail enforced).
- `forbidden_pins` in `constraints` is the authoritative denylist; `status: "forbidden"` on an individual pin entry is a secondary/explicit marker — both must be checked (defense in depth, per TRD §6).

## 2. Telemetry Payload

Sent from device to Listener.

```json
{
  "id": "pi_node_alpha",
  "timestamp": 171542000,
  "trigger_type": "CONTEXT_TRIGGER",
  "event": "HIGH_HEAT_DETECTED",
  "data": {
    "temp_c": 85.4,
    "threshold": 80.0
  },
  "current_state_hash": "a1b2c3d4"
}
```

- `trigger_type`: `CONTEXT_TRIGGER` | `CRITICAL_FAILURE`.
- `timestamp`: device-authoritative (Architecture §4.2 — "the device is the clock").
- `current_state_hash`: hash of the firmware currently running on the device at emission time — used to detect drift against the server's assigned-firmware record (feeds the Poll/Reconciliation Loop).
- For `CRITICAL_FAILURE`, `data` additionally carries `trace` (stack trace string) instead of/alongside sensor values, e.g. `{"trace": "IndexError: list index out of range ..."}`.

## 3. Agent Task (Distributor → Agent)

```json
{
  "task_id": "uuid",
  "event_id": "uuid",
  "device_id": "pi_node_alpha",
  "trigger_type": "CONTEXT_TRIGGER",
  "event": "HIGH_HEAT_DETECTED",
  "raw_payload": { "...": "the original telemetry payload" },
  "retry_count": 0
}
```

## 3a. Event Notification (Distributor → Frontend / DB)

The `Event` half of the Distributor fan-out (ARCHITECTURE §3), published for
every accepted telemetry payload — including `HEARTBEAT`, which is observed but
does not produce a `Task`. Same occurrence as §3, addressed to observers rather
than the Agent.

```json
{
  "event_id": "uuid",
  "device_id": "pi_node_alpha",
  "trigger_type": "CONTEXT_TRIGGER",
  "event": "HIGH_HEAT_DETECTED",
  "timestamp": 171542000,
  "current_state_hash": "a1b2c3d4",
  "data": { "temp_c": 85.4, "threshold": 80.0 }
}
```

- `event_id` matches the persisted `events.id`, so the Frontend can join a live
  feed entry to its History Table rows without a second lookup key.

## 4. Agent Output (Agent → Guard Rail → Sandbox)

```json
{
  "patch_id": "uuid",
  "event_id": "uuid",
  "device_id": "pi_node_alpha",
  "plan": "Enable Relay_Fan on GPIO_27 (dormant → active). Remove Lidar_X2 driver to free CPU. Loop holds fan HIGH until temp < 60C.",
  "target_file": "main.py",
  "code": "<full file contents as string>",
  "pins_referenced": [27],
  "tool_calls": [
    { "tool": "check_hardware_schema", "args": { "pin_number": 27 }, "result": "SAFE: Connected to Relay_Fan" }
  ]
}
```

- `pins_referenced` and `tool_calls` exist specifically so Guard Rail can verify FR-12/NFR-3 mechanically (every referenced pin has a corresponding successful tool call) without re-parsing the code's prose plan.

## 5. Guard Rail Result

```json
{
  "patch_id": "uuid",
  "status": "pass",
  "checks": {
    "forbidden_pin_check": "pass",
    "tool_call_provenance": "pass",
    "schema_conformance": "pass",
    "static_safety_denylist": "pass",
    "current_draw_sanity": "pass"
  },
  "reason": null
}
```

On rejection, `status: "fail"` and `reason` is a human-readable explanation (also stored for audit and fed back to the Agent identically to a Sandbox failure — see LOOPS.md §2/§4).

## 6. Sandbox Result

```json
{
  "patch_id": "uuid",
  "status": "pass",
  "runtime_seconds": 10,
  "exit_code": 0,
  "logs": "...",
  "results": null,
  "delta_firmware": null
}
```

On failure:

```json
{
  "patch_id": "uuid",
  "status": "fail",
  "runtime_seconds": 2,
  "exit_code": 1,
  "logs": "Traceback ...",
  "results": "Process exited with code 1 after 2s",
  "delta_firmware": "<diff between attempted code and last-known-good code>"
}
```

This is the literal `FAIL (Results, ΔFirmware)` artifact from the architecture diagram.

## 6a. OTA Push Payload (Deploy → Device Watchdog)

Sent server → device over the watchdog's TCP port. `fw_hash` is verified by the
device before it writes the file (TRD §6: integrity, not code-signing, for v0.1).

```json
{
  "device_id": "pi_node_alpha",
  "fw_hash": "a1b2c3d4",
  "target_file": "main.py",
  "code": "<full file contents as string>",
  "patch_id": "uuid",
  "record_type": "morph_deploy"
}
```

Device replies on the same connection:

```json
{ "device_id": "pi_node_alpha", "status": "accepted", "fw_hash": "a1b2c3d4", "reason": null }
```

- `status`: `accepted` | `rejected`. `rejected` carries a `reason` (e.g.
  `hash_mismatch`) and the device keeps running its current firmware — a failed
  push must never leave the device in an unsafe or half-written state (NFR-6).
- `record_type` mirrors the History Table enum (§7) so the device's own log line
  distinguishes a morph from a patch from a rollback.

## 6b. Poll / Reconciliation (Device → Server)

The `Poll id / firmware` and `Polled Poll(id)` exchange from ARCHITECTURE §3.
Device sends `GET /poll?id=<device_id>&current_state_hash=<hash>`; server replies:

```json
{
  "poll_id": "uuid",
  "device_id": "pi_node_alpha",
  "assigned_fw_hash": "a1b2c3d4",
  "in_sync": true
}
```

- `poll_id` is generated per poll and is the value stored in `history.poll_id`.
- `in_sync` is `false` when `assigned_fw_hash` differs from the device's reported
  `current_state_hash`; the device then re-requests the artifact directly
  (`GET /firmware?id=<device_id>`, returning an OTA Push Payload as above). This
  is the missed-push safety net (LOOPS.md §3).

## 7. History Table (canonical ledger row)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID (PK) | |
| `time` | timestamp | device-authoritative event time, plus a separate `received_at`/`deployed_at` for server-side timing |
| `poll_id` | string/nullable | last poll id associated with this record, if applicable |
| `event_id` | UUID (FKEY → `events.id`) | |
| `patch_id` | UUID (FKEY → `patches.id`, nullable for pure reversion/rollback rows referencing a prior patch) | |
| `device_id` | string (FKEY → `devices.id`) | |
| `fw_hash` | string | hash of the deployed firmware artifact |
| `record_type` | enum | `morph_deploy` \| `patch_deploy` \| `reversion` \| `rollback` |
| `status` | enum | `deployed` \| `superseded` \| `failed` |

## 8. RAG Document Types

| Type | Source | Purpose |
|---|---|---|
| **Docs of Node** | Per-device hardware schema + current connection/context schema | Grounds the Agent in physical reality for a specific device |
| **Docs on comp.** | Sensor Driver Library (validated snippets, e.g. `DHT11` class, `MPU6050` I2C setup) | Reusable, pre-vetted code the Agent should prefer over writing drivers from scratch |
| **History** | Prior `Event`/`Patch`/`HistoryRecord` rows | Grounds the Agent in "what worked last time for a similar failure/context" |

## 10. Behavior Manifest (Agent → Validator, `manifest_compiler` mode)

The declarative contract the model proposes instead of source code
(RESEARCH.md §2). **Data, never code**: unknown fields are rejected, conditions
are typed structures, and no field is ever evaluated as an expression.

```json
{
  "manifest_version": "1.0",
  "manifest_id": "manifest-cooling-1",
  "device_id": "pi_node_alpha",
  "event_id": "event-heat-1",
  "trigger_type": "CONTEXT_TRIGGER",
  "trigger_event": "HIGH_HEAT_DETECTED",
  "current_firmware_hash": "a1b2c3d4e5f60718",
  "capability_registry_version": "1.0.0",
  "requested_capabilities": ["read_temperature", "fan_on", "fan_off", "emit_heartbeat"],
  "sensor_inputs": ["temperature_c"],
  "actuator_outputs": ["fan"],
  "activation_condition": { "metric": "temperature_c", "operator": ">=", "value": 80.0 },
  "recovery_condition":   { "metric": "temperature_c", "operator": "<",  "value": 70.0 },
  "maximum_duration_seconds": 300,
  "control_period_seconds": 1.0,
  "resource_budget": {
    "max_cpu_ms_per_step": 5.0,
    "max_memory_kb": 256,
    "max_actuator_transitions_per_minute": 12
  },
  "fallback_behavior": "enter_safe_idle",
  "rationale": "Device is over the heat threshold; hold cooling until it recovers."
}
```

**Field notes:**
- `operator`: one of `>=` `<=` `>` `<` `==` `!=`. A closed enum, never parsed from a string.
- `metric` / capability / actuator names must match `^[a-z][a-z0-9_]{1,39}$` before they are used as a lookup key anywhere.
- A condition may also be a one-level group: `{"all_of": [ <condition>, ... ]}` or `{"any_of": [...]}`. Nesting is not permitted — the validator has to decide whether activation and recovery are disjoint, and that stays decidable only while the shape is flat.
- `fallback_behavior`: `enter_safe_idle` | `hold_cooling` | `restore_previous_firmware`.
- `maximum_duration_seconds` is the lease. It is bounded by `MAX_LEASE_SECONDS` at validation and again on the device.
- Canonical serialization (sorted keys, no whitespace, ASCII, no non-finite floats) gives `manifest_hash = sha256(canonical_json)`.

## 11. Capability Registry (read-only)

Separate artifact from the hardware schema, separately versioned
(RESEARCH.md §3). One file per major version at
`registry/capability_registry_v<major>.json`.

```json
{
  "capability_registry_version": "1.0.0",
  "metrics":   { "temperature_c": { "unit": "celsius", "produced_by": "read_temperature" } },
  "actuators": { "fan": { "connected_device": "Relay_Fan", "states": ["on", "off"],
                          "default_state": "off", "safe_state_when_hot": "on" } },
  "capabilities": {
    "fan_on": {
      "kind": "actuator",
      "description": "Request the cooling fan on.",
      "compatible_hardware": ["Relay_Fan"],
      "permitted_pins": [27],
      "input_type": "none",
      "output_type": "actuator_intent",
      "metric": null,
      "actuator": "fan",
      "resource_cost": { "cpu_ms_per_step": 0.2, "memory_kb": 4 },
      "actuator_limits": { "actuator": "fan", "state": "on", "current_ma": 12.0,
                           "max_transitions_per_minute": 12, "min_hold_seconds": 5.0 },
      "template": "actuator_set_state",
      "template_params": { "actuator": "fan", "state": "on" },
      "safety_preconditions": ["pin_present_in_hardware_schema", "pin_not_forbidden",
                               "hardware_type_matches", "actuator_declared_in_manifest",
                               "metrics_available"],
      "requires_metrics": ["temperature_c"],
      "safe_fallback": "enter_safe_idle",
      "is_fallback_safe": true
    }
  }
}
```

- `kind`: `sensor` | `actuator` | `telemetry` | `safety`.
- `template`: `sensor_read` | `actuator_set_state` | `telemetry_emit` | `safe_idle`. The compiler emits only from these.
- `safety_preconditions` is a closed set, each name mapping to a check the validator implements: `pin_present_in_hardware_schema`, `pin_not_forbidden`, `hardware_type_matches`, `actuator_declared_in_manifest`, `metrics_available`.
- A loaded registry additionally carries `content_hash` — the SHA-256 of the file's own bytes, recorded in every compiler report and firmware package.

## 12. Manifest Validation Result (Validator → pipeline)

Same shape as the Guard Rail Result (§5), for the same reason: a rejection is a
structured, storable verdict rather than an opinion.

```json
{
  "manifest_id": "manifest-cooling-1",
  "manifest_hash": "…64 hex…",
  "status": "pass",
  "checks": {
    "manifest_version": "pass", "device_identity": "pass", "registry_version": "pass",
    "known_capabilities": "pass", "sensor_binding": "pass", "actuator_binding": "pass",
    "capability_preconditions": "pass", "pin_permissions": "pass", "lease_bounds": "pass",
    "control_period": "pass", "condition_consistency": "pass", "fallback_reachable": "pass",
    "resource_budget": "pass"
  },
  "errors": [],
  "capability_registry_version": "1.0.0",
  "capability_registry_hash": "…64 hex…"
}
```

## 13. Controller Program (compiled artifact)

What a validated manifest compiles into, and what gets hashed and signed. It is
**data interpreted by an immutable runtime**, not source that is executed
(RESEARCH.md §4).

```json
{
  "program_version": "1.0",
  "pattern": "thermal_cooling_v1",
  "manifest_id": "manifest-cooling-1",
  "manifest_hash": "…64 hex…",
  "event_id": "event-heat-1",
  "device_id": "pi_node_alpha",
  "source_firmware_hash": "a1b2c3d4e5f60718",
  "capability_registry_version": "1.0.0",
  "capability_registry_hash": "…64 hex…",
  "control_period_seconds": 1.0,
  "maximum_duration_seconds": 300,
  "emergency_temp_c": 95.0,
  "sensors": [ { "capability": "read_temperature", "metric": "temperature_c", "pin": 17 } ],
  "rules": [
    { "rule_id": "r_activate", "description": "cool while the activation condition holds",
      "condition": { "metric": "temperature_c", "operator": ">=", "value": 80.0 },
      "emit": [ { "capability": "fan_on", "kind": "actuator", "actuator": "fan",
                  "state": "on", "pin": 27, "current_ma": 12.0,
                  "min_hold_seconds": 5.0, "max_transitions_per_minute": 12 } ] }
  ],
  "fallback": [ { "capability": "enter_safe_idle", "kind": "safety" } ],
  "fallback_behavior": "enter_safe_idle",
  "resource_budget": { "max_cpu_ms_per_step": 5.0, "max_memory_kb": 256,
                       "max_actuator_transitions_per_minute": 12,
                       "estimated_cpu_ms_per_step": 1.2, "estimated_memory_kb": 24 },
  "min_hold_ticks": 5
}
```

- `rules` are ordered and evaluated in order; `condition: null` means every step.
- `emergency_temp_c` records the policy value the artifact was compiled under, for audit. The supervisor enforces its own live value, never this copy.
- `min_hold_ticks` is `ceil(min_hold_seconds / control_period_seconds)` from the registry — the manifest cannot shorten it.
- `artifact_hash = sha256(canonical_json(program))`.

## 13a. Compiler Report

```json
{
  "status": "pass",
  "manifest_id": "manifest-cooling-1",
  "manifest_hash": "…", "artifact_hash": "…",
  "pattern": "thermal_cooling_v1",
  "capability_registry_version": "1.0.0", "capability_registry_hash": "…",
  "capabilities_used": ["emit_heartbeat", "fan_off", "fan_on", "read_temperature"],
  "pins_used": [17, 27],
  "rules": ["r_activate", "r_recover", "r_heartbeat"],
  "errors": [],
  "rendering": "# controller … (text listing, never executed)"
}
```

## 14. Actuator Intent and Intent Decision

The only channel between replaceable firmware and the immutable layer. A
controller returns intents; the supervisor decides what happens (RESEARCH.md §7).

```json
{
  "intent_id": "manifest-cooling-1:42:r_activate:0",
  "manifest_id": "manifest-cooling-1",
  "capability": "fan_on",
  "kind": "actuator",
  "tick": 42,
  "reason": "rule r_activate",
  "actuator": "fan", "state": "on", "pin": 27,
  "event": null, "trigger_type": null,
  "current_ma": 12.0, "value": null
}
```

- `kind`: `actuator` | `telemetry` | `safety`.
- `intent_id` is derived, never random, so two identical runs produce identical ids and a counterexample trace can be diffed.

The supervisor answers with one decision per intent, recorded whether or not it
was accepted — a rejection is the evidence the supervisor did its job:

```json
{ "intent": { "…": "the intent above" }, "accepted": false,
  "reason": "emergency_override: cooling is required at 96.2C" }
```

## 15. Simulation Scenario and World Snapshot

A scenario is the reproducible unit of behavioural evidence: physical
conditions, a tick budget, injected faults, and a seed (RESEARCH.md §5).

```json
{
  "name": "gradual_overheat",
  "description": "Sustained load drives the device past the threshold over ~25 ticks.",
  "ticks": 120,
  "cooling_is_sufficient": true,
  "expects_activation": true,
  "world": {
    "seed": 20260827, "tick_seconds": 1.0, "ambient_c": 25.0, "initial_device_c": 45.0,
    "heat_generation_c_per_s": 2.0, "load": 1.0, "k_ambient": 0.02, "k_fan": 0.15,
    "fan_effectiveness": 1.0, "sensor_noise_c": 0.3, "sensor_fault": "none",
    "sensor_fault_from_tick": 0, "stuck_high_c": 99.0, "stuck_low_c": 40.0,
    "spike_at_tick": null, "spike_c": 0.0, "load_schedule": []
  },
  "faults": {
    "controller_crash_tick": null, "server_offline_from_tick": null,
    "supervisor_restart_at_tick": null, "duplicate_trigger_count": 1
  }
}
```

- `sensor_fault`: `none` | `stuck_high` | `stuck_low`.
- `cooling_is_sufficient: false` marks a scenario where no control policy can hold the device under the critical limit. The verifier does not check the temperature bound there and records why.
- One trace row (`WorldSnapshot`) carries both the truth and the claim, so a sensor fault can be told apart from a control failure:

```json
{ "tick": 29, "time_s": 29.0, "device_temp_c": 80.4, "sensor_temp_c": 80.61,
  "ambient_c": 25.0, "fan_state": "off", "load": 1.0, "sensor_fault": "none" }
```

## 16. Verification Report (Verifier → pipeline)

One scenario, one seed, one verdict (RESEARCH.md §6). A suite is a list of
these plus an overall status; a failed run fails the suite, and a failed suite
never reaches the signer.

```json
{
  "scenario": "gradual_overheat",
  "seed": 20260827,
  "manifest_id": "manifest-cooling-1",
  "artifact_hash": "…64 hex…",
  "status": "pass",
  "properties": [
    { "name": "cooling_latency",
      "description": "cooling is commanded within 2 ticks of the activation threshold, and before the supervisor intervenes",
      "status": "pass",
      "detail": "threshold 80.0C read at tick 29, cooling commanded at 29 (latency 0 ticks)",
      "counterexample_tick": null }
  ],
  "counterexample": [],
  "peak_device_temp_c": 80.5,
  "peak_sensor_temp_c": 80.61,
  "activation_latency_ticks": 0,
  "recovery_time_ticks": 2,
  "actuator_transitions": 8,
  "transitions_per_minute": 4.0,
  "time_above_critical_ticks": 0,
  "resource_use": { "steps": 120, "cpu_ms_total": 132.0, "cpu_ms_per_step": 1.1,
                    "memory_kb": 24, "artifact_bytes": 2274 },
  "supervisor": { "accepted": 128, "rejected": 0, "emergency_activations": 0,
                  "safe_state_entries": 0, "rejections_by_reason": {} },
  "controller_faulted": false,
  "fault_reason": null
}
```

- Property `status` is `pass` | `fail` | `skipped`. **`skipped` is never a pass**: it records a property that could not be decided — a scenario where cooling is physically insufficient, or one that never reaches the threshold — together with the reason.
- Properties checked: `cooling_latency`, `declared_capabilities_only`, `pins_within_schema`, `actuator_envelope`, `supervisor_integrity`, `critical_temperature_bound`, `oscillation_bound`, `finite_lease`, `fallback_reachable`, `control_budget`.
- `counterexample` carries the trace rows (§15) around the first failing tick, so a rejection can be understood without re-running.

## 17. Signed Firmware Package (Server → Device, `manifest_compiler` mode)

Replaces the §6a OTA Push Payload in manifest mode. §6a checked a content hash,
which authenticates nothing; this binds an artifact to a device, an origin, a
point in a sequence and a bounded lease (RESEARCH.md §9).

```json
{
  "package_version": "1.0",
  "device_id": "pi_node_alpha",
  "manifest_hash": "…64 hex…",
  "artifact_hash": "…64 hex…",
  "base_firmware_hash": "a1b2c3d4e5f60718",
  "capability_registry_version": "1.0.0",
  "sequence_number": 5,
  "lease_duration_seconds": 300,
  "issued_at": 1770000000,
  "artifact": { "…": "the Controller Program of §13" },
  "signature": "…64 hex HMAC-SHA256…"
}
```

- `signature` covers the canonical JSON of every field except itself, the full artifact included. Covering only `artifact_hash` would leave the bytes unsigned — the exact gap §6a had.
- `lease_duration_seconds: null` is a durable install (an auto-patch). An integer is a temporary morph the device expires locally.
- Key: HMAC-SHA256 from `CAEF_OTA_HMAC_KEY`. Production needs asymmetric device identities; a shared symmetric key means any holder can mint a package.

The device answers with a verdict, and a rejection is named:

```json
{ "accepted": false, "reason": "sequence 5 is not above the last accepted 5",
  "rejection": "replayed_sequence", "device_id": "pi_node_alpha",
  "artifact_hash": "…", "sequence_number": 5 }
```

- `rejection`: `invalid_signature` | `wrong_device` | `artifact_mismatch` | `manifest_mismatch` | `stale_base_firmware` | `replayed_sequence` | `lease_too_long` | `unsupported_registry` | `malformed_package`.
- Checked in that order — internal consistency, then signature, then identity and freshness, then local policy — so a tampered package is named for what it is rather than reported as a generic signature failure.

## 18. Device State (persisted on the device)

A/B slots, the active lease and the replay watermark, on the device's own disk
rather than only in the server's database (RESEARCH.md §8/§10). Written
atomically: temp file in the same directory, `fsync`, then `os.replace`.

```json
{
  "state_version": 1,
  "device_id": "pi_node_alpha",
  "slots": {
    "A": { "slot": "A", "artifact": { "…": "Controller Program (§13)" },
           "artifact_hash": "…", "manifest_id": "manifest-monitor-1",
           "sequence_number": 0, "installed_at_wall": 1770000000.0, "lease": null },
    "B": { "slot": "B", "artifact": { "…": "…" }, "artifact_hash": "…",
           "manifest_id": "manifest-cooling-1", "sequence_number": 1,
           "installed_at_wall": 1770000123.0,
           "lease": { "duration_seconds": 300, "elapsed_seconds": 42.0,
                      "installed_at_wall": 1770000123.0 } }
  },
  "active_slot": "A",
  "last_known_good_slot": "A",
  "candidate_slot": "B",
  "candidate_status": "probation",
  "probation_ticks_remaining": 3,
  "failure_count": 0,
  "last_accepted_sequence": 1
}
```

- `candidate_status`: `none` | `probation` | `active` | `failed`.
- During probation the **candidate is what runs** while `active_slot` still names what the device would fall back to. Conflating the two is how a device ends up with no known-good left.
- A lease expires on whichever clock has advanced further — persisted `elapsed_seconds` or wall time since `installed_at_wall`. A stopped clock cannot extend a lease and neither can a rewound one. A device whose RTC an attacker controls would need a secure monotonic counter; that is out of scope (RESEARCH.md §14).
- `last_accepted_sequence` moves on **acceptance**, not activation, so a package that failed probation is not replayable.

## 19. Deployment Ledger (server)

Sits alongside the §7 History Table rather than replacing it: the baseline keeps
its ledger and its tests, and this adds the states that ledger cannot express
(RESEARCH.md §11).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID (PK) | |
| `device_id` | string (FKEY → `devices.id`) | |
| `event_id` | UUID (FKEY → `events.id`, nullable) | |
| `mode` | enum | `source_generation` \| `manifest_compiler` — both arms, one table |
| `state` | enum | see below |
| `manifest_id` / `manifest_hash` / `artifact_hash` | string/nullable | manifest mode |
| `capability_registry_version` / `base_firmware_hash` | string/nullable | manifest mode |
| `sequence_number` / `lease_duration_seconds` | int/nullable | manifest mode |
| `patch_id` | UUID (FKEY → `patches.id`, nullable) | source-generation mode |
| `server_received_at` / `updated_at` | timestamp | server clock |
| `device_event_time` | int/nullable | the device's own clock — recorded, never used for ordering |
| `reason` | text/nullable | why the last transition happened |

`state`: `proposed` → `manifest_validated` → `compiled` → `simulation_verified`
→ `signed` → `delivery_attempted` → `accepted_by_device` → `active_on_device`,
with `rejected`, `reverted` and `rolled_back` as terminals.

The legal graph is enforced, and two edges are the point of the whole table:

- `delivery_attempted` **cannot** reach `active_on_device`. A send is not an arrival; the device has to answer first.
- `accepted_by_device` is not `active_on_device`. Probation can still fail and a lease can expire before promotion.

Each change appends a `deployment_transitions` row:

| Column | Type | Notes |
|---|---|---|
| `id` | integer (PK, autoincrement) | **this is what defines order** |
| `deployment_id` | UUID (FKEY → `deployments.id`) | |
| `from_state` / `to_state` | enum | `from_state` is null for the opening row |
| `at` | timestamp | server clock; two rows may share one |
| `device_event_time` | int/nullable | device clock, recorded for correlation only |
| `detail` | text/nullable | |

Ordering comes from the autoincrement id, never from a timestamp and never from
the device's clock: timestamps collide, clocks move backwards, and an untrusted
device timestamp is not an ordering primitive.

## 19a. Demo Trace (`demo/safe_demo.py`)

The machine-readable companion to the printed timeline.

```json
{
  "seed": 20260827, "scenario": "gradual_overheat", "lease_seconds": 40,
  "server_offline_at_tick": 44,
  "moments": [ { "step": 1, "tick": 0, "label": "baseline firmware running",
                 "detail": "…", "device_temp_c": 45.0, "fan_state": "off" } ],
  "ticks": [ { "tick": 29, "device_temp_c": 80.47, "sensor_temp_c": 81.21,
               "fan_state": "off", "supervisor_state": "normal",
               "emergency_active": false, "running_slot": "A",
               "running_manifest": "baseline-monitor", "lease_remaining_s": null,
               "events": [], "intents": ["telemetry:HEARTBEAT"] } ],
  "manifest": { "…": "§10" },
  "compiler_report": { "…": "§13a" },
  "verification": { "…": "a suite of §16 reports" },
  "package": { "…": "§17" },
  "ledger_transitions": [ { "id": 1, "from": null, "to": "proposed", "detail": "…" } ],
  "outcome": { "running_slot": "A", "running_manifest": "baseline-monitor",
               "active_slot": "A", "last_known_good_slot": "B",
               "last_accepted_sequence": 1, "final_device_temp_c": 68.17,
               "peak_device_temp_c": 81.36, "supervisor_rejections": 0,
               "emergency_activations": 0, "ledger_state": "reverted" }
}
```

## 20. Naming Consistency Rule

Field and event names in code must match this document exactly
(`trigger_type`, `CONTEXT_TRIGGER`, `CRITICAL_FAILURE`, `current_state_hash`,
`fw_hash`, `patch_id`, `event_id`, `poll_id`, `device_id`). If a future change
renames one of these, update this file in the same commit.
The `manifest_compiler` shapes (§10–§17) extend the same rule: `manifest_id`,
`manifest_hash`, `artifact_hash`, `capability_registry_version`,
`sequence_number`, `lease_duration_seconds` and the `ActuatorIntent` field names
are the canonical spellings.
