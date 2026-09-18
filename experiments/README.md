# Experiment harness

One command compares three adaptation pipelines across the same scenarios,
seeds and proposal intents:

```sh
python -m experiments.run_experiments
python -m experiments.plot_results     # optional; needs matplotlib
```

Results land in `results/`: `runs.json` / `runs.csv` (one row per trial) and
`summary.json` / `summary.csv` (one row per arm), plus PNGs if you ran the
plotting step. No API key, no Docker and no network are needed.

## The arms

| Arm | Static gate | Behavioural gate | Actuator owner | Lease | Local rollback | OTA authentication |
|---|---|---|---|---|---|---|
| `source_unrestricted` | none | none | the generated firmware | none | none | content hash only |
| `source_guarded` | Guard Rail (real, v0.1) | none | the generated firmware | none | none | content hash only |
| `manifest_compiler` | validator | closed-loop verifier | the immutable supervisor | local, bounded | A/B + probation | HMAC + sequence + base hash |

## What is genuinely being compared, and what is not

**The two baselines are separable.** Guard Rail is an independent component with
its own module and tests; the unrestricted arm simply does not call it. That is
a real difference, not a simulated one.

**The Docker sandbox stage is not in this harness.** The candidates here are
*step-shaped* — they define `step(observation) -> list[intent]` so they can be
driven tick by tick against the same virtual world as the compiled controllers.
The v0.1 sandbox runs a whole-file firmware as a process; running one against
the other would measure nothing. It is recorded as `not_applicable` rather than
counted as a pass. The whole-file path with the real sandbox is covered by
`tests/test_e2e_scenarios.py`, which needs Docker.

**The step-shape is a deviation and is recorded as one.** What it preserves is
what the comparison is about: the baseline candidate is still model-authored
Python, it is still executed, and it still commands actuators directly with
nothing between it and the hardware. What it gives up is the process lifecycle.

**Proposals come from deterministic stubs, not a live model.** Both arms are
driven from the same distribution of *intents* (`experiments/intents.py`), each
rendered in the arm's own language. A live-model proposal step exists in
`server/agent/manifest_agent.py` and is exercised against a scripted fake in the
test suite; it has not been run against a real model in this repository, and
`--llm` says so rather than pretending otherwise.

## Intents

Every arm is asked to express the same eight intents — one sound, seven flawed.
Several have **no rendering in the manifest language at all**: there is no field
in which to name a pin, none in which to skip provenance for one, and no way to
ask for an actuator transition every tick. Those are reported as
`not_expressible` and excluded from every rate, because "the arm rejected it"
and "the arm could not be asked" are different findings.

## Denominators

Every rate names its own, because the interesting ones are easy to inflate by
choosing a different one:

- `unsafe_acceptance_rate` — over unsafe proposals **made**, so an arm cannot
  improve it by being asked fewer bad questions.
- `false_rejection_rate` — over sound proposals made. The cost of strictness,
  which a table of safety numbers alone would hide.
- `sound_adaptation_success_rate` — over sound proposals: given a good proposal,
  did the arm end up with a working, safe, self-terminating adaptation?
- `cooling_effectiveness_rate` — did the deployed firmware command cooling at
  all, whatever else was wrong with it. Separated so "it cooled but never ended"
  is visible as the distinct failure it is.
- A rate over an empty denominator is reported as `null`, never as `0`.

## Reproducibility

Every trial is seeded. `--seeds 1 2 3 4 5` by default; the same seed produces
the same trace. The morph lease (`--lease`, default 40 simulated seconds) also
sets the observation horizon, so every arm is measured over the same window
whether or not its proposal was accepted.
