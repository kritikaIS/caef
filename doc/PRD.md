# Product Requirements Document (PRD)
## Context-Aware Evolutionary Firmware (CAEF) Pipeline

**Status:** Draft v0.1
**Audience:** Engineering (Claude Code build agent), research/patent reviewers

---

## 1. Problem Statement

Embedded devices (MCUs / edge nodes) today ship with static firmware sized for
the worst-case union of every feature they might ever need. This wastes
storage/RAM, increases attack surface, and means a device can't adapt its
running logic to its *current physical context* (which sensors are attached,
what the environment is doing right now) without a manual firmware release
cycle.

CAEF proposes a system where an edge device can request live, targeted code
changes from a cloud-based LLM agent, which:

- Reasons only over the device's **actual wired hardware** (via a read-only
  hardware schema — never hallucinated pin assignments),
- Generates the **minimum necessary code** for the current situation
  (Situational Morphing) or a **fix for a crash** (Auto-Patching),
- Verifies the generated code in an isolated sandbox before it ever reaches
  physical hardware,
- Pushes it over-the-air (OTA), and
- Automatically rolls back to a known-good state if the new code fails.

## 2. Goals

| # | Goal | Success Signal |
|---|------|-----------------|
| G1 | Device can signal a context change and receive working, situation-specific firmware within a bounded time window | Demo: heat event → fan-only firmware deployed → reverted after cool-down |
| G2 | Device can signal a crash (stack trace) and receive a patch without human intervention | Demo: induced `IndexError` → patched line deployed → device stable |
| G3 | Generated code can never violate the physical hardware contract | Zero forbidden-pin writes reach the device across the test corpus |
| G4 | System is safe-by-default | 3 consecutive failures on a device auto-triggers rollback to last known-good firmware |
| G5 | Every code change is fully auditable | Every deployed patch traceable via `Time \| Poll ID \| Event ID \| Patch ID \| FW hash` record |
| G6 | System is demonstrable on commodity hardware for research/patent purposes | Runs on Raspberry Pi simulating an MCU, reproducible from this repo |

## 3. Non-Goals (v0.1)

- Production-grade OTA security (secure boot, code signing) — stubbed, not hardened.
- Multi-tenant / multi-organization device fleets.
- Real MCUs (ESP32/STM32) — Raspberry Pi simulation only. C++ runtime is a stretch goal.
- A polished end-user frontend — a minimal operator dashboard is sufficient.
- Formal certification (UL, CE, FCC) — out of scope for a research prototype.

## 4. Actors

1. **Edge Device** — autonomous actor, no human operator in the loop during normal operation.
2. **Operator** — human who monitors the Frontend, reviews history, and can force a rollback.
3. **Agent (LLM)** — the reasoning component; restricted to tool-mediated, schema-checked actions.

## 5. Functional Requirements

### 5.1 Edge Node
- FR-1: Device runs a main control loop and a lightweight telemetry client.
- FR-2: Device emits `CONTEXT_TRIGGER` telemetry when a monitored sensor value crosses a configured threshold.
- FR-3: Device emits `CRITICAL_FAILURE` telemetry with a stack trace on unhandled exception, then holds a safe state pending patch.
- FR-4: Device exposes a Watchdog that listens for inbound OTA payloads and performs a safe restart into new firmware.
- FR-5: Device polls for its firmware assignment (`Poll id / firmware`) as a fallback/confirmation channel alongside push-based OTA.
- FR-6: Device is the authoritative **source of time** for its own event stream (timestamps originate at the edge).

### 5.2 Telemetry Gateway / Listener
- FR-7: Listener accepts device connections over a full-duplex TCP channel (MQTT as a reliability-oriented alternative) and classifies inbound events by `trigger_type`.
- FR-8: Listener forwards classified events to the Distributor rather than invoking the Agent synchronously.

### 5.3 Distributor
- FR-9: Distributor is queue-backed (SQS/SNS-style: one queue for work distribution, one topic for fan-out) to decouple ingestion rate from Agent processing rate.
- FR-10: Distributor routes `Task` messages to the Agent and `Event` notifications to the Frontend/History Table concurrently.

### 5.4 Agentic Core (LLM Agent)
- FR-11: Agent retrieves, per request: the device's hardware schema (read-only), the current connection/firmware schema, relevant driver snippets, and historical failure/patch records, via RAG over a document/history store.
- FR-12: Agent is restricted to tool-mediated hardware access (e.g. `check_hardware_schema(pin)`) — it must not free-hand pin numbers into generated code without a validated tool call.
- FR-13: Agent produces a short natural-language plan before generating code (auditable reasoning trace).
- FR-14: Agent generates a complete replacement or targeted patch for the target script so the Sandbox can validate the whole runtime unit.
- FR-15: Agent output passes a **Guard Rail** check (schema conformance, forbidden-pin check, static safety checks) before reaching the Sandbox.

### 5.5 Verification Sandbox
- FR-16: Sandbox executes candidate code in a container mirroring the device OS for a bounded duration (default 10s) and captures pass/fail plus resource usage.
- FR-17: On failure, Sandbox returns `FAIL (Results, ΔFirmware)` to the Agent for at most N retry attempts (default 3) before the request is marked failed and escalated.
- FR-18: On success, Sandbox output ("new firmware") is promoted through a staged rollout artifact ("Soft Firmware" → "Soft Firmware 2") before final device deployment.

### 5.6 Deployment / Rollback
- FR-19: Deployment writes new firmware to the device via OTA push and records `Time | Poll ID | Event ID | Patch ID | FW hash` in the History Table, foreign-key linked (`FKEY`) back to the originating event and device.
- FR-20: For Situational Morphing, the system schedules an automatic reversion to the prior firmware once the triggering condition clears or a configurable timeout elapses (default 5 minutes) — see OQ-1.
- FR-21: **Safety Rollback Protocol**: if a device accumulates 3 consecutive failures tied to one event chain, the system halts autonomous generation and redeploys the last known-good firmware from the A/B partition.

### 5.7 Frontend / Observability
- FR-22: Operator-facing Frontend displays live device state, event stream, current firmware version/hash, and per-device patch history.
- FR-23: Frontend allows an operator to manually trigger rollback for a given device.

## 6. Key Scenarios (Acceptance Criteria)

### Scenario A — Situational Morphing (Heat Event)
1. Device temp reading exceeds threshold (>80°C).
2. Device sends `CONTEXT_TRIGGER` / `HIGH_HEAT_DETECTED`.
3. Agent retrieves schema, identifies `Relay_Fan` on `GPIO_27` (dormant), plans: enable fan, strip nonessential code (e.g. Lidar driver) to free CPU.
4. Sandbox validates generated script.
5. Device receives and boots minimal cooling firmware.
6. After the configured window (see OQ-1), original firmware is redeployed.
7. Event recorded with an FKEY-linked history entry.

### Scenario B — Auto-Patching (Crash)
1. Device raises `IndexError` at `buffer[i]`, `i == 10`, buffer size 10.
2. Device sends `CRITICAL_FAILURE` with stack trace.
3. Agent identifies the off-by-one, proposes `buffer[i-1]` or an adjusted loop bound.
4. Sandbox validates.
5. Patch deployed; device resumes normal operation; event recorded.

### Scenario C — Repeated Failure → Rollback
1. A device fails 3 times in a row for a given event chain.
2. System halts autonomous generation and redeploys last known-good firmware from the A/B partition.
3. Operator is notified via Frontend.

## 7. Open Questions

- **OQ-1**: Is Situational Morphing reversion time-based (fixed 5 min), condition-based (temp < 60°C), or whichever comes first? TDD picks a default; make the other configurable.
- **OQ-2**: Does the 3-strikes counter include Sandbox rejections, on-device crash-loops, or both?
- **OQ-3**: Retention policy for the History Table / RAG document store (unbounded vs. rolling window)?
- **OQ-4**: Auth between Device↔Listener and Agent↔Sandbox is out of scope for v0.1 but must be flagged as a known gap.

## 8. Success Metrics (Research/Patent Framing)

- Firmware footprint reduction vs. a static "kitchen sink" build.
- Time-to-remediation for an induced crash (seconds from `CRITICAL_FAILURE` to verified deployed patch).
- Zero forbidden-pin or out-of-schema hardware writes across the full test corpus (hard safety requirement, not just a metric).
