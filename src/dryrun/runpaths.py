"""Layout of one run directory under <state>/runs/<run_id> (always on the workspace's filesystem)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def run_id(self) -> str:
        return self.root.name

    ws_up = property(lambda self: self.root / "ws.up")
    ws_wk = property(lambda self: self.root / "ws.wk")
    tmp_lower = property(lambda self: self.root / "tmp.lower")
    tmp_up = property(lambda self: self.root / "tmp.up")
    tmp_wk = property(lambda self: self.root / "tmp.wk")
    cache = property(lambda self: self.root / "cache")
    decoys = property(lambda self: self.root / "decoys")
    stdout = property(lambda self: self.root / "stdout")
    stderr = property(lambda self: self.root / "stderr")
    trace = property(lambda self: self.root / "trace")
    record = property(lambda self: self.root / "record.json")
    changeset = property(lambda self: self.root / "changeset.json")
    meta = property(lambda self: self.root / "meta.json")

    def cache_up(self, i: int) -> Path:
        return self.cache / f"{i}.up"

    def cache_wk(self, i: int) -> Path:
        return self.cache / f"{i}.wk"

    def create(self) -> "RunPaths":
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for d in (self.ws_up, self.ws_wk, self.tmp_lower, self.tmp_up, self.tmp_wk, self.cache, self.decoys):
            d.mkdir(exist_ok=True, mode=0o700)
        return self
