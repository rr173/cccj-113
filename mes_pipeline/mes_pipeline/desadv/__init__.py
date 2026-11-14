"""EDIFACT DESADV（发货通知）接入器。

只实现项目约定的 DESADV 子集（UTF-8、UNA/UNB/UNZ/UNH/BGM/DTM/NAD/CPS/PAC/
GIN/LIN/QTY/FTX/UNT），不做库存增减、采购订单管理或物料谱系计算。

模块划分：

- errors.py    带字节/段定位的解析异常
- tokenizer.py 有界字节块流式读取：UNA 识别、释放符感知的切段/切元素（字节级）
- parser.py    约定子集的状态机解析与消息内层级/字段校验
- store.py     SQLite 关系表 schema、交换事务与重传/冲突识别
- service.py   导入编排（流式、逐交换事务、故障注入）
- queries.py   发货行路径查询与包装子树汇总
"""
from __future__ import annotations

from .errors import DesadvConflict, DesadvError
from .queries import get_line, list_exchanges, list_messages, subtree_summary
from .service import import_file
from .store import open_store
from .tokenizer import ServiceChars, default_service_chars

__all__ = [
    "DesadvError",
    "DesadvConflict",
    "ServiceChars",
    "default_service_chars",
    "import_file",
    "open_store",
    "list_exchanges",
    "list_messages",
    "get_line",
    "subtree_summary",
]
