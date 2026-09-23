"""Static triage (spec §4). The read-only fast path is an allowlist of commands AND flags; anything the
allowlist does not cover is shadowed. Unparseable input is shadowed, never allowed."""
from __future__ import annotations

import fnmatch
import os
import re
import stat
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
def _matches_flag(arg: str, flag: str) -> bool:
    """True if `arg` may select `flag`: long options also match any abbreviation (GNU getopt and git's
    parse-options accept unambiguous prefixes), short options also match inside a cluster like `-qf`."""
    if flag.startswith("--"):
        name = arg.split("=", 1)[0]
        return name.startswith("--") and len(name) > 2 and flag.startswith(name)
    return arg.startswith("-") and not arg.startswith("--") and flag[1] in arg[1:]


def _no(flags: tuple[str, ...]):
    def check(args: list[str]) -> bool:
        return not any(_matches_flag(a, f) for a in args for f in flags)
    return check


def _any(args: list[str]) -> bool:
    return True


_GIT_READ = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "remote"}
# --submodule=diff runs a child git in every nested repo a diff touches, including ones only in history.
_GIT_BAD_FLAGS = ("--output", "--ext-diff", "--textconv", "--show-signature", "--submodule")
# Signature placeholders (--pretty %GG/%GS/%G?, ref-filter %(signature...)) verify signatures, which runs gpg
# or ssh-keygen.
_GPG_PLACEHOLDERS = ("%G", "%(signature")
_BRANCH_OK = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes", "--merged",
              "--no-merged", "--contains", "--no-contains", "--sort", "--format", "--color", "--no-color",
              "--column", "--no-column", "-l"}


def _git_ok(args: list[str]) -> bool:
    if not args or args[0] not in _GIT_READ:
        return False
    sub, rest = args[0], args[1:]
    if any(_matches_flag(a, f) for a in rest for f in _GIT_BAD_FLAGS):
        return False
    if any(p in a for a in rest for p in _GPG_PLACEHOLDERS):
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
    "cmp": _any, "jq": _any, "realpath": _any, "basename": _any, "dirname": _any, "true": _any,
    "diff": _no(("-l", "--paginate")),  # -l pipes through pr
    "tail": _no(("-f", "-F", "--follow")),
    "file": _no(("-C", "--compile", "-z", "-Z", "--uncompress", "--uncompress-noreport")),  # -z execs decompressors
    "tree": _no(("-o", "-R")),  # -R with -H writes 00Tree.html into every directory
    "date": _no(("-s", "--set")), "env": lambda a: a == [],
    "grep": _no(("--pre",)),
    # sort and rg are deliberately absent: both have flags that execute helper programs
    # (sort --compress-program, rg --pre/--hostname-bin/--search-zip). They are shadowed instead.
    # egrep/fgrep are absent: on Debian/Ubuntu they are scripts that look `grep` up in PATH.
}
_BUILTINS = {"echo", "printf", "pwd", "type", "true"}  # bash never looks these up in PATH
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


def _root_owned(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_uid == 0 and not st.st_mode & 0o022


def _system_program(name: str, path_var: str) -> bool:
    """True if the program bash would run for `name` is a root-owned file in a root-owned directory. A PATH
    entry the agent can write to (an activated .venv/bin, node_modules/.bin, a relative entry) could hold a
    program of the same name that would otherwise run outside the shadow."""
    for d in path_var.split(":"):
        if not d.startswith("/"):
            return False  # empty or relative entries resolve against the command's cwd
        p = os.path.join(d, name)
        if not (os.path.isfile(p) and os.access(p, os.X_OK)):
            continue
        real = os.path.realpath(p)
        # Every ancestor too: a user-writable ancestor could have the directory renamed and replaced.
        bases = (os.path.realpath(d), os.path.dirname(real))
        dirs = {a for b in bases for a in (b, *map(str, Path(b).parents))}
        return _root_owned(real) and all(_root_owned(x) for x in dirs)
    return False


def _redirect_ok(node: Node) -> bool:
    """Only `>/dev/null`-style redirects and fd duplications like `2>&1` / `>&2`. A plain `> 2` writes a
    file named "2" and is not read-only."""
    # tree-sitter puts every word after the operator into the destination (`> out /dev/null` has two), and
    # bash writes to the first, so anything but exactly one destination is refused.
    dests = [c for c in node.children if c.is_named and c.type != "file_descriptor"]
    if len(dests) != 1 or dests[0].type not in ("word", "number"):
        return False
    value = _text(dests[0])
    ops = {c.type for c in node.children if not c.is_named}
    if value == "/dev/null":
        return True
    return value.isdigit() and bool(ops & {">&", "<&"})


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


# Keys may follow the section header on the same line (`[core] fsmonitor = x`), so match after `]` too.
_CONFIG_DANGER = re.compile(
    r"\[\s*(filter|include|includeif)\b"
    r"|(^|\])\s*(fsmonitor|external|textconv|command|program|showsignature|hookspath|submodule\w*)\s*(=|$)",
    re.IGNORECASE | re.MULTILINE)
_DIFF_SECTION = re.compile(r"\[\s*diff\s*\"", re.IGNORECASE)
_CONFIG_READ_CAP = 1024 * 1024


_INDEX_READ_CAP = 64 * 1024**2
_GITLINK_MODE = (0o160000).to_bytes(4, "big")  # index entry mode of an embedded repository


class _Unreadable(Exception):
    pass


def _safe_bytes(path: Path, cap: int) -> bytes | None:
    """A small regular file's bytes, read without following symlinks or blocking on FIFOs; None if absent.
    Raises _Unreadable for anything else (not regular, larger than cap, permission denied)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise _Unreadable(str(path)) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            raise _Unreadable(str(path))
        return os.read(fd, cap + 1)
    finally:
        os.close(fd)


def git_config_safe(workspace: Path | None, home: Path, env: dict[str, str] | None = None) -> bool:
    """True only if no config git will read can make a 'read' command run a program. Anything we cannot
    resolve reliably (gitfile worktrees, submodules, GIT_* overrides, oversized files) counts as unsafe."""
    env = env or {}
    if any(k.startswith("GIT_") for k in env):
        return False
    files = [Path(home) / ".gitconfig", Path(home) / ".config" / "git" / "config", Path("/etc/gitconfig")]
    if env.get("XDG_CONFIG_HOME"):
        files.append(Path(env["XDG_CONFIG_HOME"]) / "git" / "config")
    if workspace is not None:
        dotgit = Path(workspace) / ".git"
        if os.path.lexists(dotgit) and not dotgit.is_dir():
            return False  # gitfile: the real config lives elsewhere
        # `git status` recurses into submodules, which read .git/modules/<name>/config.
        if os.path.lexists(Path(workspace) / ".gitmodules") or os.path.lexists(dotgit / "modules"):
            return False
        # commondir moves the config elsewhere; status/diff rewrite the index, which runs post-index-change.
        if os.path.lexists(dotgit / "commondir") or os.path.lexists(dotgit / "hooks" / "post-index-change"):
            return False
        files += [dotgit / "config", dotgit / "config.worktree"]
    try:
        if workspace is not None:
            # An embedded repo in the index (even without .gitmodules) makes status/diff run a child git in
            # it, with that repo's own config and hooks. A stray match inside an object id only costs a shadow.
            index = _safe_bytes(Path(workspace) / ".git" / "index", _INDEX_READ_CAP)
            if index is not None and _GITLINK_MODE in index:
                return False
            # A split index keeps its entries (gitlinks included) in .git/sharedindex.<sha>.
            if any(n.startswith("sharedindex.") for n in os.listdir(Path(workspace) / ".git")):
                return False
        for f in files:
            raw = _safe_bytes(f, _CONFIG_READ_CAP)
            if raw is None:
                continue
            text = raw.decode("utf-8", "replace")
            if (_CONFIG_DANGER.search(text) or _DIFF_SECTION.search(text)
                    or any(p in text for p in _GPG_PLACEHOLDERS)):
                return False
    except _Unreadable:
        return False
    return True


def _single_command(root: Node, cmds: list[Node], *, allow_background: bool = False) -> Node | None:
    """The only command node when the whole program is exactly one simple command (no list, pipe,
    redirect, subshell or substitution); optionally with a trailing `&`."""
    if len(cmds) != 1:
        return None
    named = [c for c in root.children if c.is_named]
    anon = [c.type for c in root.children if not c.is_named]
    if named != [cmds[0]] or any(t not in ("&",) or not allow_background for t in anon):
        return None
    return cmds[0]


# --- main -------------------------------------------------------------------------------------------
def classify(command: str, workspace: Path | None, policy: Policy, *, home: Path | None = None,
             env: dict[str, str] | None = None) -> Triage:
    home = Path(home) if home is not None else Path.home()
    tree = _PARSER.parse(command.encode("utf-8", "surrogateescape"))
    root = tree.root_node
    if root.has_error:
        return Triage("shadow", "could not parse")
    cmds = _commands(root)
    issued_by_hook = (env or {}).get("DRYRUN_BIN") or str(home / ".local" / "bin" / "dryrun")
    for cmd in cmds:
        argv = _loose_argv(cmd)
        names_dryrun = os.path.basename(argv[0]) == "dryrun" or argv[0] == issued_by_hook if argv else False
        if len(argv) >= 2 and argv[1] == "apply" and names_dryrun:
            # Only the exact form the hook itself issues counts; anything else is denied by the cascade.
            only = _single_command(root, cmds)
            exact = _argv(only) if only is not None else None
            ok = (exact is not None and len(exact) == 5 and exact[0] == issued_by_hook
                  and exact[1] == "apply" and exact[3] == "--token")
            return Triage("apply", "dryrun apply", apply_args=(exact[2], exact[4]) if ok else None)
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
        # The allowlist is matched against the literal argv of a single simple command, never against the
        # raw string: "uvicorn *" must not allow "uvicorn app; rm -rf ~".
        only = _single_command(root, cmds, allow_background=True)
        literal = _argv(only) if only is not None else None
        allowed = literal is not None and any(fnmatch.fnmatchcase(" ".join(literal), pat)
                                              for pat in policy.dev_server_allowlist)
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
            if name not in _BUILTINS and not _system_program(name, (env or {}).get("PATH") or _DEFAULT_PATH):
                ok = False
                break
        if ok and (not uses_git or git_config_safe(workspace, home, env)):
            return Triage("read_only", "read-only allowlist")
    return Triage("shadow", "effect must be observed")
