# FINDINGS.md
## CAEF v0.2 — what the contract-constrained build actually showed

A lab record, not a pitch. `README.md` is the public document and
`RESEARCH.md` is the specification; this file is what was *learned* while
building to that specification, including the parts that were surprising, the
parts that came out worse than expected, and the parts that are true only under
conditions worth naming.

Written 2026-08-28, against commit `1dc1614`, on top of the v0.1 baseline at
`432b392`. Everything below is reproducible from this repository.

---

## 1. Provenance of every number here

```sh
python -m pytest -q                      # 393 passed, 17 skipped
python -m demo.safe_demo                 # all 13 steps
python -m experiments.run_experiments    # 600 trials, 56s
```

The experiment configuration, verbatim from `results/summary.json`:

| | |
|---|---|
| arms | `source_unrestricted`, `source_guarded`, `manifest_compiler` |
| scenarios | `gradual_overheat`, `sudden_spike`, `noisy_threshold`, `sensor_stuck_high`, `firmware_crash` |
| seeds | 1, 2, 3, 4, 5 |
| proposal intents | `sound`, `forbidden_pin`, `no_provenance`, `never_cools`, `useless_threshold`, `actuator_chatter`, `unbounded_run`, `incoherent_request` |
| trials | 200 per arm (5 × 5 × 8) |
| lease | 40 simulated seconds (also the observation horizon) |
| server taken away | 10 ticks after the proposal |
| proposals from | deterministic stub agents — **no live model was involved** |
| wall clock | 56 s |

**The single most important caveat, stated first:** the proposal distribution is
synthetic. `StubManifestAgent` and `StubSourceAgent` render eight hand-designed
intents, one sound and seven flawed. These results measure *what each pipeline
does with a given distribution of model behaviour*, not *what distribution a real
model produces*. Every rate below should be read as conditional on that.

---

## 2. Headline result

| Metric | `source_unrestricted` | `source_guarded` | `manifest_compiler` |
|---|---|---|---|
| trials | 200 | 200 | 200 |
| **not expressible in the language** | 0 | 0 | **75** |
| proposals actually made | 200 | 200 | 125 |
| accepted | 200 | 125 | 25 |
| unsafe proposals accepted | **100%** | 57.1% | **0%** |
| runs ending in a safety violation | **100%** | 77.5% | **0%** |
| false rejection (sound proposals refused) | 0% | 0% | **0%** |
| sound proposals that adapted safely | 0% | 0% | **100%** |
| worst-trial peak temperature | 107.06 °C | 107.06 °C | **95.87 °C** |
| mean ticks above the 100 °C limit | 2.36 | 4.20 | **0.00** |
| forbidden-pin writes reaching the actuator | **65** | 0 | 0 |
| mean activation latency (deployed runs) | 1.43 | 1.37 | **1.36** ticks |
| mean recovery time (deployed runs) | 4.44 | 4.98 | **3.75** ticks |
| mean actuator transitions (deployed runs) | 6.96 | 10.04 | **2.60** |
| rollback success | 0% | n/a — none attempted | **100%** |
| adaptation ended without a server | 0% | 0% | **100%** |
| stale update rejected | 0% | 0% | **100%** |
| mean artifact size | 1044 B | 1044 B | 1490 B |
| accounted controller CPU | not measured | not measured | 4.2 ms |
| model retries | 1.0 | 1.0 | 1.0 |

Where each pipeline refused, and what the survivors went on to violate:

| Arm | Rejected at | Violations among survivors |
|---|---|---|
| `source_unrestricted` | nothing | `finite_lease` 200 · `critical_temperature_bound` 28 · `oscillation_bound` 25 · `pins_within_schema` 25 |
| `source_guarded` | Guard Rail 75 | `finite_lease` 125 · `critical_temperature_bound` 50 · `oscillation_bound` 25 |
| `manifest_compiler` | validator 25 · compiler 25 · **verifier 50** | none |

---

## 3. What is genuinely interesting in that table

### 3.1 The decisive column is `not_expressible`, not the acceptance rate

75 of 200 intents — three of the eight — have **no rendering in the manifest
language at all**:

- `forbidden_pin`: there is no field in a manifest in which to put a pin number.
- `no_provenance`: pins are resolved by the compiler from the registry, so there
  is no provenance step for a model to skip.
- `actuator_chatter`: the minimum actuator hold is derived from the registry's
  limits, and a manifest cannot shorten it.

This is a different kind of result from "the gate caught it". A gate that
catches 100% of a failure mode is a gate that can be wrong; a language in which
the failure mode is unsayable has no failure rate to measure. That distinction
is the strongest thing in this build, and it is why the harness reports
`not_expressible` separately and excludes it from every rate — folding it in as
success would credit the arm for questions nobody put to it.

### 3.2 Guard Rail does real, measurable work — and it is exactly bounded

Guard Rail rejected 75 of 200 proposals and drove forbidden-pin writes from 65
to 0. That is not a strawman baseline; it is a component doing its job.

What it cannot do is equally precise. Three intents survived it every time:

| Intent | Why a static gate cannot see it |
|---|---|
| `never_cools` | Ordinary Python that reads a sensor and returns nothing. Nothing in the text is wrong. |
| `useless_threshold` | `200.0` is a legal float. The defect is that the device does not survive to reach it. |
| `actuator_chatter` | Every individual statement is fine. The damage is in the trajectory. |

All three are **properties of a run, not of a text**. No amount of AST analysis
decides them, and a ten-second sandbox does not either — surviving ten seconds
is not evidence about the following hour. They are precisely the 50
`critical_temperature_bound` and 25 `oscillation_bound` violations left in the
guarded arm, and precisely what the closed-loop verifier caught (50 of the
manifest arm's 100 rejections).

### 3.3 The uncomfortable one: strictness without a local safety layer made things *worse*

`source_guarded` scored **worse than `source_unrestricted`** on time above the
critical limit — 4.20 vs 2.36 mean ticks — and worse on mean peak temperature
(84.0 vs 80.7 °C).

The mechanism is not subtle once seen. Rejecting a proposal leaves the device on
its baseline firmware, and in the v0.1 architecture the baseline has *no local
safety layer at all*: no supervisor, no emergency policy, nothing that acts when
the device climbs past 100 °C. The unrestricted arm at least deployed something
that sometimes cooled.

**Rejecting a bad proposal is not the same as protecting the device.** A safety
argument built only on "we refused the bad ones" is incomplete, and this is the
number that shows it. The manifest arm's 0.00 ticks above critical comes as much
from the supervisor's emergency policy — which acts on *rejected* runs too, at
95.87 °C peak — as from the gates.

This was not an anticipated result. It emerged from making the observation
horizon uniform across arms (see §5.2).

### 3.4 `finite_lease` fires on every accepted source-arm run, by construction

200 of 200 and 125 of 125. Not because the generated code was bad, but because
**a generated firmware file has no lease**. It runs until something replaces it,
and the only thing that would is a server-side timer — which is exactly what a
network partition removes.

That single architectural fact accounts for most of the unrestricted arm's 100%
unsafe-outcome rate. It is worth separating in any write-up: the source arms
failed a *behavioural* property 28/200 and 50/200 of the time, and an
*architectural* one always.

### 3.5 Safety did not cost adaptiveness

The result that would have undermined the whole thesis is a manifest arm that is
safe because it refuses everything. It did not happen:

- false rejection rate **0%** — the sound intent was accepted in all 25 trials;
- mean activation latency **1.36 ticks** vs 1.43 and 1.37 for the baselines —
  statistically indistinguishable at this sample size, and certainly not slower;
- mean recovery **3.75 ticks**, the fastest of the three;
- mean actuator transitions **2.60** vs 6.96 and 10.04 — the compiler's
  registry-derived minimum hold, visible as smoother control.

The adaptation is as fast and rather smoother. What it additionally does is end
by itself.

### 3.6 Artifact size: contracts are not smaller, and that is fine

1490 B compiled vs 1044 B of source. The compiled controller carries provenance
the source does not — manifest id and hash, event id, device id, base firmware
hash, registry version and content hash, the policy value it was compiled under.
Auditability costs bytes. Worth stating so nobody claims compactness as a
benefit it does not have.

---

## 4. Properties established, and how strongly

Strength matters more than the checkmark. Three tiers:

### Tier 1 — true by construction (cannot fail; tests are regression guards)

| Property | Why it cannot fail |
|---|---|
| No model-authored text is executed in manifest mode | The artifact is a JSON document interpreted by fixed code. An AST scan asserts no `exec`/`eval`/`compile`/`__import__` in `server/compiler/` or `server/manifest/`. |
| Firmware cannot modify the supervisor, schema, registry or rollback path | No instruction in the controller language addresses them. An artifact imports nothing. |
| A manifest cannot name a GPIO pin | No field accepts one. Pins are resolved by the compiler from the registry. |
| The schema and registry are read-only to the agent | No writer exists in the codebase. Tested by source scan across `server/agent/` and `server/manifest/`. |
| CLAUDE.md §4's "no pin literal without a logged tool call" | Satisfied vacuously: there are no pin literals in model output. |

Tier 1 is the strongest tier and also the least impressive-sounding, because
"this cannot happen" reads as a weaker claim than "we caught 100% of it". It is
the opposite.

### Tier 2 — enforced deterministically and tested

Manifest validation (13 checks), compilation determinism (byte-identical for
identical input), the ten verification properties, the nine package rejections,
supervisor intent validation, emergency precedence, safe-state cooling, local
lease expiry, restart survival, A/B probation, local rollback, ledger transition
legality. Each has a named test; `README.md` maps claim → test file.

### Tier 3 — true only in simulation, or under stated assumptions

| Property | The assumption |
|---|---|
| Device never exceeds the critical limit | Only in scenarios where cooling is *physically sufficient*. Two scenarios are marked unwinnable and the verifier **skips** the bound there with a reason, rather than passing. |
| Lease expiry survives restart | Uses wall time for the restart case. A device whose RTC an attacker controls needs a secure monotonic counter. |
| Package authenticity | Symmetric HMAC. Any key holder can mint a package. |
| Supervisor owns actuators | No privilege boundary — any process that can import the module can call the actuator port directly. The guarantee is about what *compiled firmware* can express. |

---

## 5. Defects found by building, not by reviewing

Three real bugs surfaced only when something was actually run. Recorded because
"we built it and the tests passed" would have hidden all three.

### 5.1 The control-budget property failed every crash scenario

`prop_control_budget` required a controller running past its lease to have
yielded. A controller that *faulted* never got the chance — it stopped being
stepped entirely. The property was failing artifacts for the correct behaviour.

Fix: losing control to the supervisor is a stronger form of yielding than
yielding. Found by running the demo with a short lease, not by any test.

### 5.2 Verification warmed up from a cold device

Scenarios started at 45 °C and took ~29 ticks to cross the threshold. An
artifact with a 25-second lease therefore *expired before its own scenario got
interesting*, and was failed for never commanding cooling.

The deeper error: the artifact is compiled **in response to** a situation, so it
must be verified against a device already in one. `Scenario.for_verification()`
now starts activation-expecting scenarios hot and caps the tick budget at the
lease plus a tail. This also made the horizon uniform across experiment arms,
which is what exposed §3.3.

### 5.3 A revert could leave a relay energised by firmware that no longer existed

The outgoing morph left the fan on; the incoming baseline declared no actuator
capability; nobody owned the fan. A stuck relay.

The first fix was worse than the bug: having the supervisor manage unowned
actuators *in both directions* turned it into a thermostat at 70 °C, which kept
the device off the 80 °C activation threshold and **stopped the adaptation loop
ever starting**. The demo caught it immediately — "the device never reported a
heat event". The supervisor was quietly doing the firmware's job, and the whole
experiment would have measured the wrong thing.

Final rule: **release, never engage**. Above the safe-state engage point the
actuator is left alone — not the supervisor's to start, not the supervisor's to
stop. Engagement stays in the emergency policy and the safe state.

---

## 6. Baseline preservation, verified

The v0.1 pipeline is the experimental control, so it had to be provably
untouched:

- `git diff 432b392..HEAD` on `server/guardrail/`, `server/sandbox/`,
  `server/deploy/{deployer,rollback,scheduler}.py`, `server/listener/`,
  `server/distributor/`, `server/orchestrator.py`, `server/api.py`, `frontend/`
  and `edge_node/{main,drivers,telemetry,watchdog}.py` → **no changes**.
- The only deletions in the whole diff are one import line widened
  (`server/db/models.py`) and one test-fixture tuple gaining two tables
  (`tests/conftest.py`).
- The pre-existing test modules alone still report **127 passed, 17 skipped** —
  identical to before the work started.
- Total: 69 files changed, 14,585 insertions, 37 deletions; 58 new files.

`config.py`, `server/schemas.py` and `server/db/models.py` were extended only.
`ADAPTATION_MODE` defaults to `source_generation`, so an existing checkout
behaves exactly as it did.

---

## 7. What this build does *not* show

Listed explicitly, because each is a claim someone could mistakenly draw:

1. **Nothing about live model behaviour.** `LLMManifestAgent` exists and is
   tested against a scripted fake. It has never run against a real model here.
   `--llm` refuses rather than producing numbers.
2. **Nothing about real hardware.** No Pi, no DHT11, no relay. The thermal model
   was chosen for legibility, not fitted to any board.
3. **Nothing about the Docker sandbox's value.** It is retained and skipped in
   this environment. The behavioural claims deliberately do not rest on it.
4. **Nothing about unmodelled situations.** Verification is evidence about the
   eleven scenarios that exist. A situation nobody wrote is covered by nothing.
5. **Nothing about adversaries with code execution, the HMAC key, the state file
   or the clock.** All explicitly out of the threat model.
6. **Nothing about scale.** One device, one actuator, one metric, three
   compilation patterns.
7. **Nothing legal.** No opinion on novelty, non-obviousness or patentability.

Also worth carrying forward: **Guard Rail's pin detection is shape-based** — it
reads driver constructors, `pin=` keyword arguments and `GPIO_n` literals. A
candidate that computes or indirects a pin through a variable evades it. This
was discovered while writing the source stub agent (the first draft used a dict
literal and Guard Rail saw no pins at all), and the stub was rewritten into the
idiom `server/agent/prompts.py` actually asks for, so the baseline is measured
inside its design envelope rather than outside it. The evasion affects v0.1 only;
a manifest cannot name a pin.

---

## 8. Design decisions worth defending later

| Decision | Alternative rejected | Why |
|---|---|---|
| Artifact is data interpreted by fixed code | Emit Python from templates and execute it | Makes "firmware cannot touch the supervisor" structural rather than policy. Assertable by AST scan. |
| Default mode stays `source_generation` | Default to the safer mode | Recommending ≠ silently switching an operator's pipeline. The demo, compose and `.env.example` select it explicitly. |
| Compiler matches patterns in **both** directions | Accept any superset of a pattern's requirements | A capability requested but silently unused means the proposal did not get what it asked for. Refusing is the point. |
| All manifest-mode adaptations are leased | Durable auto-patches, as in v0.1 | Every contract-constrained behaviour is bounded; durability is re-issuance. Documented divergence. |
| Unwinnable scenarios are **skipped**, not passed | Pass, or exclude the scenario | A pass a controller did not earn makes the whole report worthless. |
| Rates over empty denominators report `null` | Report 0 | 0% is the most flattering possible lie about an unmeasured quantity. |
| `not_expressible` counted separately | Count as a rejection | "The gate caught it" and "the question cannot be asked" are different findings. |
| Experiment source arms are step-shaped | Run whole-file firmware as a process | Needed for tick-level comparison against the same world. A real deviation, recorded in metadata and `experiments/README.md`. |
| Baseline arms execute in a subprocess | In-process `exec`, or Docker | A `while True` candidate must not hang the harness. Called what it is: a process boundary and a timeout, not a sandbox. |

---

## 9. Next experiments, in priority order

1. **The live-model arm.** Everything downstream is model-agnostic and the stub
   establishes the deterministic floor. Sampling a real model N times at
   temperature > 0 through *both* arms would turn §3.1 from a grammatical claim
   into an empirical one: what does a model actually try to write, and how often
   does it propose something all three gates pass but a human would not? That
   last quantity — the false-negative rate of the whole pipeline — is the one
   number this build cannot produce and most needs.
2. **Property-set sensitivity.** Every manifest-arm rejection came from one of
   ten properties, 50 of them from the verifier alone. Ablate them one at a time
   and report which are load-bearing. A property that never fires is either
   redundant or untested by the scenario set, and it matters which.
3. **Scenario-set adequacy.** Generate scenarios adversarially against a
   *passing* artifact — search the world's parameter space for a trajectory that
   violates a property the fixed scenarios miss. This attacks §7.4 directly and
   is the honest response to "verification only covers what you wrote".
4. **Formal treatment.** The controller program is a finite rule table over
   numeric comparisons. Several properties — activation latency under monotone
   heating, minimum hold, lease finiteness — look provable over the artifact
   directly, no simulation needed. That would move them from Tier 3 to Tier 1.
5. **Registry expressiveness curve.** The current registry has eight
   capabilities and three patterns. As it grows toward what real adaptation
   needs, at what point does `not_expressible` stop being the dominant defence?
   That curve is the honest limit of this whole approach and nobody has plotted
   it.
