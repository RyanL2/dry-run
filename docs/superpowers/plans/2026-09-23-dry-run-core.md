# Dry Run Core (Sub-project 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Claude Code `PreToolUse` gate. It shadow-runs Bash commands in an isolated copy-on-write sandbox, judges the observed effect with clause-cited rules, and commits exactly the reviewed ChangeSet.

**Architecture:**
- A stdlib-only hook client talks NDJSON over a unix socket to a long-lived Python daemon.
- The daemon triages the command, builds a bwrap sandbox (overlays, tmpfs hides, decoys, seccomp) inside a systemd user scope, and parses the upper dirs and strace log into an EffectRecord and a ChangeSet.
- A rules cascade decides allow/ask/deny.
- On commit, the hook rewrites the command to `dryrun apply <id> --token <t>`. That command applies the ChangeSet with fd-confined, journaled renames.

**Tech Stack:** Python ≥ 3.10 (stdlib, PyYAML, tree-sitter 0.26 + tree-sitter-bash 0.25), bubblewrap 0.11.0 (built from source), strace 5.16+, systemd user scopes, pytest + hypothesis + jsonschema. Linux/WSL2 x86_64.

**Spec:** `docs/superpowers/specs/2026-09-22-dry-run-core-design.md`. Also read `docs/ARCHITECTURE.md` (diagrams, isolation table §7.1), `docs/harm-policy.md`, and `docs/spike0-results.md`, which records measured facts that override older text.

## How to run things (read first)

- Code lives in the Windows worktree. **All tests run inside WSL as the non-root user `dryrundev`**, never as root:
  ```powershell
  wsl.exe -d Ubuntu-22.04 -u dryrundev --cd /mnt/c/Users/rylei/github/dry-run/.claude/worktrees/core-spec -- bash scripts/dev/test.sh tests/unit -q
  ```
- One-time setup (already done on this machine): `scripts/dev/setup-wsl-user.sh` (as root), then `scripts/dev/bootstrap.sh` (as dryrundev). This creates the venv `~/.venvs/dryrun` and bwrap at `~/.local/share/dryrun/bwrap/0.11.0/bwrap`.
- Tests marked `sandbox` launch real bwrap sandboxes. They create workspaces only under `~/dryrun-tests/` and a private state dir, never under real project paths.
- Commit from the Windows side with git in the worktree (files are LF via `.gitattributes`).

## Global Constraints

- Python `>=3.10`. Use `from __future__ import annotations`; no 3.11-only stdlib (`tomllib`, `ExceptionGroup`).
- Runtime deps limited to `pyyaml`, `tree-sitter`, `tree-sitter-bash`. The hook (`src/dryrun/hook.py`) imports **stdlib only**, and `src/dryrun/__init__.py` stays empty.
- Isolation is CRITICAL (spec §3.2 S1–S11). Every channel I1–I15 has a canary. A user command never runs outside `sandbox.spawn`, and any setup failure means `ask`.
- Fail to ask: every fault on the hook path yields `permissionDecision: "ask"`, exit 0, valid JSON.
- Only `src/dryrun/sandbox/spawn.py` (user commands), `src/dryrun/cli.py` (installer) and `src/dryrun/canary.py` (fixed-argv isolation harness) may import `subprocess` or call `os.exec*`/`os.spawn*`. A static test enforces this.
- Dry Run never runs repository-controlled code outside the sandbox (S10): git queries go through `spawn.run_readonly`.
- The daemon refuses root unless `--allow-root` (F17).
- Defaults (spec §7):

  | Setting | Default |
  |---|---|
  | wall clock | 30 s |
  | memory | 2 GiB |
  | tasks | 512 |
  | nice / ionice | 19 / idle |
  | file size | 1 GiB |
  | disk budget | 2 GiB |
  | free-space floor | max(5 GiB, 10%) |
  | tmp snapshot | 2000 entries / 64 MiB total / 16 MiB per file |
  | pending TTL | 15 min |

- Schemas: `dryrun.effect/1`, `dryrun.changeset/1`, `dryrun.rpc/1`, in `schemas/`.
- No network I/O anywhere in the shipped tool.

## Review Focus

1. **A workspace that contains a mount point, or equals `$HOME` or `/`.** Overlay setup fails or would overlap the state dir. Expect `ask` with a clear reason, never a crash and never an unsandboxed run. The test goes in Task 16 (`test_pipeline_refuses_broad_or_mounted_workspace`).
2. **File names with spaces, newlines, leading dashes, unicode, or invalid UTF-8.** Expect correct ChangeSet ops, JSON that round-trips, and commit fidelity. Invalid UTF-8 is refused with `ask`. Tests go in Task 9 (`test_extract_odd_names`) and Task 20 (a fidelity scenario).
3. **The user edits the workspace while a shadow runs, or between shadow and apply.** Expect `lower_changed` → `ask + rerun`, or `dryrun apply` exit 4 with nothing written. Tests go in Task 15 (`test_apply_conflict_writes_nothing`) and Task 16 (`test_pipeline_lower_changed`).
4. **The daemon is down, slow, or returns garbage, and hook stdin is malformed or huge.** Expect `ask` JSON, exit 0, within the deadline. Tests go in Task 17.
5. **Two parallel Bash tool calls, or `dryrun apply` run twice with the same token.** Expect independent runs, and a single-use token (the second apply exits 3). Tests go in Task 14 (`test_redeem_is_single_use`) and Task 16 (`test_pipeline_parallel_runs_are_independent`).

## File Structure

```
pyproject.toml                         package metadata, entry points, pytest markers
schemas/effect.schema.json             dryrun.effect/1
schemas/changeset.schema.json          dryrun.changeset/1
schemas/rpc.schema.json                dryrun.rpc/1 request/response
scripts/dev/test.sh                    run pytest in WSL as dryrundev
src/dryrun/__init__.py                 empty (hook must import stdlib only)
src/dryrun/data/policy.yaml            default policy (spec §7)
src/dryrun/config.py                   Config/ShadowConfig/Policy + loader
src/dryrun/paths.py                    state/runtime/socket/bwrap locations, private dirs
src/dryrun/types.py                    FsEntry, EffectRecord, ChangeOp, ChangeSet, Decision
src/dryrun/fingerprint.py              lstat fingerprints, digests, submount detection
src/dryrun/sandbox/__init__.py         empty
src/dryrun/sandbox/seccomp.py          x86_64 cBPF filter builder
src/dryrun/sandbox/layout.py           SandboxSpec -> bwrap argv (pure)
src/dryrun/sandbox/tmpsnap.py          copied /tmp lower snapshot
src/dryrun/sandbox/decoys.py           decoy dirs + canary tokens + scanner
src/dryrun/sandbox/spawn.py            THE launcher: preflight, scope, strace, watchdogs, run_readonly
src/dryrun/sandbox/trace.py            strace log -> execs, net attempts
src/dryrun/sandbox/assemble.py         per-run layout assembly shared by pipeline and canaries
src/dryrun/effects/__init__.py         empty
src/dryrun/effects/upper.py            upper dir -> FsEntry list + ChangeOps + refused
src/dryrun/effects/gitstate.py         repo root, recoverability, blob shas, merged refs
src/dryrun/effects/record.py           annotate entries, git effects, flags, EffectRecord
src/dryrun/judge/__init__.py           empty
src/dryrun/judge/rules.py              harm-policy clauses H1-H10, T1-T6
src/dryrun/judge/cascade.py            decision cascade (ARCHITECTURE §4)
src/dryrun/judge/model.py              EffectJudge protocol + NullJudge
src/dryrun/triage.py                   tree-sitter-bash classifier
src/dryrun/store.py                    runs, sessions, ledger, tokens, decision log, cleanup
src/dryrun/confined.py                 O_NOFOLLOW dir-fd path operations
src/dryrun/commit.py                   apply/journal/verify/recover
src/dryrun/pipeline.py                 one pretool request end to end
src/dryrun/rpc.py                      NDJSON framing, client + server helpers
src/dryrun/daemon.py                   asyncio server, deadlines, canary gate
src/dryrun/hook.py                     stdlib-only Claude Code hook client
src/dryrun/canary.py                   isolation canaries I1-I15
src/dryrun/cli.py                      dryrun apply|recover|doctor|daemon|install|status
tests/conftest.py                      fixtures: cfg, state dir, workspaces under ~/dryrun-tests
tests/unit/...                         pure tests (no sandbox)
tests/sandbox/...                      real bwrap tests (marker: sandbox)
tests/isolation/test_canaries.py       I1-I15 + S2 static check + S4 symlink swap
tests/fidelity/...                     real run vs shadow+commit
tests/hook/...                         fail-to-ask fault injection
bench/latency.py, bench/overhead.py, bench/overlay_factorial.py
research/memory/ledger.jsonl           experiment ledger (cards C-0001..C-0003)
```

## Tasks

Each task is in its own file under `docs/superpowers/plans/2026-09-23-dry-run-core/`:

| # | Task | File |
|---|---|---|
| 0 | Spike 0 (done, see `docs/spike0-results.md`) | — |
| 1 | Package skeleton, config, paths, test runner | `task-01-skeleton.md` |
| 2 | Types and JSON schemas | `task-02-types-schemas.md` |
| 3 | Fingerprints and submount detection | `task-03-fingerprint.md` |
| 4 | Seccomp filter | `task-04-seccomp.md` |
| 5 | bwrap layout builder | `task-05-layout.md` |
| 6 | /tmp snapshot and decoys | `task-06-tmpsnap-decoys.md` |
| 7 | The launcher (`spawn`) | `task-07-spawn.md` |
| 8 | strace parser | `task-08-trace.md` |
| 9 | Upper-dir extraction | `task-09-upper.md` |
| 10 | Git state | `task-10-gitstate.md` |
| 11 | Run assembly and EffectRecord builder | `task-11-assemble-record.md` |
| 12 | Rules, cascade, model slot | `task-12-rules-cascade.md` |
| 13 | Triage | `task-13-triage.md` |
| 14 | Store | `task-14-store.md` |
| 15 | Confined ops and commit engine | `task-15-commit.md` |
| 16 | Pipeline, RPC, daemon | `task-16-pipeline-daemon.md` |
| 17 | Hook client | `task-17-hook.md` |
| 18 | CLI and install | `task-18-cli.md` |
| 19 | Isolation canaries I1–I15 | `task-19-canaries.md` |
| 20 | Fidelity suite | `task-20-fidelity.md` |
| 21 | Benchmarks and cards C-0001..C-0003 | `task-21-bench.md` |
| 22 | End-to-end smoke, README, final review | `task-22-e2e-docs.md` |
