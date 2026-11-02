"""音乐库静默批次的数据与文案；不依赖数据库或推送客户端。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

MUSIC_SUMMARY_EVENT = "MUSIC_LIBRARY_SUMMARY"
MUSIC_DEBOUNCE_SEC = 60
MUSIC_STAGES = {
    "MUSIC_TRACK_ADDED": "入库",
    "MUSIC_TRACK_REMOVED": "移出库",
}
# 两阶段各留四条预览，为渠道标题、时间及静默说明保留 UTF-8 字节预算。
_PREVIEW_LIMIT = 4


def _short(text: str, size: int = 160) -> str:
    raw = text.replace("\n", " ").replace("\r", " ").encode("utf-8")
    return raw.decode() if len(raw) <= size else raw[: size - 3].decode("utf-8", "ignore") + "..."


def _track_line(row: Dict[str, Any]) -> str:
    title = (row.get("title") or "").strip() or f"曲目#{row.get('track_id') or '?'}"
    artist = (row.get("artist") or "").strip()
    album = (row.get("album") or "").strip()
    line = f"《{title}》"
    if artist:
        line += f" - {artist}"
    if album:
        line += f"（{album}）"
    return _short(line)


def build_music_summary(pending: Dict[str, Any]) -> Dict[str, Any]:
    tracks = list(pending.get("tracks", {}).values())
    stage_counts_raw = pending.get("stage_counts")
    stage_counts = stage_counts_raw if isinstance(stage_counts_raw, dict) else {}
    by_type: Dict[str, int] = {}
    sections: List[Dict[str, Any]] = []
    for stage, label in MUSIC_STAGES.items():
        rows = [row for row in tracks if stage in row.get("stages", [])]
        try:
            counted = int(stage_counts.get(stage) or 0)
        except (TypeError, ValueError):
            counted = 0
        # 兼容旧状态：无 stage_counts 时以明细条数为准
        count = counted if counted > 0 else len(rows)
        if count <= 0:
            continue
        by_type[stage] = count
        preview = [_track_line(row) for row in rows[:_PREVIEW_LIMIT]]
        sections.append(
            {
                "stage": stage,
                "label": label,
                "count": count,
                "preview": preview,
                "omitted": max(0, count - len(preview)),
            }
        )
    return {
        "music_summary": True,
        "batch_id": pending["batch_id"],
        "count": sum(by_type.values()),
        "track_count": int(sum(by_type.values())),
        "by_type": by_type,
        "sections": sections,
        "period_start": datetime.fromtimestamp(pending["first_activity_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "period_end": datetime.fromtimestamp(pending["last_activity_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "debounce_sec": MUSIC_DEBOUNCE_SEC,
    }


def format_music_summary(data: Dict[str, Any]) -> str:
    lines = ["音乐库本轮汇总"]
    for section in data.get("sections", []):
        lines.append(f"{section['label']}：{section['count']} 首")
        lines.extend("· " + text for text in section.get("preview") or [])
        if section.get("omitted"):
            lines.append(f"· 另有 {section['omitted']} 首未展开")
    lines.append(f"活动时段：{data.get('period_start', '')} 至 {data.get('period_end', '')}")
    lines.append(f"连续 {MUSIC_DEBOUNCE_SEC} 秒无新增；仅代表本轮静默，不代表整个扫描任务完成。")
    return "\n".join(lines)
