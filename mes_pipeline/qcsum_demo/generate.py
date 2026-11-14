"""生成演示材料（八组场景批次文件）与规模材料（十万批次、百万样本）。

用法：
    python -m qcsum_demo.generate --root qcsum_demo/materials --scenario demo
    python -m qcsum_demo.generate --root /tmp/qcsum_bench --scenario bench \
        --lots 100000 --samples 1000000

demo 场景把 mes_pipeline.qcsum.scenarios 里的每一批落成有序 JSON 文件，
并写 manifest.json（场景/步骤标题 + 文件清单），供 qcsum_demo/verify.py 重放。
bench 场景写 lots.ndjson / samples.ndjson（确定性种子，可重复生成）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mes_pipeline.qcsum.datagen import generate_lots, generate_samples
from mes_pipeline.qcsum.scenarios import scenarios


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def gen_demo(root: Path) -> None:
    manifest = []
    for sc_id, sc_title, steps in scenarios():
        sc_dir = root / "steps"
        sc_steps = []
        for i, (step_title, batch) in enumerate(steps, 1):
            name = f"{sc_id}_{i:02d}.json"
            _write_json(sc_dir / name, batch)
            sc_steps.append({"step": step_title, "file": f"steps/{name}",
                             "batch_id": batch["batch_id"]})
        manifest.append({"scenario": sc_id, "title": sc_title, "steps": sc_steps})
    _write_json(root / "manifest.json", {"scenarios": manifest})
    print(f"已生成演示材料：{root}（{sum(len(s['steps']) for s in manifest)} 批）")


def gen_bench(root: Path, n_lots: int, n_samples: int, seed: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lots_path = root / "lots.ndjson"
    samples_path = root / "samples.ndjson"
    with lots_path.open("w", encoding="utf-8") as fh:
        for row in generate_lots(n_lots, seed=seed):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with samples_path.open("w", encoding="utf-8") as fh:
        for row in generate_samples(n_samples, n_lots, seed=seed):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"已生成规模材料：{root}（lots={n_lots}, samples={n_samples}, seed={seed}）")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成质检汇总演示/规模材料")
    parser.add_argument("--root", required=True)
    parser.add_argument("--scenario", choices=("demo", "bench"), required=True)
    parser.add_argument("--lots", type=int, default=100_000)
    parser.add_argument("--samples", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.scenario == "demo":
        gen_demo(root)
    else:
        gen_bench(root, args.lots, args.samples, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
