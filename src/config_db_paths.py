"""
宿主机数据库路径：与 docker-compose 常见挂载一致。
配置项为空时，若容器内对应文件可读则自动填入（挂载 ≠ 写入 config.json）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

# 与 README / faq / 仓库 config/config.json 对齐；scheduler 多候选适配不同飞牛版本目录
_DB_PATH_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "logger_db_path": ("/usr/trim/var/eventlogger_service/logger_data.db3",),
    "backup_db_path": ("/usr/trim/var/backup_service/basic_backup.db3",),
    "trim_media_db_path": ("/usr/local/apps/@appdata/trim.media/database/trimmedia.db",),
    "trim_activity_db_path": (
        "/usr/local/apps/@appdata/trim.media/database/trimactivity.db",
    ),
    "photo_db_path": ("/usr/local/apps/@appdata/trim.photos/db/photo.db",),
    "music_db_path": ("/usr/local/apps/@appdata/trim.music/db/music.db",),
    "scheduler_db_path": (
        "/var/apps/fn-scheduler/var/scheduler.db",
        "/usr/local/apps/@appdata/fn-scheduler/database/scheduler.db",
        "/usr/local/apps/@appdata/fn-scheduler/scheduler.db",
    ),
}

_SCHEDULER_SEARCH_ROOTS: Tuple[str, ...] = (
    "/var/apps/fn-scheduler/var",
    "/usr/local/apps/@appdata/fn-scheduler",
)

_DB_PATH_ENV_VARS: Dict[str, str] = {
    "logger_db_path": "LOGGER_DB_PATH",
    "backup_db_path": "BACKUP_DB_PATH",
    "trim_media_db_path": "TRIM_MEDIA_DB_PATH",
    "trim_activity_db_path": "TRIM_ACTIVITY_DB_PATH",
    "photo_db_path": "PHOTO_DB_PATH",
    "music_db_path": "MUSIC_DB_PATH",
    "scheduler_db_path": "SCHEDULER_DB_PATH",
}


def _is_readable_db_file(path: str) -> bool:
    p = (path or "").strip()
    if not p:
        return False
    try:
        return os.path.isfile(p) and os.access(p, os.R_OK)
    except OSError:
        return False


def _find_scheduler_db() -> str:
    for cand in _DB_PATH_CANDIDATES.get("scheduler_db_path", ()):
        if _is_readable_db_file(cand):
            return cand
    for root in _SCHEDULER_SEARCH_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            for hit in sorted(base.rglob("scheduler.db")):
                if hit.is_file() and os.access(hit, os.R_OK):
                    return str(hit)
        except OSError:
            continue
    return ""


def discover_db_path(key: str) -> str:
    """返回容器内首个可读的数据库文件路径；无则空字符串。"""
    k = (key or "").strip()
    if k == "scheduler_db_path":
        return _find_scheduler_db()
    for cand in _DB_PATH_CANDIDATES.get(k, ()):
        if _is_readable_db_file(cand):
            return cand
    return ""


def diagnostic_db_path(key: str, configured: str = "") -> str:
    """
    诊断用路径：优先已配置值；为空时回退到已知候选。

    与 discover_db_path 不同：不要求当前进程可读。音乐库等 0700 目录下，
    文件「看起来不存在/配置为空」时仍要用标准路径生成 ACL 命令。
    """
    cur = (configured or "").strip()
    if cur:
        return cur
    k = (key or "").strip()
    cands = _DB_PATH_CANDIDATES.get(k, ())
    if not cands:
        return ""
    for cand in cands:
        try:
            # 可读或至少能看到文件/父目录时优先用该候选
            if os.path.isfile(cand) or os.path.isdir(os.path.dirname(cand)):
                return cand
        except OSError:
            continue
    return cands[0]


def apply_discovered_db_paths(
    cfg: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    为 cfg 中空白的库路径键填入探测结果。
    返回 (新 dict, 人类可读说明列表，供日志或提示)。

    宿主（FPK）上：可读探测失败时仍回退到标准路径（如 music.db），
    以便空配置也按默认路径访问/做 ACL 诊断；Docker 内不填宿主路径。
    """
    from utils.access_guidance import in_container

    out: Dict[str, Any] = dict(cfg)
    notes: List[str] = []
    for key in _DB_PATH_CANDIDATES:
        cur = str(out.get(key) or "").strip()
        if cur:
            continue
        env_name = _DB_PATH_ENV_VARS.get(key, "")
        env_value = (os.getenv(env_name) or "").strip() if env_name else ""
        if env_value:
            out[key] = env_value
            notes.append(f"已根据环境变量 {env_name} 设置 {key}={env_value}")
            continue
        found = discover_db_path(key)
        if not found and not in_container():
            found = diagnostic_db_path(key, "")
            if found:
                out[key] = found
                notes.append(f"已使用标准默认路径 {key}={found}")
                continue
        if not found:
            continue
        out[key] = found
        notes.append(f"已根据挂载自动识别 {key}={found}")
    return out, notes


DB_PATH_CONFIG_KEYS: Tuple[str, ...] = tuple(_DB_PATH_CANDIDATES.keys())
