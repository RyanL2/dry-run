### Task 13: Triage (F3, spec §4)

**Files:**
- Create: `src/dryrun/triage.py`, `tests/unit/test_triage.py`

**Interfaces:**
- Consumes: `Policy` (Task 1); `Hit`, `text_hits` (Task 12)
- Produces:
  - `Triage(cls: str, reason: str, text: list[Hit], apply_args: tuple[str, str] | None, dev_server: bool)`
  - `classify(command: str, workspace: Path | None, policy: Policy, *, home: Path | None = None) -> Triage`
  - `git_config_safe(workspace: Path | None, home: Path) -> bool`

**Classification order** (first match wins):
1. `has_error` → `shadow` ("could not parse").
2. Any simple command is `dryrun apply …` → `apply`, with `apply_args=(run_id, token)` when both are present.
3. Any T-rule hit on any simple command, including the parsed body of `bash -c '<literal>'` / `sh -c` → `non_shadowable`.
4. A backgrounding `&`, or `nohup`/`setsid`/`disown`, a dev-server pattern, `tail -f`/`-F`, or a `--watch` argument → `long_running`. `dev_server` is True when the full command matches `policy.dev_server_allowlist` (fnmatch).
5. The read-only fast path. It applies only when all of these hold; otherwise → `shadow`:
   - every node is in the allowed node set: program, list, pipeline, command, command_name, word, string/raw_string without expansions, number, concatenation of literals, and redirects only to `/dev/null` or fd duplications;
   - every simple command passes its allowlist validator;
   - for git read commands, `git_config_safe` holds.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_triage.py`:
```python
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
    "r''m -rf build",                       # A quote removal
    "rm${IFS}-rf${IFS}src",                 # B IFS expansion
    "$(echo rm) -rf src",                   # C command substitution
    "echo cm0gLXJmIHNyYw== | base64 -d | sh",  # D encoded pipeline
    "find . -name '*.py' -delete",          # E alternative binary
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


@pytest.mark.parametrize("cmd", ["npm test", "make", "pytest -q", "rm -rf build", "sed -i s/a/b/ x.py"])
def test_everything_else_shadows(cmd):
    assert cls(cmd) == "shadow"


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `... bash scripts/dev/test.sh tests/unit/test_triage.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'dryrun.triage'`)

- [ ] **Step 3: Implement**

`src/dryrun/triage.py`:
```python
"""Static triage (spec §4). The read-only fast path is an allowlist of commands AND flags; anything the
allowlist does not cover is shadowed. Unparseable input is shadowed, never allowed."""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from dryrun.config import Policy
from dryrun.judge.rules import Hit, text_hits

_PARSER = Parser(Language(tree_sitter_bash.language()))
_LITERAL = {"word", "raw_string", "number", "string", "string_content", "concatenation", "\"", "'"}
_STRUCTURE = {"program", "list", "pipeline", "command", "command_name", "redirected_statement",
              "file_redirect", "file_descriptor", "&&", "||", ";", "|", ">", ">>", "2>", "&>", ">&", "<"}
_BG = {"nohup", "setsid", "disown"}
_WATCH = re.compile(r"^--watch(=.*)?$")
_DEV = [("npm", "run", "dev"), ("npm", "run", "start"), ("npm", "start"), ("pnpm", "dev"), ("yarn", "dev"),
        ("vite",), ("next", "dev"), ("uvicorn",), ("flask", "run"), ("python", "-m", "http.server"),
        ("python3", "-m", "http.server"), ("rails", "server"), ("hugo", "server")]


@dataclass
class Triage:
    cls: str
    reason: str
    text: list[Hit] = field(default_factory=list)
    apply_args: tuple[str, str] | None = None
    dev_server: bool = False


# ---------------------------------------------------------------------------------------------------
def _text(node: Node) -> str:
    return node.text.decode("utf-8", "surrogateescape")


def _literal_value(node: Node) -> str | None:
    """The shell value of a literal word, or None if it contains any expansion."""
    t = node.type
    if t in ("word", "number"):
        raw = _text(node)
        # Expansions, globs (a file named "--pre=x" could become a flag), brace and tilde expansion.
        if any(ch in raw for ch in "$`*?[{") or raw.startswith("~") or "=~" in raw or ":~" in raw:
            return None
        return re.sub(r"\\(.)", r"\1", raw)
    if t == "raw_string":
        return _text(node)[1:-1]
    if t == "string":
        parts = []
        for child in node.children:
            if child.type == "string_content":
                parts.append(_text(child))
            elif child.type != '"':
                return None
        return "".join(parts)
    if t == "concatenation":
        vals = [_literal_value(c) for c in node.children]
        return None if any(v is None for v in vals) else "".join(vals)
    return None


def _commands(node: Node) -> list[Node]:
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "command":
            out.append(n)
        stack.extend(reversed(n.children))
    return out


def _argv(cmd: Node) -> list[str] | None:
    """Literal argv of a simple command, or None if any word is not literal."""
    argv = []
    for child in cmd.children:
        if child.type == "command_name":
            v = _literal_value(child.children[0]) if child.children else None
        elif child.type in ("word", "raw_string", "string", "number", "concatenation"):
            v = _literal_value(child)
        else:
            return None
        if v is None:
            return None
        argv.append(v)
    return argv


def _loose_argv(cmd: Node) -> list[str]:
    """Best-effort argv for T-rule detection (unquoted text where values are not literal)."""
    words = []
    for child in cmd.children:
        if child.type == "command_name":
            words.append(_literal_value(child.children[0]) or _text(child))
        elif child.type in ("word", "raw_string", "string", "number", "concatenation"):
            words.append(_literal_value(child) or _text(child))
    return words


def _nested_scripts(argv: list[str]) -> list[str]:
    name = os.path.basename(argv[0]) if argv else ""
    if name in ("bash", "sh", "zsh", "dash") and "-c" in argv:
        i = argv.index("-c")
        if i + 1 < len(argv):
            return [argv[i + 1]]
    return []


def _all_argvs(source: str, depth: int = 0) -> list[list[str]]:
    tree = _PARSER.parse(source.encode("utf-8", "surrogateescape"))
    out = []
    for cmd in _commands(tree.root_node):
        argv = _loose_argv(cmd)
        if not argv:
            continue
        out.append(argv)
        if depth < 3:
            for script in _nested_scripts(argv):
                out.extend(_all_argvs(script, depth + 1))
        name = os.path.basename(argv[0])
        if name in ("sudo", "doas", "xargs", "env", "nohup", "time", "nice", "timeout") and len(argv) > 1:
            out.append([a for a in argv[1:] if not a.startswith("-")] or argv[1:])
    return out


# --- read-only allowlist ----------------------------------------------------------------------------
def _no(flags: tuple[str, ...]):
    def check(args: list[str]) -> bool:
        return not any(a == f or a.startswith(f + "=") or (len(f) == 2 and a.startswith(f) and not a.startswith("--"))
                       for a in args for f in flags)
    return check


_GIT_READ = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "remote"}
_GIT_BAD_FLAGS = ("--output", "--ext-diff", "--textconv", "-o")
_BRANCH_OK = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes", "--merged",
              "--no-merged", "--contains", "--no-contains", "--sort", "--format", "--color", "--no-color",
              "--column", "--no-column", "-l"}


def _git_ok(args: list[str]) -> bool:
    if not args or args[0] not in _GIT_READ:
        return False
    sub, rest = args[0], args[1:]
    if any(a == f or a.startswith(f + "=") for a in rest for f in _GIT_BAD_FLAGS):
        return False
    if sub == "branch":
        return all(a in _BRANCH_OK or a.startswith("--sort=") or a.startswith("--format=")
                   or a.startswith("--contains") or a.startswith("--merged") for a in rest)
    if sub == "remote":
        return rest in ([], ["-v"], ["--verbose"])
    return True


_ALLOW = {
    "ls": lambda a: True, "cat": lambda a: True, "head": lambda a: True, "wc": lambda a: True,
    "stat": lambda a: True, "du": lambda a: True, "df": lambda a: True, "pwd": lambda a: True,
    "echo": lambda a: True, "printf": lambda a: True, "which": lambda a: True, "type": lambda a: True,
    "uname": lambda a: True, "whoami": lambda a: True, "id": lambda a: True, "diff": lambda a: True,
    "cmp": lambda a: True, "jq": lambda a: True, "realpath": lambda a: True, "basename": lambda a: True,
    "dirname": lambda a: True, "true": lambda a: True,
    "tail": _no(("-f", "-F", "--follow")), "file": _no(("-C", "--compile")), "tree": _no(("-o",)),
    "date": _no(("-s", "--set")), "env": lambda a: a == [], "sort": _no(("-o", "--output")),
    "grep": _no(("--pre",)), "egrep": lambda a: True, "fgrep": lambda a: True,
    "rg": _no(("--pre", "--pre-glob", "-z", "--search-zip")),
}


def _redirect_ok(node: Node) -> bool:
    """Only `N>/dev/null`, `&>/dev/null` and fd duplications like `2>&1`."""
    target = [c for c in node.children if c.type in ("word", "number")]
    if not target:
        return False
    value = _text(target[-1])
    return value == "/dev/null" or value.isdigit()


def _structure_ok(node: Node) -> bool:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "file_redirect":
            if not _redirect_ok(n):
                return False
            continue
        if n.type == "command":
            continue  # validated via argv
        if n.type not in _STRUCTURE and n.type not in _LITERAL:
            return False
        stack.extend(n.children)
    return True


_CONFIG_DANGER = re.compile(r"^\s*\[\s*(filter|include|includeif)\b|^\s*(fsmonitor|external|textconv|command)\s*=",
                            re.IGNORECASE | re.MULTILINE)
_DIFF_SECTION = re.compile(r"^\s*\[\s*diff\s*\"", re.IGNORECASE | re.MULTILINE)


def git_config_safe(workspace: Path | None, home: Path) -> bool:
    files = [Path(home) / ".gitconfig", Path(home) / ".config" / "git" / "config", Path("/etc/gitconfig")]
    if workspace is not None:
        files.append(Path(workspace) / ".git" / "config")
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
            continue
        if _CONFIG_DANGER.search(text) or _DIFF_SECTION.search(text):
            return False
    return True


# --- main -------------------------------------------------------------------------------------------
def classify(command: str, workspace: Path | None, policy: Policy, *, home: Path | None = None) -> Triage:
    home = Path(home) if home is not None else Path.home()
    tree = _PARSER.parse(command.encode("utf-8", "surrogateescape"))
    root = tree.root_node
    if root.has_error:
        return Triage("shadow", "could not parse")
    cmds = _commands(root)
    for cmd in cmds:
        argv = _loose_argv(cmd)
        if len(argv) >= 2 and os.path.basename(argv[0]) == "dryrun" and argv[1] == "apply":
            args = argv[2:]
            run_id = args[0] if args and not args[0].startswith("-") else None
            token = args[args.index("--token") + 1] if "--token" in args and args.index("--token") + 1 < len(args) else None
            return Triage("apply", "dryrun apply", apply_args=(run_id, token) if run_id and token else None)
    hits = text_hits(_all_argvs(command))
    if hits:
        return Triage("non_shadowable", hits[0].evidence, text=hits)
    background = any(c.type == "&" for n in [root, *[x for x in _walk(root) if x.type == "list"]] for c in n.children)
    argvs = [_loose_argv(c) for c in cmds]
    first = argvs[0] if argvs else []
    dev = any(tuple(first[:len(p)]) == p for p in _DEV)
    follow = any(os.path.basename(a[0]) == "tail" and any(x in ("-f", "-F", "--follow") for x in a[1:])
                 for a in argvs if a)
    watch = any(_WATCH.match(x) for a in argvs for x in a[1:])
    bg_cmd = any(a and os.path.basename(a[0]) in _BG for a in argvs)
    if background or dev or follow or watch or bg_cmd:
        allowed = any(fnmatch.fnmatchcase(command.strip(), pat) for pat in policy.dev_server_allowlist)
        return Triage("long_running", "long-running or background process", dev_server=allowed)
    if _structure_ok(root) and cmds:
        ok = True
        uses_git = False
        for cmd in cmds:
            argv = _argv(cmd)
            if not argv:
                ok = False
                break
            name = argv[0]
            if name == "git":
                uses_git = True
                if not _git_ok(argv[1:]):
                    ok = False
                    break
            elif name not in _ALLOW or not _ALLOW[name](argv[1:]):
                ok = False
                break
        if ok and (not uses_git or git_config_safe(workspace, home)):
            return Triage("read_only", "read-only allowlist")
    return Triage("shadow", "effect must be observed")


def _walk(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `... bash scripts/dev/test.sh tests/unit/test_triage.py -q`
Expected: PASS (all parametrized cases). If a GuardFall case lands in `read_only`, fix the validator; never loosen the test.

- [ ] **Step 5: Commit**

```bash
git add src/dryrun/triage.py tests/unit/test_triage.py
git commit -m "feat: tree-sitter triage with command+flag allowlist and T-rule detection"
```
