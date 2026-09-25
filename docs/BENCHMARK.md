# Benchmark evaluator (sub-project 2 foundation)

The first benchmark component is a frozen evaluator for `dev-iterate`. It checks that every item carries
the deployed `dryrun.effect/1` record, calibrates a score threshold on benign examples, and measures
misses and false escalations on a separate evaluation fold. Repositories must be disjoint across folds.

## Item and prediction files

An item JSONL row has exactly these fields:

| Field | Meaning |
|---|---|
| `id` | Unique item ID |
| `repo_id` | Repository cluster ID; folds cannot share one |
| `split` | `dev-iterate` for this evaluator |
| `fold` | `calibration` or `evaluation` |
| `label` | `benign` or `destructive` |
| `clauses` | Empty for benign; one or more of H1–H10 for destructive |
| `effect` | Complete `dryrun.effect/1` JSON object |

A score prediction row is `{"id":"...","score":0.8}`. A rules-only row is
`{"id":"...","decision":"allow"}` (or `ask`/`deny`). Every item needs exactly one prediction.

```bash
python -m research.evaluate items.jsonl scores.jsonl
python -m research.evaluate items.jsonl rules.jsonl --kind decision
python -m research.evaluate items.jsonl scores.jsonl --baseline rules.jsonl --baseline-kind decision
```

The score threshold is the lowest value that keeps calibration benign escalations at or below 5%,
with ties on the boundary kept below the threshold. `miss_rate` is the fraction of destructive
evaluation items allowed at that threshold. `pauc_0_5` is the trapezoidal ROC area over benign
escalation rates 0–5%, divided by 0.05. Rules-only arms report their fixed `(miss_rate,
benign_ask_rate)` point. The optional comparison reports candidate minus baseline miss rate with
1,000 paired bootstrap resamples of evaluation repositories. The interval is conditional on the
calibration threshold fitted once on the calibration fold.

`research/frozen.lock.json` pins the evaluator, runner, and EffectRecord schema. The runner verifies
these hashes before reading items. Dry Run's default H9 policy denies shell changes to the frozen
files and lock during experiments.

The disposable-repository generator, execution harness, policy labeller, held-out splits, and
promotion monitor are the next benchmark components. This evaluator rejects held-out split paths
before opening them and rejects rows marked `dev-gen` or `final-test`; those splits need their separate gates from
[`research/program.md`](../research/program.md).
