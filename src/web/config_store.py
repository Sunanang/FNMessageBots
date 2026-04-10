"""
Web 配置存储辅助：配置文件读写、URL 列表拼拆、标题前缀解析。
"""

from __future__ import annotations

import json
from pathlib import Path

from config import TITLE_PREFIX_DEFAULT


def title_prefix_from_dict(d: dict, key: str = "title_prefix") -> str:
    """无 title_prefix 时用默认；显式空/空白则返回空。"""
    if key not in d:
        return TITLE_PREFIX_DEFAULT
    v = d[key]
    if v is None:
        return TITLE_PREFIX_DEFAULT
    return v.strip() if isinstance(v, str) else str(v).strip()


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

