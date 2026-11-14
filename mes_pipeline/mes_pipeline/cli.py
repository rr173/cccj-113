"""命令行入口：导入、封存、重算、轨迹查询、配料计算。

示例
----
python -m mes_pipeline.cli seal     --config config.json --store ./store --package ./raw/pkg-a
python -m mes_pipeline.cli seal-set --config config.json --store ./store --set-id S1 \\
    --package pkg-a --package pkg-b
python -m mes_pipeline.cli recompute --config config.json --store ./store --set-id S1 --version v1
python -m mes_pipeline.cli query    --config config.json --store ./store --set-id S1 \\
    --batch SB-xxxx
python -m mes_pipeline.cli query    --config config.json --store ./store --set-id S1 \\
    --line LINE-A --label B-REWORK

配料计算（输入为原料库存/化验/证书/炉次要求，与 MES 活动轨迹无关）：
python -m mes_pipeline.cli charge-solve --store ./store \\
    --inventory inv.csv --assays assays.csv --certs certs.csv --heat heat.json
python -m mes_pipeline.cli charge-show --store ./store --calc-id <calc_id>

量具 R&R（输入为零件×操作员×重复测量的试验数据，与工序轨迹/配料无关）：
python -m mes_pipeline.cli grr-run --store ./store \\
    --study study.json --observations observations.csv --alpha 0.25
python -m mes_pipeline.cli grr-show --store ./store --calc-id <calc_id>

实耗批次谱系（只依据外购批声明与实际领料/产出记录，不从轨迹/配料/量具推测）：
python -m mes_pipeline.cli lineage-load --store ./store \\
    --materials materials.json --operations operations.json --calc-id <id>
python -m mes_pipeline.cli lineage-composition --store ./store --calc-id <id> --batch B-1
python -m mes_pipeline.cli lineage-recall --store ./store --calc-id <id> --suspect RM-X

供应商属性宽表的依赖发现与规则核查（只研究一张表内字段值的对应关系）：
python -m mes_pipeline.cli deps-discover --store ./store \
    --columns deps_demo/s1/columns.json --rows deps_demo/s1/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal --calc-id demo-s1
python -m mes_pipeline.cli deps-check --store ./store \
    --columns deps_demo/s1/columns.json --rows deps_demo/s1/rows.jsonl \
    --k 3 --min-rows 2 --null-policy equal --key supplier --key part
python -m mes_pipeline.cli deps-check --store ./store \
    --columns deps_demo/s1/columns.json --rows deps_demo/s1/rows.jsonl \
    --k 3 --min-rows 2 --null-policy ignore --left supplier --right name
python -m mes_pipeline.cli deps-explain --store ./store --k 3 --min-rows 2 \
    --columns deps_demo/s4/columns.json --rows deps_demo/s4/rows.jsonl \
    --left supplier --right name
python -m mes_pipeline.cli deps-show --store ./store --calc-id demo-s1
python -m mes_pipeline.cli deps-bench --k 3 --min-rows 2 \
    --columns deps_bench/columns.json --rows deps_bench/rows.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import PipelineConfig
from .pipeline import Pipeline


def _build(args: argparse.Namespace) -> Pipeline:
    if not args.config:
        print("该命令需要 --config", file=sys.stderr)
        raise SystemExit(2)
    config = PipelineConfig.load(args.config)
    return Pipeline(config, args.store)


def cmd_import(args: argparse.Namespace) -> int:
    pipe = _build(args)
    events, withdrawals, corrections = pipe.import_package(args.package)
    payload = {
        "event_count": len(events),
        "withdrawal_count": len(withdrawals),
        "correction_count": len(corrections),
        "first_events": [
            {
                "source_event_id": e.raw.source_event_id,
                "line_code": e.raw.line_code,
                "utc_time": e.utc_time.isoformat(),
                "wall_clock": e.raw.wall_clock,
            }
            for e in sorted(events, key=lambda x: x.sort_key)[:5]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_seal(args: argparse.Namespace) -> int:
    pipe = _build(args)
    print(json.dumps(pipe.seal(args.package), ensure_ascii=False, indent=2))
    return 0


def cmd_seal_set(args: argparse.Namespace) -> int:
    pipe = _build(args)
    path = pipe.create_sealed_set(args.set_id, args.package)
    print(json.dumps({"sealed_set": args.set_id, "path": path}, ensure_ascii=False, indent=2))
    return 0


def cmd_recompute(args: argparse.Namespace) -> int:
    pipe = _build(args)
    out = pipe.recompute(args.set_id, version_id=args.version)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    pipe = _build(args)
    report = pipe.read_report(args.set_id, args.version)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    pipe = _build(args)
    if not args.batch and not (args.line and args.label):
        print("查询需提供 --batch 或 (--line 与 --label)", file=sys.stderr)
        return 2
    result = pipe.query_trace(
        args.set_id,
        synthetic_batch_id=args.batch,
        line_batch=(args.line, args.label) if args.line else None,
        version_id=args.version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# 配料计算
# --------------------------------------------------------------------------- #
# 退出码：0 最优 / 3 约束无解 / 4 输入不合法 / 5 求解未收敛
_CHARGE_EXIT = {
    "OPTIMAL": 0,
    "INFEASIBLE": 3,
    "INVALID_INPUT": 4,
    "NODE_LIMIT_EXCEEDED": 5,
}


def cmd_charge_solve(args: argparse.Namespace) -> int:
    from .charge import service  # 延迟导入，保持主管线启动轻量

    result = service.solve_charge(
        inventory=args.inventory,
        assays=args.assays,
        certificates=args.certs,
        heat=args.heat,
        store_dir=args.store,
        calc_id=args.calc_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _CHARGE_EXIT.get(result["status"], 1)


def cmd_charge_show(args: argparse.Namespace) -> int:
    from .charge import service

    try:
        result = service.load_result(args.store, args.calc_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _CHARGE_EXIT.get(result.get("status"), 0)


# --------------------------------------------------------------------------- #
# 量具 R&R（交叉型）
# --------------------------------------------------------------------------- #
# 退出码：0 计算完成（含个别统计量不可定义）/ 4 输入不合法 / 6 设计不完整
_GRR_EXIT = {
    "OK": 0,
    "OK_PARTIAL": 0,
    "INVALID_INPUT": 4,
    "INCOMPLETE_DESIGN": 6,
}


def cmd_grr_run(args: argparse.Namespace) -> int:
    from .gage import service  # 延迟导入，保持主管线启动轻量

    result = service.run_study(
        study=args.study,
        observations=args.observations,
        store_dir=args.store,
        alpha=args.alpha,
        calc_id=args.calc_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _GRR_EXIT.get(result["status"], 1)


def cmd_grr_show(args: argparse.Namespace) -> int:
    from .gage import service

    try:
        result = service.load_result(args.store, args.calc_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _GRR_EXIT.get(result.get("status"), 0)


# --------------------------------------------------------------------------- #
# 实耗批次谱系（lineage；只依据外购批声明与实际领料/产出记录）
# --------------------------------------------------------------------------- #
# 退出码：0 装载/查询成功 / 2 结果或批次不存在 / 4 输入不合法
def cmd_lineage_load(args: argparse.Namespace) -> int:
    from .lineage import service

    result = service.load_lineage(
        materials=args.materials,
        operations=args.operations,
        store_dir=args.store,
        calc_id=args.calc_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == service.STATUS_OK else 4


def cmd_lineage_composition(args: argparse.Namespace) -> int:
    from .lineage import service

    try:
        result = service.show_composition(args.store, args.calc_id, args.batch)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_lineage_recall(args: argparse.Namespace) -> int:
    from .lineage import service

    try:
        result = service.recall(args.store, args.calc_id, args.suspect)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# 单表依赖发现与规则核查（deps）
# --------------------------------------------------------------------------- #
# 退出码：0 完成 / 2 结果不存在 / 4 资料、参数或规则不合法 / 7 规模超限
_DEPS_EXIT = {
    "OK": 0,
    "INVALID_INPUT": 4,
    "SCALE_EXCEEDED": 7,
}


def _deps_args(args: argparse.Namespace) -> dict:
    return {
        "columns": args.columns,
        "rows": args.rows,
        "k": args.k,
        "min_rows": args.min_rows,
        "null_policy": args.null_policy,
    }


def cmd_deps_discover(args: argparse.Namespace) -> int:
    from .deps import service

    result = service.discover_table(
        columns_path=args.columns, rows_path=args.rows,
        k=args.k, min_rows=args.min_rows, null_policy=args.null_policy,
        store_dir=args.store, calc_id=args.calc_id,
        include_rows=args.include_column_rows,
    )
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _DEPS_EXIT.get(result.get("status"), 1)


def cmd_deps_check(args: argparse.Namespace) -> int:
    from .deps import service
    from .deps.engine import RuleError
    from .deps.io import DepsInputError, DepsParameterError, ScaleExceeded

    try:
        result = service.check_rule(
            columns_path=args.columns, rows_path=args.rows,
            k=args.k, min_rows=args.min_rows, null_policy=args.null_policy,
            key_columns=args.key if args.key else None,
            left=args.left, right=args.right,
            store_dir=args.store, calc_id=args.calc_id,
        )
    except (RuleError, DepsInputError, DepsParameterError) as exc:
        print(json.dumps({"status": "INVALID_INPUT",
                          "errors": list(exc.problems),
                          "verdict": None, "counterexample": None},
                         ensure_ascii=False, indent=2))
        return 4
    except ScaleExceeded as exc:
        print(json.dumps({"status": "SCALE_EXCEEDED",
                          "errors": list(exc.problems)},
                         ensure_ascii=False, indent=2))
        return 7
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _DEPS_EXIT.get(result.get("status"), 1)


def cmd_deps_explain(args: argparse.Namespace) -> int:
    from .deps import service
    from .deps.engine import RuleError
    from .deps.io import DepsInputError, DepsParameterError, ScaleExceeded

    try:
        result = service.explain_rule(
            columns_path=args.columns, rows_path=args.rows,
            left=args.left or [], right=args.right,
            k=args.k, min_rows=args.min_rows, null_policy=args.null_policy,
        )
    except (RuleError, DepsInputError, DepsParameterError) as exc:
        print(json.dumps({"status": "INVALID_INPUT",
                          "errors": list(exc.problems)},
                         ensure_ascii=False, indent=2))
        return 4
    except ScaleExceeded as exc:
        print(json.dumps({"status": "SCALE_EXCEEDED",
                          "errors": list(exc.problems)},
                         ensure_ascii=False, indent=2))
        return 7
    print(json.dumps({"status": "OK", **result}, ensure_ascii=False, indent=2))
    return 0


def cmd_deps_bench(args: argparse.Namespace) -> int:
    """对生成样本实测发现耗时与最高内存，并把结论写入结果文件。"""
    import time

    from .deps import service

    started = time.perf_counter()
    result = service.discover_table(
        columns_path=args.columns, rows_path=args.rows,
        k=args.k, min_rows=args.min_rows, null_policy=args.null_policy,
        store_dir=args.store, calc_id=args.calc_id,
    )
    elapsed = time.perf_counter() - started
    try:
        import resource
        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = round(peak_kb / 1024, 1)
        basis = ("Linux ru_maxrss 单位为 KiB，取进程整个生命周期的最高值；"
                 "包含解释器与依赖本身")
    except ImportError:  # pragma: no cover - 非 POSIX
        peak_mb = None
        basis = "该平台不提供 ru_maxrss，未记录最高内存"
    payload = {
        "status": result.get("status"),
        "calc_id": result.get("calc_id"),
        "columns_file": args.columns,
        "rows_file": args.rows,
        "k": args.k,
        "min_rows": args.min_rows,
        "null_policy": args.null_policy,
        "elapsed_seconds": round(elapsed, 2),
        "peak_memory_mb": peak_mb,
        "peak_memory_basis": basis,
        "row_count": result.get("row_count"),
        "column_count": result.get("column_count"),
        "unique_key_count": len(result.get("unique_keys") or []),
        "functional_dependency_count": len(
            result.get("functional_dependencies") or []),
        "complete": result.get("complete"),
        "stats": result.get("stats"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return _DEPS_EXIT.get(result.get("status"), 1)


def cmd_deps_show(args: argparse.Namespace) -> int:
    from .deps import service

    try:
        result = service.load_result(args.store, args.calc_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _DEPS_EXIT.get(result.get("status"), 0)


# --------------------------------------------------------------------------- #
# EDIFACT DESADV 发货通知接入（纯标准库子包，延迟导入）
# --------------------------------------------------------------------------- #
def cmd_desadv(args: argparse.Namespace) -> int:
    from .desadv import cli as desadv_cli

    # 把 --store 映射为 desadv 子命令期望的 --db，其余参数原样转发；
    # REMAINDER 可能保留用户输入的分隔符 --，这里剥掉
    forwarded_args = [a for a in args.desadv_argv if a != "--"]
    forwarded = ["--db", str(Path(args.store) / "desadv.sqlite")] + forwarded_args
    return desadv_cli.main(forwarded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mes_pipeline", description="MES 流程分析管线")
    parser.add_argument("--config", default=None,
                        help="管线配置 JSON（时钟 + 工艺规范）；配料计算命令不需要")
    parser.add_argument("--store", default="store", help="封存库目录（默认 ./store）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser("import", help="只读解析导出包")
    p_imp.add_argument("--package", required=True)
    p_imp.set_defaults(func=cmd_import)

    p_seal = sub.add_parser("seal", help="封存导出包（复制 + 哈希）")
    p_seal.add_argument("--package", required=True)
    p_seal.set_defaults(func=cmd_seal)

    p_set = sub.add_parser("seal-set", help="声明纳入分析的封存集合")
    p_set.add_argument("--set-id", required=True)
    p_set.add_argument("--package", action="append", required=True,
                       help="可重复传入多个 package_id")
    p_set.set_defaults(func=cmd_seal_set)

    p_rec = sub.add_parser("recompute", help="重算分析并版本化报告")
    p_rec.add_argument("--set-id", required=True)
    p_rec.add_argument("--version", default=None)
    p_rec.set_defaults(func=cmd_recompute)

    p_rep = sub.add_parser("report", help="读取已存报告")
    p_rep.add_argument("--set-id", required=True)
    p_rep.add_argument("--version", default=None)
    p_rep.set_defaults(func=cmd_report)

    p_q = sub.add_parser("query", help="轨迹查询")
    p_q.add_argument("--set-id", required=True)
    p_q.add_argument("--batch", default=None, help="合成批次 ID")
    p_q.add_argument("--line", default=None, help="产线代码")
    p_q.add_argument("--label", default=None, help="产线内 batch_label")
    p_q.add_argument("--version", default=None)
    p_q.set_defaults(func=cmd_query)

    p_cs = sub.add_parser("charge-solve", help="配料计算：读取库存/化验/证书/炉次要求并求解")
    p_cs.add_argument("--inventory", required=True, help="原料库存表 CSV")
    p_cs.add_argument("--assays", required=True, help="化验结果表 CSV")
    p_cs.add_argument("--certs", required=True, help="证书登记表 CSV")
    p_cs.add_argument("--heat", required=True, help="炉次要求 JSON")
    p_cs.add_argument("--calc-id", default=None, help="可选：指定本次计算 ID")
    p_cs.set_defaults(func=cmd_charge_solve)

    p_ch = sub.add_parser("charge-show", help="查看某次配料计算结果")
    p_ch.add_argument("--calc-id", required=True)
    p_ch.set_defaults(func=cmd_charge_show)

    p_gr = sub.add_parser("grr-run", help="量具 R&R：载入试验定义与观测行并分析")
    p_gr.add_argument("--study", required=True, help="试验定义 JSON")
    p_gr.add_argument("--observations", required=True, help="观测行 CSV")
    p_gr.add_argument("--alpha", type=float, default=0.25,
                      help="交互项检验水准（默认 0.25）")
    p_gr.add_argument("--calc-id", default=None, help="可选：指定本次分析 ID")
    p_gr.set_defaults(func=cmd_grr_run)

    p_gs = sub.add_parser("grr-show", help="按试验 ID 查看某次量具 R&R 分析结果")
    p_gs.add_argument("--calc-id", required=True)
    p_gs.set_defaults(func=cmd_grr_show)

    p_ll = sub.add_parser("lineage-load",
                          help="实耗批次谱系：装载外购批声明与加工操作记录并计算")
    p_ll.add_argument("--materials", required=True, help="外购原料批声明 JSON")
    p_ll.add_argument("--operations", required=True, help="加工操作记录 JSON")
    p_ll.add_argument("--calc-id", default=None, help="可选：指定本次计算 ID")
    p_ll.set_defaults(func=cmd_lineage_load)

    p_lc = sub.add_parser("lineage-composition",
                          help="查看某已知批的生成/剩余质量与按外购批拆分的构成")
    p_lc.add_argument("--calc-id", required=True)
    p_lc.add_argument("--batch", required=True, help="要查询的批次 ID")
    p_lc.set_defaults(func=cmd_lineage_composition)

    p_lr = sub.add_parser("lineage-recall",
                          help="对指定外购原料批发起在库召回测算")
    p_lr.add_argument("--calc-id", required=True)
    p_lr.add_argument("--suspect", action="append", required=True,
                      help="可疑外购批 ID，可重复传入多个")
    p_lr.set_defaults(func=cmd_lineage_recall)

    p_dd = sub.add_parser(
        "deps-discover",
        help="单表依赖发现：求最小唯一键与最小函数依赖（覆盖全部列组合）")
    p_dd.add_argument("--columns", required=True, help="列清单 JSON")
    p_dd.add_argument("--rows", required=True, help="逐行 JSONL 文件")
    p_dd.add_argument("--k", type=int, required=True,
                      help="最大组合长度（1..列数）")
    p_dd.add_argument("--min-rows", type=int, default=2,
                      help="证据门槛：参与行数少于它即证据不足（≥2）")
    p_dd.add_argument("--null-policy", choices=("equal", "ignore"), default="equal",
                      help="空值口径（默认 equal）")
    p_dd.add_argument("--calc-id", default=None, help="可选：结果 ID")
    p_dd.add_argument("--output", default=None, help="可选：把结果另存为 JSON 文件")
    p_dd.add_argument("--include-column-rows", action="store_true",
                      help="在结果中附每列每个取值的完整 row_id 列表（默认只给摘要）")
    p_dd.set_defaults(func=cmd_deps_discover)

    p_dc = sub.add_parser(
        "deps-check",
        help="规则核查：给定唯一键或单右部依赖，返回成立/违反/证据不足与反例")
    p_dc.add_argument("--columns", required=True, help="列清单 JSON")
    p_dc.add_argument("--rows", required=True, help="逐行 JSONL 文件")
    p_dc.add_argument("--k", type=int, required=True, help="最大组合长度（1..列数）")
    p_dc.add_argument("--min-rows", type=int, default=2, help="证据门槛（≥2）")
    p_dc.add_argument("--null-policy", choices=("equal", "ignore"), default="equal")
    p_dc.add_argument("--key", action="append", default=None,
                      help="唯一键的列名，可重复传入以表示组合键")
    p_dc.add_argument("--left", action="append", default=None,
                      help="依赖左部列名，可重复；可省略表示空左部")
    p_dc.add_argument("--right", default=None, help="依赖右部列名")
    p_dc.add_argument("--calc-id", default=None, help="可选：结果 ID")
    p_dc.set_defaults(func=cmd_deps_check)

    p_de = sub.add_parser(
        "deps-explain",
        help="解释一条违反的依赖：加一列的核查结果与右部列真正的最小左部")
    p_de.add_argument("--columns", required=True, help="列清单 JSON")
    p_de.add_argument("--rows", required=True, help="逐行 JSONL 文件")
    p_de.add_argument("--k", type=int, required=True, help="最大组合长度")
    p_de.add_argument("--min-rows", type=int, default=2, help="证据门槛（≥2）")
    p_de.add_argument("--null-policy", choices=("equal", "ignore"), default="equal")
    p_de.add_argument("--left", action="append", default=None,
                      help="依赖左部列名，可重复；省略表示空左部")
    p_de.add_argument("--right", required=True, help="依赖右部列名")
    p_de.set_defaults(func=cmd_deps_explain)

    p_db = sub.add_parser(
        "deps-bench", help="对生成样本实测依赖发现的耗时与最高内存")
    p_db.add_argument("--columns", required=True, help="列清单 JSON")
    p_db.add_argument("--rows", required=True, help="逐行 JSONL 文件")
    p_db.add_argument("--k", type=int, required=True)
    p_db.add_argument("--min-rows", type=int, default=2)
    p_db.add_argument("--null-policy", choices=("equal", "ignore"), default="equal")
    p_db.add_argument("--calc-id", default=None)
    p_db.set_defaults(func=cmd_deps_bench)

    p_ds = sub.add_parser("deps-show", help="按 ID 查看已保存的依赖发现/核查结果")
    p_ds.add_argument("--calc-id", required=True)
    p_ds.set_defaults(func=cmd_deps_show)

    p_edi = sub.add_parser(
        "desadv",
        help="EDIFACT DESADV 接入：参数原样转发给 mes_pipeline.desadv.cli，"
             "数据库固定为 <store>/desadv.sqlite；例如 "
             "desadv -- desadv-import --file a.edi --block-size 7")
    p_edi.add_argument("desadv_argv", nargs=argparse.REMAINDER,
                       help="desadv 子命令及其参数（建议以 -- 分隔）")
    p_edi.set_defaults(func=cmd_desadv)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
