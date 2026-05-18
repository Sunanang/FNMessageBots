"""轮询汇总批次内去重（防止双线程入队、刷盘竞态等导致同一条事件出现两次）。"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping


def _entry_cursor(item: Mapping[str, Any]) -> str:
    entry = item.get("entry")
    return str(getattr(entry, "cursor", "") or "").strip()


def fingerprint_poll_batch_item(item: Mapping[str, Any]) -> str:
    """生成批次项稳定指纹；同一轮汇总中相同指纹只保留一条。"""
    ed_raw = item.get("event_data")
    ed: Dict[str, Any] = ed_raw if isinstance(ed_raw, dict) else {}
    source = str(item.get("source") or ed.get("_source") or "db").strip()
    event_type = str(item.get("event_type") or ed.get("_source_event_id") or "").strip()

    if source == "docker":
        # 与 DockerEventsPoller dedup_key 一致：cid + action + timeNano，勿省略纳秒否则会误合并同容器短时间多次同类事件。
        sc = str(ed.get("_source_cursor") or "").strip()
        if sc:
            return f"docker:{event_type}:{sc}"
        cid = str(ed.get("container_id_full") or ed.get("container_id") or "").strip()
        action = str(ed.get("docker_action") or "").strip()
        tn = ed.get("engine_time_nano", "")
        return f"docker:{event_type}:{cid}:{action}:{tn}"

    row_id = item.get("row_id")
    if row_id is not None and str(row_id).strip() != "":
        return f"{source}:{event_type}:row:{row_id}"

    cursor = str(ed.get("_source_cursor") or _entry_cursor(item) or "").strip()
    if cursor:
        return f"{source}:{event_type}:cursor:{cursor}"

    db_event_id = str(item.get("db_event_id") or ed.get("_source_event_id") or "").strip()
    brief_bits = [
        str(ed.get("container_name") or ""),
        str(ed.get("task_name") or ""),
        str(ed.get("guid") or ed.get("item_guid") or ""),
    ]
    brief = "|".join(b for b in brief_bits if b)[:120]
    return f"{source}:{event_type}:{db_event_id}:{brief}"


def dedupe_poll_batch_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按指纹去重，保留首次出现的项。"""
    if not items:
        return items
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fp = fingerprint_poll_batch_item(item)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(item)
    return out
