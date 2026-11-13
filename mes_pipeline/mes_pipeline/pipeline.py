"""管线门面：导入、封存、重算、轨迹查询四个交付入口。

报告按版本持久化：撤回单或条码修正触发重算时生成新版本，
旧版本（含旧批次的路径报告）仍可按 version_id 读取。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .analysis import AnalysisResult, analyze
from .config import PipelineConfig
from .ingest import (
    create_sealed_set,
    load_sealed_set,
    parse_package,
    seal_package,
    verify_seal,
)
from .linking import link_batches
from .models import BarcodeCorrection, CalibratedEvent, Withdrawal
from .state_machine import interpret_group


class Pipeline:
    """围绕封存库目录工作的有状态门面。"""

    def __init__(self, config: PipelineConfig, store_dir: str | Path):
        self.config = config
        self.store_dir = Path(store_dir)

    # ------------------------------------------------------------------ #
    # 入口 1：导入（只读解析，不修改原始目录）
    # ------------------------------------------------------------------ #
    def import_package(self, package_dir: str | Path):
        """解析导出包，返回 (校准事件, 撤回单, 条码修正)。"""
        return parse_package(package_dir, self.config)

    # ------------------------------------------------------------------ #
    # 入口 2：封存（原样复制 + 哈希 + 显式封存集合）
    # ------------------------------------------------------------------ #
    def seal(self, package_dir: str | Path) -> dict:
        sealed = seal_package(package_dir, self.store_dir)
        return {
            "package_id": sealed.package_id,
            "sealed_path": str(sealed.path),
            "sha256": sealed.sha256,
            "sealed_at": sealed.sealed_at,
        }

    def create_sealed_set(self, sealed_set_id: str, package_ids: Sequence[str]) -> str:
        return str(
            create_sealed_set(self.store_dir, sealed_set_id, list(package_ids))
        )

    # ------------------------------------------------------------------ #
    # 内部：从封存库装载一个封存集合的全部数据
    # ------------------------------------------------------------------ #
    def _load_sealed_data(self, sealed_set_id: str):
        package_ids = load_sealed_set(self.store_dir, sealed_set_id)
        all_events: List[CalibratedEvent] = []
        all_withdrawals: List[Withdrawal] = []
        all_corrections: List[BarcodeCorrection] = []
        for pid in package_ids:
            if not verify_seal(self.store_dir, pid):
                raise RuntimeError(f"封存包 {pid} 哈希校验失败，拒绝分析")
            pkg_dir = self.store_dir / "packages" / pid
            events, withdrawals, corrections = parse_package(pkg_dir, self.config)
            all_events.extend(events)
            all_withdrawals.extend(withdrawals)
            all_corrections.extend(corrections)
        return package_ids, all_events, all_withdrawals, all_corrections

    # ------------------------------------------------------------------ #
    # 入口 3：重算（撤回 / 修正后按关联批次整体重建；报告版本化）
    # ------------------------------------------------------------------ #
    def recompute(self, sealed_set_id: str, version_id: Optional[str] = None) -> dict:
        package_ids, events, withdrawals, corrections = self._load_sealed_data(sealed_set_id)

        groups, link_meta = link_batches(events, withdrawals, corrections)
        batches = tuple(interpret_group(self.config.process_spec, g) for g in groups)
        result: AnalysisResult = analyze(batches)

        version_id = version_id or datetime.now(timezone.utc).strftime("v%Y%m%d%H%M%S%f")
        report_dir = self.store_dir / "reports" / sealed_set_id / version_id
        report_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "sealed_set_id": sealed_set_id,
            "version_id": version_id,
            "package_ids": package_ids,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "link_meta": link_meta,
            "analysis": result.to_dict(),
        }
        (report_dir / "report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        latest = self.store_dir / "reports" / sealed_set_id / "latest.json"
        latest.write_text(
            json.dumps({"latest_version": version_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "version_id": version_id,
            "report_path": str(report_dir / "report.json"),
            "link_meta": link_meta,
            "cross_check": result.cross_check,
        }

    def list_report_versions(self, sealed_set_id: str) -> List[str]:
        base = self.store_dir / "reports" / sealed_set_id
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def read_report(self, sealed_set_id: str, version_id: Optional[str] = None) -> dict:
        """读取报告；version_id 缺省时读最新版本。旧版本路径报告永久可读。"""
        if version_id is None:
            latest = self.store_dir / "reports" / sealed_set_id / "latest.json"
            version_id = json.loads(latest.read_text(encoding="utf-8"))["latest_version"]
        path = self.store_dir / "reports" / sealed_set_id / version_id / "report.json"
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # 入口 4：轨迹查询（对实时重算结果或已存报告均可）
    # ------------------------------------------------------------------ #
    def query_trace(
        self,
        sealed_set_id: str,
        synthetic_batch_id: Optional[str] = None,
        *,
        line_batch: Optional[tuple[str, str]] = None,
        version_id: Optional[str] = None,
    ) -> dict:
        """按合成批次 ID 或产线批次 (line_code, batch_label) 查询轨迹。"""
        report = self.read_report(sealed_set_id, version_id)
        hits = []
        for summary in report["analysis"]["batch_summaries"]:
            if synthetic_batch_id and summary["synthetic_batch_id"] != synthetic_batch_id:
                continue
            if line_batch and f"{line_batch[0]}/{line_batch[1]}" not in summary["members"]:
                continue
            hits.append(summary)
        if not hits:
            raise KeyError(
                f"未找到轨迹: batch={synthetic_batch_id} line_batch={line_batch} "
                f"version={version_id or 'latest'}"
            )
        bid = hits[0]["synthetic_batch_id"]
        violations = [
            v for v in report["analysis"]["violations"]
            if v["synthetic_batch_id"] == bid
        ]
        dangling = [
            d for d in report["analysis"]["dangling_operations"]
            if d["synthetic_batch_id"] == bid
        ]
        return {
            "version_id": report["version_id"],
            "summary": hits[0],
            "violations": violations,
            "dangling_operations": dangling,
        }
