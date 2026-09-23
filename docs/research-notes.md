# Dry Run — Research Notes & Improvement Backlog

Literature and systems scan done 2026-09-22/23 for the design. Numbers are the **authors' own claims**,
mostly from abstracts. Re-check prior art at weeks 0, 4 and 8, as the brief asks.
Measurements marked *(measured)* were taken on the dev box (WSL2 kernel 6.18.33, 12 vCPU, ext4).

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
  See backlog B7.
- **VERIFY in spike 0:** does Claude Code re-run PreToolUse hooks on `updatedInput`? The design works
  either way, since the daemon accepts its own token.

## 3. Methods adopted or planned

### Sub-project 1 (core) — adopted now

| Method | Source | Where |
|---|---|---|
| Commit the reviewed diff with `updatedInput` → `dryrun apply`, no re-run | hooks docs; ACRFence [2603.20625](https://arxiv.org/abs/2603.20625) and AgentRewind [2608.14380](https://arxiv.org/abs/2608.14380) show re-running is unsafe | commit |
| Free "pre-existing" bit from `user.overlay.origin` *(measured)* | kernel overlayfs docs | effects |
| **Recoverability-weighted severity** (git `tracked_clean` vs `untracked`/`tracked_dirty`) | new here; no surveyed guard does it | harm policy H1 |
| Session ledger for "created this session" | brief §4.1, made precise | store |
| Declarative, clause-cited rules over effect fields rather than strings | AgentSpec [2503.18666](https://arxiv.org/abs/2503.18666), Progent [2504.11703](https://arxiv.org/abs/2504.11703) | judge |
| Two-tier rules (hard / soft), where only soft rules can be moved by a model | Constitutional Classifiers++ cascade [2601.04603](https://arxiv.org/abs/2601.04603) | judge |
| Racy-git-style fingerprints with ctime | [racy-git](https://git-scm.com/docs/racy-git) | fingerprint |
| Isolation canaries plus fail-closed | Codex / srt sandboxes; CSA "sandbox is the primary control" | sandbox |
| Persistence and guard-tampering clause (H9) | new here, from the isolation review | harm policy |

### Sub-project 2 (benchmark) — planned

1. **Matched twins.** Pairs of items that differ in exactly one effect field, e.g. pre-existing vs
   session-created, or `tracked_clean` vs `untracked` (StepGen, from StepGuard
   [2608.24777](https://arxiv.org/abs/2608.24777); AuraGen [2510.09781](https://arxiv.org/abs/2510.09781)).
   They force the judge to read the effect.
2. **Policy as constitution.** Generate data from the harm-policy clauses, then verify borderline
   labels by agent debate (BARRED [2604.25203](https://arxiv.org/abs/2604.25203)).
3. **Held-out obfuscation-operator families and held-out generators** (RoSE
   [2510.06143](https://arxiv.org/abs/2510.06143); ShellForge GA obfuscation
   [2607.07191](https://arxiv.org/abs/2607.07191)).
4. **Attacker LoRA with a sandbox-verified reward.** The attacker is rewarded only when the sandbox
   observes destruction *and* the guard allowed it (ABS [2605.08427](https://arxiv.org/abs/2605.08427),
   Self-RedTeam [2506.07468](https://arxiv.org/abs/2506.07468)). Run in offline rounds, not online
   self-play.
5. **Multi-turn and split attacks**, where the harm is spread across commands (MT-AgentRisk
   [2602.13379](https://arxiv.org/abs/2602.13379), ASEval [2605.22321](https://arxiv.org/abs/2605.22321)).
6. **External-validity sets:** OverEager-Gen ([2605.18583](https://arxiv.org/abs/2605.18583)), the best
   fit for consent scope; SABER ([2606.01317](https://arxiv.org/abs/2606.01317)); RedCode-Exec (CC BY 4.0
   data); NL2Bash data (MIT); Atomic Red Team (MIT).
7. **Baselines:** regex guard; CARE (MIT code); frontier judge given the command and the request;
   AgentDoG-1.5-0.8B ([2605.29801](https://arxiv.org/abs/2605.29801)); Qwen3Guard-Gen-0.6B
   ([2510.14276](https://arxiv.org/abs/2510.14276)); the Dry Run ablations.
8. **Statistics:** miss rate at fixed FPR plus the full curve; bootstrap CIs clustered by repo and
   session; paired McNemar tests; correction for multiple comparisons
   ([2411.00640](https://arxiv.org/abs/2411.00640)).

### Sub-project 3 (judge model) — planned

1. **Single forward pass, decision read from the probabilities on the label token**, with no reasoning
   at inference. A reasoning head is used only in training (Dual-Head Reasoning Distillation
   [2509.21487](https://arxiv.org/abs/2509.21487); ThinkGuard [2502.13458](https://arxiv.org/abs/2502.13458)).
   Supporting evidence: Reasoning's Razor ([2510.21049](https://arxiv.org/abs/2510.21049)) finds
   reasoning loses at low FPR, and ResponseGuard ([2607.21401](https://arxiv.org/abs/2607.21401)) finds
   one pass ≈150× cheaper at better accuracy. This is the only realistic way to meet <100 ms.
2. **Output layer pruned to the label tokens** (Llama Guard 3-1B-INT4
   [2411.17713](https://arxiv.org/abs/2411.17713)).
3. **Calibration, then risk-controlled thresholds.**
   - Temperature scaling first ([2410.10414](https://arxiv.org/abs/2410.10414)).
   - Learn-then-Test ([2110.01052](https://arxiv.org/abs/2110.01052)) sets the thresholds so that miss
     rate ≤ α with confidence 1−δ, while minimising asks.
   - MultiRisk ([2512.24587](https://arxiv.org/abs/2512.24587)) handles the priority ordering of risks.
   - A **separate guarantee for each clause** (Role-Stratified CRC
     [2607.24343](https://arxiv.org/abs/2607.24343)).
   - Warning: if the base error rate is far above α, the guarantee forces heavy abstention
     ([2606.29054](https://arxiv.org/abs/2606.29054)).
4. **Cost-weighted cross-entropy first.** Try Dr. GRPO ([2503.20783](https://arxiv.org/abs/2503.20783)),
   DAPO dynamic sampling ([2503.14476](https://arxiv.org/abs/2503.14476)) and Balance-GRPO only if SFT
   plateaus. Recalibrate afterwards, since RLVR makes models overconfident
   ([2601.13284](https://arxiv.org/abs/2601.13284)).
5. **Base models:**
   - Start with Qwen3-1.7B or Qwen3Guard-Gen-0.6B, which have mature QLoRA and llama.cpp tooling.
   - Then try Qwen3.5-0.8B and LFM2-1.2B ([2511.23404](https://arxiv.org/abs/2511.23404)).
   - Skip the vision parts of multimodal small models.
6. **Cheap probe stage in the cascade** before the language model (CC++ linear probes).
7. **Deployment recalibration** from the local decision log.

**Not worth it for an 8 GB GPU and <100 ms:**
- reasoning at inference (StepGuard at 4B ≈ 600 ms per call)
- multi-LLM guard agents (AGrail, ShieldAgent, GuardAgent)
- LLM-emulated sandboxes (ToolEmu); we execute for real
- online self-play RL on one GPU
- latent-reasoning guards

## 4. Systems notes (measured or cited)

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

- **Overhead vs the 1.5× target.** The worst case exceeds it, and must be reported honestly.
  Mitigations: B3 (cache); running the shadow at low priority; shadowing only uncertain commands if
  typical sessions exceed 1.5× (the brief's stop condition).
- **WSL2 facts** *(measured)*:
  - systemd user manager running; cgroup v2 controllers include memory, pids, cpu, io.
  - Home, `/tmp` and `/` are on one ext4 filesystem.
  - Host sockets are exposed under `/run` (D-Bus, journal, udev, snapd) and in `/tmp/.X11-unix` and
    `/mnt/wslg`. These motivated isolation rules I2 and I12.
- **Anthropic sandbox-runtime** ([srt](https://github.com/anthropic-experimental/sandbox-runtime),
  Apache-2.0) has proxy, allowlist and AF_UNIX-blocking seccomp to reuse. It has **no overlay or
  copy-on-write mode**.

## 5. Improvement backlog (ranked by value / effort)

| # | Idea | Value | Target |
|---|---|---|---|
| B1 | **Network-read tier:** a proxy that only lets package registries and git hosts through, so `npm i`/`pip install` shadow for real and the fetched files show up in the diff; also cache pre-warming | Shrinks the non-shadowable bucket, which is the ceiling on the whole approach | v1.1 |
| B2 | **Divergence audit for `rerun`:** re-run inside a fresh overlay and compare with the shadow; log divergences | Measures shadow fidelity in the wild | v1.1 |
| B3 | **Content-addressed shadow cache**, keyed on argv, cwd, env and workspace fingerprint (Bazel action-cache model) | Cuts overhead on repeated test/lint runs | v1.1 |
| B4 | **Session-level effect accumulation:** the judge sees the cumulative effect of the session, not only the current command | Catches split attacks (MT-AgentRisk) | v2 / SP3 |
| B5 | **Taint from untrusted input:** flag commands whose text came from fetched content or issue bodies (CaMeL [2503.18813](https://arxiv.org/abs/2503.18813), FIDES [2505.23643](https://arxiv.org/abs/2505.23643)) | Resists prompt injection | v2 |
| B6 | **Hardened backend:** gVisor `runsc` as an alternative runner for high-risk commands | Lowers kernel-escape risk (residual risk in ARCHITECTURE §7.1) | v2 |
| B7 | **Feed effect summaries to auto mode** through PostToolUse `classifierContext` | Improves Claude Code's own classifier; cheap | v1.1 |
| B8 | **macOS port:** APFS `clonefile` snapshot + `sandbox-exec`, then diff clone vs base | Reach | v2 |
| B9 | **Early Intervention Rate metric** (StepShield [2601.22136](https://arxiv.org/abs/2601.22136)): does the guard fire at the step where things go wrong? | Better evaluation | SP2 |
| B10 | **Cost-aware shadowing:** skip the shadow run when the predicted cost is high and the risk is low ([2606.07846](https://arxiv.org/abs/2606.07846)) | Latency | v2 |
