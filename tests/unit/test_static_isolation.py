"""S2: only the launcher may start processes for user commands. cli.py (installer: systemctl) and
canary.py (isolation harness: fixed argv, host-side target processes) start trusted fixed commands."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "dryrun"
ALLOWED = {"sandbox/spawn.py", "cli.py", "canary.py"}
PATTERN = re.compile(r"\bsubprocess\b|os\.exec\w*\(|os\.spawn\w*\(|os\.posix_spawn|os\.system\(|os\.popen\(")


def test_only_the_launcher_starts_processes():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel not in ALLOWED and PATTERN.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == []
