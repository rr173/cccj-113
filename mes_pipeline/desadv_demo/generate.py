"""DESADV 接入器演示材料生成器（纯标准库）。

生成内容（输出目录见命令行 --root）：

正常材料
~~~~~~~~
- ``01_multi.edi``      两个交换：交换1 含两条通知、父包装后到、同零件跨箱、
                        中文转义备注（默认 UNA，点小数）；交换2 自定义 UNA、
                        逗号小数
- ``02_comma.edi``      单独的自定义 UNA + 逗号小数交换（与 01 中交换2 同身份，
                        用于演示**完整字节重传**）
- ``big/100k.edi``      单交换、100000 条发货行的大文件

故障材料（每个文件前一个交换成功、坏交换随后）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- ``bad_count_mismatch.edi``     UNZ 消息数与实际不符
- ``bad_unt_mismatch.edi``       UNT 段数与实际不符
- ``bad_cycle.edi``              包装成环
- ``bad_orphan.edi``             父包装引用缺失（孤儿）
- ``bad_truncated.edi``          交换尾部未闭合
- ``conflict_same_id.edi``       与 01 中交换同身份但载荷不同
- ``recover_two_exchanges.edi``  第一个交换成功、第二个坏（恢复演示）

用法：
    python -m desadv_demo.generate --root desadv_demo/materials
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

# 默认服务字符
DEFAULT = dict(comp=":", elem="+", decimal=".", release="?", term="'")
# 一套自定义服务字符：组件 |  元素 ^  小数 ,  释放 \  保留空格  段结束 ~
ALT = dict(comp="|", elem="^", decimal=",", release="\\", term="~")


class EdiBuilder:
    """按给定服务字符拼装段，自动处理转义与 UNA。"""

    def __init__(self, chars: dict, una: bool = True) -> None:
        self.c = chars
        self.parts: List[bytes] = []
        if una:
            header = (f"UNA{chars['comp']}{chars['elem']}{chars['decimal']}"
                      f"{chars['release']} {chars['term']}")
            self.parts.append(header.encode("utf-8"))

    def _escape(self, text: str) -> str:
        """转义数据文本中的全部服务字符（含组件分隔符）。"""
        service = {self.c["comp"], self.c["elem"], self.c["release"],
                   self.c["term"]}
        out = []
        for ch in text:
            if ch in service:
                out.append(self.c["release"])
            out.append(ch)
        return "".join(out)

    def seg(self, tag: str, *elements) -> None:
        """拼段。元素可传 str（整体按数据转义）或 list/tuple（结构组件，
        组件之间不转义、每个组件文本各自按数据转义）。"""
        elem = self.c["elem"]
        rendered_elements = [tag]
        for element in elements:
            if isinstance(element, (list, tuple)):
                rendered_elements.append(
                    self.c["comp"].join(self._escape(str(c)) for c in element))
            else:
                rendered_elements.append(self._escape(str(element)))
        rendered = elem.join(rendered_elements)
        self.parts.append((rendered + self.c["term"]).encode("utf-8"))

    def raw(self, text: str) -> None:
        """追加未经转义处理的原始文本（用于故意构造坏文件）。"""
        self.parts.append(text.encode("utf-8"))

    def bytes(self) -> bytes:
        return b"\n".join(self.parts) + b"\n"


# --------------------------------------------------------------------------- #
# 正常交换
# --------------------------------------------------------------------------- #
def build_exchange_one(chars: dict, control: str = "EX-001",
                       una: bool = True) -> bytes:
    """交换1：两条通知；父包装后到；同零件跨箱；中文转义备注。"""
    b = EdiBuilder(chars, una=una)
    dec = chars["decimal"]
    b.seg("UNB", ["UNOC", "3"], "SUP-上海厂", "BUY-北京仓",
          ["261114", "0930"], control)

    # ---- 消息 1：托盘 P1 后到（先出现子箱 B1/B2，再出现 P1） ----
    b.seg("UNH", "MSG-1", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN-2026-001", "9")
    b.seg("DTM", ["137", "20261114", "102"])
    b.seg("NAD", "BY", ["CUST-7", "", "92"])    # 顺序不限：BY 在前
    b.seg("NAD", "SU", ["SUP-001", "", "92"])
    # 箱 B1（父 P1 尚未出现，演示父项后到）
    b.seg("CPS", "B1", "P1")
    b.seg("PAC", "2", "", "CT")
    b.seg("GIN", "BJ", "LBL-B1")
    b.seg("LIN", "L01", "", ["PART-阿尔法", "IN"])
    b.seg("QTY", ["12", f"3{dec}5", "PCE"])
    # 备注：含全部四类服务字符的转义 + 中文 + 保留空格
    note = _note_with_all_escapes(chars)
    b.seg("FTX", "AAI", "", "", note)
    b.seg("LIN", "L02", "", ["PART-A", "IN"])   # 同零件号 PART-A 跨箱
    b.seg("QTY", ["12", f"2{dec}5", "KGM"])
    # 箱 B2
    b.seg("CPS", "B2", "P1")
    b.seg("PAC", "1", "", "CT")
    b.seg("GIN", "BJ", "LBL-B2")
    b.seg("LIN", "L03", "", ["PART-A", "IN"])   # 与 B1 同零件号，不合并
    b.seg("QTY", ["12", "10", "PCE"])
    b.seg("FTX", "AAI", "", "", "第二箱 中文备注 空格保留")
    # 托盘 P1（父项后到），无零件，仅装下级包装
    b.seg("CPS", "P1", "")
    b.seg("PAC", "1", "", "201")
    b.seg("GIN", "BJ", "LBL-P1")
    b.seg("UNT", "23", "MSG-1")

    # ---- 消息 2：独立根托盘，多单位分别汇总 ----
    b.seg("UNH", "MSG-2", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN-2026-002", "9")
    b.seg("DTM", ["137", "20261114", "102"])
    b.seg("NAD", "SU", ["SUP-001", "", "92"])
    b.seg("NAD", "BY", ["CUST-7", "", "92"])
    b.seg("CPS", "P9", "")
    b.seg("PAC", "4", "", "201")
    b.seg("CPS", "K1", "P9")
    b.seg("PAC", "4", "", "BX")
    b.seg("LIN", "M01", "", ["PART-A", "IN"])
    b.seg("QTY", ["12", f"0{dec}25", "KGM"])    # 前导零按文本保留在编号上；数量精确
    b.seg("LIN", "M02", "", ["PART-B", "IN"])
    b.seg("QTY", ["12", "100", "PCE"])
    b.seg("UNT", "14", "MSG-2")

    b.seg("UNZ", "2", control)
    return b.bytes()


def _note_with_all_escapes(chars: dict) -> str:
    """构造包含组件/元素/段结束/释放符四类转义的中文备注。"""
    return (f"中文备注 释放符{chars['release']*2} 元素符{chars['elem']} "
            f"组件符{chars['comp']} 段结束符{chars['term']} 尾")


def build_exchange_two(chars: dict, control: str = "EX-002",
                       una: bool = True) -> bytes:
    """交换2：自定义 UNA、逗号小数（数量用逗号书写）。"""
    b = EdiBuilder(chars, una=una)
    b.seg("UNB", ["UNOC", "3"], "SUP-002", "CUST-8",
          ["261115", "1015"], control)
    b.seg("UNH", "MSG-9", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN-2026-009", "9")
    b.seg("DTM", ["137", "20261115", "102"])
    b.seg("NAD", "SU", ["SUP-002", "", "92"])
    b.seg("NAD", "BY", ["CUST-8", "", "92"])
    b.seg("CPS", "R1", "")
    b.seg("PAC", "1", "", "PAL")
    b.seg("CPS", "C1", "R1")
    b.seg("PAC", "3", "", "CT")
    b.seg("LIN", "N01", "", ["PART-C", "IN"])
    b.seg("QTY", ["12", "1,25", "KGM"])        # 逗号小数
    b.seg("LIN", "N02", "", ["PART-C", "IN"])
    b.seg("QTY", ["12", "7", "PCE"])
    b.seg("UNT", "14", "MSG-9")
    b.seg("UNZ", "1", control)
    return b.bytes()


def build_big_exchange(lines: int = 100_000) -> bytes:
    """单交换大文件：固定两层包装，每箱 10 行，共 lines 条发货行。"""
    b = EdiBuilder(DEFAULT, una=True)
    b.seg("UNB", ["UNOC", "3"], "SUP-BIG", "CUST-BIG",
          ["261116", "0800"], "EX-BIG")
    b.seg("UNH", "MSG-BIG", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN-BIG", "9")
    b.seg("DTM", ["137", "20261116", "102"])
    b.seg("NAD", "SU", ["SUP-BIG", "", "92"])
    b.seg("NAD", "BY", ["CUST-BIG", "", "92"])
    b.seg("CPS", "ROOT", "")
    b.seg("PAC", "1", "", "201")
    per_box = 10
    boxes = (lines + per_box - 1) // per_box
    line_index = 0
    for box in range(1, boxes + 1):
        box_code = f"BOX{box:06d}"             # 前导零保留
        b.seg("CPS", box_code, "ROOT")
        b.seg("PAC", "1", "", "CT")
        for _ in range(per_box):
            if line_index >= lines:
                break
            line_index += 1
            part = f"PART{(line_index % 50):03d}"
            unit = "PCE" if line_index % 2 else "KGM"
            qty = "1.25" if unit == "KGM" else str((line_index % 9) + 1)
            b.seg("LIN", f"{line_index:07d}", "", [part, "IN"])
            b.seg("QTY", ["12", qty, unit])
    # 段数：UNH,BGM,DTM,2 NAD,ROOT CPS+PAC = 7；每箱 CPS+PAC=2；
    # 每行 LIN+QTY=2；最后 UNT 自身 1
    total = 7 + boxes * 2 + lines * 2 + 1
    b.seg("UNT", str(total), "MSG-BIG")
    b.seg("UNZ", "1", "EX-BIG")
    return b.bytes()


# --------------------------------------------------------------------------- #
# 故障材料（每个文件第一个交换保持完好，第二个交换带特定故障）
# --------------------------------------------------------------------------- #
def _good_prefix(chars: dict) -> bytes:
    b = EdiBuilder(chars, una=True)
    b.seg("UNB", ["UNOC", "3"], "SUP-GOOD", "CUST-GOOD",
          ["261114", "0900"], "EX-GOOD")
    b.seg("UNH", "MSG-G", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN-GOOD", "9")
    b.seg("DTM", ["137", "20261114", "102"])
    b.seg("NAD", "SU", ["SUP-GOOD", "", "92"])
    b.seg("NAD", "BY", ["CUST-GOOD", "", "92"])
    b.seg("CPS", "G1", "")
    b.seg("PAC", "1", "", "201")
    b.seg("LIN", "G01", "", ["PART-G", "IN"])
    b.seg("QTY", ["12", "1", "PCE"])
    b.seg("UNT", "10", "MSG-G")
    b.seg("UNZ", "1", "EX-GOOD")
    return b.bytes()


def _bad_exchange(chars: dict, kind: str) -> bytes:
    b = EdiBuilder(chars, una=True)
    b.seg("UNB", ["UNOC", "3"], "SUP-BAD", "CUST-BAD",
          ["261114", "0901"], f"EX-BAD-{kind}")
    b.seg("UNH", "MSG-B", ["DESADV", "D", "96A", "UN"])
    b.seg("BGM", "351", "DN-BAD", "9")
    b.seg("DTM", ["137", "20261114", "102"])
    b.seg("NAD", "SU", ["SUP-BAD", "", "92"])
    b.seg("NAD", "BY", ["CUST-BAD", "", "92"])
    if kind == "cycle":
        b.seg("CPS", "X1", "X2")
        b.seg("PAC", "1", "", "CT")
        b.seg("CPS", "X2", "X1")          # 成环
        b.seg("PAC", "1", "", "CT")
        b.seg("UNT", "10", "MSG-B")
    elif kind == "orphan":
        b.seg("CPS", "X1", "GHOST")       # 父包装不存在
        b.seg("PAC", "1", "", "CT")
        b.seg("UNT", "8", "MSG-B")
    elif kind == "unt_mismatch":
        b.seg("CPS", "X1", "")
        b.seg("PAC", "1", "", "CT")
        b.seg("LIN", "B01", "", ["PART-B", "IN"])
        b.seg("QTY", ["12", "1", "PCE"])
        b.seg("UNT", "99", "MSG-B")      # 段数故意写错（实际 10）
    else:
        b.seg("CPS", "X1", "")
        b.seg("PAC", "1", "", "CT")
        b.seg("LIN", "B01", "", ["PART-B", "IN"])
        b.seg("QTY", ["12", "1", "PCE"])
        b.seg("UNT", "10", "MSG-B")
    if kind == "count_mismatch":
        b.seg("UNZ", "9", f"EX-BAD-{kind}")   # 消息数故意写错
    elif kind == "truncated":
        # 已落 UNT 后又开始一个未闭合的 CPS（无段结束符、无 UNZ）：
        # 解析器应在该段缺段结束符处拒绝
        b.parts.append(b"CPS+X2+")
        return b.bytes()
    else:
        b.seg("UNZ", "1", f"EX-BAD-{kind}")
    return b.bytes()


def build_recover_file(chars: dict, kind: str) -> bytes:
    return _good_prefix(chars) + _bad_exchange(chars, kind)


def build_conflict_file(chars: dict) -> bytes:
    """与交换1 同发送方/接收方/交换号，但通知单号被改动。"""
    data = bytearray(build_exchange_one(chars, control="EX-001", una=True))
    data[data.index(b"DN-2026-001"):data.index(b"DN-2026-001") + 10] = \
        b"DN-CHANGED"
    return bytes(data)


def generate(root: Path) -> List[Path]:
    root.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    def dump(name: str, data: bytes) -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
        return path

    # 正常材料
    dump("01_multi.edi",
         build_exchange_one(DEFAULT) + build_exchange_two(ALT))
    dump("02_comma_retransmit.edi", build_exchange_two(ALT))
    dump("bad_count_mismatch.edi",
         build_recover_file(DEFAULT, "count_mismatch"))
    dump("bad_unt_mismatch.edi",
         build_recover_file(DEFAULT, "unt_mismatch"))
    dump("bad_cycle.edi", build_recover_file(DEFAULT, "cycle"))
    dump("bad_orphan.edi", build_recover_file(DEFAULT, "orphan"))
    dump("bad_truncated.edi", build_recover_file(DEFAULT, "truncated"))
    dump("conflict_same_id.edi", build_conflict_file(DEFAULT))
    dump("recover_two_exchanges.edi",
         build_recover_file(DEFAULT, "unt_mismatch"))
    dump("big/100k.edi", build_big_exchange(100_000))
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成 DESADV 演示材料")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    paths = generate(Path(args.root))
    for path in paths:
        print(f"{path}  {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
