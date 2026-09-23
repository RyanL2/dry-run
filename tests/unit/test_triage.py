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
    "git log --oneline -5", "wc -l a b 2>/dev/null", "pwd && ls", "ls 2>&1", "ls >&2",
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
    "sort --compress-program=sh a.txt",
    "rg --hostname-bin=./x foo",
    "ls > 2",
    "echo x > out /dev/null",
    "echo x >& out 2",
    "echo x 2> out /dev/null",
    "file --compi -m m",
    "tail --f x",
    "tail -qf x",
    "tree -R -H . -L 1",
    "git log --show-signature",
    "git log --show-sig",
    "git diff --ext-d",
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
    home = Path("/home/u")
    t = classify("/home/u/.local/bin/dryrun apply 18f-ab12 --token XYZ", None, P, home=home)
    assert t.cls == "apply" and t.apply_args == ("18f-ab12", "XYZ")
    t = classify("/opt/dr apply 18f-ab12 --token XYZ", None, P, home=home, env={"DRYRUN_BIN": "/opt/dr"})
    assert t.apply_args == ("18f-ab12", "XYZ")
    assert classify("dryrun apply a", None, P).cls == "apply"


@pytest.mark.parametrize("cmd", ["./dryrun apply 18f-ab12 --token XYZ", "dryrun apply 18f-ab12 --token XYZ",
                                 "/tmp/dryrun apply 18f-ab12 --token XYZ"])
def test_apply_only_through_the_executable_the_hook_issues(cmd):
    t = classify(cmd, None, P, home=Path("/home/u"))
    assert t.cls == "apply" and t.apply_args is None


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


# --- final-review regressions -------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["uvicorn app; rm -rf ~", "python -m http.server 8000 && rm -rf src",
                                 "uvicorn app > ~/.bashrc", "uvicorn $(evil)"])
def test_dev_server_allowlist_needs_a_single_literal_command(cmd):
    t = classify(cmd, None, P)
    assert not t.dev_server


def test_dev_server_allowlist_still_matches_plain_command():
    assert classify("uvicorn app:main --port 8000", None, P).dev_server


@pytest.mark.parametrize("cmd", ["dryrun apply 68d2f1a3-0badc0de --token t; rm -rf ~",
                                 "echo $(dryrun apply 68d2f1a3-0badc0de --token t)",
                                 "dryrun apply 68d2f1a3-0badc0de --token t --extra",
                                 "dryrun apply 68d2f1a3-0badc0de --token t > out"])
def test_apply_must_be_the_whole_command(cmd):
    t = classify(cmd, None, P)
    assert t.cls == "apply" and t.apply_args is None


def test_git_fast_path_refuses_unresolvable_or_program_config(tmp_path: Path):
    home = tmp_path / "home"
    ws = tmp_path / "wt"
    ws.mkdir()
    (ws / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")      # linked worktree / submodule
    assert not git_config_safe(ws, home)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[gpg]\n\tprogram = /tmp/evil\n")
    assert not git_config_safe(repo, home)
    (repo / ".git" / "config").write_text("[core]\n\tbare = false\n")
    (repo / ".git" / "config.worktree").write_text("[core]\n\tfsmonitor = /tmp/evil\n")
    assert not git_config_safe(repo, home)
    (repo / ".git" / "config.worktree").unlink()
    assert git_config_safe(repo, home)
    assert not git_config_safe(repo, home, env={"GIT_CONFIG_GLOBAL": "/tmp/evil.cfg"})
    xdg = tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_text("[diff]\n\texternal = /tmp/evil\n")
    assert not git_config_safe(repo, home, env={"XDG_CONFIG_HOME": str(xdg)})
    assert classify("git status", repo, P, home=home, env={"GIT_EXTERNAL_DIFF": "x"}).cls == "shadow"


def test_git_fast_path_refuses_submodules_inline_keys_and_oversized_config(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / ".git" / "modules" / "sub").mkdir(parents=True)
    (repo / ".git" / "modules" / "sub" / "config").write_text("[core]\n\tfsmonitor = ./x.sh\n")
    assert not git_config_safe(repo, home)       # child `git status` in the submodule reads that config
    (repo / ".git" / "modules" / "sub" / "config").unlink()
    (repo / ".git" / "modules" / "sub").rmdir()
    (repo / ".git" / "modules").rmdir()
    (repo / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n')
    assert not git_config_safe(repo, home)
    (repo / ".gitmodules").unlink()
    assert git_config_safe(repo, home)
    (repo / ".git" / "config").write_text("[core] fsmonitor = /tmp/evil\n")
    assert not git_config_safe(repo, home)
    (repo / ".git" / "config").write_text("[log]\n\tshowSignature = true\n")
    assert not git_config_safe(repo, home)
    (repo / ".git" / "config").write_text("#" * (2 * 1024 * 1024) + "\n[core]\n\tbare = false\n")
    assert not git_config_safe(repo, home)       # never judge a file we did not read completely


def test_git_fast_path_refuses_hooks_that_read_commands_run(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / ".git" / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n")
    assert git_config_safe(repo, home)
    (repo / ".git" / "hooks" / "post-index-change").write_text("#!/bin/sh\n")   # run by the index refresh
    assert not git_config_safe(repo, home)
    (repo / ".git" / "hooks" / "post-index-change").unlink()
    (repo / ".git" / "config").write_text("[core]\n\thooksPath = .githooks\n")
    assert not git_config_safe(repo, home)
    (repo / ".git" / "config").write_text("[pretty]\n\tsig = %GG\n")                # runs gpg on signed commits
    assert not git_config_safe(repo, home)
    (repo / ".git" / "config").write_text("[core]\n\tbare = false\n")
    (repo / ".git" / "commondir").write_text("../elsewhere\n")                   # config lives in the common dir
    assert not git_config_safe(repo, home)


@pytest.mark.parametrize("cmd", ["git status -uno", "git ls-files -o", "git log -Sfoo --oneline"])
def test_git_short_flags_stay_on_the_fast_path(cmd):
    assert cls(cmd) == "read_only"


@pytest.mark.parametrize("cmd", ["git log --pretty=%GG", "git log --format=%G?", "git show -s --pretty=format:%GK"])
def test_git_signature_placeholders_are_shadowed(cmd):
    assert cls(cmd) != "read_only"


def _git(*args, cwd):
    import subprocess
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def test_git_fast_path_refuses_a_nested_repo_tracked_without_gitmodules(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / "vendor").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=repo)
    assert git_config_safe(repo, home)
    _git("init", "-q", cwd=repo / "vendor")
    _git("commit", "-q", "--allow-empty", "-m", "x", cwd=repo / "vendor")
    _git("add", "vendor", cwd=repo)                                   # a gitlink in the index, no .gitmodules
    assert not (repo / ".gitmodules").exists()
    assert not git_config_safe(repo, home)
    assert classify("git status", repo, P, home=home).cls != "read_only"


def test_fast_path_only_runs_root_owned_system_programs(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".venv" / "bin").mkdir(parents=True)
    fake = ws / ".venv" / "bin" / "cat"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    home = Path("/nonexistent-home")
    assert classify("cat x", ws, P, home=home, env={"PATH": "/usr/bin:/bin"}).cls == "read_only"
    assert classify("cat x", ws, P, home=home, env={"PATH": f"{ws}/.venv/bin:/usr/bin"}).cls != "read_only"
    assert classify("ls", ws, P, home=home, env={"PATH": f"{ws}/.venv/bin:/usr/bin"}).cls == "read_only"
    assert classify("cat x", ws, P, home=home, env={"PATH": ".:/usr/bin"}).cls != "read_only"   # relative entry
    assert classify("echo hi", ws, P, home=home, env={"PATH": f"{ws}/.venv/bin"}).cls == "read_only"  # builtin


@pytest.mark.parametrize("cmd", ["file -z a.lz", "file --uncompress a", "file -Z a", "diff -l a b",
                                 "git branch --format='%(signature)'", "git log --format='%(signature:key)'"])
def test_more_flags_that_start_programs_are_shadowed(cmd):
    assert cls(cmd) != "read_only"


@pytest.mark.parametrize("cmd", ["git log -p --submodule=diff", "git show --submodule=diff HEAD",
                                 "git diff --submodule=log HEAD~1 HEAD", "git diff --submod=diff"])
def test_submodule_diffs_are_shadowed(cmd):
    assert cls(cmd) != "read_only"            # starts a child git inside nested repos, even ones only in history


def test_submodule_config_disables_the_git_fast_path(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for cfg in ("[diff]\n\tsubmodule = diff\n", "[status]\n\tsubmoduleSummary = true\n"):
        (repo / ".git" / "config").write_text(cfg)
        assert not git_config_safe(repo, tmp_path / "home"), cfg


def test_egrep_and_fgrep_are_not_on_the_fast_path(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "grep").write_text("#!/bin/sh\n")
    (ws / ".venv" / "bin" / "grep").chmod(0o755)
    env = {"PATH": f"{ws}/.venv/bin:/usr/bin:/bin"}              # /usr/bin/egrep is `exec grep -E "$@"`
    for cmd in ("egrep foo a.txt", "fgrep foo a.txt"):
        assert classify(cmd, ws, P, home=Path("/nonexistent-home"), env=env).cls != "read_only"


def test_git_fast_path_refuses_a_split_index(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=repo)
    _git("update-index", "--split-index", cwd=repo)   # entries (and any gitlink) move to .git/sharedindex.*
    assert list((repo / ".git").glob("sharedindex.*"))
    assert not git_config_safe(repo, tmp_path / "home")


def test_system_program_requires_root_owned_ancestors(monkeypatch):
    import dryrun.triage as triage
    monkeypatch.setattr(triage, "_root_owned", lambda p: p != "/usr")      # pretend /usr is user-writable
    assert not triage._system_program("ls", "/usr/bin:/bin")
    monkeypatch.setattr(triage, "_root_owned", lambda p: True)
    assert triage._system_program("ls", "/usr/bin:/bin")
