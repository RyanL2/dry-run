# Spike 0 results (2026-09-23)

Environment: WSL2 Ubuntu 22.04, kernel 6.18.33.1-microsoft-standard-WSL2, x86_64, Claude Code 2.1.280.
Scripts: `scripts/spike/` (throwaway), `scripts/dev/`, `scripts/build-bwrap.sh`.

| # | Question (spec §9) | Result | Consequence |
|---|---|---|---|
| 1 | Does Claude Code re-run PreToolUse hooks on `updatedInput`? | **No.** The hook ran once; the rewritten command ran and printed `REWRITTEN_MARKER`. (Whether an interactive `ask` prompt shows the rewritten command is not verified headless; UX only.) | A legitimate `dryrun apply` never passes the hook, so denying agent-typed applies (F11) is sound. Keep the token check as defense in depth. |
| 2 | bwrap 0.11 layout: workspace overlay under a read-only `$HOME`, `/tmp` overlay, tmpfs hides | Workspace overlay works. **An overlay with lower=`/tmp` fails with EINVAL** ("overlayfs: failed to clone lowerpath"), because WSLg mounts `/tmp/.X11-unix` inside `/tmp` and unprivileged overlay can't clone a lower dir that has locked submounts. A lower dir without submounts mounted at `/tmp` works. | `/tmp` lower becomes a **copied snapshot** of own-uid regular files, dirs and symlinks, capped at 2,000 entries, 64 MiB total and 16 MiB per file, with flag `tmp_partial` when capped. Copies rather than hard links, so the real files' nlink and ctime never change. A workspace containing a mount point → `sandbox_error` → `ask`. |
| 2b | Mount targets for secret masks | A mount target can't be created on the read-only root ("Can't mkdir … Read-only file system") | Mask only secret paths that exist. Missing paths can't be created either, because home is read-only. |
| 3 | Is the recursive read-only bind really read-only for `/mnt/c`, `/usr/lib/wsl` and the real `$HOME`? | **Yes, all three blocked** | as designed |
| 4 | Is unprivileged fanotify on a decoy usable? | Not pursued. Replaced by per-run **canary tokens** in the decoy contents, scanned in stdout/stderr and in ChangeSet content (plain, base64, hex). Without network, a credential read only matters if it reaches the agent's output or a committed file, and the token scan detects exactly that. | H5 and I14 wording updated. No fanotify dependency. |
| 5 | Does `systemd-run --user --scope` enforce limits? | `MemoryMax=100M` killed a 300 MB allocation (exit 137); `TasksMax=20` blocked fork after 19 (EAGAIN). **Only the `memory` and `pids` controllers are delegated** (no `cpu`, `io`) | CPU/IO de-prioritisation uses `nice -n 19` + `ionice -c3` (both unprivileged; verified) instead of `CPUWeight`/`IOWeight` |
| 6 | Isolation of the layout (preview of the canaries) | Unix connects to the X11 and D-Bus sockets fail (ENOENT, hidden); TCP 1.1.1.1:443 fails (ENETUNREACH); `/run` and the state dir are empty inside; the decoy is served in place of `~/.ssh/id_ed25519`; the real workspace and `/tmp` are unchanged | Seccomp AF_UNIX deny stays as a second layer |
| 7 | Can strace run **outside** bwrap (so the in-sandbox seccomp can deny ptrace)? | **Yes.** `strace -f --seccomp-bpf -e trace=execve,connect bwrap …` recorded 24 execs and all 3 connect attempts, with addresses | as designed, with the order `strace → bwrap` |
| 8 | Default WSL user | **root.** | Development and tests run as a dedicated non-root user `dryrundev` (`scripts/dev/setup-wsl-user.sh`). `dryrund` refuses to run as root unless `--allow-root` is given |
| 9 | Building bwrap | 0.11.0 builds from the release tarball once `libcap-dev` is installed (apt) | Tarball sha256 `988fd6b232dafa04b8b8198723efeaccdb3c6aa9c1c7936219d5791a8b7a8646`. The binary hash is recorded at build time in `~/.local/share/dryrun/bwrap/0.11.0/bwrap.sha256`, and the daemon checks it before every spawn |

The upper-dir encoding was confirmed: a delete is a char device 0:0 whiteout (`sub/b.txt: c`), and a new file is a plain file.

## Design consequences decided after the spike

- **Scratch `$HOME` overlay dropped.**
  - bwrap can't create `/home/sbx` on the read-only root.
  - bwrap refuses overlay sources that are ancestors of one another, and the workspace usually sits
    under home.
  - Hiding home entirely would break toolchains that live there (venvs, nvm, cargo, pyenv).
  - **v1 instead:** home stays visible read-only with `$HOME` unchanged. Existing cache dirs get
    throwaway overlays. Other writes fail with EROFS, which sets `ro_write_blocked` → H2 → `ask + rerun`.
  - Safety does not depend on detecting these writes, because they are never committed.
- **Git queries run in the sandbox (S10).** `git status` on the real repo can run `core.fsmonitor` or
  clean filters from repo config. Dry Run's own git calls therefore run via `sandbox.run_readonly`.
- **Hard deny before flags.** A decoy-token hit (H5) must deny even if the run also timed out. The
  cascade now checks hard denies first.
