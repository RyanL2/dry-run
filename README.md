# Dry Run

Decide from **observed effects** before a coding agent's shell command touches real files.

Dry Run is a Claude Code `PreToolUse` hook plus a local daemon. Each Bash command is first run in a
copy-on-write shadow of the workspace with no network and no ability to affect the rest of the system.
Dry Run records what the command actually did and judges that effect against what you asked for. Then
it allows, asks or denies. On allow, it commits exactly the reviewed change instead of re-running the
command.

**Status:** v0.1: the core (sub-project 1) is implemented and tested on WSL2. That covers the shadow
sandbox with 16 isolation canaries, effect extraction, harm-policy rules, and confined journaled commit.
The benchmark (sub-project 2) and the judge model (sub-project 3) are next.

- [Install guide](docs/INSTALL.md)
- [Architecture (with diagrams)](docs/ARCHITECTURE.md)
- [Sub-project 1 design spec](docs/superpowers/specs/2026-09-22-dry-run-core-design.md)
- [Harm policy](docs/harm-policy.md)
- [Research notes (facts)](docs/research-notes.md)
- [Research program](research/program.md) and [experiment cards](research/cards/)

## Limits: please read

- **Defense in depth, not a guarantee.** Dry Run reduces risk. It does not remove it. Keep backups and
  use OS-level sandboxing as well.
- Linux and WSL2 only. Workspaces must live on a local Linux filesystem, not `/mnt/c`.
- Commands whose effects leave the machine (`git push`, publish, cloud CLIs, HTTP writes) cannot be
  shadowed. Dry Run falls back to judging them by their text and defaults to asking you.
- Kernel vulnerabilities in user namespaces or overlayfs could let a hostile command escape the shadow.
- If Dry Run is unavailable or errors, it asks you. It never silently allows.

## License

To be decided before the first release: a standard open-source license with the usual "as is, no
warranty" clause.
