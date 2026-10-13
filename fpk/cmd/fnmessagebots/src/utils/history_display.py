"""
推送记录列表展示：事件类型中文名、摘要去掉标题行与文末提示语（与 EVENT_NOTES 一致）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

from notifier.multi_platform_notifier import MultiPlatformNotifier

_EMOJI = MultiPlatformNotifier._EMOJI_RE


def _strip_emoji(s: str) -> str:
    if not s:
        return ""
    return _EMOJI.sub("", s).strip()


def _shorten_event_label(s: str) -> str:
    """去掉末尾「通知」「告警」等，与列表里「磁盘唤醒」「登录成功」等口语一致。"""
    t = (s or "").strip()
    if len(t) <= 2:
        return t
    for suf in ("通知", "告警", "提示"):
        if t.endswith(suf) and len(t) > len(suf) + 1:
            t = t[: -len(suf)].strip()
    return t


def _title_cn_from_event_key(event_key: str) -> str:
    raw = MultiPlatformNotifier.EVENT_TITLES.get(event_key, "")
    if not raw:
        return event_key or "未知事件"
    s = _strip_emoji(raw)
    for token in ("飞牛NAS-", "飞牛NAS"):
        s = s.replace(token, "")
    s = re.sub(r"\s+", " ", s).strip()
    s = s or event_key
    return _shorten_event_label(s)


def _note_plain_set() -> Set[str]:
    out: Set[str] = set()
    for v in MultiPlatformNotifier.EVENT_NOTES.values():
        p = _strip_emoji(v or "")
        if len(p) < 3:
            continue
        out.add(p)
        out.add(p.rstrip("。"))
        out.add(p.rstrip("。") + "。")
    return out


_NOTE_SET: Optional[Set[str]] = None


def _get_note_set() -> Set[str]:
    global _NOTE_SET
    if _NOTE_SET is None:
        _NOTE_SET = _note_plain_set()
    return _NOTE_SET


def _parse_detail(detail: Any) -> Optional[Dict[str, Any]]:
    if detail is None:
        return None
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str):
        try:
            o = json.loads(detail)
            return o if isinstance(o, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def resolve_event_type_label(event_type: str, detail: Optional[Dict[str, Any]] = None) -> str:
    et = (event_type or "").strip()
    if not et:
        return "未知"
    if et == "POLL_BATCH_SUMMARY":
        if detail:
            ed = detail.get("event_data")
            by_type: Dict[str, Any] = ed.get("by_type") if isinstance(ed, dict) else None
            if isinstance(by_type, dict) and by_type:
                keys = sorted(by_type.keys(), key=str)
                if len(keys) == 1:
                    return _title_cn_from_event_key(str(keys[0]))
                labels = [_title_cn_from_event_key(str(k)) for k in keys[:4]]
                suffix = f"等{len(keys)}类" if len(keys) > 4 else ""
                return "、".join(labels) + (suffix or "")
        return "多事件合并"
    return _title_cn_from_event_key(et)


def _normalize_summary_to_slash_parts(summary: str) -> List[str]:
    s = (summary or "").strip()
    if not s:
        return []
    if " / " in s:
        return [p.strip() for p in s.split(" / ") if p.strip()]
    lines = [ln.strip() for ln in s.replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(lines) >= 2:
        first = lines[0]
        if any(x in first for x in ("通知", "告警", "汇总", "合并通知", "系统事件")):
            return lines[1:]
    return lines


def _is_empty_field_part(part: str) -> bool:
    """识别“字段名:”但值为空的片段，如“认证方式:”。"""
    p = (part or "").strip()
    if not p:
        return True
    # 「磁盘 #1:」「磁盘 #2:」等为合并磁盘分节标题，虽无冒号后正文但须保留
    if re.search(r"#\d+\s*[:：]\s*$", p):
        return False
    return bool(re.match(r"^[^:：]{1,24}[:：]\s*$", p))


def clean_summary_for_list(summary: str, event_type: str, detail: Optional[Dict[str, Any]] = None) -> str:
    parts = _normalize_summary_to_slash_parts(summary)
    if not parts:
        return summary or ""
    if len(parts) >= 2:
        parts = parts[1:]
    # 去掉空字段片段，避免出现“认证方式:”这类无值信息。
    parts = [p for p in parts if not _is_empty_field_part(p)]
    notes = _get_note_set()
    while parts:
        last = parts[-1].strip()
        last_cmp = last.rstrip("。")
        drop = False
        for n in notes:
            if not n:
                continue
            nc = n.rstrip("。")
            if last == n or last == nc or last_cmp == nc or last_cmp == n.rstrip("。"):
                drop = True
                break
        if drop:
            parts = parts[:-1]
            continue
        break
    out = " ｜ ".join(parts)
    return out if out else (summary or "")


def format_push_history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    detail = _parse_detail(out.get("detail"))
    et = str(out.get("event_type") or "")
    out["event_type_label"] = resolve_event_type_label(et, detail)
    out["summary"] = clean_summary_for_list(out.get("summary") or "", et, detail)
    if detail is not None:
        out["detail"] = detail
    return out
