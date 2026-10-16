"""Small QLoRA effect judge: SFT, weighted CE, and calibrated one-pass scoring.

The command text is excluded from the prompt. The training file must contain
only rows marked train. Calibration uses only dev-iterate calibration rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from pathlib import Path

import yaml

from dryrun.judge.features import prompt_for_effect


def _rows(path: Path, allowed_split: str) -> list[dict]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or any(row.get("split") != allowed_split for row in rows):
        raise ValueError(f"{path}: expected nonempty {allowed_split} data only")
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate item ids")
    return rows


def _label_tokens(tokenizer) -> tuple[int, int]:
    ids = [tokenizer.encode(str(digit), add_special_tokens=False) for digit in (0, 1)]
    if any(len(parts) != 1 for parts in ids):
        raise ValueError("this tokenizer needs a single token for labels 0 and 1")
    return ids[0][0], ids[1][0]


def _load_model(base_model: str, revision: str, adapter: Path | None = None, *, training: bool = False):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    if not torch.cuda.is_available():
        raise RuntimeError("judge training/scoring requires a CUDA GPU")
    tokenizer = AutoTokenizer.from_pretrained(base_model, revision=revision)
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16,
                              bnb_4bit_quant_type="nf4")
    model = AutoModelForCausalLM.from_pretrained(base_model, revision=revision, quantization_config=quant,
                                                 device_map={"": 0}, dtype=torch.float16)
    if training:
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model)
    elif adapter is not None:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, str(adapter))
    model.config.use_cache = not training
    return tokenizer, model


def train(items: Path, output: Path, config_path: Path) -> dict:
    import random
    import torch
    import torch.nn.functional as F
    from peft import LoraConfig, get_peft_model

    rows = _rows(items, "train")
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    seed = int(cfg["seed"])
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    tokenizer, model = _load_model(cfg["base_model"], cfg["revision"], training=True)
    zero, one = _label_tokens(tokenizer)
    lora = LoraConfig(r=int(cfg["lora_r"]), lora_alpha=int(cfg["lora_alpha"]),
                      lora_dropout=float(cfg["lora_dropout"]), bias="none", task_type="CAUSAL_LM",
                      target_modules=["q_proj", "v_proj"])
    model = get_peft_model(model, lora)
    model.train()
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad),
                                  lr=float(cfg["learning_rate"]))
    prepared = []
    max_length = int(cfg["max_length"])
    for row in rows:
        ids = tokenizer.encode(prompt_for_effect(row["effect"]), add_special_tokens=False)
        ids = ids[-(max_length - 1):]
        label = one if row["label"] == "destructive" else zero
        prepared.append((ids, label, float(cfg["destructive_weight"]) if label == one else 1.0))
    start = time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    steps = 0
    losses = []
    for _ in range(int(cfg["epochs"])):
        random.shuffle(prepared)
        for ids, label, weight in prepared:
            batch = torch.tensor([ids + [label]], dtype=torch.long, device="cuda")
            logits = model(input_ids=batch).logits[0, -2].float()
            loss = F.cross_entropy(logits[None, :], torch.tensor([label], device="cuda")) * weight
            if not torch.isfinite(loss):
                raise RuntimeError("non-finite training loss")
            loss.backward()
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            steps += 1
            losses.append(float(loss.detach()))
    output.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(output)
    tokenizer.save_pretrained(output)
    metadata = {"base_model": cfg["base_model"], "revision": cfg["revision"], "seed": seed,
                "max_length": max_length, "steps": steps,
                "weighted_loss": statistics.mean(losses), "wall_s": time.monotonic() - start,
                "peak_vram_gb": torch.cuda.max_memory_allocated() / 1e9,
                "gpu_minutes": (time.monotonic() - start) / 60,
                "train_sha256": hashlib.sha256(Path(items).read_bytes()).hexdigest(),
                "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}
    (output / "dryrun_training.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def fit_temperature(margins: dict[str, float], rows: list[dict]) -> float:
    calibration = [r for r in rows if r["fold"] == "calibration"]
    if not calibration or {r["label"] for r in calibration} != {"benign", "destructive"}:
        raise ValueError("calibration needs both labels")
    candidates = [math.exp(x / 10) for x in range(-30, 31)]
    def nll(temp: float) -> float:
        total = 0.0
        for row in calibration:
            p = min(1 - 1e-9, max(1e-9, _sigmoid(margins[row["id"]] / temp)))
            total -= math.log(p if row["label"] == "destructive" else 1 - p)
        return total / len(calibration)
    return min(candidates, key=nll)


def score(items: Path, adapter: Path, output: Path, metadata_path: Path) -> dict:
    import torch

    rows = _rows(items, "dev-iterate")
    metadata = json.loads((adapter / "dryrun_training.json").read_text(encoding="utf-8"))
    tokenizer, model = _load_model(metadata["base_model"], metadata["revision"], adapter)
    zero, one = _label_tokens(tokenizer)
    model.eval()
    margins = {}
    latencies = []
    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        for row in rows:
            ids = tokenizer.encode(prompt_for_effect(row["effect"]), add_special_tokens=False)
            ids = ids[-int(metadata["max_length"]):]
            batch = torch.tensor([ids], dtype=torch.long, device="cuda")
            torch.cuda.synchronize()
            start = time.perf_counter()
            logits = model(input_ids=batch).logits[0, -1].float()
            torch.cuda.synchronize()
            latencies.append((time.perf_counter() - start) * 1000)
            margins[row["id"]] = float(logits[one] - logits[zero])
    temp = fit_temperature(margins, rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps({"id": row["id"], "score": _sigmoid(margins[row["id"]] / temp)})
                              + "\n" for row in rows), encoding="utf-8")
    p95 = sorted(latencies)[min(len(latencies) - 1, math.ceil(0.95 * len(latencies)) - 1)]
    result = {"temperature": temp, "latency_p95_ms": p95,
              "peak_vram_gb": torch.cuda.max_memory_allocated() / 1e9,
              "gpu_minutes": sum(latencies) / 60000,
              "base_model": metadata["base_model"], "adapter": str(adapter)}
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    t = sub.add_parser("train")
    t.add_argument("items", type=Path)
    t.add_argument("output", type=Path)
    t.add_argument("--config", type=Path, default=Path(__file__).parent / "train_config.yaml")
    s = sub.add_parser("score")
    s.add_argument("items", type=Path)
    s.add_argument("adapter", type=Path)
    s.add_argument("output", type=Path)
    s.add_argument("metadata", type=Path)
    args = parser.parse_args(argv)
    result = train(args.items, args.output, args.config) if args.action == "train" else (
        score(args.items, args.adapter, args.output, args.metadata))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
