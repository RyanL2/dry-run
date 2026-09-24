# Installing Dry Run (v0.1, Linux / WSL2)

Dry Run reduces risk. It does not remove it. Keep backups and use OS-level sandboxing as well.

## Requirements

- Linux or WSL2 on x86_64, kernel ≥ 5.13 (developed on WSL2 6.18).
- A systemd user session (`systemctl --user` works). It must delegate at least the `memory` and `pids`
  cgroup controllers (check with `cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers`).
- A **normal (non-root) user**. `dryrund` refuses to run as root.
  - On WSL the default user is often root. Create one: `sudo useradd -m you && sudo loginctl enable-linger you`.
- Packages: `strace`, `gcc`, `pkg-config`, `libcap-dev`, `git`, `python3` (≥ 3.10) with `venv`.
- **Claude Code must run inside the same Linux environment** (the hooks run where Claude Code runs).
- **Workspaces must live on a Linux filesystem** (ext4, xfs, btrfs), not `/mnt/c`.

## Steps

```bash
git clone <this repo> ~/dry-run && cd ~/dry-run
python3 -m venv ~/.venvs/dryrun
~/.venvs/dryrun/bin/pip install -e '.[dev]' meson ninja
PATH=~/.venvs/dryrun/bin:$PATH scripts/build-bwrap.sh   # builds bubblewrap 0.11.0 and pins its sha256
~/.venvs/dryrun/bin/dryrun doctor                       # must end with: isolation self-test: PASS
~/.venvs/dryrun/bin/dryrun install                      # shows a diff of ~/.claude/settings.json, asks first,
                                                        # writes ~/.local/bin wrappers, enables dryrund.service
```

`dryrun doctor` runs the 16 isolation canaries (ARCHITECTURE §7.1) inside the real sandbox layout. If
any fails, the daemon stays up but disables shadowing, and every Bash call gets `ask`.

## What changes in your Claude Code sessions

- **Read-only commands** (strict command + flag allowlist) run as usual.
- **Everything else** is first run in a shadow: copy-on-write, no network, no access to the rest of the
  system.
  - If the effect is within policy, the tool call is rewritten to `dryrun apply <id> --token <t>`. That
    applies exactly the reviewed changes and replays the command's output.
  - If not, you get an `ask` prompt with a short description of what the command would do (for example,
    "would destroy 1 pre-existing file(s) not recoverable from git: notes.txt").
- Commands whose effect leaves the machine (`git push`, publish, cloud CLIs, `sudo`, HTTP writes) always
  `ask`.
- **Your own permission rules still apply.** A hook `allow` never overrides your `ask`/`deny` rules.
- Local decision log: `~/.local/state/dryrun/decisions.jsonl`. Nothing is sent anywhere.

## Uninstall

```bash
dryrun uninstall     # removes the hook entries (diff shown first), wrappers and the service
```

## Limits

See the README. Short version:
- The shadow shares the kernel, so kernel bugs could allow an escape.
- Effects that leave the machine cannot be shadowed.
- Shadowing adds roughly 0.3–0.6 s on large repos for short commands (see `research/memory/ledger.jsonl`).
