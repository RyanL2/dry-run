### Task 20: Fidelity suite (N3): real run vs shadow + commit

**Files:**
- Create: `tests/fidelity/__init__.py` (empty), `tests/fidelity/harness.py`, `tests/fidelity/test_scenarios.py`, `tests/fidelity/test_random_ops.py`

**Interfaces:**
- Consumes: `prepare` (Task 11), `run_shadow` (Task 7), `extract` (Task 9), `apply_changeset`/`CommitError` (Task 15), `Store` (Task 14), `ChangeSet` (Task 2)
- Produces:
  - `harness.twin(scratch, setup) -> (A, B)`: two identical trees
  - `harness.real(ws, cmd)`
  - `harness.shadow_commit(ws, cmd, state) -> (ChangeSet, mtimes)`
  - `harness.tree(root) -> dict`
  - `harness.assert_same(A, B, touched_mtimes)`

**Comparison rules:**
- For every path: type, permission bits, and content or symlink target.
- For files the commit wrote: B's mtime must equal the shadow's mtime (preserved by rename).
- For every other file: A and B must have identical mtimes.
- Directory mtimes are ignored.

- [ ] **Step 1: Write the harness and failing tests**

`tests/fidelity/harness.py`:
```python
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from dryrun.commit import apply_changeset
from dryrun.config import load_config
from dryrun.effects.upper import extract
from dryrun.paths import bwrap_path
from dryrun.sandbox.assemble import prepare
from dryrun.sandbox.spawn import run_shadow
from dryrun.store import Store
from dryrun.types import ChangeSet

ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def standard_tree(ws: Path) -> None:
    (ws / "dir" / "sub").mkdir(parents=True)
    (ws / "keep.txt").write_text("keep\n")
    (ws / "other.txt").write_text("other\n")
    (ws / "script.sh").write_text("#!/bin/sh\necho hi\n")
    (ws / "dir" / "f1").write_text("one\n")
    (ws / "dir" / "f2").write_text("two\n")
    (ws / "dir" / "sub" / "f3").write_text("three\n")
    (ws / "a.log").write_text("log\n")
    (ws / "dir" / "b.log").write_text("log\n")
    (ws / "existing_link").symlink_to("keep.txt")


def twin(scratch: Path, setup=standard_tree) -> tuple[Path, Path]:
    a = scratch / "A" / "ws"
    a.mkdir(parents=True)
    setup(a)
    b = scratch / "B" / "ws"
    shutil.copytree(a, b, symlinks=True, copy_function=shutil.copy2)
    for src in [a, *a.rglob("*")]:  # copytree does not preserve dir mode/mtime exactly; sync them
        dst = b / src.relative_to(a)
        if src.is_dir() and not src.is_symlink():
            shutil.copystat(src, dst)
    return a, b


def real(ws: Path, cmd: str) -> None:
    subprocess.run(["bash", "-c", cmd], cwd=ws, env=ENV, capture_output=True, timeout=60)


def shadow_commit(ws: Path, cmd: str, state: Path) -> tuple[ChangeSet, dict[str, int]]:
    cfg = load_config(use_user_file=False)
    store = Store(state)
    run = store.new_run()
    prep = prepare(run, ws_root=ws, cwd=ws, command=cmd, env=dict(ENV), cfg=cfg, home=Path.home(),
                   state=store.root, bwrap=str(bwrap_path()))
    run_shadow(prep.spec, run_id=run.run_id, out_dir=run.root, cfg=cfg.shadow, watch_fs=run.root)
    eff = extract("workspace", ws, run.ws_up, prep.ws_base)
    cs = ChangeSet(run_id=run.run_id, roots={"workspace": str(ws)}, base_digest="-", ops=eff.ops, refused=eff.refused)
    mtimes = {op.target: op.mtime_ns for op in eff.ops if op.op == "rename_in"}
    try:
        if cs.committable:
            apply_changeset(cs, journal_path=store.journal_dir / f"{run.run_id}.json")
    finally:
        store.remove_run(run)
    return cs, mtimes


def tree(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p), None)
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode), None)
            else:
                with open(p, "rb") as f:
                    out[rel] = ("file", stat.S_IMODE(st.st_mode), f.read(), st.st_mtime_ns)
    return out


def assert_same(a: Path, b: Path, touched: dict[str, int]) -> None:
    ta, tb = tree(a), tree(b)
    assert set(ta) == set(tb), f"paths differ: only real={set(ta) - set(tb)} only shadow={set(tb) - set(ta)}"
    for rel in ta:
        ea, eb = ta[rel], tb[rel]
        assert ea[:3] == eb[:3], f"{rel}: real={ea[:3]!r:.120} shadow={eb[:3]!r:.120}"
        if ea[0] == "file":
            if rel in touched:
                assert eb[3] == touched[rel], f"{rel}: commit did not preserve the reviewed mtime"
            else:
                assert ea[3] == eb[3], f"{rel}: untouched file mtime changed"
```

`tests/fidelity/test_scenarios.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from dryrun.commit import CommitError, apply_changeset
from tests.fidelity.harness import assert_same, real, shadow_commit, twin

pytestmark = [pytest.mark.sandbox, pytest.mark.slow]

SCENARIOS = {
    "create": "echo new > new.txt",
    "append": "printf 'x' >> keep.txt",
    "delete": "rm keep.txt",
    "rm_rf_dir": "rm -rf dir",
    "mkdir_nested": "mkdir -p a/b/c && echo z > a/b/c/z.txt",
    "symlink_new": "ln -s keep.txt link",
    "symlink_replace": "ln -sfn other.txt existing_link",
    "chmod_x": "chmod +x script.sh",
    "rename_file": "mv keep.txt renamed.txt",
    "rename_dir_exdev": "mv dir moved_dir",
    "replace_dir_opaque": "rm -rf dir && mkdir dir && echo fresh > dir/f",
    "truncate": ": > keep.txt",
    "touch_only": "touch -d '2026-02-02 00:00:00' keep.txt",
    "sed_inplace": "sed -i 's/keep/kept/' keep.txt",
    "readonly_dir": "mkdir ro && echo r > ro/f && chmod 555 ro",
    "odd_names": "echo x > 'with space.txt' && echo y > $'new\\nline' && echo u > unicodé.txt && echo d > ./-dash",
    "big_file": "python3 -c \"open('big.bin','wb').write(b'\\0' * 50_000_000)\"",
    "git_init_commit": "git init -q && git add -A && git commit -qm init",
    "copy_then_delete": "cp -r dir dir2 && rm dir/f1",
    "find_delete": "find . -name '*.log' -delete",
    "file_to_dir": "rm other.txt && mkdir other.txt && echo in > other.txt/x",
    "dir_to_file": "rm -rf dir && echo now-a-file > dir",
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario(scratch: Path, name: str):
    a, b = twin(scratch)
    cmd = SCENARIOS[name]
    real(a, cmd)
    cs, touched = shadow_commit(b, cmd, scratch / "state")
    assert cs.committable, cs.refused
    assert_same(a, b, touched)
    for p in (a, b):
        for d in p.rglob("*"):
            if d.is_dir() and not d.is_symlink():
                d.chmod(0o755)


def test_hardlink_is_refused_not_committed(scratch: Path):
    a, b = twin(scratch)
    cs, _ = shadow_commit(b, "ln keep.txt hard.txt", scratch / "state")
    assert not cs.committable and {r["reason"] for r in cs.refused} >= {"hardlink_in_shadow"}
    assert not (b / "hard.txt").exists()
    with pytest.raises(CommitError) as exc:
        apply_changeset(cs, journal_path=scratch / "j.json")
    assert exc.value.code == 3
```

`tests/fidelity/test_random_ops.py`:
```python
from __future__ import annotations

import shlex
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import TEST_ROOT, force_rmtree
from tests.fidelity.harness import assert_same, real, shadow_commit, twin

pytestmark = [pytest.mark.sandbox, pytest.mark.slow]

NAMES = ["a", "b", "d/c", "d/e/f", "g h", "d"]
CONTENT = st.text(alphabet="xyz\n", min_size=0, max_size=20)


def render(op) -> str:
    kind, x, y = op
    q, r = shlex.quote(x), shlex.quote(y)
    return {
        "write": f"mkdir -p \"$(dirname {q})\" && printf %s {r} > {q}",
        "append": f"printf %s {r} >> {q}",
        "rm": f"rm -f {q}",
        "rmrf": f"rm -rf {q}",
        "mkdir": f"mkdir -p {q}",
        "symlink": f"mkdir -p \"$(dirname {q})\" && ln -sfn {r} {q}",
        "chmod": f"chmod {'755' if len(y) % 2 else '600'} {q}",
        "mv": f"mv -f {q} {shlex.quote(y if y in NAMES else 'moved')}",
        "truncate": f": > {q}",
    }[kind]


OPS = st.tuples(st.sampled_from(["write", "append", "rm", "rmrf", "mkdir", "symlink", "chmod", "mv", "truncate"]),
                st.sampled_from(NAMES), st.one_of(st.sampled_from(NAMES), CONTENT))


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(OPS, min_size=1, max_size=8))
def test_random_op_sequences(ops):
    scratch = TEST_ROOT / f"fuzz-{uuid.uuid4().hex[:10]}"
    scratch.mkdir(parents=True)
    try:
        a, b = twin(scratch)
        script = "set +e\n" + "\n".join(render(o) for o in ops) + "\ntrue\n"
        real(a, script)
        cs, touched = shadow_commit(b, script, scratch / "state")
        if not cs.committable:
            return  # refused effects are never committed; covered by test_hardlink_is_refused_not_committed
        assert_same(a, b, touched)
    finally:
        force_rmtree(scratch)
```

- [ ] **Step 2: Run the tests to verify they fail before Tasks 7–15 exist, and pass after**

Run: `... bash scripts/dev/test.sh tests/fidelity -q`
Expected: PASS (all scenarios plus the random sequences). **Treat any mismatch as a real fidelity bug** in `upper.py` or `commit.py`. Fix the code, add the minimal failing sequence as a named scenario, and never loosen `assert_same`.

- [ ] **Step 3: Commit**

```bash
git add tests/fidelity
git commit -m "test: fidelity suite comparing real runs with shadow+commit (scenarios + hypothesis)"
```
