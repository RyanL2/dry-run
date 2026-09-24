# Dry Run — Sub-project 1 (Core) Design Spec

- **Date:** 2026-09-22 (isolation requirements added 2026-09-23)
- **Status:** implemented as v0.1 (2026-09-23). Plan: `docs/superpowers/plans/2026-09-23-dry-run-core.md`.
  Known deviations from this text:
  1. **Scratch `$HOME` replaced.** It became a read-only real home with throwaway cache overlays (S11, spike 0).
  2. **H5 uses canary tokens instead of fanotify.**
  3. **CPU/IO** use `nice`/`ionice` rather than cgroup weights.
  4. **Seccomp** uses a socket-*family allowlist* (AF_INET, AF_INET6, AF_NETLINK) that also blocks AF_VSOCK.
  5. **N2 (≤ 1.5×)** is not met for short commands on large repos. The fixed pipeline cost is about
     0.5 s per shadowed command on a 20k-file repo (card C-0003); see `research/memory/ledger.jsonl`.
- **Architecture & diagrams:** [docs/ARCHITECTURE.md](../../ARCHITECTURE.md)
- **Harm policy:** [docs/harm-policy.md](../../harm-policy.md)
- **Research facts:** [docs/research-notes.md](../../research-notes.md) · **Research loop:** [research/program.md](../../../research/program.md), [cards](../../../research/cards/)
- **Source brief:** `dry-run-brief.md` (kept outside the repo). Where this spec and the brief differ,
  this spec wins; the differences are listed in §10.

## 1. Goal

Build a local gate for Claude Code's Bash tool. Before a command runs for real, the gate:

1. triages the command statically;
2. shadow-runs it in a copy-on-write overlay with no network, **with no effect on the real system**;
3. records the observed effect as a structured EffectRecord;
4. judges the effect with clause-cited rules (a model slot comes in sub-project 3);
5. returns allow, ask or deny, and on allow or approved-ask **commits exactly the reviewed
   ChangeSet** instead of re-running the command.

## 2. Scope

**In scope:**

- hook client
- daemon
- triage
- bwrap sandbox with the full isolation set (I1–I15)
- effects extractor
- git state
- rules judge with `NullJudge` model slot
- commit engine with journal and recovery
- store
- CLI: `install`, `doctor`, `apply`, `recover`, `status`
- JSON Schemas
- test suites: unit, fidelity, isolation canaries, latency
- README with a limits and liability statement

**Out of scope for sub-project 1** (each listed where it is planned):

- the benchmark and its data generator — sub-project 2
- model training and calibration — sub-project 3
- network-read tier, shadow cache, divergence audit — roadmap R1–R3 (ARCHITECTURE §13)
- the automated research loop (monitor, frozen evaluator, promotion gate) — start of sub-project 2 (ARCHITECTURE §12). Sub-project 1 uses cards and a ledger by hand for its sandbox-performance experiments (C-0001–C-0003)
- macOS and Windows-native support
- gVisor backend

## 3. Requirements

### 3.1 Functional

| ID | Requirement |
|---|---|
| F1 | `dryrun-hook prompt` stores `{session_id → latest prompt text}` in the daemon. It never blocks the prompt: on error it exits 0 with no output. |
| F2 | `dryrun-hook pretool` sends `{session_id, cwd, command, description, transcript_path, env (filtered), deadline_ms}` and prints a valid `hookSpecificOutput` JSON on stdout. It always exits 0. |
| F3 | Triage sorts commands into `read_only`, `git_read`, `apply`, `non_shadowable`, `long_running` or `shadow` (details in §4). A command that can't be parsed goes to `shadow`. |
| F4 | Shadow run, following the layout in ARCHITECTURE §7. Captures: exit code, wall time, stdout/stderr (full output kept for replay, a 4 KiB tail in the record), upper dirs, the strace log (execve/connect/sendto/sendmsg, with strace running outside bwrap) and canary-token hits from the decoys. |
| F5 | The effects extractor produces a `ChangeSet` (`dryrun.changeset/1`) and an `EffectRecord` (`dryrun.effect/1`). It follows the upper-dir rules table in ARCHITECTURE §7, filters out copy-ups that changed nothing, and handles both whiteout forms. |
| F6 | Git state: per-path recoverability and a before/after snapshot of refs (`git for-each-ref`, HEAD, stash list). Runs only when the workspace is a git repo. |
| F7 | The rules judge implements harm-policy clauses H1–H10 and T0–T6, as hard or soft tiers. It returns a verdict, the fired rule IDs with evidence, and a reason of at most 200 characters built from a template. |
| F8 | Decisions and modes follow the ARCHITECTURE §4 cascade exactly. |
| F9 | `allow + commit` and `ask + commit`: the hook returns `updatedInput` equal to the original tool input, but with `command = "dryrun apply <run_id> --token <t>"`. |
| F10 | `dryrun apply`: validate the token (single use, bound to the session and run), check fingerprints, journal, apply, fsync, verify, update the ledger, replay stdout/stderr, exit with the shadow's exit code. The failure exit codes are 3 (refused), 4 (conflict) and 5 (fidelity error). No partial state is ever left behind. |
| F11 | An agent-typed `dryrun apply …` seen by the pretool hook is **denied** (unless it carries a valid token, to cover the case where Claude Code re-runs hooks on `updatedInput`). |
| F12 | `dryrun recover` (also run on daemon start) replays journals that aren't marked done. Replay is idempotent. |
| F13 | `dryrun doctor` runs the I1–I15 canary suite and prints a pass/fail table. The daemon runs it at startup and every 6 h. Any failure disables shadowing: every pretool call gets `ask`. |
| F14 | `dryrun install` adds the hook entries (§6) to `~/.claude/settings.json` after showing a diff and getting confirmation. It also sets up the systemd user service for `dryrund`. `--uninstall` reverses both. |
| F15 | Local decision log: an append-only JSONL of `{ts, session_id, run_id, triage, record, verdict, mode, rule_ids, latency_ms}` in the state dir, with size-based rotation. No network I/O at all. |
| F16 | Run dirs are deleted once the decision is final. Pending runs expire after a TTL (default 15 min). |
| F17 | `dryrund` refuses to start as root (uid 0) unless `--allow-root` is given. Development and tests run as a dedicated non-root user (`scripts/dev/setup-wsl-user.sh`). |

### 3.2 Isolation (CRITICAL)

The shadow must not affect the real system. Treat every shadowed command as hostile.

| ID | Requirement |
|---|---|
| S1 | Implement every channel I1–I15 in ARCHITECTURE §7.1 with the stated mechanism. Each one has a canary test in `tests/isolation/` that must pass in CI and in `dryrun doctor`. |
| S2 | Exactly one function, `sandbox.spawn`, starts user commands. It refuses to run unless all of these succeed: bwrap ≥ 0.11.1 is present and its hash matches the vendored one; the seccomp filter compiles and loads; a user namespace can be created; `systemd-run --user --scope` gives a scope with the configured limits; the overlays mount. A static test checks that no other module calls `subprocess`/`os.exec*`/`os.spawn*` with user-supplied argv. |
| S3 | `require_cgroup: true` by default. Without cgroup delegation, shadowing is disabled and every call returns `ask`. |
| S4 | Commit confinement. Every path operation during apply resolves from an `O_DIRECTORY` fd on the workspace (or `/tmp`) root through `O_NOFOLLOW` component walks and `*at()` syscalls, using `renameat2(RENAME_NOREPLACE)` for creates. `..`, absolute paths and symlinked intermediate components are rejected. |
| S5 | The commit never runs anything: no git, no hooks, no user code. |
| S6 | The daemon writes only inside its state dir (mode 0700) and, during commit, only inside the confined targets. Run-dir cleanup uses fd-relative deletes, and refuses to follow symlinks or leave the state dir. |
| S7 | The disk watchdog kills the scope when upper growth exceeds `disk_budget` (default 2 GiB) or when free space would fall below `max(5 GiB, 10%)`. It polls every ≤ 50 ms. |
| S8 | Env passed into the shadow is the hook-supplied env, minus a secret denylist and minus `WSL_INTEROP`, `SSH_AUTH_SOCK`, `DBUS_SESSION_BUS_ADDRESS`, `DISPLAY`, `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR`. |
| S9 | Shadow `/tmp`: an overlay whose lower dir is a fresh **copy** of the real `/tmp`, holding only own-uid regular files, dirs and symlinks, capped at 2,000 entries, 64 MiB total and 16 MiB per file (flag `tmp_partial` when capped). Never hard links: the real files' nlink and ctime must not change. A workspace containing a mount point cannot be the lower layer, so the result is `sandbox_error` → `ask` (spike 0). |
| S10 | Dry Run never runs repository-controlled code outside the sandbox. Its own git queries (`status`, `ls-files`, `ls-tree`, `merge-base`) run through `sandbox.run_readonly`: bwrap, read-only root, no network, seccomp, with `GIT_OPTIONAL_LOCKS=0`. |
| S11 | Real `$HOME` is read-only in the shadow and `$HOME` is unchanged. Existing `home_cache_dirs` get throwaway overlays. Writes elsewhere fail with EROFS; stderr matching EROFS sets `ro_write_blocked`. Workspaces equal to `$HOME` or `/`, or containing the state dir, are refused (`ask`). |

### 3.3 Non-functional

| ID | Requirement | How it is measured |
|---|---|---|
| N1 | Added latency on the non-shadow paths: p50 < 300 ms, p95 < 500 ms, **hook process included** | `bench/latency.py` over 200 read-only and 50 non-shadowable commands |
| N2 | Shadow overhead: report the p50 ratio of shadow-path latency to native runtime over the realistic command set. Target ≤ 1.5×; the result is reported whatever it is (the worst case measured so far is 1.65×). Diagnosis follows cards C-0001–C-0003 | `bench/overhead.py` |
| N3 | Fidelity: for every scenario, the tree after shadow+commit equals the tree after a real run in content, type, mode, symlink target and (for commit mode) mtime. **0 mismatches** | `tests/fidelity/` |
| N4 | Fail to ask: 100% of the injected faults (daemon killed, hung, garbage reply, socket missing, exception in the hook) produce `ask` | `tests/hook/test_fail_closed.py` |
| N5 | Python ≥ 3.10; the hook uses the stdlib only; runtime dependencies are `tree-sitter` + `tree-sitter-bash`, `pyyaml`, `jsonschema` (tests only), `pytest`/`hypothesis` (dev) | `pyproject.toml` |

## 4. Triage details

- **`read_only`: allowlist of commands *and* flags.** Every word must be a literal. Anything with
  redirects, command or process substitution, `$` expansion, backgrounding, `eval`/`exec`/`source`,
  or pipes into anything not on the allowlist falls out.

  | Command | Allowed |
  |---|---|
  | `ls` | any flags |
  | `cat`, `head`, `wc`, `stat`, `du`, `df`, `pwd`, `echo`, `printf`, `which`, `type`, `uname`, `whoami`, `id`, `diff`, `cmp`, `jq`, `realpath`, `basename`, `dirname` | as-is |
  | `tail` | not `-f`/`-F` (those are `long_running`) |
  | `file` | not `-C` (compiles a magic file) |
  | `tree` | not `-o` (writes an output file) |
  | `date` | not `-s` |
  | `env` | no arguments at all |
  | `grep` / `rg` | not `--pre`, `--pre-glob` or `-z`/`--search-zip` (they run programs) |
  | `git status`, `git diff`, `git log`, `git show`, `git branch` (list forms only), `git rev-parse`, `git ls-files`, `git remote -v` | no `-c`, no `--output`, no `--ext-diff`, no `--textconv` |
  | `sort` | no `-o` |

  Commands that include a git read are class **`git_read`**, not `read_only`: they never run natively.
  Git config (repo, user, system, `GIT_*` env, and whichever repo git's own discovery picks) can make a
  "read" start programs: fsmonitor, textconv, hooks, credential helpers, promisor fetches in partial
  clones. Earlier versions tried to rule these out by scanning config, and every review round found one
  more. Now the whole command runs in the `run_readonly` sandbox (read-only root, no network, seccomp,
  secrets and state hidden, cgroup limits). The result is `allow + commit` with an empty ChangeSet, so
  `dryrun apply` only replays stdout, stderr and the exit code. A timeout or output over `output_max`
  gives `ask + passthrough` (`S.git_read_incomplete`); a sandbox that fails to start gives `ask`.

- **`apply`:** argv[0] is `dryrun` and argv[1] is `apply`.
- **`non_shadowable`:** harm-policy T1–T6 patterns on the parsed argv, including inside `bash -c` or
  `sh -c` strings when they can be parsed.
- **`long_running`:** a trailing `&`, `nohup`, `setsid`, or `disown`; known server/watch commands
  (`npm run dev|start|watch`, `vite`, `next dev`, `uvicorn`, `flask run`, `python -m http.server`,
  `tail -f`, `* --watch`). The dev-server allowlist in `policy.yaml` decides allow vs ask.
- **`shadow`:** everything else, including anything that can't be parsed.

## 5. Contracts

JSON Schemas live in `schemas/` and are validated in tests. Field lists are fixed as presented in
design section 2; the examples in ARCHITECTURE show their shape. Summary:

- **`dryrun.effect/1`:**
  - `schema`, `run_id`, `session_id`, `command`, `cwd`, `workspace_root`
  - `request{text, source}`
  - `triage{class, reason}`
  - `exec{exit_code, wall_ms, timed_out, stdout_tail, stderr_tail}`
  - `fs{workspace[], tmp[], home_cache{files, bytes}}` — each entry is `{op, path, kind, preexisting, in_ledger, git, build_output, bytes_before, bytes_after, mode_before, mode_after}`
  - `git{refs_changed[], index_changed, internals_touched[]}`
  - `summary{…counts}`
  - `net[]{kind, target}`
  - `procs{count, exec[]}`
  - `decoy_hits[]{path, where}`
  - `flags[]`, drawn from `timeout`, `resource_limit`, `incomplete_network`, `unsupported_entry`, `lower_changed`, `sandbox_error`, `ro_write_blocked`, `tmp_partial`
- **`dryrun.changeset/1`:**
  - `run_id`, `workspace_root`
  - `base_digest`
  - `ops[]{seq, op, target, source_upper, kind, mode, base_fp}`
  - `refused[]{path, reason}`
  - `committable` (bool)
- **`dryrun.rpc/1`:**
  - `prompt`, `pretool`, `status`, `apply_authorize` (used by `dryrun apply` to redeem a token)
  - responses: `{decision, mode, reason, rule_ids[], run_id, token?}`
- **`EffectJudge` protocol:** `score(record) -> {clause: p_harm}`. The v1 implementation is
  `NullJudge`, which returns `{}`.

## 6. Hook installation

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "dryrun-hook prompt", "timeout": 5 } ] }
    ],
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [ { "type": "command", "command": "dryrun-hook pretool", "timeout": 60 } ] }
    ]
  }
}
```

The hook's internal deadline is `timeout − 2 s`, capped by `shadow.wall_clock + 5 s`. The user's own
`ask`/`deny` permission rules still apply on top: a hook `allow` never overrides them.

## 7. Configuration (`policy.yaml` defaults)

```yaml
shadow:
  wall_clock_s: 30
  memory_max: 2G
  tasks_max: 512
  nice: 19
  ionice_class: idle   # WSL delegates only memory+pids cgroup controllers (spike 0)
  tmp_snapshot: {max_entries: 2000, max_total: 64M, max_file: 16M}
  file_size_max: 1G
  disk_budget: 2G
  free_space_floor: {abs: 5G, frac: 0.10}
  require_cgroup: true
  canary_interval_h: 6
h1: {mass_threshold: 20, overwrite_fraction: 0.9}
h4: {mass_threshold: 20}
build_output_dirs: [build, dist, target, out, node_modules, __pycache__, .venv, .pytest_cache, .mypy_cache, .next, coverage]
home_cache_dirs: [.cache, .npm, .cargo/registry, .local/share/pnpm, go/pkg/mod]
secret_paths: [.ssh, .aws, .config/gh, .netrc, .docker, .kube, .gnupg, .password-store, .config/gcloud, .azure, .claude/.credentials.json]
env_denylist: ["*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*", "AWS_*", "GH_*", "GITHUB_*", "ANTHROPIC_*", "OPENAI_*"]
dev_server_allowlist: ["npm run dev", "npm start", "vite", "next dev", "uvicorn *", "python -m http.server *"]
pending_ttl_min: 15
```

## 8. Testing strategy and acceptance

| Suite | Contents | Acceptance |
|---|---|---|
| `tests/unit/` | triage tables: every GuardFall class A–E and every #85274 pattern **must not** reach `read_only`; effects conversion on real overlay fixtures (whiteout forms, opaque dirs, no-op copy-ups, symlinks); each rule with a positive and a negative EffectRecord fixture; schema validation | all pass |
| `tests/hook/` | fault injection for fail-to-ask; protocol round-trips; `updatedInput` shape | N4 = 100% |
| `tests/fidelity/` | every pitfall in research notes §3 and ARCHITECTURE §7, plus hypothesis-generated random sequences of filesystem ops. Each runs for real on copy A and shadow+commit on copy B, then the trees are compared. Also: conflict detection (a target edited between shadow and commit), a crash mid-commit followed by recovery | N3 = 0 mismatches; conflicts always exit 4 with nothing written |
| `tests/isolation/` | canaries I1–I15 + S2 static check + S4 symlink-swap attack on commit | all blocked |
| `bench/` | latency (N1), overhead (N2) | N1 met; N2 reported |
| `tests/e2e/` (manual/opt-in) | headless `claude -p` in a throwaway repo: an allowed edit commits; `bash cleanup.sh` deleting `src/` triggers `ask`; daemon stopped → `ask` | documented run |

Safety of the tests themselves: destructive operations target only `tmp_path` fixture trees, and
isolation canaries target only canary resources that the test creates. The GuardFall-style adversarial
corpus is **not** executed in sub-project 1 tests. Triage tests only *parse* those strings.

## 9. Spike 0: verify before building on it (DONE 2026-09-23, see [spike0-results.md](../../spike0-results.md))

1. Does Claude Code re-run PreToolUse hooks on `updatedInput`? Does the user see the rewritten command
   in the `ask` prompt?
2. Does a static bwrap 0.11.1 build run on WSL2 6.18 with `--overlay` on the workspace **and** a `/tmp`
   overlay **and** tmpfs over `/run` and friends, while the workspace is under a `$HOME` that is
   bound read-only? (bwrap forbids one overlay source being an ancestor of another; the sources here
   are the workspace and `/tmp`, which are disjoint.)
3. Is the recursive read-only bind really read-only for `/mnt/c` (9p) and `/usr/lib/wsl`?
4. Does unprivileged fanotify on a decoy file fire when the decoy is read through a bwrap ro-bind?
5. Does `systemd-run --user --scope -p MemoryMax=… -p TasksMax=…` enforce the limits under WSL2?

Any "no" goes back to the design, and this spec is amended before implementation continues.

## 10. Differences from the brief (deliberate)

- **Commit mechanism.** The hook rewrites the command through `updatedInput` to `dryrun apply`. The
  daemon does not apply the change itself. This keeps the commit visible in the transcript, under
  Claude Code's own permission flow.
- **Request capture.** Through `UserPromptSubmit`, not by parsing `transcript_path`. The transcript can
  lag and has no documented schema.
- **Harm policy.** Adds H9 (persistence and guard tampering) and H10 (unsupported effects), and
  weights H1 by git recoverability.
- **Isolation.** Expanded from "no network + limits" to the fifteen-channel threat model with canaries
  and fail-closed gating (user requirement, 2026-09-23).
- **`/tmp`.** Treated as a captured, committable overlay, so that a Write-tool script in `/tmp` followed
  by `bash /tmp/x.sh` still works.
- **Positioning.** Must cite YoloFS, Cordon and pi-overlayfs, which appeared after or alongside the
  brief.

## 11. Risks specific to sub-project 1

| Risk | Handling |
|---|---|
| bwrap overlay flags behave differently than documented on WSL | Spike 0; fallback is a small launcher using `unshare` + `mount`, 7 ms measured (approach B) |
| Overlay overhead above 1.5× on real sessions | Cards C-0001–C-0003 find what drives it; R3 cache; narrow shadowing (brief §10 stop condition) |
| Many benign commands hit H7 (network) and so `ask + rerun` | Count them in the decision log; over 2% triggers roadmap R1, the network-read tier |
| Hook latency dominated by Python startup | Measure first. If the p50 of N1 is at risk, rewrite only `dryrun-hook` as a static binary; the contracts don't change |
| Kernel escape from user namespaces | Residual. Seccomp denylist; R6 gVisor backend; README says so |
