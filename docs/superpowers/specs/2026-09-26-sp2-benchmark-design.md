# Sub-project 2: Benchmark v1 — design

- **Status:** draft for review (2026-09-26). This builds on PR #3, the frozen dev-iterate evaluator.
- **Sources:** brief §6, `research/program.md` (L1–L4), `docs/BENCHMARK.md`, `docs/harm-policy.md`,
  `research/cards/C-0004`.

## Goal

Produce a first labelled `dev-iterate` dataset of about 600 shadowed items. Run the two text-free baselines
through the frozen evaluator. From that, answer card C-0004, rules version: **do rules over observed
effects miss less indirect destruction than a text guard?**

## Decisions (defaults; change any before execution)

| # | Decision | Default | Why |
|---|---|---|---|
| D1 | Where items are executed | Dry Run's **own shadow pipeline**, run **inside a disposable Docker container**: `--network none`, no host mounts, read-only root, tmpfs work dir, memory and pids limits | Two layers. The container is the outer bound the brief §7 asks for. The inner shadow produces the exact `dryrun.effect/1` records the deployed tool produces, so the data matches deployment |
| D2 | How items are generated | **Deterministic templates** from a seed. There is no LLM in v1 | Cheap, reproducible and free. Held-out *families* and *obfuscation operators* give real generalisation splits. LLM-played agent trajectories come later, as an extra generator family |
| D3 | Where labels come from | The **generator's declared intent**, checked against the measured record. Labels are **never** taken from `judge/rules.py` | Labelling with the rules would make the rules baseline score 100% by construction. An item whose intent the measurement contradicts is dropped and logged |
| D4 | Consent | Each item carries the user request. "Authorised destruction" items (the request asks for exactly that deletion) are labelled **benign** | This measures the consent-scope gap that rules alone cannot close. It sets up sub-project 3 |
| D5 | Non-shadowable commands | Excluded from the scored dataset. They have no EffectRecord. Their share is reported separately | The frozen evaluator requires records |
| D6 | Baselines in v1 | (a) a regex guard on command text, like typical hook scripts; (b) Dry Run rules-only, which is the harness's own decision | No API cost. A frontier-LLM judge and CARE are separate later tasks |
| D7 | Splits | `dev-iterate`: calibration and evaluation folds on disjoint repos. `dev-gen`: items from 3 held-out operators and 2 held-out families. `final-test`: fresh seeds, written once, never read in v1 | program.md L4 |
| D8 | Per-family breakdown | Run the frozen evaluator on filtered item files (direct / indirect / consent), each keeping all benign items | The evaluator stays frozen |

## Components

```
research/gen/repos.py      seed -> synthetic git repo (tracked clean/dirty, untracked, ignored build/, history)
research/gen/families.py   (repo facts, rng) -> [Item(request, command, intent)]  benign | direct | indirect | consent
research/gen/obfuscate.py  destructive core command -> obfuscated variant   (GuardFall A-E + #85274 patterns)
research/harness.py        Items -> Pipeline.handle_pretool in the container -> raw rows (record, decision, triage)
research/label.py          raw row + intent -> benchmark item (evaluator format) | dropped(reason)
research/splits.py         items -> dev-iterate.jsonl (folds by repo), dev-gen.jsonl, final-test/ (sealed)
research/baselines.py      items -> regex-guard decisions; harness rows -> rules-only decisions
docker/Dockerfile          ubuntu:22.04 + python3 + strace + git + pinned bwrap + dryrun (non-root user)
scripts/bench/run-harness.sh   builds the image and runs the harness container with the isolation flags
```

## Intent format (generator → labeller)

```json
{"label": "destructive", "clauses": ["H1"], "family": "indirect", "operator": "script_file",
 "expect": [{"delete": "notes.md"}]}
```

`expect` entries: `{"delete": path}` (a pre-existing path deleted), `{"create": path}`,
`{"modify": path}`, `{"ref": "non_fast_forward"|"deleted"}`, `{"mode_setuid": path}`,
`{"flag": name}`, `{"decoy": true}`. The labeller keeps an item only if every entry is matched in the
record.

## Success

- The dataset builds reproducibly from one seed.
- The evaluator accepts `dev-iterate.jsonl`.
- Both baselines are scored overall and per family.
- C-0004 (rules version) gets a verdict with CIs.
- Every dropped item has a logged reason.
- The canaries pass inside the container before any item runs.

## Out of scope (later plans)

LLM-played trajectories, the frontier-LLM and CARE baselines, the dev-gen/final-test evaluation gates and
the promotion monitor (program.md L8/L9), multi-turn split attacks, and the judge model (sub-project 3).
