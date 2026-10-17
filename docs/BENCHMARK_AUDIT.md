# Synthetic judge benchmark audit (2026-09-26)

The 76.9-second SFT run used 160 `train` items and two epochs (320 optimizer steps). Its frozen
`dev-iterate` evaluation reported 0/80 destructive misses, 0/80 benign escalations, and partial AUC
1.0. Those numbers measure performance on repeated templates, not generalization to new tasks.

## Why the result is easy

The generator creates four fixed case recipes per repository. Repository IDs change across splits,
but the model does not receive those IDs. Its input is the EffectRecord's filesystem effects, git,
network, flags, summary, and request text. Across the 160 training items there are only **five
distinct model prompts**. All **160/160 evaluation prompts occur verbatim in training**. The
calibration fold also has the same five prompts.

The four request wordings alone perfectly separate the 160 evaluation labels: the two benign
requests always ask to clean or create a build artifact, while the two destructive requests always
ask to read or list without changing files. A rule that marks a workspace effect destructive when
its request contains `without changing` or `without modifying` gets **160/160** evaluation labels
right. The EffectRecord also exposes the policy signals used by the synthetic label checker, such as
preexisting deletion, build-output status, and the protected `.envrc` path. The label field itself is
not included in the model prompt; the repeated features and wording are the problem.

As an ablation, the pinned *untrained* Qwen2.5-0.5B-Instruct base model was scored with the same
prompt, calibration fold, and frozen evaluator. It missed **40/80** destructive cases at **0/80**
benign escalations (partial AUC **0.5**). The trained adapter reached 0/80 and partial AUC 1.0.
This confirms the short fine-tune learned these few mappings; it does not establish an improvement
over production effect rules, which also missed 0/80 on this set.

The local, ignored run artifacts are under `research/generated/`: `base.scores.jsonl`,
`base.audit.json`, `sft.scores.jsonl`, and `sft.evaluation.json`. The adapter metadata is at
`judge/checkpoints/sft/dryrun_training.json`.

## Public corpora downloaded for an external check

The following official data files were downloaded into the ignored
`research/generated/external/` directory. `download-manifest.json` records their source URLs,
repository tree hashes, sizes, and SHA-256 hashes. Each file was checked against its Git blob hash.
No downloaded command or program was executed.

| Corpus | Local subset | Content | Direct effect-judge evaluation? |
|---|---:|---|---|
| [RedCode-Exec](https://github.com/AI-secure/RedCode) | 600 Bash entries, 598 distinct code strings | Risky code and prompts, dataset licensed CC BY 4.0 | No measured `dryrun.effect/1` records or matched benign class |
| [NL2Bash](https://github.com/TellinaTool/nl2bash) | 12,607 request–command pairs, 10,624 distinct commands | Natural-language Bash corpus, `data/bash` licensed MIT | No policy labels or measured effects |

These corpora are useful sources for new command families and task wording. They cannot honestly be
passed to this model or the frozen evaluator as published: the model requires an observed EffectRecord
and the evaluator requires policy labels. Any execution of the risky corpus must use the isolated,
disposable environment required by the [project brief](../../dry-run-brief.md); static inspection
alone does not produce effects.

## Next valid comparison

Create cases with varied repository states, filenames, requests, and effect patterns; split by
scenario template and generator family before training. Include pairs where the same operation is
benign or destructive depending on consent and path context. Audit exact model-prompt overlap before
scoring. Keep `dev-gen` for a promotion attempt and `final-test` for the committed analysis plan, as
specified in the [research program](../research/program.md). Re-run the frozen evaluator on the
new development set with both rules and model arms at their measured benign ask rates.

## Follow-up: consent-scoped generator (2026-10-17)

The generator was rebuilt along the lines above (see [BENCHMARK.md](BENCHMARK.md#generated-cases-and-local-baselines)).
Every case declares the consent scope its request implies, and every request kind is benign or
destructive with equal probability. Templates, vocabulary, directories, protected paths, and command
families are disjoint across splits. H8 labels come from the declared scope, not from wording.

The 40-repository-per-fold run produced 320 train, 640 dev-iterate, and 320 dev-gen cases. All 1,280
cases matched their predicted effects in the real shadow, and all passed the label checker.
`research.overlap` found **0** evaluation prompts verbatim in training for both dev-iterate (583
distinct prompts) and dev-gen (286). The best one-word request rule reached 53%.

The plain SFT reference arm used the same config and seed, with 640 optimizer steps, 4.1 GPU-minutes,
and 1.6 GB peak VRAM. Its threshold was fitted on the calibration fold. Results on the dev-iterate
evaluation fold (169 destructive, 151 benign):

| Arm | Missed | Benign asked | H1 missed | H8 missed | H9 missed |
|---|---:|---:|---:|---:|---:|
| Direct-command text guard | 159 | 7 | 64/71 | 140/150 | 35/37 |
| Production effect rules | 61 | 0 | 0/71 | 61/150 | 0/37 |
| SFT judge, threshold at ask ≤ 5% | 54 | 0 | 35/71 | 41/150 | 0/37 |

The SFT judge had partial AUC 0.746 and p95 latency 89 ms. Its paired repo-clustered miss delta against
effect rules was -0.04 (95% CI -0.15 to +0.07). This is **within noise**, so the judge alone does not
beat the rules on this set. The two arms fail differently. The rules miss only H8 consent mismatches,
which v1 rules do not judge. The judge misses half the H1 destruction that the rules always catch. The
natural next card is a cascade that keeps the rules' hard H1/H9 verdicts and uses the judge only for
H8. Dev-gen has not been read.

Local, ignored artifacts: `research/generated/sft-v2.scores.jsonl`, `sft-v2.metadata.json`,
`sft-v2.evaluation.json`, and the adapter at `judge/checkpoints/sft-v2`.
