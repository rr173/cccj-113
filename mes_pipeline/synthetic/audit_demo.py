"""独立状态机审计演示：逐行打印每个合成批次的转移解释。

与 PM4Py 统计完全解耦，只用本项目的 BatchStateMachine 逐行驱动，
便于分析人员人工核对路径统计与违规行位置。

运行（需先生成材料并完成封存）:
    python -I -m synthetic.audit_demo --config data/raw/config.json \
        --store data/store --set-id S1
"""
from __future__ import annotations

import argparse

from mes_pipeline.config import PipelineConfig
from mes_pipeline.ingest import load_sealed_set, parse_package, verify_seal
from mes_pipeline.linking import link_batches
from mes_pipeline.state_machine import BatchStateMachine


def audit(config: PipelineConfig, store: str, set_id: str) -> None:
    package_ids = load_sealed_set(store, set_id)
    all_events, all_withdrawals, all_corrections = [], [], []
    for pid in package_ids:
        assert verify_seal(store, pid), f"封存校验失败: {pid}"
        events, withdrawals, corrections = parse_package(
            f"{store}/packages/{pid}", config
        )
        all_events.extend(events)
        all_withdrawals.extend(withdrawals)
        all_corrections.extend(corrections)

    groups, _ = link_batches(all_events, all_withdrawals, all_corrections)
    for group in sorted(groups, key=lambda g: g.synthetic_batch_id):
        members = ", ".join(sorted(f"{m.line_code}/{m.batch_label}" for m in group.members))
        flag = f"[未归属:{group.unassigned_reason}]" if group.unassigned else "[已归属]"
        print(f"\n=== {group.synthetic_batch_id} {flag} 成员: {members} ===")
        sm = BatchStateMachine(config.process_spec, group.synthetic_batch_id)
        for ev in group.events:
            violations = sm.step(ev)
            r = ev.raw
            line = (
                f"  step {sm.transitions[-1].step:2d} | pkg={r.package_id} row={r.row_number:2d}"
                f" | {r.line_code} {r.operation:8s} {r.action:5s} | {r.source_event_id}"
            )
            print(line)
            for v in violations:
                edge = f" 阻断边 {v.blocking_edge[0]}->{v.blocking_edge[1]}" if v.blocking_edge else ""
                print(f"      !! {v.violation_type}{edge} 行位置={v.row_number}")
        dangling, final_missing = sm.finalize()
        for raw, reason in dangling:
            print(f"      !! 悬空 {reason}: {raw.operation} 行位置={raw.row_number}")
        for v in final_missing:
            print(f"      !! {v.violation_type}: {v.operation}")
        print(f"  返工次数={sm.rework_count} 转移步数={len(sm.transitions)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="状态机逐行审计演示")
    parser.add_argument("--config", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--set-id", required=True)
    args = parser.parse_args()
    audit(PipelineConfig.load(args.config), args.store, args.set_id)


if __name__ == "__main__":
    main()
