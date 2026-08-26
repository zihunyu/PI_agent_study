"""内置计算工具共享的参数校验函数。"""

from __future__ import annotations

import math
from typing import Any


Number = int | float


def _is_valid_number(value: Any) -> bool:
    """判断参数是不是可计算的有限数字。

    Python 中 bool 是 int 的子类，但在计算工具中 True/False 不应被当成
    1/0，因此需要显式排除。NaN 和正负无穷也会破坏 JSON 结果，所以拒绝。
    """

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def validate_two_numbers(arguments: Any) -> dict[str, Number]:
    """校验二元计算工具统一的 ``a`` 和 ``b`` 参数。"""

    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象，例如：{'a': 2, 'b': 3}")

    if "a" not in arguments or "b" not in arguments:
        raise ValueError("工具参数必须同时包含 a 和 b")

    a = arguments["a"]
    b = arguments["b"]
    if not _is_valid_number(a) or not _is_valid_number(b):
        raise ValueError("a 和 b 必须是有限数字，不能是布尔值、NaN 或无穷")

    return {"a": a, "b": b}
