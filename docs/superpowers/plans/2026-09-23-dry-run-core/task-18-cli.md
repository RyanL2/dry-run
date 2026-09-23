### Task 18: CLI and installer (F10, F12, F13, F14)

**Files:**
- Create: `src/dryrun/cli.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes:
  - `apply_run`, `recover_all` (Task 15); `Store` (Task 14); `load_config` (Task 1); `state_dir`, `socket_path` (Task 1)
  - `rpc.call` (Task 16); `daemon.main` (Task 16); `canary.run_all` (Task 19, imported lazily inside `doctor`)
- Produces, as `dryrun <cmd>`:

  | Command | What it does |
  |---|---|
  | `apply RUN_ID --token T` | exit code = the shadow's exit code, or 3/4/5 |
  | `recover` | completes interrupted commits |
  | `doctor [--json]` | preflight + canaries; exit 0 only if all pass |
  | `daemon [--allow-root] [--config P]` | runs dryrund |
  | `status` | asks the running daemon for its status |
  | `install [--yes] [--settings P] [--bin-dir D] [--no-service]` | adds the hooks, wrappers and service |
  | `uninstall [--yes] [--settings P] [--bin-dir D] [--no-service]` | removes them |

- Pure helpers, tested directly:
  - `hook_entries(bin_dir: Path) -> dict`
  - `merge_settings(settings: dict, bin_dir: Path) -> dict` (idempotent; keeps unrelated hooks)
  - `remove_settings(settings: dict) -> dict`
  - `wrapper_script(python: str, module: str, src: Path | None) -> str`
  - `service_unit(python: str, src: Path | None) -> str`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_cli.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

from dryrun.cli import hook_entries, main, merge_settings, remove_settings, service_unit, wrapper_script

OTHER = {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "my-lint"}]}]}


def test_merge_is_idempotent_and_keeps_other_hooks():
    s = {"model": "x", "hooks": json.loads(json.dumps(OTHER))}
    once = merge_settings(s, Path("/b"))
    twice = merge_settings(once, Path("/b"))
    assert once == twice
    pre = once["hooks"]["PreToolUse"]
    assert pre[0] == OTHER["PreToolUse"][0]
    assert pre[1] == {"matcher": "Bash", "hooks": [{"type": "command", "command": "/b/dryrun-hook pretool",
                                                    "timeout": 60}]}
    assert once["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "/b/dryrun-hook prompt"
    assert once["model"] == "x"
    assert s == {"model": "x", "hooks": OTHER}  # input not mutated


def test_remove_restores_original():
    s = {"hooks": json.loads(json.dumps(OTHER))}
    assert remove_settings(merge_settings(s, Path("/b"))) == s
    assert remove_settings(merge_settings({}, Path("/b"))) == {}


def test_wrapper_and_unit():
    w = wrapper_script("/v/bin/python", "dryrun.hook", Path("/src"))
    assert w.startswith("#!/bin/sh\n") and 'PYTHONPATH="/src' in w and 'exec "/v/bin/python" -m dryrun.hook "$@"' in w
    u = service_unit("/v/bin/python", None)
    assert "ExecStart=/v/bin/python -m dryrun.cli daemon" in u and "Restart=on-failure" in u


def test_install_and_uninstall_files(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": OTHER}))
    bin_dir = tmp_path / "bin"
    assert main(["install", "--yes", "--no-service", "--settings", str(settings), "--bin-dir", str(bin_dir)]) == 0
    data = json.loads(settings.read_text())
    assert any("dryrun-hook pretool" in h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"])
    assert (bin_dir / "dryrun").exists() and (bin_dir / "dryrun-hook").exists()
    assert list(tmp_path.glob("settings.json.bak-*"))
    assert main(["uninstall", "--yes", "--no-service", "--settings", str(settings), "--bin-dir", str(bin_dir)]) == 0
    assert json.loads(settings.read_text()) == {"hooks": OTHER}
    assert not (bin_dir / "dryrun-hook").exists()


def test_apply_with_bad_run_id_exits_3(tmp_path: Path, monkeypatch, capsysbinary):
    monkeypatch.setenv("DRYRUN_STATE_DIR", str(tmp_path / "state"))
    assert main(["apply", "not-a-run", "--token", "x"]) == 3
    assert b"refused" in capsysbinary.readouterr().err
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/unit/test_cli.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.cli'`)

- [ ] **Step 3: Implement**

`src/dryrun/cli.py`:
```python
"""dryrun command line."""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOOK_MARK = "dryrun-hook"
SRC_DIR = Path(__file__).resolve().parents[1]


def hook_entries(bin_dir: Path) -> dict:
    hook = str(Path(bin_dir) / "dryrun-hook")
    return {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{hook} prompt", "timeout": 5}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"{hook} pretool",
                                                      "timeout": 60}]}],
    }


def _ours(group: dict) -> bool:
    return any(HOOK_MARK in str(h.get("command", "")) for h in group.get("hooks", []))


def remove_settings(settings: dict) -> dict:
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        kept = [g for g in hooks[event] if not _ours(g)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def merge_settings(settings: dict, bin_dir: Path) -> dict:
    out = remove_settings(settings)
    hooks = out.setdefault("hooks", {})
    for event, groups in hook_entries(bin_dir).items():
        hooks.setdefault(event, []).extend(copy.deepcopy(groups))
    return out


def wrapper_script(python: str, module: str, src: Path | None) -> str:
    env = f'PYTHONPATH="{src}${{PYTHONPATH:+:$PYTHONPATH}}"\nexport PYTHONPATH\n' if src else ""
    return f'#!/bin/sh\n{env}exec "{python}" -m {module} "$@"\n'


def service_unit(python: str, src: Path | None) -> str:
    env = f"Environment=PYTHONPATH={src}\n" if src else ""
    return ("[Unit]\nDescription=Dry Run daemon (shadow-run gate for Claude Code)\n\n"
            f"[Service]\nExecStart={python} -m dryrun.cli daemon\n{env}Restart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n")


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=False)


def _edit_settings(path: Path, new_fn, yes: bool) -> bool:
    old = json.loads(path.read_text()) if path.exists() else {}
    new = new_fn(old)
    a = json.dumps(old, indent=2, sort_keys=True).splitlines(keepends=True)
    b = json.dumps(new, indent=2, sort_keys=True).splitlines(keepends=True)
    sys.stdout.writelines(difflib.unified_diff(a, b, str(path), str(path) + " (new)"))
    if old == new:
        print("settings already up to date")
        return True
    if not _confirm(f"Write {path}?", yes):
        print("aborted; nothing changed")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        (path.parent / f"{path.name}.bak-{int(time.time())}").write_text(path.read_text())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n")
    os.replace(tmp, path)
    return True


def cmd_install(args) -> int:
    bin_dir = Path(args.bin_dir)
    if not _edit_settings(Path(args.settings), lambda s: merge_settings(s, bin_dir), args.yes):
        return 1
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, module in (("dryrun", "dryrun.cli"), ("dryrun-hook", "dryrun.hook")):
        path = bin_dir / name
        path.write_text(wrapper_script(sys.executable, module, SRC_DIR))
        path.chmod(0o755)
    if not args.no_service:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "dryrund.service").write_text(service_unit(sys.executable, SRC_DIR))
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", "dryrund.service")
    print("installed. Note: your own permission ask/deny rules still apply on top of Dry Run.")
    return 0


def cmd_uninstall(args) -> int:
    if not _edit_settings(Path(args.settings), remove_settings, args.yes):
        return 1
    for name in ("dryrun", "dryrun-hook"):
        try:
            (Path(args.bin_dir) / name).unlink()
        except FileNotFoundError:
            pass
    if not args.no_service:
        _systemctl("disable", "--now", "dryrund.service")
        try:
            (Path.home() / ".config" / "systemd" / "user" / "dryrund.service").unlink()
        except FileNotFoundError:
            pass
        _systemctl("daemon-reload")
    print("uninstalled")
    return 0


def cmd_apply(args) -> int:
    from dryrun.commit import apply_run
    from dryrun.config import load_config
    from dryrun.paths import state_dir
    from dryrun.store import Store
    return apply_run(Store(state_dir()), args.run_id, args.token, cfg=load_config(),
                     out=sys.stdout.buffer, err=sys.stderr.buffer)


def cmd_recover(args) -> int:
    from dryrun.commit import recover_all
    from dryrun.paths import state_dir
    from dryrun.store import Store
    for run_id in recover_all(Store(state_dir())):
        print(f"completed interrupted commit {run_id}")
    return 0


def cmd_doctor(args) -> int:
    from dryrun.canary import run_all
    from dryrun.config import load_config
    from dryrun.paths import state_dir
    from dryrun.sandbox.spawn import preflight
    from dryrun.store import Store
    cfg = load_config()
    problems = preflight(cfg.shadow)
    results = [] if problems else run_all(cfg, Store(state_dir()))
    if args.json:
        print(json.dumps({"preflight": problems, "canaries": [r.__dict__ for r in results]}, indent=2))
    else:
        for p in problems:
            print(f"PREFLIGHT FAIL  {p}")
        for r in results:
            print(f"{'PASS' if r.passed else 'FAIL'}  {r.id:4} {r.name:<40} {r.detail}")
    ok = not problems and results and all(r.passed for r in results)
    print("isolation self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_status(args) -> int:
    from dryrun import rpc
    from dryrun.paths import socket_path
    try:
        print(json.dumps(rpc.call(socket_path(), {"op": "status"}, 2.0), indent=2))
        return 0
    except Exception as exc:
        print(f"dryrund not reachable: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dryrun")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply", help="commit a reviewed shadow run (issued by the hook)")
    a.add_argument("run_id")
    a.add_argument("--token", required=True)
    sub.add_parser("recover", help="complete interrupted commits")
    d = sub.add_parser("doctor", help="run preflight checks and isolation canaries")
    d.add_argument("--json", action="store_true")
    dm = sub.add_parser("daemon", help="run dryrund in the foreground")
    dm.add_argument("--allow-root", action="store_true")
    dm.add_argument("--config", type=Path)
    sub.add_parser("status", help="show daemon status")
    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--yes", action="store_true")
        p.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
        p.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
        p.add_argument("--no-service", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "daemon":
        from dryrun.daemon import main as daemon_main
        return daemon_main((["--allow-root"] if args.allow_root else [])
                           + (["--config", str(args.config)] if args.config else []))
    return {"apply": cmd_apply, "recover": cmd_recover, "doctor": cmd_doctor, "status": cmd_status,
            "install": cmd_install, "uninstall": cmd_uninstall}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/unit/test_cli.py tests/unit/test_static_isolation.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/cli.py tests/unit/test_cli.py
git commit -m "feat: dryrun CLI with apply, recover, doctor, status and reversible install"
```
