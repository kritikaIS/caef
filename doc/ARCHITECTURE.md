# ARCHITECTURE.md
## CAEF Pipeline — Component & Message-Flow Reference

This document exists to preserve the architecture captured in the original
whiteboard/Excalidraw diagram, in text form, so it survives without the image.
Read alongside `TDD.md` (implementation detail) and `LOOPS.md` (control-flow
detail).

**Scope note.** Everything below describes `ADAPTATION_MODE=source_generation`,
the v0.1 pipeline, which is preserved unchanged as the experimental control. The
contract-constrained pipeline (`ADAPTATION_MODE=manifest_compiler`) adds
components this map does not contain — a manifest validator, a deterministic
compiler, a closed-loop verifier, a signed-package path and an immutable
on-device supervisor — and is specified in `RESEARCH.md`. The component
vocabulary in §3 still applies to both: `Event`, `Task`, `History Table` and
`FKEY` mean the same things in either mode.

---

## 1. Component Map

```
                    ┌────────────┐
                    │  Frontend  │◄──────────────┐
                    └─────▲──────┘                │
                          │ Event                  │
┌─────────┐   tcp(full   │                         │
│   Pi    │   duplex)  ┌─┴────────┐   Task    ┌────┴─────┐
│ (Device,│◄──────────►│ Listener │──────────►│Distributor│
│ source  │  Poll id/  └──────────┘  Event     │(SQS|SNS) │
│of time) │  firmware /                         └────┬─────┘
└────┬────┘  Polled Poll(id)                          │ Task
     │                                                  ▼
     │  new fw / new firmware                    ┌──────────┐
     │◄───────────────────────────────────────── │LLM Agent │
     │        Soft Firmware / Soft Firmware 2     └────┬─────┘
     │                                                  │ schema, docs,
     │                                                  │ history (RAG)
     │                                            ┌─────▼──────┐
     │                                            │  DB / RAG  │
     │                                            │ - History  │
     │                                            │   Table    │
     │                                            │ - Docs of  │
     │                                            │   Node     │
     │                                            │ - Docs on  │
     │                                            │   comp.    │
     │                                            │ - schema   │
     │                                            └────────────┘
     │
     │            Guard rail ──► Test (Sandbox) ──pass──► new firmware
     │                               │
     │                               └─FAIL(Results, ΔFirmware)─► back to LLM Agent
```

## 2. Component Responsibilities

| Component | Role |
|---|---|
| **Pi (Device)** | Runs current firmware; is the *source of time* (its own event timestamps are authoritative); polls its own assigned firmware id as a reconciliation channel; exchanges telemetry/OTA over a full-duplex TCP channel with the Listener. |
| **Listener** | Terminates the device connection; classifies inbound signals; forwards work to the Distributor. |
| **Distributor** | Queue/topic layer modeled on SQS (work queue, at-least-once) + SNS (fan-out topic). Decouples device traffic spikes from Agent throughput. Emits `Task` to the Agent and `Event` to the Frontend/DB. |
| **LLM Agent** | Central reasoning component. Pulls from DB/RAG (schema, node docs, component docs, history), plans, generates code. |
| **DB / RAG store** | Four logical stores, may be co-located physically in v0.1: History Table (append-only patch ledger, FKEY-linked), Docs of Node (per-device schema/context), Docs on comp. (component/driver library docs), schema (canonical read-only hardware schema). |
| **Guard rail** | Deterministic pre-flight check on Agent output before any code executes. |
| **Test (Sandbox)** | Executes candidate firmware in isolation; returns pass, or `FAIL (Results, ΔFirmware)` back to the Agent. |
| **Deployment path** | On pass: produces "new firmware" → staged as "Soft Firmware" → "Soft Firmware 2" → pushed to the device as "new fw". |
| **Frontend** | Operator view fed by the Distributor's Event topic. |

## 3. Message/Artifact Vocabulary

This is the canonical vocabulary Claude Code should use consistently across
code, logs, and DB column names — do not invent synonyms mid-implementation.

| Term | Meaning |
|---|---|
| `Event` | A classified telemetry occurrence from a device (`CONTEXT_TRIGGER` or `CRITICAL_FAILURE`), fanned out for observability. |
| `Task` | The work item handed to the Agent to act on an `Event`. |
| `Poll id / firmware` | The device's periodic request "what firmware am I supposed to be running?" |
| `Polled Poll(id)` | The server's response to a poll — the currently-assigned firmware id/hash for that device. |
| `Guard rail` | The deterministic static-safety gate between Agent output and Sandbox. |
| `Test` | The Sandbox execution step. |
| `FAIL (Results, ΔFirmware)` | Sandbox failure payload returned to the Agent: what happened, and the delta between attempted and last-good firmware. |
| `new firmware` / `new fw` | Sandbox-passed candidate, on its way to the device. |
| `Soft Firmware` / `Soft Firmware 2` | The two staging checkpoints between "sandbox passed" and "safe to flash to a physical device" — see TDD.md §2.7 for the v0.1 concrete definition of each stage. |
| `History Table` | Append-only ledger: `Time \| Poll id \| Event id \| Patch id \| FW` with `FKEY` relations to device/event/patch. |
| `FKEY` | Foreign-key relationship linking a History Table row back to its originating `Device`, `Event`, and `Patch` records — required for audit traceability (PRD G5 / TRD NFR-5). |

## 4. Design Principles Encoded in the Architecture

1. **The Agent never talks to the device directly.** All device-bound artifacts pass through Guard Rail → Sandbox → staged rollout before reaching hardware.
2. **The device is the clock.** Time-of-event is device-authoritative, not server-authoritative, so causal ordering of on-device symptoms is preserved even under network jitter.
3. **Push and poll are both first-class.** OTA push is the fast path; polling is the reconciliation path for missed/lost pushes — the device should never be permanently stuck on stale firmware just because one push was dropped.
4. **Every artifact is traceable.** The `FKEY`-linked History Table exists specifically so any deployed firmware can be traced back to the plan and event that produced it (patent/audit requirement).
5. **Failure has a floor.** The `FAIL → retry` loop is bounded; it always terminates in either a successful deploy or a Safety Rollback — never an infinite retry loop (see SAFETY_PROTOCOL.md).
