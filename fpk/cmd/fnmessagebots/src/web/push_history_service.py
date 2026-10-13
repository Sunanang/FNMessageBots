"""
推送记录服务层：统一 push_history 初始化与展示格式化。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from utils.history_display import format_push_history_row


def _ensure_db_ready(load_raw_config: Callable[[], dict]) -> None:
    from utils import push_history
    if push_history.get_db_path():
        return
    from utils import push_stats
    raw = load_raw_config()
    push_stats.init(raw.get("cursor_dir", "./data/cursor"))


def get_stats(load_raw_config: Callable[[], dict]) -> Dict[str, Dict[str, int]]:
    from utils import push_history
    _ensure_db_ready(load_raw_config)
    return {
        "total": push_history.get_total_counts(),
        "today": push_history.get_today_counts(),
    }


def list_records(
    load_raw_config: Callable[[], dict],
    *,
    limit: int,
    offset: int,
    success_filter: Optional[bool],
) -> List[Dict[str, Any]]:
    from utils import push_history
    _ensure_db_ready(load_raw_config)
    rows = push_history.get_records(limit=limit, offset=offset, success_filter=success_filter)
    return [format_push_history_row(r) for r in rows]


def get_record(load_raw_config: Callable[[], dict], record_id: int) -> Optional[Dict[str, Any]]:
    from utils import push_history
    _ensure_db_ready(load_raw_config)
    row = push_history.get_record(record_id)
    if row is None:
        return None
    out = dict(row)
    if out.get("detail"):
        try:
            out["detail"] = json.loads(out["detail"]) if isinstance(out["detail"], str) else out["detail"]
        except Exception:
            pass
    return format_push_history_row(out)

