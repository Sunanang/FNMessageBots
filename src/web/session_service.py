"""
Web 会话服务：内存会话创建与空闲超时刷新。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Dict, Optional, TypedDict

class SessionEntry(TypedDict):
    last_activity: float
    scope: str


_sessions: Dict[str, SessionEntry] = {}
_sessions_lock = threading.Lock()
_last_cleanup = 0.0


def _cleanup(now: float, idle_seconds: int, force: bool = False) -> None:
    """调用方持有锁；定期清理无人再访问的过期会话。"""
    global _last_cleanup
    if not force and 0 <= now - _last_cleanup < 60:
        return
    for sid, entry in list(_sessions.items()):
        if now - entry["last_activity"] >= idle_seconds:
            del _sessions[sid]
    _last_cleanup = now


def create_session(idle_seconds: int = 900, scope: str = "/") -> str:
    """创建会话并返回 session_id。"""
    sid = secrets.token_urlsafe(32)
    with _sessions_lock:
        now = time.time()
        _cleanup(now, idle_seconds, force=True)
        _sessions[sid] = {"last_activity": now, "scope": scope}
    return sid


def session_remaining_seconds(session_id: str, idle_seconds: int, scope: Optional[str] = None) -> float:
    """读取剩余空闲时间，后台查询不会延长会话。"""
    if not session_id:
        return 0.0
    now = time.time()
    with _sessions_lock:
        _cleanup(now, idle_seconds)
        if session_id not in _sessions:
            return 0.0
        if scope is not None and _sessions[session_id]["scope"] != scope:
            return 0.0
        last = _sessions[session_id]["last_activity"]
        remaining = idle_seconds - (now - last)
        if remaining <= 0:
            del _sessions[session_id]
            return 0.0
        return remaining


def touch_session(session_id: str, idle_seconds: int, scope: Optional[str] = None) -> bool:
    """仅用户操作刷新活跃时间；已过期的会话不能恢复。"""
    if not session_id:
        return False
    now = time.time()
    with _sessions_lock:
        _cleanup(now, idle_seconds)
        entry = _sessions.get(session_id)
        if entry is None:
            return False
        if scope is not None and entry["scope"] != scope:
            return False
        if now - entry["last_activity"] >= idle_seconds:
            del _sessions[session_id]
            return False
        _sessions[session_id]["last_activity"] = now
        return True


def delete_session(session_id: str, scope: Optional[str] = None) -> None:
    with _sessions_lock:
        entry = _sessions.get(session_id)
        if entry is not None and (scope is None or entry["scope"] == scope):
            _sessions.pop(session_id, None)

