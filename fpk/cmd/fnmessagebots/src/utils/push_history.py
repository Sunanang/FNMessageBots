"""
推送记录明细：每条事件推送写入 SQLite，最多保留 1 万条，超出时删除最老的 3000 条。
供 Web 推送汇总「查看详细」使用。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo

_lock = threading.Lock()
_db_path: str = ""
MAX_RECORDS = 10000
DELETE_BATCH = 3000
MAX_DETAIL_CHARS = 5000


def _truncate_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _compact_json_value(
    value: Any,
    *,
    depth: int = 3,
    max_str: int = 400,
    max_items: int = 20,
    max_keys: int = 80,
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_text(value, max_str)
    if depth <= 0:
        if isinstance(value, dict):
            return f"<dict {len(value)} keys>"
        if isinstance(value, (list, tuple)):
            return f"<list {len(value)} items>"
        return _truncate_text(value, max_str)
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= max_keys:
                out["_truncated_keys"] = max(0, len(value) - max_keys)
                break
            out[_truncate_text(k, 80)] = _compact_json_value(
                v,
                depth=depth - 1,
                max_str=max_str,
                max_items=max_items,
                max_keys=max_keys,
            )
        return out
    if isinstance(value, (list, tuple)):
        items = [
            _compact_json_value(
                v,
                depth=depth - 1,
                max_str=max_str,
                max_items=max_items,
                max_keys=max_keys,
            )
            for v in list(value)[:max_items]
        ]
        if len(value) > max_items:
            items.append({"_truncated_items": len(value) - max_items})
        return items
    return _truncate_text(value, max_str)


def _compact_channel_results(value: Any, *, response_chars: int = 800) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            out.append({"channel": "未知渠道", "success": False, "error": _truncate_text(item, 200)})
            continue
        row: Dict[str, Any] = {
            "channel": _truncate_text(item.get("channel") or "未知渠道", 40),
            "success": bool(item.get("success")),
        }
        if item.get("error") not in (None, ""):
            row["error"] = _truncate_text(item.get("error"), 400)
        if item.get("response") is not None:
            row["response"] = _compact_json_value(
                item.get("response"),
                depth=3,
                max_str=response_chars,
                max_items=12,
                max_keys=40,
            )
        out.append(row)
    if len(value) > 20:
        out.append({"channel": "更多渠道", "success": False, "error": f"已省略 {len(value) - 20} 条"})
    return out


def _compact_detail(detail: Any, *, response_chars: int = 800, include_event_data: bool = True) -> Any:
    if not isinstance(detail, dict):
        return {"detail_preview": _truncate_text(detail, 1000)}

    out: Dict[str, Any] = {}
    for key in ("kind", "event_type", "timestamp", "failure_summary"):
        if detail.get(key) not in (None, ""):
            out[key] = _compact_json_value(detail.get(key), depth=1, max_str=800)
    if "channel_results" in detail:
        out["channel_results"] = _compact_channel_results(
            detail.get("channel_results"),
            response_chars=response_chars,
        )
    for key in (
        "batch_total",
        "batch_type_count",
        "batch_render_meta",
        "grouped_events_preview",
        "additional_info",
    ):
        if key in detail:
            out[key] = _compact_json_value(detail.get(key), depth=3, max_str=300, max_items=10, max_keys=40)
    if include_event_data and "event_data" in detail:
        out["event_data"] = _compact_json_value(
            detail.get("event_data"),
            depth=3,
            max_str=300,
            max_items=15,
            max_keys=60,
        )
    for key, value in detail.items():
        if key in out or key in {"channel_results", "event_data"}:
            continue
        out[key] = _compact_json_value(value, depth=2, max_str=300, max_items=8, max_keys=30)
    return out


def _minimal_detail(detail: Any) -> Dict[str, Any]:
    if not isinstance(detail, dict):
        return {"detail_preview": _truncate_text(detail, 1000), "storage_note": "detail_compacted"}
    out: Dict[str, Any] = {"storage_note": "detail_compacted"}
    for key in ("kind", "event_type", "timestamp", "failure_summary"):
        if detail.get(key) not in (None, ""):
            out[key] = _truncate_text(detail.get(key), 600)
    out["channel_results"] = _compact_channel_results(
        detail.get("channel_results"),
        response_chars=300,
    )
    event_data = detail.get("event_data")
    if isinstance(event_data, dict):
        preview: Dict[str, Any] = {}
        for key in ("count", "by_type", "message", "user", "IP", "name"):
            if key in event_data:
                preview[key] = _compact_json_value(event_data.get(key), depth=2, max_str=200, max_items=5, max_keys=20)
        if preview:
            out["event_data_preview"] = preview
    return out


def _serialize_detail(detail: Optional[Dict[str, Any]]) -> Optional[str]:
    if detail is None:
        return None
    candidates = [
        detail,
        _compact_detail(detail, response_chars=800, include_event_data=True),
        _compact_detail(detail, response_chars=500, include_event_data=False),
        _minimal_detail(detail),
    ]
    for candidate in candidates:
        detail_str = json.dumps(candidate, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(detail_str) <= MAX_DETAIL_CHARS:
            return detail_str
    minimal = _minimal_detail(detail)
    for row in minimal.get("channel_results", []):
        if isinstance(row, dict) and "response" in row:
            row["response"] = _truncate_text(json.dumps(row["response"], ensure_ascii=False, default=str), 120)
        if isinstance(row, dict) and "error" in row:
            row["error"] = _truncate_text(row["error"], 160)
    detail_str = json.dumps(minimal, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(detail_str) <= MAX_DETAIL_CHARS:
        return detail_str
    if isinstance(minimal.get("channel_results"), list):
        minimal["channel_results"] = [
            {
                "channel": row.get("channel"),
                "success": bool(row.get("success")),
                "error": _truncate_text(row.get("error"), 120),
            }
            for row in minimal["channel_results"]
            if isinstance(row, dict)
        ]
    detail_str = json.dumps(minimal, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(detail_str) <= MAX_DETAIL_CHARS:
        return detail_str
    return json.dumps(
        {
            "storage_note": "detail_compacted",
            "failure_summary": _truncate_text(
                detail.get("failure_summary") if isinstance(detail, dict) else detail,
                1000,
            ),
        },
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def init(cursor_dir: str) -> None:
    """初始化数据库路径并建表。cursor_dir 与 push_stats 一致。"""
    global _db_path
    if not cursor_dir:
        cursor_dir = "./data/cursor"
    abs_dir = os.path.abspath(os.path.join(os.getcwd(), cursor_dir))
    Path(abs_dir).mkdir(parents=True, exist_ok=True)
    _db_path = os.path.join(abs_dir, "push_history.db")
    _ensure_table()


def _ensure_table() -> None:
    if not _db_path:
        return
    with _lock:
        conn = sqlite3.connect(_db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    detail TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_history_created_at ON push_history(created_at)"
            )
            conn.commit()
        finally:
            conn.close()


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(_db_path)


def add_record(
    success: bool,
    event_type: str,
    summary: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """写入一条推送记录。若总条数超过 MAX_RECORDS，删除最老的 DELETE_BATCH 条。"""
    if not _db_path:
        return
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    detail_str = _serialize_detail(detail)
    if summary and len(summary) > 500:
        summary = summary[:497] + "..."
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO push_history (created_at, event_type, success, summary, detail) VALUES (?, ?, ?, ?, ?)",
                (created_at, event_type, 1 if success else 0, summary or "", detail_str),
            )
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM push_history")
            count = cur.fetchone()[0]
            if count > MAX_RECORDS:
                cur = conn.execute(
                    "SELECT id FROM push_history ORDER BY id ASC LIMIT ?", (DELETE_BATCH,)
                )
                ids = [row[0] for row in cur.fetchall()]
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(f"DELETE FROM push_history WHERE id IN ({placeholders})", ids)
                    conn.commit()
        finally:
            conn.close()


def bulk_insert(records: List[Dict[str, Any]]) -> None:
    """批量插入推送记录（用于造数等）。每条为 dict：created_at, event_type, success, summary, detail(可选)。
    插入后若总条数超过 MAX_RECORDS，会删除最老的 DELETE_BATCH 条。"""
    if not _db_path or not records:
        return
    rows = []
    for r in records:
        created_at = str(r.get("created_at", ""))[:19]
        event_type = str(r.get("event_type", "")) or "Unknown"
        success = 1 if r.get("success", True) else 0
        summary = str(r.get("summary", ""))[:500]
        if len(str(r.get("summary", ""))) > 500:
            summary = summary[:497] + "..."
        detail = r.get("detail")
        detail_str = _serialize_detail(detail)
        rows.append((created_at, event_type, success, summary or "", detail_str))
    with _lock:
        conn = _conn()
        try:
            conn.executemany(
                "INSERT INTO push_history (created_at, event_type, success, summary, detail) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM push_history")
            count = cur.fetchone()[0]
            if count > MAX_RECORDS:
                cur = conn.execute(
                    "SELECT id FROM push_history ORDER BY id ASC LIMIT ?", (DELETE_BATCH,)
                )
                ids = [row[0] for row in cur.fetchall()]
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(f"DELETE FROM push_history WHERE id IN ({placeholders})", ids)
                    conn.commit()
        finally:
            conn.close()


def get_records(
    limit: int = 50,
    offset: int = 0,
    success_filter: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """分页查询推送记录，按时间倒序。success_filter: True 仅成功，False 仅失败，None 全部。"""
    if not _db_path:
        return []
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT id, created_at, event_type, success, summary, detail FROM push_history"
            params: List[Any] = []
            if success_filter is not None:
                sql += " WHERE success = ?"
                params.append(1 if success_filter else 0)
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "event_type": r["event_type"],
                    "success": bool(r["success"]),
                    "summary": r["summary"] or "",
                    "detail": r["detail"],
                }
                for r in rows
            ]
        finally:
            conn.close()


def get_record(record_id: int) -> Optional[Dict[str, Any]]:
    """根据 id 查询单条推送记录。"""
    if not _db_path:
        return None
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT id, created_at, event_type, success, summary, detail FROM push_history WHERE id = ?",
                (record_id,),
            )
            r = cur.fetchone()
            if r is None:
                return None
            return {
                "id": r["id"],
                "created_at": r["created_at"],
                "event_type": r["event_type"],
                "success": bool(r["success"]),
                "summary": r["summary"] or "",
                "detail": r["detail"],
            }
        finally:
            conn.close()


def get_total_counts() -> Dict[str, int]:
    """返回总统计：total, success, fail（基于 SQLite push_history）。"""
    if not _db_path:
        return {"total": 0, "success": 0, "fail": 0}
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok FROM push_history")
            row = cur.fetchone()
            total = row[0] or 0
            ok = row[1] or 0
            fail = total - ok
            return {"total": total, "success": ok, "fail": fail}
        finally:
            conn.close()


def get_today_counts() -> Dict[str, int]:
    """返回当日统计：total, success, fail（Asia/Shanghai，当天字符串匹配）。"""
    if not _db_path:
        return {"total": 0, "success": 0, "fail": 0}
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok "
                "FROM push_history WHERE substr(created_at, 1, 10) = ?",
                (today,),
            )
            row = cur.fetchone()
            total = row[0] or 0
            ok = row[1] or 0
            fail = total - ok
            return {"total": total, "success": ok, "fail": fail}
        finally:
            conn.close()


def clear_all() -> None:
    """清空所有推送记录（仅用于造数脚本重新生成等）。"""
    if not _db_path:
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM push_history")
            conn.commit()
        finally:
            conn.close()


def get_db_path() -> str:
    return _db_path
