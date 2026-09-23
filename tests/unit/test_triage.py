from __future__ import annotations

from pathlib import Path

import pytest

from dryrun.config import Policy
from dryrun.triage import classify, git_config_safe

P = Policy()


def cls(cmd: str, ws: Path | None = None) -> str:
    return classify(cmd, ws, P, home=Path("/nonexistent-home")).cls


@pytest.mark.parametrize("cmd", [
    "ls -la", "cat README.md", "grep -rn foo src | head -20", "git status", "git diff HEAD~1",
    "git log --oneline -5", "wc -l a b 2>/dev/null", "pwd && ls", "rg TODO src", "sort a.txt",
    "git branch -a", "tail -n 50 log.txt", "echo hi", "stat x",
])
def test_read_only(cmd):
    assert cls(cmd) == "read_only"


# GuardFall classes A-E and anthropics/claude-code#85274 patterns must never reach the fast path.
@pytest.mark.parametrize("cmd", [
    "r''m -rf build",
    "rm${IFS}-rf${IFS}src",
    "$(echo rm) -rf src",
    "echo cm0gLXJmIHNyYw== | base64 -d | sh",
    "find . -name '*.py' -delete",
    "bash script.sh",
    "python3 -c 'import shutil; shutil.rmtree(\"src\")'",
    "ls | xargs rm",
    "V=-rf; rm $V src",
    "cat a > b",
    "sort -o out.txt in.txt",
    "rg --pre ./evil.sh foo",
    "git -c core.pager=sh log",
    "git diff --ext-diff",
    "git branch newbranch",
    "LANG=C ls",
    "env rm -rf x",
    "echo $(whoami)",
    "tree -o out.txt",
    "cat *",
    "ls ~/.ssh",
])
def test_indirection_never_read_only(cmd):
    assert cls(cmd) != "read_only"


def test_unparseable_goes_to_shadow():
    t = classify("rm -rf '", None, P)
    assert (t.cls, t.reason) == ("shadow", "could not parse")


def test_apply_is_recognised_with_args():
    t = classify("dryrun apply 18f-ab12 --token XYZ", None, P)
    assert t.cls == "apply" and t.apply_args == ("18f-ab12", "XYZ")
    assert classify("/home/u/.local/bin/dryrun apply a", None, P).cls == "apply"


@pytest.mark.parametrize("cmd,rule", [
    ("git push origin main", "T1.git_push"),
    ("npm test && git push", "T1.git_push"),
    ("bash -c 'git push --force'", "T1.git_push"),
    ("sudo apt install x", "T3.privilege"),
    ("curl -X DELETE https://api/x", "T6.http_write"),
])
def test_non_shadowable(cmd, rule):
    t = classify(cmd, None, P)
    assert t.cls == "non_shadowable" and t.text[0].rule_id == rule


@pytest.mark.parametrize("cmd,dev", [
    ("npm run dev", True), ("python -m http.server 8000", True), ("sleep 100 &", False),
    ("nohup ./server", False), ("tail -f log.txt", False), ("tsc --watch", False),
])
def test_long_running(cmd, dev):
    t = classify(cmd, None, P)
    assert t.cls == "long_running" and t.dev_server == dev


@pytest.mark.parametrize("cmd", ["npm test", "make", "pytest -q", "rm -rf build", "sed -i s/a/b/ x.py",
                                 "grep -w foo src"])
def test_everything_else_shadows(cmd):
    assert cls(cmd) in ("shadow", "read_only") and classify(cmd, None, P).cls != "long_running"


def test_git_read_falls_to_shadow_when_repo_config_can_run_programs(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "config").write_text("[core]\n\tfsmonitor = /tmp/evil\n")
    assert not git_config_safe(ws, tmp_path / "home")
    assert classify("git status", ws, P, home=tmp_path / "home").cls == "shadow"
    (ws / ".git" / "config").write_text("[core]\n\tbare = false\n[diff \"x\"]\n\ttextconv = cat\n")
    assert not git_config_safe(ws, tmp_path / "home")
    (ws / ".git" / "config").write_text("[core]\n\tbare = false\n")
    assert git_config_safe(ws, tmp_path / "home")
    assert classify("git status", ws, P, home=tmp_path / "home").cls == "read_only"
