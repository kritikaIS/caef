# LOOPS.md
## CAEF Pipeline — Control Loops Specification

This document enumerates every recurring loop in the system so each one has
an unambiguous owner, trigger, exit condition, and failure mode. Claude Code
should implement each loop as an explicit, testable unit — not as implicit
control flow buried inside a larger function.

---

## 1. Device Sensor Loop (Edge Node)

- **Owner:** `edge_node/main.py`
- **Trigger:** device boot.
- **Cadence:** fixed interval (e.g. 1s tick, per source spec's simulation).
- **Body:** read sensor(s) → compare to threshold(s) → if crossed, emit `CONTEXT_TRIGGER` and pause per-loop work (source spec: `sleep(20)` while awaiting OTA) → continue.
- **Exit condition:** never exits during normal operation; only stops on process crash (→ triggers the Crash Loop below) or explicit shutdown.
- **Failure mode:** unhandled exception inside the loop → caught by the top-level `try/except` → emits `CRITICAL_FAILURE` with the stack trace → process should hold in a safe idle state (not busy-crash-loop) awaiting a patch or rollback.

## 2. Situational Morphing Loop (Cloud)

- **Owner:** Agent + Deploy component, coordinated via the Distributor.
- **Trigger:** a `CONTEXT_TRIGGER` `Task`.
- **Steps:**
  1. Retrieve schema + current context + drivers + history (RAG).
  2. Plan (natural language, stored for audit).
  3. Generate minimal firmware for the situation (may *remove* code, e.g. Lidar driver, to free resources — this is explicitly in scope, not just additive changes).
  4. Guard Rail → Sandbox.
  5. On pass: stage as Soft Firmware → Soft Firmware 2 → OTA push → History Table write.
  6. **Schedule a reversion job** (see §2a below).
  7. On fail: return to step 3 with `ΔFirmware` results, bounded by `MAX_RETRIES` (default 3).
- **Exit condition:** either a successful deploy + scheduled reversion, or `MAX_RETRIES` exceeded → hand off to Safety Rollback Loop (§4).

### 2a. Reversion Sub-Loop
- **Owner:** a scheduled job created at deploy time (e.g. `asyncio` timer, cron-like job, or a durable scheduled task if using a real queue backend).
- **Trigger condition (pick ONE default, keep the other as a config flag per PRD OQ-1):**
  - **Default for v0.1: time-based.** Fixed window (`REVERSION_WINDOW_SECONDS`, default 300s / 5 min) from the source spec ("reflash the older version in 5 mins").
  - **Optional/config: condition-based.** Poll device telemetry; revert once the triggering metric returns below its recovery threshold (e.g. temp < 60°C per the source spec's fan example).
  - **Combined mode (if enabled):** revert on whichever condition is met first.
- **Body:** on trigger, redeploy the last known-good firmware hash (pre-morph) recorded in the History Table for this device; write a new History Table row for the reversion itself (it is its own auditable "patch" — the patch content being "restore hash X").
- **Exit condition:** reversion deploy confirmed via device poll reconciliation (§3).
- **Failure mode:** if reversion deploy itself fails Sandbox/Guard Rail (should be rare since it's a known-good artifact, but still passes through the same path for consistency) → escalate directly to Safety Rollback Loop rather than retrying morph logic again.

## 3. Poll / Reconciliation Loop

- **Owner:** Device (`edge_node/watchdog.py`) initiates; server (`deploy/`) responds.
- **Trigger:** fixed interval, independent of OTA push events (e.g. every 30–60s).
- **Body:** device sends `Poll id`; server responds with `Polled Poll(id)` = the currently-assigned firmware hash for that device.
- **Exit condition:** N/A — runs for the lifetime of the device.
- **Purpose:** catches any missed/dropped OTA push (network blip, device offline during push) — if the device's running hash doesn't match the assigned hash, the device re-requests the firmware file directly.
- **Failure mode:** if reconciliation itself repeatedly fails (device can't reach server), that is an infra/connectivity issue outside the Safety Rollback Protocol's scope — log and alert, do not attempt local auto-remediation on the device.

## 4. Auto-Patching Loop (Crash Path)

- **Owner:** Agent + Deploy, coordinated via the Distributor.
- **Trigger:** a `CRITICAL_FAILURE` `Task` (stack trace).
- **Steps:**
  1. Retrieve current source of the failing file + relevant history of similar prior crashes/patches.
  2. Plan: identify root cause from the stack trace (e.g. off-by-one).
  3. Generate a targeted fix (full-file replacement per FR-14, even though the change itself is small).
  4. Guard Rail → Sandbox.
  5. On pass: stage → deploy → History Table write. **No reversion job is scheduled** for Auto-Patching (the fix is meant to be durable, unlike a situational morph) — this distinguishes it from the Situational Morphing Loop.
  6. On fail: retry with `ΔFirmware` results, bounded by `MAX_RETRIES`.
- **Exit condition:** successful durable patch deployed, or `MAX_RETRIES` exceeded → Safety Rollback Loop.

## 5. Safety Rollback Loop (3-Strikes Protocol)

- **Owner:** Deploy/rollback component; must function without any LLM call (NFR-4).
- **Trigger:** any of:
  - Agent/Sandbox retry budget exhausted for one event chain (from Loop 2 or Loop 4), **or**
  - 3 consecutive on-device crash reports tied to firmware deployed within the same event chain (config decision — see PRD OQ-2; default: count *both* Sandbox-exhaustion and on-device crash-loop toward the same 3-strike counter, scoped per `device_id`).
- **Body:** halt all further autonomous generation attempts for this device/event chain → look up last known-good firmware hash from the A/B partition record in the History Table → redeploy it directly (bypassing Agent/Guard Rail/Sandbox entirely, since it is by definition already-verified) → write a History Table row marking this as a `rollback` patch type → notify Frontend/operator.
- **Exit condition:** device confirms (via poll reconciliation) it is running the rolled-back hash.
- **Failure mode:** if even the rollback deploy fails to reconcile (device unreachable, etc.), this becomes an operator-escalation case — outside automated remediation scope for v0.1.

## 6. Distributor Drain Loop

- **Owner:** Distributor.
- **Trigger:** continuous background process.
- **Body:** dequeue `Task`s, dispatch to Agent worker pool with per-device serialization (§ TRD 2.3); publish `Event`s to Frontend/DB subscribers.
- **Exit condition:** N/A, always running.
- **Failure mode:** if the Agent worker pool is saturated/unavailable, Tasks queue up (NFR-6: devices keep running last-good firmware while queued — this is safe by construction, not a degraded-but-unsafe state).

## 7. Loop Interaction Summary

```
Sensor Loop (device) ──CONTEXT_TRIGGER──► Situational Morphing Loop ──deploy──► Reversion Sub-Loop
Sensor Loop (device) ──CRITICAL_FAILURE─► Auto-Patching Loop ──deploy──► (no reversion)
                                            │                              │
                                     (retry cap exceeded)          (crash-loop detected)
                                            └──────────► Safety Rollback Loop ◄──────────┘
Poll/Reconciliation Loop runs independently, underneath all of the above, as the safety net for missed pushes.
```
