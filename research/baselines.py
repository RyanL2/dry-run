"""Reproducible local text and effect rule baselines for benchmark items."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from dryrun.config import load_config
from dryrun.judge.cascade import decide
from dryrun.judge.rules import evaluate
from dryrun.types import EffectRecord


def text_decision(command: str) -> str:
    """A deliberately simple direct-command guard, recorded as such in reports."""
    if re.match(r"^\s*(?:rm|rmdir|truncate|shred)\b", command):
        return "ask"
    return "allow"


def predict(items: Path, output: Path, arm: str) -> int:
    if arm not in {"text", "effect"}:
        raise ValueError("arm must be text or effect")
    policy = load_config(use_user_file=False).policy
    rows = []
    with Path(items).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            record = EffectRecord.from_json(item["effect"])
            if arm == "text":
                decision = text_decision(record.command)
            else:
                hits = evaluate(record, policy)
                decision = decide("shadow", rec=record, hits=hits).decision
            rows.append({"id": item["id"], "decision": decision})
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--arm", choices=("text", "effect"), required=True)
    args = parser.parse_args(argv)
    print(f"wrote {predict(args.items, args.output, args.arm)} {args.arm} decisions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
