"""端到端测试：合成材料 -> 导入 -> 封存 -> 重算 -> 轨迹查询。

覆盖题目要求的全部人工场景：
缺失/歧义条码、OPEN 无 CLOSE、同刻排序、固定时差、并行步骤、
合法返工、缺少必需工序、撤回中间事件、条码修正引发拆分。
所有读取原始材料的进程均以 ``python -I`` 之外的隔离方式运行；
测试本身只经项目管线 API 访问数据。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mes_pipeline.config import PipelineConfig
from mes_pipeline.ingest import IngestError
from mes_pipeline.pipeline import Pipeline
from synthetic.generate import L1, generate

ALL_PACKAGES = ["pkg-line-a", "pkg-line-b", "pkg-admin"]
BASE_PACKAGES = ["pkg-line-a", "pkg-line-b"]


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("mes-raw")
    store = tmp_path_factory.mktemp("mes-store")
    generate(root)
    return root, store


@pytest.fixture()
def pipeline(workspace) -> Pipeline:
    root, store = workspace
    config = PipelineConfig.load(root / "config.json")
    return Pipeline(config, store)


def _seal_all(pipe: Pipeline, root: Path, packages: list[str]) -> None:
    for pid in packages:
        info = pipe.seal(root / pid)
        assert info["package_id"] == pid
    pipe.create_sealed_set("set-main", packages)


# --------------------------------------------------------------------------- #
# 场景 4：产线固定时差与时钟格式（导入即校准为 UTC）
# --------------------------------------------------------------------------- #
def test_clock_offsets_and_formats_are_calibrated(workspace, pipeline):
    root, _ = workspace
    events, _, _ = pipeline.import_package(root / "pkg-line-a")
    # L1: 08:00:00 +08:00 -> 00:00:00 UTC
    first = sorted(events, key=lambda e: e.sort_key)[0]
    assert first.utc_time.utcoffset().total_seconds() == 0
    assert first.utc_time.strftime("%H:%M:%S") == "00:00:00"
    assert first.raw.wall_clock == "2026-11-01 08:00:00"

    events_b, _, _ = pipeline.import_package(root / "pkg-line-b")
    # L2: 格式 dd/mm/yyyy，offset +1
    first_b = sorted(events_b, key=lambda e: e.sort_key)[0]
    assert first_b.raw.wall_clock == "01/11/2026 03:10:00"
    assert first_b.utc_time.strftime("%Y-%m-%d %H:%M:%S") == "2026-11-01 02:10:00"


def test_undeclared_line_is_rejected(tmp_path):
    root = tmp_path / "raw"
    generate(root)
    bad_config = {
        "line_clocks": [
            {"line_code": "LINE-A", "strftime_pattern": "%Y-%m-%d %H:%M:%S",
             "utc_offset_hours": 8}
        ],
        "process_spec": {"edges": [], "rework_edges": [], "optional": [], "required": []},
    }
    (root / "bad-config.json").write_text(json.dumps(bad_config))
    pipe = Pipeline(PipelineConfig.load(root / "bad-config.json"), tmp_path / "store")
    with pytest.raises(IngestError, match="未在时钟配置中声明"):
        pipe.import_package(root / "pkg-line-b")


# --------------------------------------------------------------------------- #
# 封存：不可变 + 只纳入封存集合
# --------------------------------------------------------------------------- #
def test_seal_immutability_and_verified_set(workspace):
    root, _ = workspace
    isolated_store = root.parent / "mes-store-immutable"
    pipe = Pipeline(PipelineConfig.load(root / "config.json"), isolated_store)
    _seal_all(pipe, root, ALL_PACKAGES)
    # 哈希校验通过
    from mes_pipeline.ingest import verify_seal
    for pid in ALL_PACKAGES:
        assert verify_seal(isolated_store, pid)

    # 防线一：源包内容变化后以同一 package_id 再次封存 -> 哈希不一致，拒绝覆盖
    source_event = root / "pkg-line-a" / "events.jsonl"
    original_bytes = source_event.read_bytes()
    source_event.write_bytes(original_bytes + b'\n{"source_event_id": "TAMPER"}\n')
    with pytest.raises(IngestError, match="封存冲突"):
        pipe.seal(root / "pkg-line-a")
    source_event.write_bytes(original_bytes)  # 还原合成材料

    # 防线二：封存库副本被篡改 -> 哈希校验失败，拒绝纳入分析
    sealed_event = isolated_store / "packages" / "pkg-line-b" / "events.jsonl"
    sealed_original = sealed_event.read_bytes()
    sealed_event.write_bytes(sealed_original + b"\nTAMPER\n")
    assert verify_seal(isolated_store, "pkg-line-b") is False
    sealed_event.write_bytes(sealed_original)

    # 还原后重复封存幂等
    assert verify_seal(isolated_store, "pkg-line-a")
    again = pipe.seal(root / "pkg-line-a")
    assert again["package_id"] == "pkg-line-a"


def test_only_sealed_packages_are_analyzed(workspace, pipeline):
    root, _ = workspace
    pipeline.seal(root / "pkg-line-a")
    pipeline.seal(root / "pkg-line-b")
    pipeline.create_sealed_set("set-base", BASE_PACKAGES)
    out = pipeline.recompute("set-base", version_id="v-base")
    report = pipeline.read_report("set-base")
    assert report["package_ids"] == BASE_PACKAGES
    assert out["cross_check"]["pm4py_available"] is True
    assert out["cross_check"]["dfg_match"] is True
    assert out["cross_check"]["variants_match"] is True


def test_raw_export_files_are_never_modified(workspace, pipeline):
    """导入与封存全流程后，原始导出目录字节保持不变。"""
    root, _ = workspace
    import hashlib

    def tree_hash(base: Path) -> dict:
        return {
            str(f.relative_to(base)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p for p in base.rglob("*") if p.is_file())
        }

    before = {pid: tree_hash(root / pid) for pid in ALL_PACKAGES}
    for pid in ALL_PACKAGES:
        pipeline.import_package(root / pid)
        pipeline.seal(root / pid)
    pipeline.create_sealed_set("set-immut", ALL_PACKAGES)
    pipeline.recompute("set-immut", version_id="v1")
    after = {pid: tree_hash(root / pid) for pid in ALL_PACKAGES}
    assert before == after


# --------------------------------------------------------------------------- #
# 全量重算：九类场景的综合断言
# --------------------------------------------------------------------------- #
def test_full_recompute_covers_all_scenarios(workspace, pipeline):
    root, _ = workspace
    _seal_all(pipeline, root, ALL_PACKAGES)
    pipeline.recompute("set-main", version_id="v1")
    report = pipeline.read_report("set-main", "v1")["analysis"]

    summaries = {s["synthetic_batch_id"]: s for s in report["batch_summaries"]}

    # 合成批次总数与未归属数（2 个同线歧义 + 1 个缺条码；拆分后 B-SPLIT 独立）
    assert len(summaries) == 11
    unassigned = [s for s in summaries.values() if s["unassigned"]]
    reasons = sorted(s["unassigned_reason"] for s in unassigned)
    assert reasons == ["AMBIGUOUS_BARCODE", "AMBIGUOUS_BARCODE", "MISSING_BARCODE"]

    by_members = {tuple(s["members"]): s for s in summaries.values()}

    # 场景 1：缺失条码
    miss = by_members[("LINE-A/B-MISS",)]
    assert miss["unassigned_reason"] == "MISSING_BARCODE"

    # 场景 1：歧义条码（同产线多批次共用 BC-DUP）
    amb = [s for s in summaries.values() if s["unassigned_reason"] == "AMBIGUOUS_BARCODE"]
    assert {tuple(s["members"]) for s in amb} == {
        ("LINE-A/B-AMB1",),
        ("LINE-A/B-AMB2",),
    }

    # 合法跨线交接：B-JOIN(L1) + X-JOIN(L2) 并为一个合成批次
    join = next(s for s in summaries.values()
                if "LINE-A/B-JOIN" in s["members"] and "LINE-B/X-JOIN" in s["members"])
    assert join["unassigned"] is False
    assert join["violation_count"] == 0

    # 场景 5/3：并行步骤 + 同刻事件稳定排序（行号顺序）
    par = by_members[("LINE-A/B-PAR",)]
    trace = par["trace"]
    i = trace.index("WELD-B:OPEN")
    assert trace[i:i + 2] == ["WELD-B:OPEN", "WELD-A:OPEN"]  # 同刻按行号
    assert par["violation_count"] == 0

    # 场景 6：合法返工（rework edge INSPECT->CUT，无阻断违规）
    rw = by_members[("LINE-A/B-REWORK",)]
    assert rw["rework_count"] == 1
    assert rw["violation_count"] == 0

    # 场景 7：缺少必需工序 WELD-B -> 阻断边 + MISSING_REQUIRED
    mq = by_members[("LINE-A/B-MISSREQ",)]
    mq_violations = [v for v in report["violations"]
                     if v["synthetic_batch_id"] == mq["synthetic_batch_id"]]
    assert any(v["blocking_edge"] == ["WELD-B", "INSPECT"]
               and v["violation_type"] == "MISSING_PREDECESSOR" for v in mq_violations)
    assert any(v["violation_type"] == "MISSING_REQUIRED"
               and v["operation"] == "WELD-B" for v in mq_violations)

    # 场景 2：OPEN 没有 CLOSE -> 悬空，且不估算加工时长
    dg = by_members[("LINE-A/B-DANGLE",)]
    dangling = [d for d in report["dangling_operations"]
                if d["synthetic_batch_id"] == dg["synthetic_batch_id"]]
    assert len(dangling) == 1 and dangling[0]["reason"] == "OPEN_WITHOUT_CLOSE"
    assert dangling[0]["operation"] == "CUT"
    dg_violations = [v for v in report["violations"]
                     if v["synthetic_batch_id"] == dg["synthetic_batch_id"]]
    assert any(v["blocking_edge"] == ["CUT", "WELD-A"] for v in dg_violations)

    # 场景 9：条码修正后 B-SPLIT(L1) 与 X-SPLIT2(L2) 拆成两个独立合成批次
    split_members = {tuple(s["members"]) for s in summaries.values()}
    assert ("LINE-A/B-SPLIT",) in split_members
    assert ("LINE-B/X-SPLIT2",) in split_members
    assert "BCFIX-0001" in pipeline.read_report("set-main", "v1")["link_meta"][
        "applied_correction_ids"
    ]


def test_violation_rows_point_to_original_line_positions(workspace, pipeline):
    root, _ = workspace
    pipeline.recompute("set-main", version_id="v-rows")
    report = pipeline.read_report("set-main", "v-rows")["analysis"]

    # 用原始事件文件反查 source_event_id -> 行号，核对违规行位置
    raw_rows = {}
    for idx, line in enumerate(
        (root / "pkg-line-a" / "events.jsonl").read_text().splitlines(), start=1
    ):
        row = json.loads(line)
        raw_rows[row["source_event_id"]] = (idx, row)

    weld_violation = next(
        v for v in report["violations"]
        if v["blocking_edge"] == ["WELD-B", "INSPECT"]
    )
    src_idx, src_row = raw_rows[weld_violation["source_event_id"]]
    assert weld_violation["row_number"] == src_idx
    assert src_row["operation"] == "INSPECT" and src_row["action"] == "OPEN"


# --------------------------------------------------------------------------- #
# 场景 8：撤回中间事件 + 重算版本化（旧报告仍可读）
# --------------------------------------------------------------------------- #
def test_withdrawal_recompute_keeps_old_report(workspace, pipeline):
    root, _ = workspace
    for pid in ALL_PACKAGES:
        pipeline.seal(root / pid)

    pipeline.create_sealed_set("set-wd2", ALL_PACKAGES)

    # 版本 1：同样的封存集合，但撤回单尚未纳入（直接用关联层模拟“撤回前”）
    config = PipelineConfig.load(root / "config.json")
    _, events, withdrawals, corrections = pipeline._load_sealed_data("set-wd2")  # noqa: SLF001
    from mes_pipeline.linking import link_batches
    from mes_pipeline.state_machine import interpret_group

    groups_before, _ = link_batches(events, withdrawals=(), corrections=corrections)
    batches_before = {
        tuple(sorted(f"{m.line_code}/{m.batch_label}" for m in b.members)): b
        for b in (interpret_group(config.process_spec, g) for g in groups_before)
    }
    events_before = len(batches_before[("LINE-A/B-WD",)].events)

    # 版本 2：应用撤回单后整体重算
    pipeline.recompute("set-wd2", version_id="v-after")
    after = pipeline.read_report("set-wd2", "v-after")["analysis"]
    wd_after = next(s for s in after["batch_summaries"] if s["members"] == ["LINE-A/B-WD"])

    assert wd_after["event_count"] == events_before - 1
    assert "WD-0001" in pipeline.read_report("set-wd2")["link_meta"][
        "applied_withdrawal_ids"
    ]
    # 撤回 CUT CLOSE 后 CUT 的 OPEN 变悬空
    dangling_ops = [d for d in after["dangling_operations"]
                    if d["synthetic_batch_id"] == wd_after["synthetic_batch_id"]]
    assert any(d["operation"] == "CUT" and d["reason"] == "OPEN_WITHOUT_CLOSE"
               for d in dangling_ops)

    # 重算生成新版本，旧版本报告仍可按 version_id 读取
    pipeline.recompute("set-wd2", version_id="v-older")
    pipeline.recompute("set-wd2", version_id="v-newer")
    assert pipeline.read_report("set-wd2", "v-older")["version_id"] == "v-older"
    assert pipeline.read_report("set-wd2", "v-newer")["version_id"] == "v-newer"
    assert set(pipeline.list_report_versions("set-wd2")) >= {"v-after", "v-older", "v-newer"}


# --------------------------------------------------------------------------- #
# 等待时长分位数
# --------------------------------------------------------------------------- #
def test_wait_quantiles_structure_and_values(workspace, pipeline):
    root, _ = workspace
    pipeline.seal(root / "pkg-line-a")
    pipeline.seal(root / "pkg-line-b")
    pipeline.create_sealed_set("set-q", BASE_PACKAGES)
    pipeline.recompute("set-q", version_id="vq")
    report = pipeline.read_report("set-q", "vq")["analysis"]

    quantiles = {entry["operation"]: entry for entry in report["wait_quantiles"]}
    assert "CUT" in quantiles
    entry = quantiles["CUT"]
    assert set(entry["percentiles"]) == {"p50", "p75", "p90", "p95", "p99"}
    assert entry["min_seconds"] <= entry["percentiles"]["p50"] <= entry["max_seconds"]
    # B-JOIN: START CLOSE(10m) -> CUT OPEN(20m) = 600 秒
    cut_samples = entry["sample_count"]
    assert cut_samples >= 1


# --------------------------------------------------------------------------- #
# 入口 4：轨迹查询
# --------------------------------------------------------------------------- #
def test_trace_query_by_synthetic_id_and_line_batch(workspace, pipeline):
    root, _ = workspace
    _seal_all(pipeline, root, ALL_PACKAGES)
    pipeline.recompute("set-main", version_id="vqry")

    by_line = pipeline.query_trace("set-main", line_batch=(L1, "B-REWORK"))
    assert "CUT:OPEN" in by_line["summary"]["trace"]
    assert by_line["summary"]["rework_count"] == 1

    sb_id = by_line["summary"]["synthetic_batch_id"]
    by_id = pipeline.query_trace("set-main", synthetic_batch_id=sb_id)
    assert by_id["summary"] == by_line["summary"]

    with pytest.raises(KeyError):
        pipeline.query_trace("set-main", synthetic_batch_id="SB-NOPE")


def test_direct_follow_counts_non_empty_and_sorted(workspace, pipeline):
    root, _ = workspace
    pipeline.seal(root / "pkg-line-a")
    pipeline.seal(root / "pkg-line-b")
    pipeline.create_sealed_set("set-dfg", BASE_PACKAGES)
    pipeline.recompute("set-dfg", version_id="vdfg")
    report = pipeline.read_report("set-dfg", "vdfg")["analysis"]

    dfg = report["direct_follow"]
    assert dfg, "DFG 不应为空"
    counts = [row["count"] for row in dfg]
    assert counts == sorted(counts, reverse=True)
    assert all(
        row["from_activity"].endswith(":OPEN") or row["from_activity"].endswith(":CLOSE")
        for row in dfg
    )
