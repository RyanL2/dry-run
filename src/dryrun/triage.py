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


def _walk(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _commands(node: Node) -> list[Node]:
    return [n for n in _walk(node) if n.type == "command"]


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
    """Best-effort argv for T-rule detection (raw text where values are not literal)."""
    words = []
    for child in cmd.children:
        if child.type == "command_name":
            words.append((_literal_value(child.children[0]) if child.children else None) or _text(child))
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


def _any(args: list[str]) -> bool:
    return True


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
    "ls": _any, "cat": _any, "head": _any, "wc": _any, "stat": _any, "du": _any, "df": _any, "pwd": _any,
    "echo": _any, "printf": _any, "which": _any, "type": _any, "uname": _any, "whoami": _any, "id": _any,
    "diff": _any, "cmp": _any, "jq": _any, "realpath": _any, "basename": _any, "dirname": _any, "true": _any,
    "tail": _no(("-f", "-F", "--follow")), "file": _no(("-C", "--compile")), "tree": _no(("-o",)),
    "date": _no(("-s", "--set")), "env": lambda a: a == [], "sort": _no(("-o", "--output")),
    "grep": _no(("--pre",)), "egrep": _any, "fgrep": _any,
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
            token = None
            if "--token" in args and args.index("--token") + 1 < len(args):
                token = args[args.index("--token") + 1]
            return Triage("apply", "dryrun apply", apply_args=(run_id, token) if run_id and token else None)
    hits = text_hits(_all_argvs(command))
    if hits:
        return Triage("non_shadowable", hits[0].evidence, text=hits)
    background = any(c.type == "&" for n in _walk(root) if n.type in ("program", "list") for c in n.children)
    argvs = [_loose_argv(c) for c in cmds]
    first = argvs[0] if argvs else []
    dev = any(tuple(first[:len(p)]) == p for p in _DEV)
    follow = any(a and os.path.basename(a[0]) == "tail" and any(x in ("-f", "-F", "--follow") for x in a[1:])
                 for a in argvs)
    watch = any(_WATCH.match(x) for a in argvs for x in a[1:])
    bg_cmd = any(a and os.path.basename(a[0]) in _BG for a in argvs)
    if background or dev or follow or watch or bg_cmd:
        allowed = any(fnmatch.fnmatchcase(command.strip(), pat) for pat in policy.dev_server_allowlist)
        return Triage("long_running", "long-running or background process", dev_server=allowed)
    if cmds and _structure_ok(root):
        ok, uses_git = True, False
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
