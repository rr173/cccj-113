"""质检汇总模块错误类型。"""


class QcsumError(Exception):
    """质检汇总模块错误基类。"""


class QcsumValidation(QcsumError):
    """输入不合法：JSON 结构、批次、行或操作违反约定。"""


class QcsumConflict(QcsumError):
    """批次号已成功登记，但新送内容与其不一致。"""


class QcsumState(QcsumError):
    """内部状态错误：辅助计数不变量被破坏，或库状态不允许该操作。"""
