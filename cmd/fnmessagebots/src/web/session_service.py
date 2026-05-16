"""
Web 会话服务：内存会话创建与空闲超时刷新。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Dict

_sessions: Dict[str, Dict[str, float]] = {}
_sessions_lock = threading.Lock()


def create_session() -> str:
    """创建会话并返回 session_id。"""
    sid = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[sid] = {"last_activity": time.time()}
    return sid


def touch_session(session_id: str, idle_seconds: int) -> bool:
    """若会话有效则刷新活跃时间并返回 True。"""
    if not session_id:
        return False
    now = time.time()
    with _sessions_lock:
        if session_id not in _sessions:
            return False
        last = _sessions[session_id]["last_activity"]
        if now - last > idle_seconds:
            del _sessions[session_id]
            return False
        _sessions[session_id]["last_activity"] = now
        return True

