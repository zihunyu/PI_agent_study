"""内置工作区工具的零依赖参数校验。"""

from __future__ import annotations

import math
from typing import Any


def object_args(
    value: Any,
    *,
    allowed: set[str],
    required: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("工具参数必须是对象")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"工具参数包含未知字段：{', '.join(sorted(unknown))}")
    missing = (required or set()) - set(value)
    if missing:
        raise ValueError(f"工具参数缺少字段：{', '.join(sorted(missing))}")
    return dict(value)


def text_arg(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} 必须是{'字符串' if allow_empty else '非空字符串'}")
    if "\x00" in value:
        raise ValueError(f"{name} 不能包含 NUL")
    return value


def int_arg(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return int(value)


def number_arg(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return result
