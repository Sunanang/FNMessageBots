"""
Web 配置存储辅助：配置文件读写、URL 列表拼拆、标题前缀解析、推送渠道启用状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from config import TITLE_PREFIX_DEFAULT
from utils.value_parser import as_bool

# 渠道类型 → config.json 扁平字段（仅写入「已开启」的渠道，供通知器使用）
CHANNEL_TYPE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("wechat", "wechat_webhook_url"),
    ("dingtalk", "dingtalk_webhook_url"),
    ("feishu", "feishu_webhook_url"),
    ("bark", "bark_url"),
    ("pushplus", "pushplus_params"),
    ("magic_push", "magic_push_params"),
    ("smtp", "smtp_params"),
)


def title_prefix_from_dict(d: dict, key: str = "title_prefix") -> str:
    """无 title_prefix 时用默认；显式空/空白则返回空。"""
    if key not in d:
        return TITLE_PREFIX_DEFAULT
    v = d[key]
    if v is None:
        return TITLE_PREFIX_DEFAULT
    return v.strip() if isinstance(v, str) else str(v).strip()


def config_load_error(config_file: Path) -> str:
    """若 config.json 不可读或 JSON 非法则返回错误说明，否则返回空串。"""
    if not config_file.exists():
        return ""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as e:
        return f"config.json 不是合法 JSON：{e}"
    except OSError as e:
        return f"无法读取配置文件：{e}"


def load_raw_config(config_file: Path) -> dict:
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # 配置损坏时回退空配置，避免 UI 崩溃
            return {}
    return {}


def save_raw_config(config_file: Path, data: dict) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def split_urls(raw: str):
    if not raw:
        return []
    return [u.strip() for u in str(raw).split("|") if u.strip()]


def join_urls(urls):
    clean = [u.strip() for u in urls if u and u.strip()]
    return "|".join(clean)


def channels_from_raw(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    供配置页展示：优先读 push_channels（含 enabled）；
    旧配置仅有扁平字段时，全部视为开启。
    """
    stored = raw.get("push_channels")
    if isinstance(stored, list) and stored:
        out: List[Dict[str, Any]] = []
        for item in stored:
            if not isinstance(item, dict):
                continue
            ch_type = str(item.get("type") or "").strip()
            url = (item.get("url") or "").strip()
            if not ch_type or not url:
                continue
            if url.startswith("${") and url.endswith("}"):
                continue
            out.append(
                {
                    "type": ch_type,
                    "url": url,
                    "enabled": as_bool(item.get("enabled", True), True),
                }
            )
        if out:
            return out

    out = []
    for ch_type, key in CHANNEL_TYPE_KEYS:
        for url in split_urls(raw.get(key, "") or ""):
            if url.startswith("${") and url.endswith("}"):
                continue
            out.append({"type": ch_type, "url": url, "enabled": True})
    return out


def normalize_push_channels(channels: Any) -> List[Dict[str, Any]]:
    """规范化前端提交的渠道列表。"""
    if not isinstance(channels, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in channels:
        if not isinstance(item, dict):
            continue
        ch_type = str(item.get("type") or "").strip()
        url = (item.get("url") or "").strip()
        if not ch_type or not url:
            continue
        out.append(
            {
                "type": ch_type,
                "url": url,
                "enabled": as_bool(item.get("enabled", True), True),
            }
        )
    return out


def sync_channel_flat_keys(channels: List[Dict[str, Any]]) -> Dict[str, str]:
    """仅把已开启渠道写入扁平字段，关闭的渠道只保留在 push_channels。"""
    buckets = {key: [] for _, key in CHANNEL_TYPE_KEYS}
    type_to_key = dict(CHANNEL_TYPE_KEYS)
    for ch in channels:
        if not as_bool(ch.get("enabled", True), True):
            continue
        key = type_to_key.get(str(ch.get("type") or ""))
        url = (ch.get("url") or "").strip()
        if key and url:
            buckets[key].append(url)
    return {key: join_urls(urls) for key, urls in buckets.items()}
