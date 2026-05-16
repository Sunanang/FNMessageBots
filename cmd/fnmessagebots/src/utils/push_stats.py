"""
推送统计：总条数/成功/失败、当日条数/成功/失败，持久化到 cursor_dir/push_stats.json
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any
from zoneinfo import ZoneInfo

_lock = threading.Lock()
_stats_path: str = ""

_DEFAULT = {"total": {"success": 0, "fail": 0}, "daily": {}}


def init(cursor_dir: str) -> None:
    """初始化统计文件路径。cursor_dir 可为相对路径（相对当前工作目录）。同时初始化推送记录 DB。"""
    global _stats_path
    if not cursor_dir:
        cursor_dir = "./data/cursor"
    abs_dir = os.path.abspath(os.path.join(os.getcwd(), cursor_dir))
    Path(abs_dir).mkdir(parents=True, exist_ok=True)
    _stats_path = os.path.join(abs_dir, "push_stats.json")
    try:
        from utils.push_history import init as init_push_history
        init_push_history(cursor_dir)
    except Exception:
        pass


def _load() -> Dict[str, Any]:
    if not _stats_path or not os.path.isfile(_stats_path):
        return _DEFAULT.copy()
    try:
        with open(_stats_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "total" in data and "daily" in data:
                return data
    except Exception:
        pass
    return _DEFAULT.copy()


def _save(data: Dict[str, Any]) -> None:
    if not _stats_path:
        return
    try:
        with open(_stats_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _today_key() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def record(success: bool) -> None:
    """记录一次推送结果。"""
    with _lock:
        data = _load()
        if success:
            data["total"]["success"] = data["total"].get("success", 0) + 1
        else:
            data["total"]["fail"] = data["total"].get("fail", 0) + 1
        key = _today_key()
        if key not in data["daily"]:
            data["daily"][key] = {"success": 0, "fail": 0}
        if success:
            data["daily"][key]["success"] = data["daily"][key].get("success", 0) + 1
        else:
            data["daily"][key]["fail"] = data["daily"][key].get("fail", 0) + 1
        _save(data)


def get_total() -> Dict[str, int]:
    """返回总统计：total, success, fail。"""
    with _lock:
        data = _load()
    t = data.get("total", {})
    s = t.get("success", 0)
    f = t.get("fail", 0)
    return {"total": s + f, "success": s, "fail": f}


def get_today() -> Dict[str, int]:
    """返回当日统计：total, success, fail。"""
    with _lock:
        data = _load()
    key = _today_key()
    d = data.get("daily", {}).get(key, {})
    s = d.get("success", 0)
    f = d.get("fail", 0)
    return {"total": s + f, "success": s, "fail": f}


def get_stats_path() -> str:
    """返回当前统计文件路径（供 Web 读取用）。"""
    return _stats_path
