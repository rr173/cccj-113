"""导入与封存。

职责：
1. 从导出包目录读取事件 / 撤回单 / 条码修正（原始文件只读，永不修改）；
2. 用显式声明的产线时钟把 wall_clock 校准为 UTC；
3. 计算内容哈希并把字节复制进封存库，封存后不可变；
4. 封存集合（sealed set）显式列出纳入分析的 package_id，分析只认封存集合。
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import PipelineConfig
from .models import BarcodeCorrection, CalibratedEvent, RawEvent, Withdrawal


class IngestError(ValueError):
    """导入阶段的数据或配置错误。"""


@dataclass(frozen=True)
class SealedPackage:
    package_id: str
    path: Path
    sha256: Dict[str, str]  # 相对路径 -> 哈希
    sealed_at: str


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _read_rows(path: Path) -> List[dict]:
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    else:
        raise IngestError(f"不支持的事件文件格式: {path.name}（需要 .csv 或 .jsonl）")
    return rows


def _norm(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def calibrate_utc(wall_clock: str, pattern: str, offset_hours: float) -> datetime:
    """按产线显式声明的格式与固定 offset 解析墙钟，返回 UTC aware datetime。"""
    try:
        local_naive = datetime.strptime(wall_clock, pattern)
    except ValueError as exc:
        raise IngestError(f"墙钟 '{wall_clock}' 不符合声明格式 '{pattern}': {exc}") from exc
    return local_naive.replace(
        tzinfo=timezone(timedelta(hours=offset_hours))
    ).astimezone(timezone.utc)


def parse_package(
    package_dir: str | Path, config: PipelineConfig
) -> Tuple[List[CalibratedEvent], List[Withdrawal], List[BarcodeCorrection]]:
    """解析一个导出包，返回校准事件、撤回单、条码修正。"""
    package_dir = Path(package_dir)
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.exists():
        raise IngestError(f"导出包缺少 manifest.json: {package_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_id = str(manifest["package_id"])

    event_files = sorted(package_dir.glob("events.*"))
    withdrawal_files = sorted(package_dir.glob("withdrawals.*"))
    correction_files = sorted(package_dir.glob("barcode_corrections.*"))
    if not (event_files or withdrawal_files or correction_files):
        raise IngestError(
            f"导出包 {package_id} 不含任何 events/withdrawals/barcode_corrections 文件"
        )
    events: List[CalibratedEvent] = []
    seen_ids: set[str] = set()
    for ef in event_files:
        for idx, row in enumerate(_read_rows(ef), start=1):
            sid = _norm(row.get("source_event_id"))
            if not sid:
                raise IngestError(f"{ef.name} 第 {idx} 行缺少 source_event_id")
            if sid in seen_ids:
                raise IngestError(f"source_event_id 重复: {sid}（{package_id}）")
            seen_ids.add(sid)
            line_code = _norm(row.get("line_code")) or ""
            if line_code not in config.clocks:
                raise IngestError(
                    f"事件 {sid} 的产线 '{line_code}' 未在时钟配置中声明，"
                    f"已知产线: {sorted(config.clocks)}"
                )
            clock = config.clocks[line_code]
            wall_clock = _norm(row.get("wall_clock")) or ""
            try:
                utc_time = calibrate_utc(
                    wall_clock, clock.strftime_pattern, clock.utc_offset_hours
                )
            except IngestError as exc:
                raise IngestError(f"事件 {sid} 时间解析失败: {exc}") from exc
            raw = RawEvent(
                source_event_id=sid,
                line_code=line_code,
                batch_label=_norm(row.get("batch_label")) or "",
                operation=_norm(row.get("operation")) or "",
                action=(_norm(row.get("action")) or "").upper(),
                wall_clock=wall_clock,
                row_number=idx,
                package_id=package_id,
                handover_barcode=_norm(row.get("handover_barcode")),
            )
            if not raw.batch_label or not raw.operation:
                raise IngestError(f"事件 {sid} 缺少 batch_label 或 operation")
            events.append(CalibratedEvent(raw=raw, utc_time=utc_time))

    withdrawals: List[Withdrawal] = []
    for wf in sorted(package_dir.glob("withdrawals.*")):
        for idx, row in enumerate(_read_rows(wf), start=1):
            wid = _norm(row.get("withdrawal_id"))
            sid = _norm(row.get("source_event_id"))
            if not wid or not sid:
                raise IngestError(f"{wf.name} 第 {idx} 行撤回单字段不完整")
            withdrawals.append(
                Withdrawal(
                    withdrawal_id=wid,
                    source_event_id=sid,
                    package_id=package_id,
                    reason=_norm(row.get("reason")) or "",
                )
            )

    corrections: List[BarcodeCorrection] = []
    for cf in sorted(package_dir.glob("barcode_corrections.*")):
        for idx, row in enumerate(_read_rows(cf), start=1):
            cid = _norm(row.get("correction_id"))
            if not cid:
                raise IngestError(f"{cf.name} 第 {idx} 行缺少 correction_id")
            corrections.append(
                BarcodeCorrection(
                    correction_id=cid,
                    line_code=_norm(row.get("line_code")) or "",
                    batch_label=_norm(row.get("batch_label")) or "",
                    old_barcode=_norm(row.get("old_barcode")),
                    new_barcode=_norm(row.get("new_barcode")),
                    package_id=package_id,
                )
            )
    return events, withdrawals, corrections


# --------------------------------------------------------------------------- #
# 封存
# --------------------------------------------------------------------------- #
def _hash_tree(src: Path) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        hashes[str(f.relative_to(src))] = digest
    return hashes


def seal_package(package_dir: str | Path, store_dir: str | Path) -> SealedPackage:
    """把导出包原样复制进封存库并记录哈希。原始目录不做任何修改。

    同名 package 再次封存时，内容哈希必须一致，否则拒绝（原始导出不可变）。
    """
    package_dir = Path(package_dir)
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    package_id = str(manifest["package_id"])
    hashes = _hash_tree(package_dir)

    store_dir = Path(store_dir)
    dest = store_dir / "packages" / package_id
    seal_record = store_dir / "packages" / f"{package_id}.seal.json"
    if dest.exists():
        prior = json.loads(seal_record.read_text(encoding="utf-8"))
        if prior["sha256"] != hashes:
            raise IngestError(
                f"封存冲突: package_id={package_id} 已存在但内容哈希不一致，"
                "原始导出不可变，禁止覆盖"
            )
        return SealedPackage(package_id, dest, hashes, prior["sealed_at"])

    shutil.copytree(package_dir, dest)
    sealed_at = datetime.now(timezone.utc).isoformat()
    record = {
        "package_id": package_id,
        "sha256": hashes,
        "sealed_at": sealed_at,
        "source_path": str(package_dir),
    }
    seal_record.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return SealedPackage(package_id, dest, hashes, sealed_at)


def verify_seal(store_dir: str | Path, package_id: str) -> bool:
    """重新计算封存包哈希，与封存记录核对。"""
    store_dir = Path(store_dir)
    record = json.loads(
        (store_dir / "packages" / f"{package_id}.seal.json").read_text(encoding="utf-8")
    )
    current = _hash_tree(store_dir / "packages" / package_id)
    return current == record["sha256"]


def create_sealed_set(
    store_dir: str | Path, sealed_set_id: str, package_ids: Sequence[str]
) -> Path:
    """显式声明纳入分析的封存包集合。只纳入已封存且哈希校验通过的包。"""
    store_dir = Path(store_dir)
    members: List[dict] = []
    for pid in package_ids:
        seal_file = store_dir / "packages" / f"{pid}.seal.json"
        if not seal_file.exists():
            raise IngestError(f"package {pid} 尚未封存，不能纳入封存集合")
        if not verify_seal(store_dir, pid):
            raise IngestError(f"package {pid} 封存哈希校验失败，内容可能被篡改")
        record = json.loads(seal_file.read_text(encoding="utf-8"))
        members.append({"package_id": pid, "sha256": record["sha256"]})
    set_path = store_dir / "sealed_sets" / f"{sealed_set_id}.json"
    set_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sealed_set_id": sealed_set_id,
        "packages": members,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    set_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return set_path


def load_sealed_set(store_dir: str | Path, sealed_set_id: str) -> List[str]:
    path = Path(store_dir) / "sealed_sets" / f"{sealed_set_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [m["package_id"] for m in payload["packages"]]
