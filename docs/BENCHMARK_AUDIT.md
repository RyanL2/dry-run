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
