"""脱敏日志、错误与历史中的常见推送凭据（Telegram bot、魔法推送路径 token、query）。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

_QUERY_RE = re.compile(r"\?[^\s'\")]*")
# 字面量与 %3A 编码的 Telegram bot token
_TELEGRAM_TOKEN_RE = re.compile(r"(?i)(/bot)([0-9]+(?::|%3a)[A-Za-z0-9_-]+)")
# 魔法推送：token 放在 /api/push/<token> 或 /api/inbound/<token>
_MAGIC_PATH_TOKEN_RE = re.compile(r"(?i)(/api/(?:push|inbound)/)([^/?#\s'\"\\]+)")


def redact_secrets(value: Any, request_url: str = "") -> str:
    """擦除文本中的 Telegram / 魔法推送凭据及 query 串。"""
    message = str(value or "")
    for raw in (request_url, message):
        text = str(raw or "")
        for pattern in (_TELEGRAM_TOKEN_RE, _MAGIC_PATH_TOKEN_RE):
            match = pattern.search(text)
            if not match:
                continue
            secret = match.group(2)
            if secret:
                message = message.replace(secret, "***")
                message = message.replace(unquote(secret), "***")
    message = _TELEGRAM_TOKEN_RE.sub(r"\1***", message)
    message = _MAGIC_PATH_TOKEN_RE.sub(r"\1***", message)
    # 再对解码后的副本扫一遍，覆盖仅出现 %3A 形态的残留
    decoded = unquote(message)
    if decoded != message:
        cleaned = _TELEGRAM_TOKEN_RE.sub(r"\1***", decoded)
        cleaned = _MAGIC_PATH_TOKEN_RE.sub(r"\1***", cleaned)
        if "***" in cleaned and cleaned != decoded:
            message = cleaned
    return _QUERY_RE.sub("?***", message)


def redact_json(value: Any, request_url: str = "") -> Any:
    """递归脱敏 dict/list/str。"""
    if isinstance(value, str):
        return redact_secrets(value, request_url)
    if isinstance(value, dict):
        return {key: redact_json(item, request_url) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_json(item, request_url) for item in value]
    return value
