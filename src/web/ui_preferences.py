"""外观偏好独立持久化，不触发监控热加载或覆盖推送配置。"""
import json
from pathlib import Path
from typing import Any, Dict
from web.config_store import save_raw_config


def load_preferences(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("界面偏好文件必须为 JSON 对象")
    return data


def save_theme(path: Path, theme: str) -> None:
    data = load_preferences(path)
    data["theme"] = theme
    save_raw_config(path, data)
