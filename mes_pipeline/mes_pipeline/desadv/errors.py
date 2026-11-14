"""DESADV 接入器的异常类型。

所有报错都携带可定位信息：优先给出段在原文件中的字节偏移（左闭右开），
无法归属到完整段时给出文件级字节偏移；段标签已知时一并附上。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class DesadvError(Exception):
    """输入不合法或结构不符合约定子集。

    Attributes
    ----------
    message:
        人可读的拒绝原因。
    byte_offset:
        问题在原文件中的字节位置；归属到完整段时取段起点。
    end_byte_offset:
        段级问题时为段结束位置（左闭右开，含段结束符），否则为 None。
    segment_tag:
        能识别出段标签时给出（如 ``"CPS"``）。
    segment_seq:
        消息内段序号（UNH=1，与 UNT 计数口径一致），未进入消息时为 None。
    exchange_seq:
        文件内第几个交换（从 1 开始），尚无法确定时为 None。
    """

    message: str
    byte_offset: Optional[int] = None
    end_byte_offset: Optional[int] = None
    segment_tag: Optional[str] = None
    segment_seq: Optional[int] = None
    exchange_seq: Optional[int] = None

    def __str__(self) -> str:
        parts = [self.message]
        loc = []
        if self.exchange_seq is not None:
            loc.append(f"交换#{self.exchange_seq}")
        if self.segment_tag:
            loc.append(f"段 {self.segment_tag}")
        if self.segment_seq is not None:
            loc.append(f"段序号 {self.segment_seq}")
        if self.byte_offset is not None:
            if self.end_byte_offset is not None:
                loc.append(f"字节 [{self.byte_offset},{self.end_byte_offset})")
            else:
                loc.append(f"字节 {self.byte_offset}")
        if loc:
            parts.append("（" + "；".join(loc) + "）")
        return "".join(parts)


class DesadvConflict(DesadvError):
    """同发送方/接收方/交换号的交换字节与库内已提交版本不同。

    触发时本次交换整段回滚，旧资料保持不动。
    """
