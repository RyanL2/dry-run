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
    try:
        assert_same(a, b, touched)
    finally:
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
