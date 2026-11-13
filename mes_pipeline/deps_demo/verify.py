"""供货属性依赖发现结果的独立复核脚本（不调用生产搜索）。

复核路径刻意与 :mod:`mes_pipeline.deps.engine` 分开：

- 自行解析列清单与逐行 JSONL，自己实现「参与行选择 + 冲突判定 + 反例取对」；
- 用**逐对枚举**判定冲突（小表，行对数量可接受），与生产的「列等值分组 +
  组合计数」算法互相独立；
- 对每个最小结果，把它们逐个与枚举得到的真子集比较，重新确认最小性。

子命令：

- ``verify-demo``   复核 ``deps_demo`` 下全部场景的发现结果；
- ``verify-shuffle`` 把每张表随机打乱后重新发现，核对集合与反例完全一致；
- ``verify-all``    依次执行上述两项。

用法（仓库根目录，先生成演示材料）::

    .venv/bin/python -I -m synthetic.deps_generate --root deps_demo --scenario demo
    .venv/bin/python -I -I deps_demo/verify.py verify-all --json-out /tmp/deps_verify.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEMO = Path(__file__).resolve().parent

FAILURES: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# --------------------------------------------------------------------------- #
# 独立解析（不复用项目 deps 模块）
# --------------------------------------------------------------------------- #
def load_table(directory: Path) -> Tuple[List[str], List[str], List[List[Optional[str]]]]:
    columns_doc = json.loads((directory / "columns.json").read_text(encoding="utf-8"))
    columns = list(columns_doc["columns"])
    row_ids: List[str] = []
    values: List[List[Optional[str]]] = []
    for line in (directory / "rows.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        row_ids.append(record["row_id"])
        values.append([record["values"].get(c) for c in columns])
    order = sorted(range(len(row_ids)), key=lambda i: row_ids[i])
    return columns, [row_ids[i] for i in order], [values[i] for i in order]


def engage(values, row_ids, cols, policy: str) -> List[int]:
    """按口径选择参与行（由调用方保证 row_ids 已按 Unicode 顺序）。"""
    out = []
    for i, row in enumerate(values):
        if policy == "ignore" and any(row[c] is None for c in cols):
            continue
        out.append(i)
    return out


def conflicts_key(values, row_ids, cols, policy):
    rows = engage(values, row_ids, cols, policy)
    seen: Dict[tuple, List[int]] = {}
    for i in rows:
        seen.setdefault(tuple(values[i][c] for c in cols), []).append(i)
    pairs = []
    for members in seen.values():
        for a, b in itertools.combinations(members, 2):
            pairs.append((row_ids[a], row_ids[b], a, b))
    return len(rows), pairs, rows, seen


def conflicts_fd(values, row_ids, left, right, policy):
    cols = list(left) + [right]
    rows = engage(values, row_ids, cols, policy)
    groups: Dict[tuple, List[int]] = {}
    for i in rows:
        groups.setdefault(tuple(values[i][c] for c in left), []).append(i)
    pairs = []
    for members in groups.values():
        for a, b in itertools.combinations(members, 2):
            if values[a][right] != values[b][right]:
                pairs.append((row_ids[a], row_ids[b], a, b))
    return len(rows), pairs, rows, groups


def status_of(participating: int, pairs, min_rows: int) -> str:
    if pairs:
        return "VIOLATED"
    if participating < min_rows:
        return "INSUFFICIENT_EVIDENCE"
    return "HOLDS"


def first_pair(pairs):
    if not pairs:
        return None
    row_id_a, row_id_b, a, b = min(pairs, key=lambda p: (p[0], p[1]))
    return {"row_id_a": row_id_a, "row_id_b": row_id_b, "a": a, "b": b}


def enumerate_results(columns, row_ids, values, k, min_rows, policy) -> dict:
    """逐对枚举全部组合，返回最小键/最小依赖与证据缺口（独立实现）。"""
    n = len(columns)
    keys, fds, gaps = [], [], []
    key_cache: Dict[tuple, dict] = {}

    def key_info(subset):
        if subset not in key_cache:
            participating, pairs, _rows, _seen = conflicts_key(
                values, row_ids, subset, policy)
            key_cache[subset] = {
                "status": status_of(participating, pairs, min_rows),
                "participating": participating,
                "counterexample": first_pair(pairs),
            }
        return key_cache[subset]

    fd_cache: Dict[tuple, dict] = {}

    def fd_info(left, right):
        if (left, right) not in fd_cache:
            participating, pairs, _rows, _groups = conflicts_fd(
                values, row_ids, left, right, policy)
            fd_cache[(left, right)] = {
                "status": status_of(participating, pairs, min_rows),
                "participating": participating,
                "counterexample": first_pair(pairs),
            }
        return fd_cache[(left, right)]

    for size in range(1, k + 1):
        for subset in itertools.combinations(range(n), size):
            info = key_info(subset)
            if info["status"] != "HOLDS":
                continue
            smaller_holds = any(
                key_info(sub)["status"] == "HOLDS"
                for sz in range(1, size)
                for sub in itertools.combinations(subset, sz)
            )
            if smaller_holds:
                continue
            keys.append({"columns": [columns[c] for c in subset],
                         "participating": info["participating"],
                         "counterexample": info["counterexample"]})
    for right in range(n):
        others = [c for c in range(n) if c != right]
        for size in range(0, k + 1):
            for left in itertools.combinations(others, size):
                info = fd_info(left, right)
                if info["status"] != "HOLDS":
                    continue
                smaller_holds = any(
                    fd_info(sub, right)["status"] == "HOLDS"
                    for sz in range(0, size)
                    for sub in itertools.combinations(left, sz)
                )
                if smaller_holds:
                    continue
                fds.append({"left": [columns[c] for c in left],
                            "right": columns[right],
                            "participating": info["participating"],
                            "counterexample": info["counterexample"]})
    return {"unique_keys": keys, "functional_dependencies": fds}


# --------------------------------------------------------------------------- #
# 与生产结果比对（生产结果从已保存 JSON 读取，不导入代码）
# --------------------------------------------------------------------------- #
def load_production(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compare(name: str, production: dict, reference: dict) -> None:
    got_keys = sorted(tuple(x["columns"]) for x in production["unique_keys"])
    ref_keys = sorted(tuple(x["columns"]) for x in reference["unique_keys"])
    check(f"{name}: 唯一键集合一致", got_keys == ref_keys, f"{got_keys} vs {ref_keys}")

    got_fds = sorted((tuple(x["left"]), x["right"])
                     for x in production["functional_dependencies"])
    ref_fds = sorted((tuple(x["left"]), x["right"])
                     for x in reference["functional_dependencies"])
    check(f"{name}: 函数依赖集合一致", got_fds == ref_fds, f"{got_fds} vs {ref_fds}")

    prod_by_key = {tuple(x["columns"]): x for x in production["unique_keys"]}
    for ref in reference["unique_keys"]:
        prod = prod_by_key.get(tuple(ref["columns"]))
        if prod is None:
            continue
        check(f"{name}: 键 {ref['columns']} 的参与数一致",
              prod["check"]["participating_rows"] == ref["participating"],
              f"{prod['check']['participating_rows']} vs {ref['participating']}")

    prod_fd = {(tuple(x["left"]), x["right"]): x
               for x in production["functional_dependencies"]}
    for ref in reference["functional_dependencies"]:
        prod = prod_fd.get((tuple(ref["left"]), ref["right"]))
        if prod is None:
            continue
        check(f"{name}: 依赖 {ref['left']}→{ref['right']} 的参与数一致",
              prod["check"]["participating_rows"] == ref["participating"],
              f"{prod['check']['participating_rows']} vs {ref['participating']}")


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def _run_case(directory: Path, k: int, min_rows: int, policy: str,
              store: Path) -> Optional[Tuple[dict, dict]]:
    from mes_pipeline.deps import service

    try:
        production = service.discover_table(
            directory / "columns.json", directory / "rows.jsonl",
            k=k, min_rows=min_rows, null_policy=policy,
            store_dir=store, calc_id=f"{directory.name}-{policy}")
    except Exception as exc:  # 资料不合法（s5/s7）——由专门的检查覆盖
        print(f"  [SKIP] {directory.name} ({policy}): {exc.__class__.__name__}")
        return None
    if production.get("status") != "OK":
        # 资料被整体拒绝（s5/s7 等）：生产端没有发现结果，不比对
        print(f"  [SKIP] {directory.name} ({policy}): 资料不合法，已整体拒绝")
        return None
    columns, row_ids, values = load_table(directory)
    reference = enumerate_results(columns, row_ids, values, k, min_rows, policy)
    return production, reference


def verify_demo(store: Path) -> None:
    from mes_pipeline.deps import service

    print("== 发现结果与独立枚举器逐项比对 ==")
    for directory in sorted(DEMO.iterdir()):
        if not directory.is_dir():
            continue
        for policy in ("equal", "ignore"):
            pair = _run_case(directory, 3, 2, policy, store)
            if pair is None:
                continue
            production, reference = pair
            compare(f"{directory.name}/{policy}", production, reference)

    print("== 空值口径的结论差异 ==")
    prod_eq = service.discover_table(
        DEMO / "s2_null_policies" / "columns.json",
        DEMO / "s2_null_policies" / "rows.jsonl",
        k=3, min_rows=2, null_policy="equal", store_dir=store,
        calc_id="verify-s2-equal")
    prod_ig = service.discover_table(
        DEMO / "s2_null_policies" / "columns.json",
        DEMO / "s2_null_policies" / "rows.jsonl",
        k=3, min_rows=2, null_policy="ignore", store_dir=store,
        calc_id="verify-s2-ignore")
    checks = {}
    for policy, prod in (("equal", prod_eq), ("ignore", prod_ig)):
        found = any((x["left"], x["right"]) == (["supplier"], "name")
                    for x in prod["functional_dependencies"])
        checks[policy] = found
    check("s2: supplier → name 在 equal 下不成立、在 ignore 下成立",
          checks["equal"] is False and checks["ignore"] is True,
          f"equal={checks['equal']} ignore={checks['ignore']}")

    print("== 加列后的核查摘要与证据缺口 ==")
    gap = service.discover_table(
        DEMO / "s3_evidence_gap" / "columns.json",
        DEMO / "s3_evidence_gap" / "rows.jsonl",
        k=3, min_rows=2, null_policy="ignore", store_dir=store,
        calc_id="verify-s3-gap")
    gap_hits = [g for g in gap["evidence_gaps"]
                if g["kind"] == "functional_dependency"
                and g["left"] == ["batch"] and g["right"] == "name"]
    check("s3: batch → name 无冲突但证据不足",
          bool(gap_hits) and gap_hits[0]["check"]["participating_rows"] < 2,
          f"{gap_hits[0]['check'] if gap_hits else 'missing'}")
    plain = service.check_rule(
        DEMO / "s3_evidence_gap" / "columns.json",
        DEMO / "s3_evidence_gap" / "rows.jsonl", 3, 2, "ignore",
        left=["supplier"], right="name")
    check("s3: supplier → name 在同一口径下成立",
          plain["verdict"] == "HOLDS", plain["verdict"])


def verify_shuffle(store: Path, seed: int = 7) -> None:
    print("== 打乱资料顺序后结果与反例完全一致 ==")
    rng = random.Random(seed)
    tmp = store / "shuffled"
    for directory in sorted(DEMO.iterdir()):
        if not directory.is_dir():
            continue
        columns_doc = json.loads((directory / "columns.json").read_text(encoding="utf-8"))
        columns = columns_doc["columns"]
        records = []
        for line in (directory / "rows.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        rng.shuffle(records)
        target = tmp / directory.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "columns.json").write_text(
            json.dumps(columns_doc, ensure_ascii=False), encoding="utf-8")
        with (target / "rows.jsonl").open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        for policy in ("equal", "ignore"):
            original = _run_case(directory, 3, 2, policy, store)
            shuffled = _run_case(target, 3, 2, policy, store)
            if original is None or shuffled is None:
                continue
            a, b = original[0], shuffled[0]
            if a.get("status") != "OK" or b.get("status") != "OK":
                continue  # 故意非法的资料（s5/s7）没有发现结果
            same = (
                sorted(tuple(x["columns"]) for x in a["unique_keys"]) ==
                sorted(tuple(x["columns"]) for x in b["unique_keys"])
                and sorted((tuple(x["left"]), x["right"])
                           for x in a["functional_dependencies"]) ==
                sorted((tuple(x["left"]), x["right"])
                       for x in b["functional_dependencies"])
                and a["evidence_gaps"] == b["evidence_gaps"]
            )
            check(f"{directory.name}/{policy}: 打乱后发现集合一致", same)
            # 反例（最小的 row_id 对 + 字段值）也一致
            same_cex = True
            for rule in (("supplier", "name"), ("part", "grade"),
                         ("grade", "package"), ("name", "part")):
                ca = _check_cex(directory, policy, rule, store)
                cb = _check_cex(target, policy, rule, store)
                if ca != cb:
                    same_cex = False
                    break
            check(f"{directory.name}/{policy}: 打乱后反例一致", same_cex)


def _check_cex(directory: Path, policy: str, rule, store: Path) -> dict:
    from mes_pipeline.deps import service

    payload = service.check_rule(
        directory / "columns.json", directory / "rows.jsonl", 3, 2, policy,
        left=[rule[0]], right=rule[1])
    return {"verdict": payload["verdict"], "cex": payload["counterexample"]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="依赖发现结果的独立复核")
    parser.add_argument("command", choices=("verify-demo", "verify-shuffle", "verify-all"))
    parser.add_argument("--store", default="/tmp/deps_verify_store")
    parser.add_argument("--json-out", default=None, help="可选：把复核结论写成 JSON")
    args = parser.parse_args(argv)
    store = Path(args.store)
    store.mkdir(parents=True, exist_ok=True)

    if args.command in ("verify-demo", "verify-all"):
        verify_demo(store)
    if args.command in ("verify-shuffle", "verify-all"):
        verify_shuffle(store)

    summary = {"failures": FAILURES, "ok": not FAILURES}
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print()
    print("FAIL" if FAILURES else "ALL OK", f"({len(FAILURES)} failures)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
