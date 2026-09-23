# Dry Run — Research Notes (facts only)

Literature and systems scan done 2026-09-22/23 for the design. Numbers are the **authors' own claims**,
mostly from abstracts. Re-check prior art at weeks 0, 4 and 8, as the brief asks.
Measurements marked *(measured)* were taken on the dev box (WSL2 kernel 6.18.33, 12 vCPU, ext4).

This file holds **facts only**. Hypotheses and planned methods live as experiment cards in
[`research/cards/`](../research/cards/), run by the research loop (ARCHITECTURE §12). Deferred
engineering features are in the roadmap (ARCHITECTURE §13).

---

## 1. Positioning: what exists, and where Dry Run differs

Dry Run's claimed contribution is the **combination** of:
(a) shadow execution before commit,
(b) a judge over the observed effect,
(c) consent scope derived from the user's request,
(d) committing the reviewed diff instead of re-running the command.

| Work | What it does | Missing relative to Dry Run |
|---|---|---|
| **YoloFS** — "Don't Let AI Agents YOLO Your Files", arXiv [2604.13536](https://arxiv.org/abs/2604.13536), GPL-2.0, ships Claude Code PreToolUse hooks | Kernel filesystem that stages changes until approval; snapshots; path rules for allow/deny/ask | Approval by path rules plus a human; no effect-vs-request judge; kernel module; no network isolation. **Closest prior art; cite and compare.** |
| **Cordon** — arXiv [2606.17573](https://arxiv.org/abs/2606.17573) | "Semantic transactions": shadow state, an outbox for external effects, validation before commit | Works at the tool-RPC level, not on arbitrary Bash; validation is not tied to consent from the request |
| **Fault-Tolerant Sandboxing for AI Coding Agents** — arXiv [2512.12806](https://arxiv.org/abs/2512.12806) | Policy interception plus transactional snapshots; 100% rollback, ≈14.5% overhead | Rolls back after the fact; rules only |
| **pi-overlayfs** ([repo](https://github.com/JerryAZR/pi-overlayfs), Sep 2026) | Copy-on-write fork for each call; confirms staged changes **outside** the project root once per turn | Changes inside the project are auto-approved; no judge; admits it misses `find -delete` and `xargs rm` |
| **CARE** — arXiv [2607.21642](https://arxiv.org/abs/2607.21642), ISSRE'26, code MIT ([repo](https://github.com/prisma-research/CARE)) | Static: canonicalise → attribute → resolve, with an LLM judge only on uncertain cases. F1 85.64% at 0.91% FPR, 2.32 ms. Reduces realised harm on RedCode-gen **to** 37.33% | Never executes; no conversation context. **Baseline in sub-project 2**; the dataset is not released |
| Claude Code auto mode ([article](https://www.anthropic.com/engineering/claude-code-auto-mode)) | Two-stage classifier over user messages and tool calls | Judges text. 17% FNR on real over-eager actions, from **n=52** (FPR 0.4% on n=10k). Compare only on our own benchmark |
| agent-undo, hermes checkpoints, DeltaBox ([2605.22781](https://arxiv.org/abs/2605.22781)), doover, mgit, TClone ([2605.17320](https://arxiv.org/pdf/2605.17320)) | Snapshot and undo; mgit reviews whole tasks | Undo after the fact, or review at task granularity |
| Open Agent Passport ([2603.20953](https://arxiv.org/abs/2603.20953)), AgentGuard ([2609.16287](https://arxiv.org/abs/2609.16287)) | Deterministic or learned pre-action policies | Never execute |

**Evidence for judging effects rather than text:**

- **SafeClawBench** ([2606.18356](https://arxiv.org/abs/2606.18356)): 291 of 347 harms seen in the
  sandbox came from commands that passed text-level safety checks.
- **Cloak and Detonate** ([2607.02357](https://arxiv.org/abs/2607.02357)): obfuscation beats static
  scanners more than 90% of the time; running the code and watching behaviour catches 97% at 2% FPR.
- **ShellSieve** ([2606.15549](https://arxiv.org/abs/2606.15549)): 69–98.6% of 1,709 real denylists are
  fragile.
- **GuardFall** ([CSA note](https://labs.cloudsecurityalliance.org/research/csa-research-note-guardfall-ai-coding-agent-shell-injection/)):
  10 of 11 agents are vulnerable to five classes of rewriting — quote removal, `$IFS`, command
  substitution, encoded pipelines, alternative flags or binaries. Claude Code was not among those tested.
- [anthropics/claude-code#85274](https://github.com/anthropics/claude-code/issues/85274) is still open.

**Counter-evidence to respect:** [2509.11686](https://arxiv.org/abs/2509.11686) found that execution
traces added little when fine-tuning code LLMs. So the EffectRecord stays compact and fixed-format, and
the "text vs effect vs both" ablation has to *show* that the effect helps.

## 2. Facts from Claude Code's hooks docs that shaped the design

Source: [hooks reference](https://code.claude.com/docs/en/hooks), read 2026-09-22.

- **Fail-closed has to be built in.** A command/HTTP PreToolUse hook that times out or errors
  **does not block**. The hook client itself must emit `ask`.
- **`updatedInput` replaces the whole tool input.** Permission rules are evaluated against the
  rewritten input, and it can be paired with `allow` or `ask`. This is how we commit reviewed
  ChangeSets.
- **`allow` does not override the user's own `ask`/`deny` permission rules.** Document this in the
  install notes.
- **`defer` exists** but only works in `-p` mode with a single tool call.
- **Deny precedence** across hooks: deny > defer > ask > allow.
- **Transcript JSONL may lag** and has no documented schema. Capture the request with a
  `UserPromptSubmit` hook instead (its `prompt` field).
- **Default hook timeout is 600 s.** We set our own (60 s) and keep the internal deadline 2 s under it.
- **`classifierContext` (PostToolUse, v2.1.236+)** can hand observed effects to auto mode's classifier.
  See roadmap R7 (ARCHITECTURE §13).
- **VERIFY in spike 0:** does Claude Code re-run PreToolUse hooks on `updatedInput`? The design works
  either way, since the daemon accepts its own token.

## 3. Systems facts (measured or cited)

- **Unprivileged overlay:** `userxattr` forces `redirect_dir=nofollow` and `metacopy=off` and turns off
  `index`.
  - Hard links break on copy-up *(measured)*.
  - Directory rename returns EXDEV, and `mv` falls back to copy then delete.
  - chmod, touch and `open(O_RDWR)` copy up unchanged files, which is noise to filter.
  - Kernel ≥ 6.7 also uses xattr whiteouts, so both whiteout forms must be handled.
  - Changing the lower dir while it is mounted gives undefined behaviour, so re-fingerprint after the
    shadow run.
- **bwrap overlay flags** need ≥ 0.11.0; Ubuntu 22.04 ships 0.6.1 and 24.04 ships 0.9.0, so vendor a
  static build (as Codex does).
- **Latency** *(measured)*:

  | Operation | Time |
  |---|---|
  | unshare + overlay mount | 7 ms |
  | fork+exec | 0.74 ms |
  | upper-dir walk, 22k entries | 16 ms |
  | fingerprint, 20k files | 55 ms |
  | strace `--seccomp-bpf` on execve/connect | +3% |
  | tracing every `openat` | 3.5× |
  | plain ptrace | 23× |
  | copy-up of a 1 GiB file on a 1-byte append | 1.9 s |
  | worst-case write-heavy workload | **1.65×** native |

- **Overhead vs the 1.5× target.** The worst case exceeds it, and must be reported honestly. What
  drives it is an open question, tested by cards C-0001–C-0003.
  Mitigations: R3 (cache); running the shadow at low priority; shadowing only uncertain commands if
  typical sessions exceed 1.5× (the brief's stop condition).
- **WSL2 facts** *(measured)*:
  - systemd user manager running; cgroup v2 controllers include memory, pids, cpu, io.
  - Home, `/tmp` and `/` are on one ext4 filesystem.
  - Host sockets are exposed under `/run` (D-Bus, journal, udev, snapd) and in `/tmp/.X11-unix` and
    `/mnt/wslg`. These motivated isolation rules I2 and I12.
- **Anthropic sandbox-runtime** ([srt](https://github.com/anthropic-experimental/sandbox-runtime),
  Apache-2.0) has proxy, allowlist and AF_UNIX-blocking seccomp to reuse. It has **no overlay or
  copy-on-write mode**.


## 4. Citation index (sources already found, keyed to cards)

This is not a reading list. A paper is read and extracted into the five fields (Mechanism /
Assumptions / Evidence / Smallest adaptation / Falsification) **only when** an experiment raises a
question that it might answer (research loop rule L6).

| Card | Sources on file |
|---|---|
| C-0004 effect vs text | SafeClawBench [2606.18356](https://arxiv.org/abs/2606.18356); Cloak and Detonate [2607.02357](https://arxiv.org/abs/2607.02357); counter-evidence [2509.11686](https://arxiv.org/abs/2509.11686); CoT Red-Handed [2505.23575](https://arxiv.org/abs/2505.23575) |
| C-0005 SFT + threshold | calibration of guard models [2410.10414](https://arxiv.org/abs/2410.10414); Learn-then-Test [2110.01052](https://arxiv.org/abs/2110.01052); Conformal Risk Control [2208.02814](https://arxiv.org/abs/2208.02814); per-clause guarantees [2607.24343](https://arxiv.org/abs/2607.24343); limits of certification [2606.29054](https://arxiv.org/abs/2606.29054); single-pass decision read from label-token probabilities [2509.21487](https://arxiv.org/abs/2509.21487), [2510.21049](https://arxiv.org/abs/2510.21049), [2607.21401](https://arxiv.org/abs/2607.21401); base models Qwen3Guard [2510.14276](https://arxiv.org/abs/2510.14276), LFM2 [2511.23404](https://arxiv.org/abs/2511.23404) |
| C-0006 cost-weighted CE | none yet; derived from decision theory. Search only if C-0005 shows a ranking gap |
| C-0007 GRPO | Dr. GRPO [2503.20783](https://arxiv.org/abs/2503.20783); DAPO [2503.14476](https://arxiv.org/abs/2503.14476); Balance-GRPO in StepGuard [2608.24777](https://arxiv.org/abs/2608.24777); overconfidence after RL [2601.13284](https://arxiv.org/abs/2601.13284) |
| C-0008 attacker LoRA | ABS [2605.08427](https://arxiv.org/abs/2605.08427); Self-RedTeam [2506.07468](https://arxiv.org/abs/2506.07468); ShellForge [2607.07191](https://arxiv.org/abs/2607.07191) |
| Benchmark design (sub-project 2 spec) | matched twins: StepGuard [2608.24777](https://arxiv.org/abs/2608.24777), AuraGen [2510.09781](https://arxiv.org/abs/2510.09781); held-out generators RoSE [2510.06143](https://arxiv.org/abs/2510.06143); label verification BARRED [2604.25203](https://arxiv.org/abs/2604.25203); multi-turn MT-AgentRisk [2602.13379](https://arxiv.org/abs/2602.13379); external sets OverEager-Gen [2605.18583](https://arxiv.org/abs/2605.18583), SABER [2606.01317](https://arxiv.org/abs/2606.01317), RedCode (CC BY 4.0 data), NL2Bash (MIT data), Atomic Red Team (MIT); error bars [2411.00640](https://arxiv.org/abs/2411.00640); baselines AgentDoG-1.5 [2605.29801](https://arxiv.org/abs/2605.29801), Qwen3Guard-Gen-0.6B, CARE |

## 5. Measured on the v0.1 implementation (2026-09-23, WSL2 6.18, 12 vCPU)

Raw rows are in `research/memory/ledger.jsonl`.

- **N1: hook latency on non-shadowed commands** (read-only and non-shadowable, including Python
  start-up): p50 **87 ms**, p95 **101 ms** over 250 calls. Target p50 < 300 ms and p95 < 500 ms: **met**.
- **C-0003: short commands on a 20k-file git repo** (30 commands, median native 3 ms):
  - median added latency **566 ms**;
  - fixed stages make up **87%** of it (prepare 211 ms, post-run 270 ms), the sandboxed run only 82 ms.
  - The N2 ratio target (≤ 1.5×) is **not met** for millisecond commands, and a ratio is the wrong metric
    for them. Follow-up cards C-0009 to C-0011 separate three causes: fingerprint walks, sandboxed git
    queries, and the `/tmp` copy.
- **Background `git gc --auto` races the shadow.** Committing 20k files started a detached auto-gc that
  repacked loose objects while a later command was being shadowed. The real worktree was untouched, but
  the lower layer changed mid-run, and `lower_changed` exists to catch exactly that (ask + rerun). In real
  sessions this will cause occasional false asks right after large commits. Worth counting in the decision
  log.
- **Fidelity:** 22 named scenarios and about 100 hypothesis-generated operation sequences produced the
  same tree as a real run.
  - Checked: content, type, mode and symlink target, plus the reviewed mtime on committed files.
  - `.git/index` is compared semantically, because it caches device/inode numbers.
  - One real bug was found and fixed: a file replaced by a directory was wrongly treated as a conflict.
