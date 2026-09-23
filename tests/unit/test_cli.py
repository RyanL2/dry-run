from __future__ import annotations

import json
from pathlib import Path

from dryrun.cli import main, merge_settings, remove_settings, service_unit, wrapper_script

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
