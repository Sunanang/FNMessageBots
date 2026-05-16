"""
SQLite 只读 URI：与 main._probe_db_readable、NAS 上实测可连形式一致。

说明：曾使用 Path.as_uri() 将 ``@`` 编成 ``%40``，在部分飞牛/嵌入式 SQLite 上会导致
``sqlite3.connect(..., uri=True)`` 打开 ``@appdata`` 路径失败，进而 _probe_db_readable 失败、
影视/相册轮询器根本不会创建（日志里只有 DBLogPoller/BackupDBPoller）。

WAL 库在部分系统上仅用 ``mode=ro`` 仍可能触发 journal 相关写入路径而报
``attempt to write a readonly database``；故连接与探测均提供 ``immutable=1`` 兜底。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional


def _abs_db_path(db_path: str) -> str:
    raw = (db_path or "").strip()
    if not raw:
        raise ValueError("数据库路径为空")
    return os.path.abspath(os.path.expanduser(raw))


def sqlite_readonly_uri(db_path: str) -> str:
    """返回 ``file:/绝对路径?mode=ro``（单斜杠 + 未编码 ``@``，与常见 NAS 行为一致）。"""
    p = _abs_db_path(db_path)
    return f"file:{p}?mode=ro"


def sqlite_readonly_immutable_uri(db_path: str) -> str:
    """返回 ``file:/绝对路径?mode=ro&immutable=1``，用于部分 NAS 只读/WAL 场景兜底。"""
    p = _abs_db_path(db_path)
    return f"file:{p}?mode=ro&immutable=1"


def _sqlite_err_retriable_with_immutable(exc: sqlite3.Error) -> bool:
    msg = str(exc).lower()
    return (
        "unable to open" in msg
        or "readonly" in msg
        or "read-only" in msg
        or "attempt to write a readonly database" in msg
    )


def connect_readonly_with_fallback(
    db_path: str,
    timeout: float = 5.0,
    *,
    table_probe_sql: Optional[str] = None,
    prefer_immutable: bool = False,
) -> sqlite3.Connection:
    """依次尝试只读连接；可选执行表级探测（WAL 库在只读挂载上常需 immutable）。"""
    last_err: Optional[sqlite3.Error] = None
    uri_builders = (
        (sqlite_readonly_immutable_uri, sqlite_readonly_uri)
        if prefer_immutable
        else (sqlite_readonly_uri, sqlite_readonly_immutable_uri)
    )
    for make_uri in uri_builders:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(make_uri(db_path), uri=True, timeout=timeout)
            conn.execute("SELECT 1").fetchone()
            if table_probe_sql:
                conn.execute(table_probe_sql).fetchone()
            return conn
        except sqlite3.Error as e:
            last_err = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if table_probe_sql and not _sqlite_err_retriable_with_immutable(e):
                raise
    if last_err:
        raise last_err
    raise sqlite3.Error("sqlite connect failed")


def probe_readonly_sqlite(
    db_path: str,
    sql: str = "SELECT 1",
    timeout: float = 5.0,
    *,
    table_probe_sql: Optional[str] = None,
) -> None:
    """连接并执行只读探测 SQL；与 connect_readonly_with_fallback 使用相同 URI 策略。"""
    conn = connect_readonly_with_fallback(
        db_path,
        timeout=timeout,
        table_probe_sql=table_probe_sql,
    )
    try:
        if table_probe_sql is None:
            conn.execute(sql).fetchone()
    finally:
        conn.close()
