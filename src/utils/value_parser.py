"""通用值解析工具。"""

from __future__ import annotations

from typing import Any


def as_bool(value: Any, default: bool = False) -> bool:
    """稳健布尔解析：兼容 bool/数字/字符串（如 'true'/'false'）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)
