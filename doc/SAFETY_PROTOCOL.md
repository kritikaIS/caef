# SAFETY_PROTOCOL.md
## CAEF Pipeline — Safety Rollback & Guard Rail Specification

This document is the single source of truth for every safety mechanism in
the system. If any other doc conflicts with this one on a safety behavior,
this document wins. Claude Code should treat every requirement below as a
hard constraint, not a suggestion — these are the claims the project's
patentability rests on (PRD §8, TDD implementation should make them testable).

---

## 1. Defense-in-Depth Layers

| Layer | What it stops | Can it be bypassed? |
|---|---|---|
| 1. Read-only hardware schema | Agent cannot rewrite physical reality even if it wanted to | No — enforced at filesystem/tool permission level, not by prompt instruction alone |
| 2. Tool-mediated pin access (`check_hardware_schema`) | Agent hallucinating a pin/connection that doesn't exist | No — Guard Rail cross-checks every pin literal in generated code against a logged tool call |
| 3. Guard Rail (static, deterministic, pre-execution) | Forbidden-pin writes, disallowed operations, schema drift | No — runs unconditionally before Sandbox, no "trusted" fast path skips it |
| 4. Sandbox (dynamic, isolated, time-boxed) | Crashes, hangs, resource exhaustion | No — even Guard-Rail-passed code must still pass Sandbox |
| 5. Staged rollout (Soft Firmware → Soft Firmware 2) | A false-positive Sandbox pass reaching a physical device without a second checkpoint | Configurable strictness, but v0.1 default keeps both stages |
| 6. A/B partition + Safety Rollback Protocol | A patch that passes every prior layer but still misbehaves on real hardware | This is the last line of defense — it must work even if every layer above fails |

## 2. Guard Rail — Exact Rules

Guard Rail runs after Agent generation and before any code executes anywhere
(including Sandbox). It is deterministic: same input always produces the same
verdict, no model call inside Guard Rail itself.

1. **Forbidden pin check**: reject if any pin in `schema.constraints.forbidden_pins`, or any pin whose schema entry has `status: "forbidden"`, appears in the generated code (string/AST scan for `GPIO_<n>` patterns, cross-checked against actual usage, not just text mentions in comments).
2. **Tool-call provenance check**: reject if any `GPIO_<n>` referenced in the code lacks a corresponding successful `check_hardware_schema(n)` tool call in the Agent's trace for this `patch_id`.
3. **Schema conformance check**: reject if the code assumes a device/protocol not present in the schema for the pin it targets (e.g. writing I2C setup code to a pin schema-declared as `digital_1wire`).
4. **Static safety denylist**: reject on disallowed constructs in generated firmware code (config-driven denylist — starting set: arbitrary `subprocess`, `eval`/`exec`, unbounded `while True` with no yield/sleep, raw socket servers beyond the defined telemetry client).
5. **Current-draw sanity check** (best-effort, v0.1): if `max_gpio_current` constraints are violated by an obviously-declared actuator config in the generated code, reject. (This is best-effort static analysis, not a substitute for real hardware protection — document this limitation, don't oversell it.)

Any single failed check is sufficient for rejection. Guard Rail returns a
structured `reason` (see DATA_SCHEMAS.md §5); this is fed back to the Agent
identically to a Sandbox failure and counts toward the same retry budget.

## 3. Sandbox — Exact Rules

1. Runs in a container that mirrors the device OS/runtime version.
2. Time-boxed: default 10 seconds (`SANDBOX_TIMEOUT_SECONDS`), configurable.
3. Resource-limited: CPU and memory caps set at container level, not left to defaults.
4. Verdict is `pass` only if: process starts cleanly, does not raise an unhandled exception within the window, and does not exceed resource limits.
5. On `fail`, returns `{results, delta_firmware}` (DATA_SCHEMAS.md §6) — never silently discards the failure detail, since it's needed both for the Agent's next attempt and for the audit trail.

## 4. Retry Budget

- `MAX_RETRIES = 3` (config default, applies uniformly whether failures come from Guard Rail or Sandbox — both count toward the same budget for a given `event_id`).
- On exceeding the budget: the event is marked `escalated`; no further Agent generation attempts are made for that event chain; control passes directly to the Safety Rollback Protocol (§5).
- The retry budget is **per event**, not per device — a device is not "locked out" globally because one event chain failed; a new, unrelated event can still trigger its own fresh attempt (with its own budget), subject to the crash-loop counter in §5 if failures start compounding.

## 5. Safety Rollback Protocol (3-Strikes)

**Trigger conditions (both count toward the same per-device strike counter,
per PRD OQ-2 resolution — document any change to this decision here):**
- A retry-budget exhaustion (§4) for an event chain, **or**
- A device-reported crash within a bounded window (default 10 minutes) of a patch/morph deploy tied to that event chain.

**On the 3rd strike for a given device:**
1. Halt all further autonomous generation for that device until an operator clears the strike counter (do not silently auto-resume).
2. Look up the last `HistoryRecord` with `status: "deployed"` and `record_type` in (`patch_deploy`, `reversion`, `rollback`) that is known-good (i.e., not itself a strike-triggering deploy) for this device.
3. Redeploy that artifact directly from the A/B partition record — **bypassing Agent, Guard Rail, and Sandbox entirely**, since it is by definition already-verified. This is the one deploy path in the system with no LLM in the loop, by design (NFR-4).
4. Write a `HistoryRecord` with `record_type: "rollback"`.
5. Notify the Frontend/operator; require explicit operator acknowledgment before the strike counter resets for that device.

**Why rollback must not depend on the LLM:** if the LLM/Agent is itself
degraded, unavailable, or the source of the repeated failures, a safety path
that still calls it is not a safety path. The rollback lookup and redeploy
must be pure deterministic logic against the History Table.

## 6. A/B Partition Model

- Each device maintains two firmware slots: `active` and `inactive`.
- Every successful deploy writes to `inactive`, then flips the device to boot from it (making it the new `active`) only after the device confirms a successful boot/heartbeat.
- The previous `active` slot becomes the new `inactive` slot — it is **not** immediately overwritten, so "last known-good" always has a physical, immediately-flashable artifact available for the Safety Rollback Protocol, with no regeneration required.
- The Safety Rollback Protocol's redeploy is, in the common case, simply "boot from the inactive slot" rather than a full OTA re-push, when the inactive slot is already the known-good version — this is the fast, reliable path and should be preferred over re-pushing bytes from the server when possible.

## 7. Operator Override

- The Frontend's manual rollback control (PRD FR-23) invokes the exact same code path as the automated Safety Rollback Protocol (§5, steps 2–4) — there must be only one rollback implementation, manually or automatically triggered, not two divergent ones.

## 8. Explicit Non-Goals of This Protocol (v0.1)

- Does not protect against a malicious operator with legitimate access.
- Does not include cryptographic code-signing (flagged as a known gap, see TRD §6).
- Does not attempt automatic root-cause analysis of *why* three strikes occurred beyond what's already in the History Table logs — that is left for operator review.
