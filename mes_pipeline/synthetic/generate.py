"""人工合成 MES 导出材料（确定性，无随机数）。

覆盖场景：
  1. 缺失 / 歧义交接条码（B-MISS、B-AMB1、B-AMB2）
  2. OPEN 没有 CLOSE（B-DANGLE）
  3. 同刻事件稳定排序（B-PARALLEL、B-JOIN）
  4. 产线固定时差与不同时钟格式（L1 offset+8 / L2 offset+1）
  5. 并行步骤（WELD-A / WELD-B）
  6. 合法返工（B-REWORK，经返工边 INSPECT->CUT 重入）
  7. 缺少必需工序（B-MISSREQ 跳过 WELD-B）
  8. 撤回一条中间事件（B-WD 的 CUT CLOSE 被撤回）
  9. 条码修正引发批次拆分（B-SPLIT 改码后与 B-SPLIT2 分离）

运行: python -m synthetic.generate --root <输出根目录>
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

L1 = "LINE-A"
L2 = "LINE-B"

CLOCKS = {
    L1: {"pattern": "%Y-%m-%d %H:%M:%S", "offset": 8},
    L2: {"pattern": "%d/%m/%Y %H:%M:%S", "offset": 1},
}

PROCESS_SPEC = {
    "edges": [
        ["START", "CUT"],
        ["CUT", "WELD-A"],
        ["CUT", "WELD-B"],
        ["WELD-A", "INSPECT"],
        ["WELD-B", "INSPECT"],
        ["INSPECT", "PACK"],
    ],
    "rework_edges": [["INSPECT", "CUT"]],
    "optional": [],
    "required": ["START", "CUT", "WELD-A", "WELD-B", "INSPECT", "PACK"],
}


def wall(utc_dt: datetime, line: str) -> str:
    cfg = CLOCKS[line]
    local = utc_dt.astimezone(timezone(timedelta(hours=cfg["offset"])))
    return local.strftime(cfg["pattern"])


class EventBuilder:
    def __init__(self, line: str):
        self.line = line
        self.rows: List[dict] = []
        self._seq = 0

    def add(
        self,
        utc_dt: datetime,
        batch: str,
        operation: str,
        action: str,
        barcode: Optional[str],
    ) -> str:
        self._seq += 1
        sid = f"EV-{self.line}-{self._seq:04d}"
        self.rows.append(
            {
                "source_event_id": sid,
                "line_code": self.line,
                "batch_label": batch,
                "operation": operation,
                "action": action,
                "wall_clock": wall(utc_dt, self.line),
                "handover_barcode": barcode,
            }
        )
        return sid

    def pair(self, utc_open, utc_close, batch, op, barcode, action="OPEN"):
        """生成两条配对事件；action 指定第一条（第二条固定取反）。"""
        if action not in ("OPEN", "CLOSE"):
            raise ValueError(
                f"pair() 的 action 必须是 OPEN/CLOSE，收到 {action!r}；"
                "若误把条码传入此位请使用 barcode= 关键字"
            )
        close_action = "CLOSE" if action == "OPEN" else "OPEN"
        self.add(utc_open, batch, op, action, barcode)
        self.add(utc_close, batch, op, close_action, barcode)


def _write_jsonl(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_manifest(path: Path, package_id: str) -> None:
    path.write_text(
        json.dumps({"package_id": package_id}, ensure_ascii=False), encoding="utf-8"
    )


def generate(root: str | Path) -> Path:
    root = Path(root)
    pkg_a = root / "pkg-line-a"
    pkg_b = root / "pkg-line-b"
    pkg_admin = root / "pkg-admin"
    for p in (pkg_a, pkg_b, pkg_admin):
        p.mkdir(parents=True, exist_ok=True)

    a = EventBuilder(L1)
    b = EventBuilder(L2)
    base = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)

    def t(minutes: int) -> datetime:
        return base + timedelta(minutes=minutes)

    # ---- B-JOIN: 完整合法路径，含并行同刻 OPEN，条码 BC-1 跨线关联 ----
    bj = "B-JOIN"
    a.pair(t(0), t(10), bj, "START", "BC-1")
    a.pair(t(20), t(30), bj, "CUT", "BC-1")
    a.add(t(40), bj, "WELD-A", "OPEN", "BC-1")   # 同刻并行 OPEN（行号在前）
    a.add(t(40), bj, "WELD-B", "OPEN", "BC-1")   # 同刻，靠行号稳定排序
    a.add(t(50), bj, "WELD-A", "CLOSE", "BC-1")
    a.add(t(55), bj, "WELD-B", "CLOSE", "BC-1")
    a.pair(t(70), t(80), bj, "INSPECT", "BC-1")
    a.pair(t(90), t(100), bj, "PACK", "BC-1")

    # L2 下游批次，同一交接条码；offset+1、不同格式
    xj = "X-JOIN"
    b.pair(t(130), t(140), xj, "INSPECT", "BC-1")
    b.pair(t(150), t(160), xj, "PACK", "BC-1")

    # ---- B-MISS: 无交接条码 -> MISSING_BARCODE 未归属 ----
    bm = "B-MISS"
    a.pair(t(200), t(210), bm, "START", None)
    a.pair(t(220), t(230), bm, "CUT", None)

    # ---- B-AMB1 / B-AMB2: 共用条码 BC-DUP -> AMBIGUOUS_BARCODE ----
    for label in ("B-AMB1", "B-AMB2"):
        a.pair(t(240), t(250), label, "START", "BC-DUP")
        a.pair(t(260), t(270), label, "CUT", "BC-DUP")

    # ---- B-DANGLE: CUT 只有 OPEN；后续 WELD-A 前序未完成 ----
    bd = "B-DANGLE"
    a.pair(t(300), t(310), bd, "START", "BC-DG")
    a.add(t(320), bd, "CUT", "OPEN", "BC-DG")          # 悬空 OPEN
    a.pair(t(340), t(350), bd, "WELD-A", "BC-DG")      # 前序 CUT 未完成

    # ---- B-PARALLEL: 并行同刻事件排序专项 ----
    bp = "B-PAR"
    a.pair(t(400), t(410), bp, "START", "BC-PAR")
    a.pair(t(420), t(430), bp, "CUT", "BC-PAR")
    a.add(t(440), bp, "WELD-B", "OPEN", "BC-PAR")      # 同刻但行号在前
    a.add(t(440), bp, "WELD-A", "OPEN", "BC-PAR")      # 同刻行号在后
    a.add(t(450), bp, "WELD-B", "CLOSE", "BC-PAR")
    a.add(t(455), bp, "WELD-A", "CLOSE", "BC-PAR")
    a.pair(t(470), t(480), bp, "INSPECT", "BC-PAR")
    a.pair(t(490), t(500), bp, "PACK", "BC-PAR")

    # ---- B-REWORK: INSPECT 后经返工边回到 CUT ----
    br = "B-REWORK"
    a.pair(t(520), t(530), br, "START", "BC-RW")
    a.pair(t(540), t(550), br, "CUT", "BC-RW")
    a.pair(t(560), t(570), br, "WELD-A", "BC-RW")
    a.pair(t(560), t(575), br, "WELD-B", "BC-RW")
    a.pair(t(590), t(600), br, "INSPECT", "BC-RW")
    # 返工重入
    a.pair(t(620), t(630), br, "CUT", "BC-RW")
    a.pair(t(640), t(650), br, "WELD-A", "BC-RW")
    a.pair(t(640), t(655), br, "WELD-B", "BC-RW")
    a.pair(t(670), t(680), br, "INSPECT", "BC-RW")
    a.pair(t(690), t(700), br, "PACK", "BC-RW")

    # ---- B-MISSREQ: 跳过 WELD-B -> 前序违规 + 缺失必需工序 ----
    bq = "B-MISSREQ"
    a.pair(t(720), t(730), bq, "START", "BC-MQ")
    a.pair(t(740), t(750), bq, "CUT", "BC-MQ")
    a.pair(t(760), t(770), bq, "WELD-A", "BC-MQ")      # 没有 WELD-B
    a.pair(t(790), t(800), bq, "INSPECT", "BC-MQ")     # 阻断边 WELD-B->INSPECT
    a.pair(t(810), t(820), bq, "PACK", "BC-MQ")

    # ---- B-WD: 中间事件 CUT CLOSE 将被撤回 ----
    bw = "B-WD"
    a.pair(t(840), t(850), bw, "START", "BC-WD")
    a.add(t(860), bw, "CUT", "OPEN", "BC-WD")
    wd_close = a.add(t(870), bw, "CUT", "CLOSE", "BC-WD")  # 撤回目标
    a.pair(t(890), t(900), bw, "WELD-A", barcode="BC-WD")

    # ---- B-SPLIT(L1) / X-SPLIT2(L2): 初始共用 BC-SPLIT 合法交接，修正后拆分 ----
    bs1, bs2 = "B-SPLIT", "X-SPLIT2"
    a.pair(t(920), t(930), bs1, "START", "BC-SPLIT")
    a.pair(t(940), t(950), bs1, "CUT", "BC-SPLIT")
    b.pair(t(920), t(930), bs2, "START", "BC-SPLIT")
    b.pair(t(940), t(950), bs2, "CUT", "BC-SPLIT")

    _write_jsonl(pkg_a / "events.jsonl", a.rows)
    _write_manifest(pkg_a / "manifest.json", "pkg-line-a")
    _write_jsonl(pkg_b / "events.jsonl", b.rows)
    _write_manifest(pkg_b / "manifest.json", "pkg-line-b")

    # 撤回单保留撤回单号，指向 source_event_id
    _write_jsonl(
        pkg_a / "withdrawals.jsonl",
        [
            {
                "withdrawal_id": "WD-0001",
                "source_event_id": wd_close,
                "reason": "质检复检，撤回 CUT 完工记录",
            }
        ],
    )

    # 条码修正包：独立封存包，纳入与否决定是否拆分
    _write_jsonl(
        pkg_admin / "barcode_corrections.jsonl",
        [
            {
                "correction_id": "BCFIX-0001",
                "line_code": L1,
                "batch_label": bs1,
                "old_barcode": "BC-SPLIT",
                "new_barcode": "BC-SPLIT-NEW",
            }
        ],
    )
    _write_manifest(pkg_admin / "manifest.json", "pkg-admin")

    config = {
        "line_clocks": [
            {
                "line_code": L1,
                "strftime_pattern": CLOCKS[L1]["pattern"],
                "utc_offset_hours": CLOCKS[L1]["offset"],
            },
            {
                "line_code": L2,
                "strftime_pattern": CLOCKS[L2]["pattern"],
                "utc_offset_hours": CLOCKS[L2]["offset"],
            },
        ],
        "process_spec": PROCESS_SPEC,
    }
    (root / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description="生成合成 MES 导出材料")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    out = generate(args.root)
    print(f"合成材料已写入: {out}")


if __name__ == "__main__":
    main()
