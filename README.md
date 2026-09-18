# CAEF — Contract-Constrained Adaptive Edge Firmware

CAEF is a pure-software research prototype for autonomous edge adaptation. An
LLM proposes a declarative behavior contract, but deterministic components
validate, compile, simulate and authorize it. An immutable local supervisor
enforces actuator permissions, bounded leases and A/B rollback even when the
server is unavailable.

Everything runs on a laptop. There is no hardware, no MCU toolchain and no
network dependency, and the default demo needs no API key.

## Overview

A simulated edge device gets hot. It reports the situation. A cloud agent
proposes a change to the device's behaviour, and something has to decide whether
that change is safe to install.

The original version of this project (still here, still runnable, still tested)
answered that by having the model write a complete Python firmware file, then
vetting the file: an AST guard rail for forbidden pins and disallowed
constructs, and a Docker sandbox to see whether it crashed within ten seconds.
That is the `source_generation` mode, and it is the experimental control.

The mode this repository is now about — `manifest_compiler` — never lets the
model write code at all. It proposes a **Behavior Manifest**: a declarative
contract naming capabilities from a fixed registry and stating conditions as
typed numeric comparisons. Deterministic components take it from there, and an
immutable supervisor on the device has the last word regardless of what any of
them decided.

## Research Question

> Does constraining an LLM to propose a declarative behavior contract —
> validated, compiled, simulated and signed by deterministic components, and
> enforced at runtime by an immutable local supervisor — reduce unsafe
> deployments compared with unrestricted LLM source generation, while retaining
> useful context adaptation?

The comparison is implemented, not argued: `experiments/` runs three pipelines
over the same scenarios, seeds and proposal intents and writes the numbers to
JSON and CSV.

## Core Contribution

1. **A behaviour contract instead of a program.** The model's output is data.
   There is no field in a manifest in which a pin number, an expression, an
   import or a shell command can be placed and later evaluated.
2. **A deterministic compiler over a closed capability registry.** The artifact
   is assembled from prevalidated templates and is byte-identical for identical
   input. Model-authored text never enters it.
3. **Behavioural verification before authorization.** The compiled controller is
   run in a closed-loop virtual world across seeded scenarios and judged against
   a fixed list of properties. A failed verification is never signed.
4. **An immutable local supervisor that owns the actuators.** Firmware submits
   typed intents; the supervisor validates each one against the registry, the
   hardware schema and the current safety state, and can refuse any of them
   without crashing.
5. **Local leases and local rollback.** A temporary adaptation expires on the
   device's own clock and reverts to last-known-good with no server, no network
   and no model in the path.

Each of these is enforced by code and covered by tests. Where something holds
only by construction, or only in simulation, this document says so.

## Why Arbitrary Code Generation Is Unsafe

Not as a general claim about language models — as a claim about what a static
gate over generated source can and cannot decide.

The experiment drives eight *proposal intents* through each pipeline: one sound
and seven flawed. Guard Rail, running for real on the generated Python, catches
the ones that are visible in the text — a forbidden pin literal, a pin used with
no schema check, an unbounded loop. It does not catch, and cannot:

- **a controller that never cools.** Perfectly ordinary Python. The device
  reaches 107 °C in the simulation.
- **a threshold set at 200 °C.** Legal, syntactically clean, and the device
  cooks while the controller waits for a temperature it will not survive.
- **a relay toggled every tick.** Nothing in the source says "damage the
  hardware"; the damage is in the trajectory.
- **firmware that never ends.** A generated file runs until something replaces
  it, and the only thing that would is a server-side timer — which is exactly
  what a network partition removes.

Three of those are behavioural: they are properties of a run, not of a text, and
no amount of static analysis over the text will decide them. The fourth is
architectural. A ten-second sandbox does not decide them either — surviving ten
seconds is not evidence about anything that happens in the following hour.

The contract-constrained mode responds to each differently. Two of the four
intents above are not merely rejected — **they cannot be expressed**: there is no
field in a manifest in which to name a pin, and the minimum actuator hold comes
from the registry, so "toggle every tick" is not a thing a proposal can ask for.
The other two are rejected, by the closed-loop verifier and the validator
respectively, with a counterexample trace attached.

## System Architecture

```
                     UNTRUSTED                          DETERMINISTIC
   ┌───────────┐   ┌────────────┐   ┌───────────┐   ┌──────────┐   ┌────────┐
   │  Listener │──►│ Distributor│──►│    LLM    │──►│Validator │──►│Compiler│
   │(telemetry)│   │(queue/topic│   │  Agent    │   │(schema + │   │(registry
   └───────────┘   │            │   │(or stub)  │   │ registry)│   │templates)
         ▲         └────────────┘   └───────────┘   └──────────┘   └───┬────┘
         │                                                             │
         │                                                             ▼
         │                            ┌───────────┐   ┌──────────┐  ┌────────────┐
         │                            │  Signer   │◄──│ Verifier │◄─│ Controller │
         │                            │ (HMAC +   │   │(closed-  │  │  program   │
         │                            │ sequence) │   │ loop sim)│  │  (DATA)    │
         │                            └─────┬─────┘   └──────────┘  └────────────┘
         │                                  │ signed package
   ┌─────┴──────────────────────────────────▼──────────────────────────────────┐
   │                             VIRTUAL DEVICE                                 │
   │  ┌──────────────────────────────────────────────────────────────────────┐ │
   │  │  IMMUTABLE: package verification · supervisor · A/B slots · lease     │ │
   │  │             emergency policy · safe state · local rollback            │ │
   │  └───────────────▲──────────────────────────────┬───────────────────────┘ │
   │      ActuatorIntent│                            │ applies (or refuses)     │
   │  ┌───────────────┴──────────┐        ┌──────────▼──────────────┐          │
   │  │ REPLACEABLE: the compiled │        │  ThermalWorld (closed   │          │
   │  │ controller program (data) │        │  loop, seeded, ticked)  │          │
   │  └───────────────────────────┘        └─────────────────────────┘          │
   └────────────────────────────────────────────────────────────────────────────┘
```

### Trust boundaries

| Boundary | What it means here |
|---|---|
| **The LLM is untrusted** | Its output is parsed into a closed shape and checked against this device before anything else looks at it. Nothing it writes is executed, in either mode of the manifest pipeline. |
| **Schemas and capability definitions are read-only** | The hardware schema and the capability registry have no writer anywhere in the codebase — not a permission, a missing code path. A test asserts it across the agent and manifest packages. |
| **The compiler and verifier are deterministic** | No clock, no randomness outside a seed, no model call. The same input produces the same artifact bytes and the same verification report. |
| **The local supervisor owns actuators** | A controller returns intents and holds no reference to an actuator port. Emergency policy runs before controller intents each tick and outranks them. |
| **OTA packages are authenticated** | HMAC-SHA256 over the whole package including the artifact, bound to a device, a sequence number and a base firmware hash. Hash verification alone is not authorization. |
| **Temporary behavior expires locally** | The lease is charged on the device's own clock and persisted, so expiry survives a restart and does not need the server. |
| **Rollback does not depend on the model or cloud** | Last-known-good is an artifact in the device's own inactive slot. Reverting reads local state and nothing else. |

## Safe Adaptation Flow

```
   telemetry ─► Task
      │
      ├─ 1. propose        LLM or deterministic stub → Behavior Manifest (data)
      ├─ 2. validate       13 checks vs hardware schema + capability registry
      ├─ 3. compile        registry templates only → controller program + hash
      ├─ 4. verify         closed-loop simulation, 8 scenarios × N seeds,
      │                    10 properties, counterexample on failure
      ├─ 5. sign           HMAC-SHA256, sequence number, base firmware hash
      ├─ 6. deliver        the device verifies the package again, locally
      ├─ 7. probation      candidate runs in the inactive slot for N healthy ticks
      ├─ 8. activate       slots flip; the previous one becomes last-known-good
      └─ 9. expire         the device's own lease runs out → revert, no server
```

Each arrow is a gate, and a failure at any of them is terminal for that
proposal. The server ledger records which one it reached: `proposed`,
`manifest_validated`, `compiled`, `simulation_verified`, `signed`,
`delivery_attempted`, `accepted_by_device`, `active_on_device`, `rejected`,
`reverted`, `rolled_back`. The transition graph is enforced, and two edges carry
most of the weight: `delivery_attempted` cannot reach `active_on_device` — a send
is not an arrival — and `accepted_by_device` is not activation either, because
probation can still fail.

## Behavior Manifest Example

```json
{
  "manifest_version": "1.0",
  "manifest_id": "m-event29-sound",
  "device_id": "pi_node_alpha",
  "event_id": "event-29",
  "trigger_type": "CONTEXT_TRIGGER",
  "trigger_event": "HIGH_HEAT_DETECTED",
  "current_firmware_hash": "4571e7c5e17c1fa6b7189ad79c4afa0f6082252287146abe191b1517fed82b5c",
  "capability_registry_version": "1.0.0",
  "requested_capabilities": [
    "read_temperature", "fan_on", "fan_off",
    "emit_heartbeat", "emit_context_event", "emit_critical_event", "enter_safe_idle"
  ],
  "sensor_inputs": ["temperature_c"],
  "actuator_outputs": ["fan"],
  "activation_condition": { "metric": "temperature_c", "operator": ">=", "value": 80.0 },
  "recovery_condition":   { "metric": "temperature_c", "operator": "<",  "value": 60.0 },
  "maximum_duration_seconds": 40,
  "control_period_seconds": 1.0,
  "resource_budget": {
    "max_cpu_ms_per_step": 3.6,
    "max_memory_kb": 64,
    "max_actuator_transitions_per_minute": 12
  },
  "fallback_behavior": "enter_safe_idle",
  "rationale": "HIGH_HEAT_DETECTED: hold cooling while temperature is at or above 80.0C and release it below 60.0C. Lease 40s."
}
```

That is a real manifest, taken verbatim from `results/demo/demo_trace.json`
after a demo run (`--lease 40`, hence the 40-second lease).

Note what is not in it: no pin, no import, no expression, no loop, no duration
without a bound. `requested_capabilities` are names from a registry the model
cannot write to; the pins those capabilities are permitted to touch are resolved
by the compiler, from the registry, at compile time.

Unknown fields are rejected. So are `"operator": "or 1==1"`, `"value": "80;
import os"`, and a `metric` that is not a bare identifier — the closed enum and
the strict types make each of them a parse failure rather than a clause.

## Closed-Loop Virtual Environment

The baseline simulation computed each sensor reading from elapsed wall time and
`random`, which means turning the fan on could not change the next reading and
no cooling behaviour was verifiable. `ThermalWorld` closes the loop:

```
T[t+1] = T[t] + dt · ( heat_rate · load
                     − k_ambient      · (T[t] − ambient)
                     − k_fan · eff · fan · (T[t] − ambient) )
```

First-order lumped capacitance, chosen to be legible and monotone. It is **not**
a calibrated thermal model of any real board, no physical hardware was involved
in fitting it, and none of its constants should be read as characterising one.
What matters is that `fan ∈ {0,1}` and `k_fan > 0`, so fan state provably
influences future readings.

The world advances by explicit ticks — never by sleeping — and every stochastic
quantity comes from one seeded generator, so a run is reproducible from its seed
alone. Eleven scenarios: `normal`, `gradual_overheat`, `sudden_spike`,
`noisy_threshold`, `sensor_stuck_high`, `sensor_stuck_low`, `ineffective_fan`,
`firmware_crash`, `network_loss_after_deploy`, `server_failure_after_deploy`,
`repeated_duplicate_triggers`.

Two of them are marked as physically unwinnable — an ineffective fan, a sensor
stuck low — and the verifier **skips** the temperature bound there with the
reason recorded, rather than passing a controller for surviving something it had
no means of surviving.

## Local Safety Supervisor

The supervisor lives outside the replaceable firmware. In `manifest_compiler`
mode the firmware artifact is data, so there is no mechanism by which it could
reach the supervisor's code even in principle — an artifact imports nothing,
because an artifact is a JSON document.

- Every actuator change goes through the supervisor. A controller returns
  `ActuatorIntent` messages and holds no reference to an actuator port.
- Local emergency policy runs **before** controller intents each tick and
  outranks them. While cooling is required, an intent that would stop it is
  rejected — not deferred, not merged.
- Intents are validated individually against the registry, the schema and the
  installed program's declared capabilities, and one bad intent does not stop
  the others or crash the supervisor.
- A controller that faults, misses heartbeats or exceeds its control budget
  loses control to a deterministic safe state. For the thermal prototype that
  state **keeps cooling on while the device is hot** — stopping the firmware is
  not by itself safe.
- Actuators no installed capability owns are released to their registry default,
  so a revert cannot leave a relay energised by firmware that no longer exists.

What this does not claim: there is no privilege boundary, no MMU and no secure
element. A process that can import the supervisor module can also call the
actuator port directly. The guarantee is about what *compiled firmware* can
express, not about an attacker who already runs code on the device.

## Signed OTA and A/B Rollback

The v0.1 OTA payload carried a content hash, and the device checked that the
code hashed to it. That authenticates nothing — anyone who can reach the port
can send code and a matching hash. **Hash verification is not authorization.**

A `FirmwarePackage` binds an artifact to a device, an origin, a point in a
sequence and a bounded lease, and the signature covers the whole package
including the artifact bytes. The device rejects, by name: `invalid_signature`,
`wrong_device`, `artifact_mismatch`, `manifest_mismatch`, `stale_base_firmware`,
`replayed_sequence`, `lease_too_long`, `unsupported_registry`,
`malformed_package` — checked consistency-first, so tampering is reported as
what it is rather than as a generic signature failure.

Signing uses standard-library HMAC-SHA256 with a key from
`CAEF_OTA_HMAC_KEY`. A shared symmetric key means any holder can mint a package;
production would need asymmetric device identities and secure key storage.

A/B state lives on the device, not only in the server database: slot A, slot B,
active slot, last-known-good slot, candidate status, failure count, active lease,
last accepted sequence number — written atomically (temp file, `fsync`,
`os.replace`). Install writes the candidate to the inactive slot, verifies it
locally, runs it in **probation**, and promotes it only after N healthy ticks;
the outgoing slot becomes last-known-good and is not overwritten. Probation
failure, lease expiry or repeated crashes revert locally.

A lease expires on whichever clock has advanced further — the persisted elapsed
count or wall time since installation — so a stopped clock cannot extend it and
neither can a rewound one. A device whose real-time clock an attacker controls
would need a secure monotonic counter; that is out of scope here.

## Running Without an LLM

This is the default and the recommended path. No API key, no Docker, no network.

```sh
pip install -r requirements.txt
python -m demo.safe_demo
```

The stub agent is not a canned blob: it reads the same registry, builds a real
manifest and sends it through every downstream gate. It also carries a variant
switch, which is how the experiment drives the same distribution of model
mistakes through every pipeline.

## Running With an LLM

`server/agent/manifest_agent.py` contains `LLMManifestAgent`, which proposes a
manifest over LangChain against any OpenAI-compatible endpoint and retries on
rejection with the deterministic reason fed back. Configure it in `.env`:

```sh
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o
# LLM_BASE_URL=https://openrouter.ai/api/v1     # or Groq, or a local Ollama
ADAPTATION_MODE=manifest_compiler
```

**Stated plainly:** that class is exercised in the test suite against a scripted
fake LLM, and it has **not** been run against a live model in this repository.
The experiment harness therefore refuses `--llm` rather than pretending it has
numbers from one. The gates downstream of the proposal do not care which agent
produced the manifest — that is the design — but "we ran it with a real model"
is not a claim this README makes.

## Reproducing the Main Demo

```sh
python -m demo.safe_demo
```

Prints a thirteen-step timeline and writes a machine-readable trace to
`results/demo/demo_trace.json`. Abridged output from an actual run:

```
   1. [tick   0]  45.00C fan=off baseline firmware running
      slot A, manifest=baseline-monitor, pattern=monitor_only_v1; declares no
      actuator, so it cannot turn the fan on
   3. [tick  29]  80.47C fan=off HIGH_HEAT_DETECTED raised
   5. [tick  29]  80.47C fan=off manifest validated
      13 deterministic checks against the hardware schema and capability registry 1.0.0
   6. [tick  29]  80.47C fan=off compiled deterministically
      pattern=thermal_cooling_v1, rules=['r_activate', 'r_recover', 'r_critical',
      'r_context', 'r_heartbeat'], artifact=99d9f52b5785d362
   7. [tick  29]  80.47C fan=off verified in the closed loop
      8 runs passed across 8 scenarios, 10 properties each; under gradual_overheat:
      peak 81.361C, activation latency 0 ticks, recovery 2 ticks
   8. [tick  29]  80.47C fan=off signed package installed into the inactive slot
      seq=1 lease=40s sig=0b0d4298d37ff81e… → slot B, verified locally by the device
   9. [tick  34]  57.93C fan=on  probation passed, candidate activated
  10. [tick  44]  66.08C fan=off cooling is working
      80.47C → 54.33C at its lowest, 2 fan transitions
  11. [tick  44]  66.08C fan=off server disconnected
  12. [tick  69]  62.13C fan=off local lease expired
  13. [tick  69]  62.13C fan=off previous firmware restored automatically
      running slot A (manifest=baseline-monitor); no server, no model, no network
```

Steps 11–13 are the point. The server is switched off *before* the lease runs
out, so the reversion that follows cannot have come from it.

Options: `--seed`, `--lease` (the demo's one compressed knob), `--out`,
`--verbose`.

## Experiment Modes

```sh
python -m experiments.run_experiments
python -m experiments.plot_results     # optional; needs matplotlib
```

| Arm | Static gate | Behavioural gate | Actuator owner | Lease | Local rollback | OTA authentication |
|---|---|---|---|---|---|---|
| `source_unrestricted` | none | none | the generated firmware | none | none | content hash only |
| `source_guarded` | Guard Rail (the real v0.1 component) | none | the generated firmware | none | none | content hash only |
| `manifest_compiler` | validator | closed-loop verifier | the immutable supervisor | local, bounded | A/B + probation | HMAC + sequence + base hash |

The two baselines are genuinely separable: Guard Rail is an independent module
with its own tests, and the unrestricted arm simply does not call it.

Three limits of the harness, recorded rather than glossed:

- **The Docker sandbox stage is not in it.** The candidates are *step-shaped*
  (`step(observation) -> intents`) so they can be driven tick by tick against the
  same world; the v0.1 sandbox runs a whole-file firmware as a process, and
  running one against the other would measure nothing. It is recorded as
  `not_applicable`, never counted as a pass. The whole-file path with the real
  sandbox is covered by `tests/test_e2e_scenarios.py`, which needs Docker.
- **Proposals come from deterministic stubs**, not a live model (see above).
- **Some intents are `not_expressible`** in the manifest language rather than
  rejected by it. They are excluded from every rate and reported separately,
  because "the arm rejected it" and "the arm could not be asked" are different
  findings.

## Metrics

Written to `results/runs.{json,csv}` (one row per trial) and
`results/summary.{json,csv}` (one row per arm): adaptation success rate, unsafe
proposal rate, unsafe acceptance rate, false rejection rate, peak temperature,
time above critical, activation latency, recovery time, actuator transitions,
forbidden-pin writes, rollback success rate, stale-update rejection rate,
availability during server failure, artifact size, execution resource use, model
retries, and token use when a real model is enabled (zero for the stub).

Every rate names its own denominator, because the interesting ones are easy to
inflate by choosing a different one — and a rate over an empty denominator is
reported as `null`, never as `0`.

### Measured results

From an actual run of the default configuration on this repository:

```sh
python -m experiments.run_experiments
```

600 trials — 3 arms × 5 scenarios (`gradual_overheat`, `sudden_spike`,
`noisy_threshold`, `sensor_stuck_high`, `firmware_crash`) × 5 seeds × 8 proposal
intents. 55 seconds. Proposals from the deterministic stub agents, so the same
distribution of model behaviour reaches every arm.

| Metric | `source_unrestricted` | `source_guarded` | `manifest_compiler` |
|---|---|---|---|
| proposals made | 200 | 200 | 125 |
| intents **not expressible** in the language | 0 | 0 | **75** |
| unsafe proposals accepted | **100%** | 57.1% | **0%** |
| runs ending in a safety violation | **100%** | 77.5% | **0%** |
| sound proposals rejected (false rejection) | 0% | 0% | **0%** |
| sound proposals that adapted safely | 0% | 0% | **100%** |
| peak device temperature, worst trial | 107.1 °C | 107.1 °C | **95.9 °C** |
| mean ticks above the critical limit | 2.36 | 4.20 | **0.00** |
| forbidden-pin writes reaching the actuator | **65** | 0 | 0 |
| mean activation latency, deployed runs | 1.43 ticks | 1.37 ticks | 1.36 ticks |
| mean recovery time, deployed runs | 4.44 ticks | 4.98 ticks | 3.75 ticks |
| mean actuator transitions, deployed runs | 6.96 | 10.04 | **2.60** |
| rollbacks that succeeded | 0% | n/a (none attempted) | **100%** |
| adaptations that ended without a server | 0% | 0% | **100%** |
| stale updates rejected | 0% | 0% | **100%** |
| mean artifact size | 1044 B | 1044 B | 1490 B |
| model retries | 1.0 | 1.0 | 1.0 |
| tokens | 0 (stub) | 0 (stub) | 0 (stub) |

Where each pipeline refused a proposal:

| | rejected at |
|---|---|
| `source_unrestricted` | nothing rejected |
| `source_guarded` | Guard Rail: 75 |
| `manifest_compiler` | validator: 25 · compiler: 25 · **closed-loop verifier: 50** |

Which properties the surviving deployments went on to violate:

| | violations |
|---|---|
| `source_unrestricted` | `finite_lease` 200 · `critical_temperature_bound` 28 · `oscillation_bound` 25 · `pins_within_schema` 25 |
| `source_guarded` | `finite_lease` 125 · `critical_temperature_bound` 50 · `oscillation_bound` 25 |
| `manifest_compiler` | none |

### Reading these numbers honestly

- **The headline is not "0% vs 100%".** It is *where* the difference comes from:
  75 of the 200 intents could not be expressed as a manifest at all, 50 more were
  caught only by the closed-loop verifier — a gate the baseline does not have —
  and 25 each by the validator and the compiler.
- **Guard Rail does real work.** It rejected 75 proposals, exactly the ones
  whose flaw is visible in the text, and drove forbidden-pin writes to zero. The
  50 `critical_temperature_bound` violations that remain are the ones it cannot
  see: a controller that never cools, and one whose threshold is set past the
  device's survival.
- **`source_guarded` scores *worse* than `source_unrestricted` on time above the
  critical limit** (4.20 vs 2.36 mean ticks), because a rejection leaves the
  device on a baseline with no local supervisor at all. Rejecting a bad proposal
  is not the same as protecting the device, and in a pipeline with no local
  safety layer it can leave the device worse off.
- **`finite_lease` fires on every accepted source-arm run** because a generated
  firmware has no lease by construction. That single architectural fact accounts
  for most of the unrestricted arm's 100%.
- **`not_expressible` is not a score.** It is counted separately and excluded
  from every rate.
- **False rejection is 0% for all three arms**, so the manifest pipeline's
  safety numbers did not come from being indiscriminately strict — the one sound
  intent was accepted every time, and adapted with the same latency as the
  baselines.
- **These are simulation results** over the scenarios in this repository, driven
  by stub proposals rather than a live model. They are evidence about this
  design under these conditions and nothing wider.

[`doc/FINDINGS.md`](doc/FINDINGS.md) is the longer record: the same numbers with
their provenance, the three defects that surfaced only by running things, which
properties hold by construction rather than by experiment, and what this build
explicitly does not show.


## Test Suite

```sh
python -m pytest -q
```

Last run on this repository: **393 passed, 17 skipped**. The skips are the
Docker-dependent modules — the v0.1 sandbox and the whole-file end-to-end
scenarios — which skip rather than passing vacuously when no Docker daemon is
reachable. Nothing in the contract-constrained pipeline needs Docker.

Among what the suite pins down:

| Claim | Where |
|---|---|
| Unknown manifest fields are rejected | `tests/test_manifest.py` |
| Arbitrary Python cannot be placed in a condition field | `tests/test_manifest.py` |
| Unknown capabilities are rejected | `tests/test_manifest.py`, `tests/test_compiler.py`, `tests/test_supervisor.py` |
| An actuator cannot target an incompatible pin | `tests/test_manifest.py`, `tests/test_supervisor.py` |
| Identical manifests compile to identical artifacts | `tests/test_compiler.py` |
| Nothing model-authored is ever executed in manifest mode | `tests/test_compiler.py` (AST scan) |
| The fan affects simulated temperature | `tests/test_sim_world.py` |
| A high-heat scenario activates cooling within the permitted latency | `tests/test_verifier.py` |
| Invalid behaviour produces a counterexample trace | `tests/test_verifier.py` |
| Firmware cannot stop cooling during an emergency | `tests/test_supervisor.py` |
| A crash leaves the device cooling, then reverts locally | `tests/test_supervisor.py`, `tests/test_virtual_device.py` |
| Unsigned, modified, misaddressed, replayed and stale packages are rejected | `tests/test_ota_package.py` |
| A morph reverts after its local lease with no server | `tests/test_virtual_device.py`, `tests/test_demo.py` |
| A supervisor restart does not erase the lease | `tests/test_virtual_device.py` |
| Failed delivery is not recorded as active | `tests/test_manifest_pipeline.py`, `tests/test_ledger.py` |
| A failed verification is never signed | `tests/test_manifest_pipeline.py` |
| The complete safe-mode demo works with no API key | `tests/test_demo.py` |
| The v0.1 baseline still behaves exactly as before | the pre-existing modules, unchanged |

## Repository Structure

| Path | What |
|---|---|
| `registry/` | The capability registry — read-only, versioned, hashed |
| `schemas/` | Device hardware schemas — read-only |
| `server/manifest/` | Behavior Manifest models, canonical hashing, registry loader, validator |
| `server/compiler/` | Templates, deterministic compiler, immutable controller runtime |
| `server/sim/` | ThermalWorld, seeded scenarios, the tick harness |
| `server/verify/` | Property verifier and verification reports |
| `server/ota/` | Signed firmware packages and key handling |
| `server/manifest_pipeline.py` | Propose → validate → compile → verify → sign → deliver |
| `server/deploy/ledger.py` | The eleven-state deployment ledger |
| `server/mcp/` | Optional MCP tool surface (no MCP dependency) |
| `edge_node/supervisor.py` | The immutable local safety supervisor |
| `edge_node/slots.py` | A/B slots, leases, replay watermark, atomic persistence |
| `edge_node/virtual_device.py` | The device: install, probation, lease, local rollback |
| `demo/safe_demo.py` | The thirteen-step reproducible demo |
| `experiments/` | The three-arm comparison harness |
| `doc/RESEARCH.md` | The specification this mode is built to |
| `doc/FINDINGS.md` | The lab record: results, defects found by building, what this does not show |
| `doc/DATA_SCHEMAS.md` | Every wire shape, v0.1 (§1–§9) and manifest mode (§10–§19a) |
| — *preserved v0.1 baseline* — | |
| `server/agent/` | The source-generation Agent, prompts, RAG, hardware-schema tool |
| `server/guardrail/` | The AST guard rail |
| `server/sandbox/` | The Docker verification sandbox |
| `server/{listener,distributor,api}` | Telemetry gateway, queue/topic, poll endpoints |
| `server/deploy/{deployer,rollback,scheduler}.py` | v0.1 rollout, reversion, Safety Rollback Protocol |
| `frontend/` | Operator dashboard |

## Threat Model

**In scope.** A model that proposes something unsafe, incoherent, ineffective or
unbounded — through error or because it was prompted into it. A firmware
artifact that misbehaves at runtime. A network partition, a server outage, a
device restart. An update that is stale, replayed, misaddressed, modified or
unsigned. A sensor that lies.

**Out of scope, and not defended against.** An attacker who already executes
code on the device: this is a simulation with no privilege boundary, no MMU and
no secure element, so any process that can import the supervisor can also drive
the actuator port directly. Anyone holding the shared HMAC key, who can mint
packages the device will accept. An attacker who can write the device's state
file or control its real-time clock. A malicious operator with legitimate
access. Supply-chain compromise of this repository or its dependencies.

**Deliberately weak, and named as such.** Symmetric signing instead of
asymmetric device identity. Wall-clock-assisted lease expiry instead of a secure
monotonic counter. A subprocess-and-timeout boundary in the experiment harness,
which is a process boundary and a wall clock — not a sandbox.

## Limitations

- **This is a simulation.** No hardware was involved at any point. Nothing here
  has been run on a Raspberry Pi, a real DHT11 or a real relay.
- **The thermal model is an approximation** chosen for legibility, not a
  calibrated model of any board.
- **Verification is over the scenarios that exist.** It is evidence about
  modelled situations, not a proof over all possible ones. A situation nobody
  wrote a scenario for is not covered by anything here.
- **The Docker sandbox is retained but is not evidence of behavioural safety.**
  It shows a candidate did not crash inside a window. That is all it shows, and
  the parts of this system that make behavioural claims do not rely on it.
- **`supervisor_integrity` holds by construction, not by experiment.** No
  capability kind addresses the supervisor, so the property cannot fail; the
  verifier asserts it as a regression check, and it is reported that way.
- **Guard Rail's pin detection is shape-based.** It reads pins from driver
  constructors, `pin=` keyword arguments and `GPIO_n` literals. A candidate that
  computes or indirects a pin through a variable can evade the scan. This
  affects the v0.1 baseline; the manifest mode is unaffected, because a manifest
  cannot name a pin at all.
- **The LLM proposer is untested against a live model** in this repository.
- **All manifest-mode adaptations are leased**, including ones answering a
  crash. The v0.1 auto-patch is durable; the contract-constrained mode bounds
  everything and re-issues instead. That is a deliberate divergence.
- **One device, one actuator, one metric.** Multi-device fleets, multi-actuator
  coordination and richer capability sets are unexplored.

## Academic and Patent Positioning

The contribution this prototype is meant to support is architectural: moving the
model's output from *a program* to *a contract*, and moving the enforcement
point from a cloud-side reviewer to an immutable local supervisor with a bounded
lease. The properties that follow — no pin literal in model output, byte-identical
compilation, behavioural verification before authorization, expiry without a
server — are the ones implemented and tested here.

What this repository provides for that argument is a working, reproducible,
seeded implementation with a measured comparison against the unconstrained
approach. It provides no legal opinion. Whether any of this is novel,
non-obvious or patentable is a question for a prior-art search and a qualified
attorney, and nothing here should be read as an answer to it.

## Future Work

- Asymmetric device identity and secure key storage, replacing the shared HMAC
  key.
- A secure monotonic counter for lease expiry, removing the wall-clock
  assumption.
- Richer capability registries: multiple actuators, coordinated control,
  additional compilation patterns — each addition being a reviewed code change,
  which is the property that makes the registry worth having.
- Formal treatment of the property set: today they are decided by simulation
  over seeded scenarios, and some of them are amenable to proof over the
  controller program directly, since it is a finite rule table.
- A ROS 2 adapter for multi-node robotic simulations. Not required by this
  prototype and not implemented; the local implementation works first, and an
  adapter would follow it.
- A real-hardware bring-up, at which point every claim in this document would
  need re-establishing on the hardware rather than transferred to it.

## Safety Disclaimer

**This is academic simulation software. It is not production-ready embedded
safety software and must not be used as any part of one.**

There is no hardware in the loop, no secure boot, no code signing with
device-held private keys, no real MCU toolchain, no functional-safety process,
no certification against any standard, and no protection against an attacker who
already holds the signing key or can write the device's state file. The safety
properties described here are properties of this simulation, verified by the
tests named beside them, under the scenarios that exist in this repository.

Adapting real firmware on real hardware raises questions this prototype does not
answer.
