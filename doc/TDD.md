# Technical Design Document (TDD)
## CAEF Pipeline — v0.1 Implementation Design

This is the authoritative "how to build it" document. Claude Code should treat
this as the primary implementation blueprint, cross-referenced with
`DATA_SCHEMAS.md`, `LOOPS.md`, and `SAFETY_PROTOCOL.md`.

---

## 1. Repository Layout

```
caef-pipeline/
├── CLAUDE.md
├── docs/
│   ├── PRD.md
│   ├── TRD.md
│   ├── TDD.md
│   ├── ARCHITECTURE.md
│   ├── LOOPS.md
│   ├── DATA_SCHEMAS.md
│   └── SAFETY_PROTOCOL.md
├── edge_node/                 # runs on the Pi (or locally, simulating the Pi)
│   ├── main.py                 # the "dumb device" loop (telemetry client + watchdog)
│   ├── watchdog.py              # listens for OTA, performs safe restart
│   └── config.py
├── server/
│   ├── listener/                # Telemetry Gateway
│   │   └── listener.py
│   ├── distributor/              # queue abstraction (Task queue + Event topic)
│   │   └── distributor.py
│   ├── agent/                     # Agentic Core
│   │   ├── agent.py
│   │   ├── tools.py                # check_hardware_schema, etc.
│   │   ├── prompts.py
│   │   └── rag/
│   │       ├── retriever.py
│   │       └── indexer.py
│   ├── guardrail/
│   │   └── guardrail.py
│   ├── sandbox/
│   │   ├── sandbox_runner.py
│   │   └── Dockerfile.sandbox
│   ├── deploy/
│   │   └── deployer.py            # OTA push + staged rollout + rollback
│   └── db/
│       ├── models.py               # History Table, FKEY relations
│       └── migrations/
├── frontend/
│   └── app.py                      # minimal operator dashboard
├── schemas/
│   └── schema_dev01.json           # example device hardware schema (read-only to Agent)
├── tests/
│   ├── test_guardrail.py
│   ├── test_sandbox.py
│   ├── test_agent_planning.py
│   └── test_e2e_scenarios.py
└── docker-compose.yml               # brings up listener, distributor, agent, sandbox, db, frontend
```

## 2. Component Design

### 2.1 Edge Node (`edge_node/`)
- `main.py` mirrors the source spec's Step-1 client script: sensor read loop, threshold check, UDP telemetry send on `CONTEXT_TRIGGER`/`CRITICAL_FAILURE`.
- `watchdog.py` runs as a second thread/process: listens on a TCP port for inbound OTA pushes, writes the new file, and performs a controlled restart (`os.execv` or subprocess replace) — never a raw `exit()` that could strand the device without a supervisor.
- Device timestamps every outbound event locally (FR-6) — do not rely on server-assigned time for the record's `time` field; server may additionally stamp receipt time for latency metrics.
- Device polling: a lightweight interval loop (`GET /poll?id=<device_id>`) confirms the currently-assigned firmware hash, used to reconcile missed OTA pushes.

### 2.2 Listener (`server/listener/`)
- Binds a UDP socket (heartbeats/simple triggers) and a TCP socket (full-duplex, for triggers that expect an eventual ack/response — e.g. confirming a patch was received).
- On receipt, validates JSON shape against the Telemetry Payload schema, stamps `received_at`, and pushes to the Distributor. Malformed payloads are logged and dropped, not forwarded.

### 2.3 Distributor (`server/distributor/`)
- v0.1 default: an in-process/local implementation (e.g. `asyncio.Queue` per device, or Redis lists if Redis is already in the stack) that mimics an SQS work queue + SNS fan-out topic, so the design maps 1:1 onto real AWS primitives later without a rewrite.
- Two logical channels:
  - **Task queue**: one consumer group — the Agent. At-least-once delivery.
  - **Event topic**: fan-out to (a) DB writer (History/Event log) and (b) Frontend live feed.
- Serializes tasks **per device_id** (a single in-flight generation per device at a time) to avoid two conflicting patches racing for the same target file.

### 2.4 Agentic Core (`server/agent/`)
- Built on LangChain; model is pluggable but defaults to a Claude or GPT-4-class model per the source spec (config value, not hardcoded).
- **Retrieval** (`rag/retriever.py`) pulls, per event:
  1. Device hardware schema (`schema_dev01.json`) — read-only.
  2. Current connection/context schema (what's actually running now).
  3. Relevant driver snippets (Sensor Driver Library).
  4. Similar historical failures + their successful patches (History Table + vector similarity).
- **Planning**: Agent must emit a short natural-language plan (see prompt pattern in `prompts.py`) before code — this plan is stored verbatim for audit (NFR-5).
- **Tools** (`tools.py`): `check_hardware_schema(pin_number)` is the only sanctioned way the Agent learns whether a pin is safe/what's attached. The Agent's system prompt instructs it to never reference a GPIO pin in generated code without a preceding successful tool call for that pin in the same turn.
- **Generation**: Agent emits full-file replacement code (not a diff) for the target script, per FR-14, so Sandbox validates a coherent whole.
- **Retry loop**: on Sandbox `fail`, the Agent receives `{results, ΔFirmware}` and retries, capped at `MAX_RETRIES = 3` (config). Exceeding the cap marks the event `escalated` and hands off to Safety Rollback (`SAFETY_PROTOCOL.md`).

### 2.5 Guard Rail (`server/guardrail/`)
- Pure static checker, no LLM call, deterministic, fast. Runs after generation, before Sandbox.
- Checks:
  1. Every `GPIO_<n>` literal appearing in generated code must correspond to a pin that (a) exists in the schema and (b) was validated via a tool call in the Agent's trace for this request.
  2. No pin in `schema.constraints.forbidden_pins` appears anywhere in the generated code.
  3. Basic static safety: no dangerous imports/calls disallowed for edge firmware (e.g. no arbitrary `subprocess`/`eval` in generated code — config-driven denylist).
- On rejection, returns a structured reason; this is treated by the Agent exactly like a Sandbox failure (counts toward the same retry budget) — see LOOPS.md.

### 2.6 Verification Sandbox (`server/sandbox/`)
- Docker container mirrors the Pi's OS/Python version.
- Runs the candidate script for `SANDBOX_TIMEOUT_SECONDS` (default 10), enforces CPU/mem limits, captures stdout/stderr/exit code.
- Verdict: `pass` if the process starts, doesn't crash within the window, and (if applicable) hits a defined "healthy" heartbeat marker; `fail` otherwise, with logs returned as `ΔFirmware` results for the Agent's next attempt.

### 2.7 Deploy / Staged Rollout (`server/deploy/`)
- On Sandbox `pass`, artifact is written to a **staging slot** ("Soft Firmware"). A second confirmation pass (idempotency / re-run check, or simply a promotion gate) produces "Soft Firmware 2" before the artifact is marked deployable — this two-stage naming from the source diagram is implemented in v0.1 as: stage 1 = sandbox-passed artifact, stage 2 = artifact confirmed written to the A/B inactive partition record, ready for push.
- OTA push writes the artifact to the device's inactive A/B partition reference and signals the device to switch/restart.
- Writes a `History Table` row: `time, poll_id, event_id, patch_id, fw_hash` with `FKEY → device_id`, `FKEY → event_id`.
- For Situational Morphing events, schedules a reversion job (see LOOPS.md §2) that redeploys the prior firmware hash.

### 2.8 DB (`server/db/`)
- `models.py` defines: `Device`, `Event`, `Patch`, `HistoryRecord` (FKEYs: `HistoryRecord.device_id → Device.id`, `HistoryRecord.event_id → Event.id`, `HistoryRecord.patch_id → Patch.id`).
- SQLite for v0.1 (zero-infra demo); models written with an ORM (SQLAlchemy) so swapping to Postgres later is a connection-string change, not a rewrite.

### 2.9 Frontend (`frontend/`)
- Minimal dashboard: device list + current status, live event feed (subscribes to Event topic), history table view per device, a "force rollback" button per device that calls the same rollback path as the automatic Safety Rollback Protocol.

## 3. Sequence: Situational Morphing (reference)

```
Device --UDP/TCP--> Listener --Task+Event--> Distributor
Distributor --Task--> Agent
Agent --retrieve--> RAG(schema, docs, history)
Agent --tool call--> check_hardware_schema(27)
Agent --generate--> code
Agent --submit--> GuardRail
GuardRail --pass--> Sandbox
Sandbox --pass--> Deploy(stage1: Soft Firmware)
Deploy --confirm--> Deploy(stage2: Soft Firmware 2)
Deploy --OTA push--> Device
Deploy --write--> HistoryTable
Deploy --schedule--> ReversionJob(+5min / cooldown)
Distributor --Event--> Frontend, DB
```

Failure branch: `Sandbox --fail(Results, ΔFirmware)--> Agent` (loop, capped at 3) → on cap exceeded → `SAFETY_PROTOCOL.md` rollback.

## 4. Build Order (recommended for Claude Code)

1. Data schemas + DB models (`DATA_SCHEMAS.md`, `server/db/models.py`) — everything else depends on these shapes.
2. Edge Node simulator (`edge_node/main.py`) — needed to generate test traffic early.
3. Listener + Distributor (local/in-process queue implementation) — get telemetry flowing end-to-end with a stub Agent that just echoes.
4. Guard Rail (deterministic, no LLM dependency — easiest to unit test well).
5. Sandbox runner (Docker-based execution harness) — test with hand-written good/bad scripts before wiring the Agent.
6. Agentic Core (RAG + tools + prompts + retry loop) — wire in last since it's the most expensive/slow piece to iterate on.
7. Deploy/staged rollout + History Table writes.
8. Safety Rollback Protocol + reversion scheduler.
9. Frontend (thin layer on top of everything above).
10. End-to-end scenario tests (Scenario A/B/C from PRD §6).

## 5. Testing Strategy

- Unit test Guard Rail and Sandbox independent of the LLM (deterministic, fast, run in CI every commit).
- Agent tests use recorded/mocked LLM responses for deterministic CI runs; a smaller "live" test suite (manually triggered) exercises the real model.
- E2E tests run the full docker-compose stack and drive the Edge Node simulator through Scenarios A, B, and C from the PRD, asserting on History Table contents and final device firmware hash.

## 6. Configuration

All tunables (Sandbox timeout, max retries, reversion window, forbidden pins denylist extensions, model name/provider) live in a single `config.py`/`.env`, never hardcoded inline — Claude Code should treat any magic number touching safety behavior as a config bug if left inline in implementation code.
