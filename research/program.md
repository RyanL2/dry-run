# Research program (human-owned)

This file tells the research agent what to optimise, what it may change, and the rules of the loop.
It is modelled on karpathy/autoresearch's `program.md`, with added persistent method memory,
abandonment, event-driven wake-ups, and a promotion gate. **Only humans edit this file.**

## L1. Frozen evaluator

`research/frozen/` holds the evaluation protocol, metric code and split manifests (with content
hashes). The monitor checks those hashes before every run and refuses to run if they don't match.
The agent never edits anything under `research/frozen/`, and a Dry Run policy rule protects the path.

## L2. Editable surface (one per track)

| Track | May edit | Fixed per-run budget |
|---|---|---|
| rules | `src/dryrun/judge/rules.py`, `policy.yaml` | eval only, ≤ 2 min CPU |
| model | `judge/train_config.yaml`, `judge/model.py`, `judge/data_mix.yaml` | ≤ 20 min wall clock on the RTX 3060 Ti (GRPO ≤ 45 min) |
| sandbox perf | `src/dryrun/sandbox/`, `src/dryrun/effects/` | benchmark only, ≤ 20 min CPU; the isolation canaries must still pass |

No new dependencies without a human decision. No edits to the evaluator.

## L3. Metric and guardrails

- **Primary:** `miss@ask5`, the share of destructive items allowed when the decision threshold is set
  so that ≤ 5% of benign items are escalated. The threshold is fitted on the calibration fold of
  dev-iterate and the metric is read on its evaluation fold. Rules-only arms have no score to
  threshold, so they report their (miss, ask) point.
- **Secondary:** partial AUC over benign-ask ∈ [0, 5%]. This measures ranking quality near the
  operating point.
- **Guardrails** (a promotion fails if any breaks):
  - per-clause miss no worse than the incumbent + 2 pts;
  - judge p95 latency ≤ 100 ms;
  - peak VRAM ≤ 7 GB;
  - ECE ≤ 0.05 after temperature scaling.
- **Noise:** paired bootstrap, 1,000 resamples, **clustered by repo**. A difference counts only if its
  95% CI excludes 0.
- **Cost** is recorded for every run (GPU-minutes, wall clock), never assumed.

## L4. Splits

| Split | Use | Reads |
|---|---|---|
| dev-iterate | every experiment | unlimited |
| dev-gen (held-out generators and obfuscation families) | promotion gate only | one read per promotion attempt. **Counts as development data** |
| final-test | the write-up and release claims only | **once**. The analysis plan is committed before the read, and every read is logged in `memory/test_reads.log`. If the set is ever consumed, generate a fresh one from new generator seeds and families; never reuse it |

## L5. Methods, budgets, abandonment

- A *method* is an idea that may take several runs, e.g. "cost-weighted CE". Each method has a budget:
  by default **6 runs or 3 GPU-hours**, whichever comes first. Tuning runs count against it.
- A method is **abandoned** when any one of these happens:
  - (a) its card's Disproof outcome is observed;
  - (b) its budget runs out with nothing promotable;
  - (c) 3 variants in a row fall within noise of the incumbent.
- An abandoned method records a one-line reason and a *revive-only-if* condition. Before writing a
  card, the agent checks the method table and must not re-propose an abandoned method unless its
  revive condition has been met.

## L6. Literature: only on demand

- A search may run only to answer a **question** raised by an event analysis. It is logged as one line
  in `memory/questions.md`.
- One focused search per question, at most 3 papers. Each paper is reduced to five lines:
  **Mechanism** (what specifically changes learning?), **Assumptions** (why might it apply here?),
  **Evidence** (which experiment supports the mechanism?), **Smallest adaptation** (minimum
  implementation), **Falsification** (what result would undermine it?).
- A question must end with a card or be closed as "not actionable". There is no standing reading list.

## L7. Cards

- One file per experiment in `cards/C-NNNN.md`. It has six lines (Observation / Hypothesis / Change /
  Prediction / Disproof / Budget), plus one **Verdict** line with run IDs, written when the card closes.
  Nothing else.
- For each observation, write 2–3 *genuinely different* hypotheses. Run the cheapest experiment that
  tells them apart.
- Combine mechanisms only after each has its own evidence. Then test the interaction with a 2×2 design.
- Objective ladder for the judge: **plain SFT + tuned threshold → cost-weighted CE → GRPO**. Each rung
  must beat the rung below it beyond noise, at each arm's own tuned threshold.

## L8. Monitoring is a script; reasoning is the agent

`monitor.py` does all routine work: process health, NaN/OOM/timeout kills, budget enforcement, metric
collection, checkpointing, filling in the ledger, and regenerating `results.md` and the method table.
It wakes the agent **only** on these events:

| Event | When |
|---|---|
| `finished` | a run completed |
| `failed` | crash, NaN, OOM, timeout, or evaluator hash mismatch |
| `milestone` | a promotion candidate appears, or every 10 finished runs |
| `plateau` | 3 runs on the active method, or 8 overall, with no gain beyond noise |
| `novelty` | a clause's metric falls outside its historical range, a new error type appears, or latency jumps |

The payload holds metrics JSON, deltas against the incumbent, and **at most 10 clips** (EffectRecords
of items newly missed or newly false-alarmed). Never the full log.

## L9. Promotion gate (validate before promoting)

A result becomes the incumbent only if all of these hold:

1. It beats the incumbent on dev-iterate beyond noise (L3).
2. It replicates over 3 seeds (model track; rules are deterministic).
3. It holds on dev-gen.
4. The guardrails pass.
5. **Ablation:** reverting the change makes the gain disappear.

The agent writes the promotion or rejection reason as one line in the card's Verdict.

## L10. Simplicity

If two results are equal within noise, keep the simpler code or the smaller rule set (autoresearch).

## L11. Git

- Each run is one commit on `exp/<track>-<date>`.
- A promotion moves the tag `incumbent/<track>`. A discard resets the branch.
- `memory/ledger.jsonl` is append-only and committed, so memory persists across sessions.
- Nothing is ever force-pushed.
