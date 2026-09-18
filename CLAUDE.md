# CLAUDE.md
## Instructions for Claude Code — CAEF Pipeline Build

This file is read by Claude Code at the start of every session in this repo.
It is the operating manual: what this project is, how it's organized, and
the rules that must not be broken while building it.
Divide project into milestones, keep me in the loop of what you are implementing, maintain version history by commitiing to git but do not add yourself as co author.

---

## 1. What This Project Is

CAEF (Context-Aware Evolutionary Firmware) is a research/patent-prototype
pipeline where an edge device (simulated on a Raspberry Pi) can request
live, LLM-generated firmware changes from a cloud agent — either to adapt to
a sensor-driven situation ("Situational Morphing") or to self-heal from a
crash ("Auto-Patching"). Every change is schema-constrained, sandboxed, and
auto-reversible.

**Your job on this project is to build the software described in `/docs`.
You are not the runtime Agent described in the design — you are the
engineer implementing the whole pipeline, including the code that will
later drive a *different* LLM call (the in-pipeline Agent) at runtime.**
Don't conflate "Claude Code building this repo" with "the Agentic Core
component inside the repo" — they are different things with different jobs.

## 2. Required Reading Order

Before writing code, read, in this order:
1. `docs/PRD.md` — what and why.
2. `docs/TRD.md` — the interface/requirement contract between components.
3. `docs/TDD.md` — the concrete implementation plan and repo layout (this is your primary blueprint).
4. `docs/ARCHITECTURE.md` — the component map and message vocabulary (use these exact names in code).
5. `docs/LOOPS.md` — every control loop, its trigger and exit condition.
6. `docs/DATA_SCHEMAS.md` — every JSON/DB shape; treat as the contract for all payloads.
7. `docs/SAFETY_PROTOCOL.md` — non-negotiable safety rules; violating these is a build failure even if tests pass.

If any of these documents conflict, `SAFETY_PROTOCOL.md` wins on safety
questions; `TDD.md` wins on implementation-detail questions; `PRD.md` wins on
scope questions (what's in v0.1 vs. later).

## 3. Build Order

Follow `TDD.md §4` exactly:
1. Data schemas + DB models
2. Edge Node simulator
3. Listener + Distributor (with a stub Agent)
4. Guard Rail
5. Sandbox runner
6. Agentic Core (real LLM wiring)
7. Deploy / staged rollout / History Table writes
8. Safety Rollback Protocol
9. Frontend
10. End-to-end scenario tests

Do not skip ahead to the Agentic Core before Guard Rail and Sandbox exist and
are independently tested — the Agent must never be the first thing that
touches real code execution.

## 4. Hard Rules (do not violate, even if asked to "simplify for now")

- **Never let generated firmware code skip Guard Rail or Sandbox.** No "fast path," no "trusted patch," no exceptions — see `SAFETY_PROTOCOL.md §1`.
- **Never give the Agent write access to the hardware schema file.** Read-only at the filesystem/tool level.
- **Never let a GPIO pin literal appear in generated code without a corresponding logged `check_hardware_schema` tool call** for that pin in the same patch's trace.
- **Never implement Safety Rollback as something that calls the LLM.** It must be pure, deterministic logic against the History Table (`SAFETY_PROTOCOL.md §5`).
- **Never hardcode safety-relevant magic numbers inline** (retry caps, sandbox timeout, reversion window, forbidden pins). These live in config (`TDD.md §6`).
- **Never invent new field/event names** that aren't in `DATA_SCHEMAS.md`. If a new field is genuinely needed, update `DATA_SCHEMAS.md` in the same change, don't let code and docs drift.
- **The rollback code path must be single-implementation.** Manual (operator button) and automatic (3-strikes) rollback call the same function — don't write it twice (`SAFETY_PROTOCOL.md §7`).

## 5. Conventions

- Python for all server-side components and the Edge Node simulator (per `TRD.md §1`). C++ edge runtime is a stretch goal only — do not build it unless explicitly asked.
- Use an ORM (SQLAlchemy) for DB models even on SQLite, so the swap to Postgres later is a config change.
- Config via a single `config.py`/`.env`, not scattered constants.
- Every component that produces or consumes one of the JSON shapes in `DATA_SCHEMAS.md` should validate against it (e.g. `pydantic` models mirroring the schema doc) rather than trusting ad hoc dicts.
- Tests for Guard Rail and Sandbox must not depend on a live LLM call — use fixtures/mocked Agent output (`TDD.md §5`).
- Keep the queue/topic abstraction (Distributor) behind an interface so the in-process/local v0.1 implementation can later be swapped for real SQS/SNS without touching Listener/Agent code.

## 6. What "Done" Looks Like for v0.1

The three PRD scenarios (`PRD.md §6`) pass end-to-end against the
docker-compose stack:
- **Scenario A** (Situational Morphing / heat event) — firmware morphs, then reverts.
- **Scenario B** (Auto-Patching / crash) — patch generated, validated, deployed, durable (no reversion).
- **Scenario C** (repeated failure) — Safety Rollback Protocol fires on the 3rd strike, without any further LLM call, and the Frontend shows the rollback.

Plus: zero forbidden-pin writes anywhere in the test corpus, and every
deployed artifact traceable via the `FKEY`-linked History Table.

## 7. Out of Scope — Do Not Build Unless Explicitly Requested

- Real code-signing / secure boot.
- Multi-tenant fleet management.
- Real MCU (ESP32/STM32) toolchains.
- Hosted vector DB / production queue infra (local simulations are correct for v0.1 — see `TRD.md §7`).
- Auth/TLS between components (flagged gap, not a build task, for v0.1).

## 8. When In Doubt

If a requirement is ambiguous (see the Open Questions in `PRD.md §7`), pick
the stated default in `LOOPS.md`/`SAFETY_PROTOCOL.md`, implement it behind a
config flag if an alternative is mentioned, and note the decision in a
comment referencing the doc section — don't silently pick a third option not
discussed in the docs.
