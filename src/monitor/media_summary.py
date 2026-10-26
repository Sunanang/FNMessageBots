"""影视静默批次的数据与文案；不依赖数据库或推送客户端。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Dict, List

MEDIA_SUMMARY_EVENT = "MEDIA_LIBRARY_SUMMARY"
MEDIA_DEBOUNCE_SEC = 60
MEDIA_STAGES = {"TRIM_RESOURCE_ADDED": "入库", "TRIM_SCRAPE_SUCCESS": "刮削完成"}
_PREVIEW_LIMIT = 4


def _short(text: str, size: int = 160) -> str:
    raw = text.replace("\n", " ").replace("\r", " ").encode("utf-8")
    return raw.decode() if len(raw) <= size else raw[:size - 3].decode("utf-8", "ignore") + "..."


def build_media_summary(pending: Dict[str, Any]) -> Dict[str, Any]:
    resources = list(pending.get("resources", {}).values())
    by_type = {}
    sections = []
    for stage, label in MEDIA_STAGES.items():
        rows = [row for row in resources if stage in row.get("stages", [])]
        if not rows:
            continue
        by_type[stage] = len(rows)
        types = Counter(str(row.get("item_type") or "Video") for row in rows)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            if row.get("item_type") == "Episode" and row.get("series_guid"):
                key = f"series:{row['series_guid']}:{row.get('season_number')}"
            else:
                key = str(row["resource_key"])
            groups.setdefault(key, []).append(row)
        preview = []
        for group in list(groups.values())[:_PREVIEW_LIMIT]:
            row = group[0]
            title = row.get("series_title") if row.get("item_type") == "Episode" else row.get("title")
            title = title or row.get("title") or row.get("filename") or "未命名资源"
            line = f"《{title}》"
            if row.get("season_number") is not None:
                line += f" 第{row['season_number']}季"
            episodes = sorted({int(item["episode_number"]) for item in group
                               if isinstance(item.get("episode_number"), int)})
            if episodes:
                line += " 第" + "、".join(str(number) for number in episodes[:12]) + "集"
                if len(episodes) > 12:
                    line += f"等共{len(episodes)}集"
            preview.append(_short(line))
        sections.append({"stage": stage, "label": label, "count": len(rows), "types": dict(types),
                         "preview": preview, "omitted": max(0, len(groups) - len(preview))})
    return {"media_summary": True, "batch_id": pending["batch_id"], "count": sum(by_type.values()),
            "resource_count": len(resources), "by_type": by_type, "sections": sections,
            "period_start": datetime.fromtimestamp(pending["first_activity_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "period_end": datetime.fromtimestamp(pending["last_activity_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "debounce_sec": MEDIA_DEBOUNCE_SEC}


def format_media_summary(data: Dict[str, Any]) -> str:
    lines = ["影视处理本轮汇总"]
    names = {"Movie": "电影", "TV": "剧集", "Season": "季", "Episode": "单集", "Video": "视频"}
    for section in data.get("sections", []):
        counts = "、".join(f"{names.get(kind, kind)} {count} 项" for kind, count in section["types"].items())
        lines.append(f"{section['label']}：{section['count']} 个去重条目（{counts}）")
        lines.extend("· " + text for text in section["preview"])
        if section["omitted"]:
            lines.append(f"· 另有 {section['omitted']} 组未展开")
    lines.append(f"活动时段：{data.get('period_start', '')} 至 {data.get('period_end', '')}")
    lines.append(f"连续 {MEDIA_DEBOUNCE_SEC} 秒无新增；仅代表本轮静默，不代表整个任务完成。")
    return "\n".join(lines)
