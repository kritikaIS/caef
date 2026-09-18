# Technical Requirements Document (TRD)
## CAEF Pipeline

This document translates the PRD into concrete technical requirements,
interfaces, and constraints. It is the contract between components; TDD.md
describes *how* these are implemented internally.

---

## 1. System Components

| Component | Language/Runtime | Responsibility |
|---|---|---|
| Edge Node | Python (sim) / C++ (stretch) | Run current firmware, monitor sensors, emit telemetry, apply OTA |
| Telemetry Gateway (Listener) | Python, TCP (full-duplex) or MQTT | Accept device connections, classify events |
| Distributor | SQS-style queue + SNS-style topic (can be simulated in-process for v0.1, e.g. Redis lists / `boto3` + LocalStack, or a simple asyncio queue) | Decouple ingestion from processing, fan out to Frontend/History |
| Agentic Core (Agent) | Python, LangChain | RAG retrieval, planning, code generation, tool-mediated hardware checks |
| Guard Rail | Python (pre-sandbox static checker) | Schema conformance + forbidden-pin + static-safety check on generated code before it is executed anywhere |
| Verification Sandbox | Docker container mirroring device OS | Time-boxed execution of candidate firmware |
| DB / RAG Store | SQLite or Postgres (structured) + a vector store (e.g. Chroma) for RAG documents | History Table, Docs of Node, Docs on comp., schema storage |
| Frontend | Minimal web app (e.g. FastAPI + simple HTML/JS, or Streamlit) | Operator observability + manual rollback |

## 2. Interfaces

### 2.1 Device → Listener (Telemetry)
- Transport: UDP (low latency, best-effort) for routine heartbeats; TCP full-duplex for events that require an eventual reply/ack (`CONTEXT_TRIGGER`, `CRITICAL_FAILURE`).
- Payload: JSON, see `DATA_SCHEMAS.md §2` (Telemetry Payload).
- Device also **polls** (`Poll id / firmware`) on an interval as a fallback channel — see LOOPS.md §3.

### 2.2 Listener → Distributor
- Listener publishes a `Task` message (work item for the Agent) and an `Event` message (observability/history record) for every classified telemetry payload.
- Task queue: at-least-once delivery; the Agent must be idempotent per `event_id`.

### 2.3 Distributor → Agent
- Agent consumes `Task` messages, one event chain at a time per device (serialize per-device to avoid concurrent conflicting patches to the same target).

### 2.4 Agent → Guard Rail → Sandbox
- Agent emits: `{ plan (text), code (string), target_file, event_id, device_id, patch_id }`.
- Guard Rail either passes the payload through unchanged or returns a rejection with reason (no code reaches the Sandbox on rejection).
- Sandbox returns: `{ status: pass|fail, logs, runtime_seconds, exit_code }`.
- On `fail`, Sandbox result (`ΔFirmware`, i.e. diff/results) is fed back to the Agent for a bounded number of retries (default 3, see FR-17).

### 2.5 Sandbox → Deployment
- On `pass`, the artifact is written to a staged rollout slot ("Soft Firmware"), optionally promoted to a second staging slot ("Soft Firmware 2") before being marked deployable — see LOOPS.md §2 for the exact staging semantics chosen for v0.1.
- Deployment writes a History Table row (`Time | Poll ID | Event ID | Patch ID | FW hash`) with `FKEY` to `device_id` and `event_id`.

### 2.6 Deployment → Device (OTA)
- Server pushes new firmware file to device; device Watchdog validates and performs a safe restart.
- Device also confirms via its poll channel that it has picked up the assigned firmware (reconciliation against push, to handle missed pushes).

### 2.7 History/Event → Frontend
- Distributor's Event topic fan-outs to the Frontend for live display, and to the DB for the History Table.

## 3. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | Guard Rail check must run and complete before any generated code executes anywhere — no exceptions. |
| NFR-2 | Sandbox execution is time-boxed (default 10s, configurable) and resource-limited (CPU/mem caps on the container). |
| NFR-3 | All hardware pin references in generated code must resolve through `check_hardware_schema` or an equivalent validated tool call — free-text pin numbers not backed by a tool call are a Guard Rail rejection. |
| NFR-4 | Safety Rollback must be reachable even if the Agent/LLM is unavailable (i.e. rollback logic must not itself depend on an LLM call). |
| NFR-5 | Every deployed artifact must be traceable end-to-end (event → plan → generated code → sandbox result → deployed hash) for audit/patent-evidence purposes. |
| NFR-6 | System should degrade gracefully: if the Distributor queue backs up, devices continue running their last-good firmware indefinitely (no forced unsafe state). |
| NFR-7 | v0.1 targets a single-device, single-operator demo; horizontal scaling of the Distributor/Agent is a documented future concern, not a build requirement. |

## 4. Constraints

- The Agent must never receive **write** access to the hardware schema file — read-only, enforced at the filesystem/tool layer, not just by convention.
- The Agent must never be permitted to skip the Sandbox step, regardless of "confidence."
- Retries are capped; after cap is reached the event is marked `escalated` and Safety Rollback is invoked (FR-21) rather than looping indefinitely.
- Simulation phase targets Raspberry Pi + Python end-to-end (per source spec, Step 1/2); do not scope in real MCU toolchains for v0.1.

## 5. Data Persistence Requirements

- **History Table**: append-only; columns at minimum `time, poll_id, event_id, patch_id, fw_hash, device_id (FKEY), status`.
- **Docs of Node**: per-device documents (hardware schema, current connection schema) — RAG-indexed.
- **Docs on comp.** (component docs): driver library snippets, prior validated patches — RAG-indexed.
- Vector store and relational store may be the same engine for v0.1 (e.g. SQLite + a local embedding index) — no requirement to use a hosted vector DB for the prototype.

## 6. Security / Safety Requirements (v0.1 scope)

- Forbidden pins (`schema.constraints.forbidden_pins`) must be enforced at both Guard Rail and Sandbox layers (defense in depth).
- OTA payloads should include a hash the device verifies before flashing (integrity, not full code-signing, for v0.1).
- No requirement for TLS/auth in v0.1 (flag as explicit known gap, do not silently skip documenting it).

## 7. Out of Scope for TRD (deferred to TDD or future work)
- Exact queue technology choice (real AWS SQS/SNS vs. local simulation) — TDD decides based on "runs on a laptop for a demo" constraint.
- Exact vector DB choice.
- Concurrency model details for multi-device fleets.

## 8. v0.1 Stack Resolutions

Decisions taken for the build, resolving the choices left open above. Anything
marked *(deviation)* departs from a default stated elsewhere in the docs and was
explicitly requested by the project owner.

| Concern | Resolution | Note |
|---|---|---|
| Structured store | **Supabase Postgres** via SQLAlchemy; local SQLite fallback when `DATABASE_URL` is unset | *(deviation)* §1 and CLAUDE.md §7 default to local SQLite only. Offline tests and E2E still run on SQLite, so PRD G6 reproducibility holds. |
| RAG store | **SQLite FTS5** keyword index, local file (`RAG_DB_PATH`) | Permitted by §5 ("may be the same engine … no hosted vector DB required"). Corpus is one device schema + a handful of driver snippets + history rows; semantic embedding deferred behind `rag/retriever.py`. |
| Agentic Core | **LangChain** + `langchain-openai`, `LLM_BASE_URL` configurable | Matches §1. Any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, local Ollama) works without a code change, per §1's "model is pluggable". |
| Distributor | in-process `asyncio.Queue` behind a `Distributor` interface | §7's laptop-demo constraint; SQS/SNS swap without touching Listener/Agent (CLAUDE.md §5). |
| Device ↔ Listener | stdlib `socket`, UDP heartbeats + TCP events | §2.1; no MQTT broker in v0.1. |
| Frontend + poll endpoint | FastAPI + one HTML page, SSE live feed | §1 lists FastAPI or Streamlit; FastAPI also serves the `Poll id / firmware` endpoint the device already needs (LOOPS.md §3). |
