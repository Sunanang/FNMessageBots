"""
配置页事件目录：分类、默认勾选、VM 事件发现与 UI 展示构建。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from monitor.sqlite_uri import connect_readonly_with_fallback
from valid_event_ids import MONITOR_EVENT_IDS

# 事件分类（顺序即展示顺序）；不在此处的事件不会在 UI 中展示
EVENT_CATEGORIES = [
    ("login", "登录与认证", ["LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout"]),
    ("ssh", "SSH", ["SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED"]),
    ("security", "安全", [
        "FW_ENABLE", "FW_DISABLE", "SECURITY_PORTCHANGED", "WAN_IP_CHANGED",
    ]),
    ("hardware", "硬件与告警", [
        "CPU_USAGE_ALARM", "CPU_USAGE_RESTORED", "CPU_TEMPERATURE_ALARM",
        "MEMORY_USAGE_ALARM", "MEMORY_USAGE_RESTORED",
    ]),
    ("disk", "磁盘与存储", ["FoundDisk", "InsertDisk", "EjectDisk", "StorageBroken", "STORAGE_DEGRADED", "DiskWakeup", "DiskSpindown", "DISK_IO_ERR"]),
    ("ups", "UPS", ["UPS_ENABLE", "UPS_DISABLE", "UPS_ONBATT", "UPS_ONBATT_LOWBATT", "UPS_ONLINE"]),
    ("share_protocol", "共享协议", [
        "WEBDAV_ENABLED", "WEBDAV_DISABLED", "SAMBA_ENABLED", "SAMBA_DISABLED",
        "DLNA_ENABLED", "DLNA_DISABLED", "FTP_ENABLED", "FTP_DISABLED", "NFS_ENABLED", "NFS_DISABLED",
    ]),
    ("app_manage", "应用管理", [
        "APP_CRASH", "APP_UPDATE_FAILED",
        "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
        "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
        "APP_STARTED", "APP_STOPPED", "APP_UPDATED",
        "APP_INSTALLED", "APP_AUTO_STARTED", "APP_UNINSTALLED",
    ]),
    ("file_ops", "文件操作", [
        "ARCHIVING_SUCCESS", "DeleteFile", "MovetoTrashbin", "SHARE_EVENTID_DEL", "SHARE_EVENTID_PUT",
    ]),
    ("vm", "虚拟机", [
        "STATUS_RUNNING_VM", "SHUTDOWN_VM", "DESTROY_VM",
    ]),
    ("vm_backup", "备份任务", [
        "BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS",
    ]),
    ("vm_media", "影视库", [
        "MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT", "MEDIA_USER_CREATED",
        "TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS",
    ]),
    ("vm_photo", "相册", [
        "PHOTO_SHARE_CREATED",
        "PHOTO_SHARE_EXPIRED",
        "PHOTO_DEVICE_REGISTERED",
        "FACE_RECOGNITION_UPDATED",
    ]),
    ("docker", "Docker 容器", [
        "DOCKER_CONTAINER_CREATE",
        "DOCKER_CONTAINER_START",
        "DOCKER_CONTAINER_STOP",
        "DOCKER_CONTAINER_DIE",
        "DOCKER_CONTAINER_OOM",
        "DOCKER_CONTAINER_KILL",
        "DOCKER_CONTAINER_PAUSE",
        "DOCKER_CONTAINER_UNPAUSE",
        "DOCKER_CONTAINER_RESTART",
        "DOCKER_CONTAINER_DESTROY",
    ]),
]

# 三方应用事件（独立配置区；当前仅任务计划）
THIRD_PARTY_EVENT_CATEGORIES = [
    # 任务计划三类不在 DEFAULT_SELECTED_EVENTS 中，安装/重置默认后需用户在界面手动勾选
    ("scheduler", "任务计划", [
        "SCHEDULER_TASK_SUCCESS", "SCHEDULER_TASK_FAILED", "SCHEDULER_TASK_CONDITION_FAILED",
    ]),
]

# 虚拟机事件标准顺序（来自数据库 eventId）
VM_EVENT_PREFERRED_ORDER = [
    "CREATE_VM",
    "DELETE_VM",
    "EDIT_VM",
    "OVA_EXPORT_VM",
    "START_VM",
    "SHUTDOWN_VM",
    "PAUSE_VM",
    "RESUME_VM",
    "REBOOT_VM",
    "STATUS_RUNNING_VM",
    "STATUS_SHUTOFF_VM",
    "STATUS_PAUSED_VM",
    "STATUS_RESUMED_VM",
    "STATUS_REBOOTED_VM",
]

# 不在 UI 中提供选择（内部使用的系统事件）
EVENT_IDS_HIDDEN_IN_UI = {"APP_START", "APP_STOP"}

# 应用生命周期事件（默认不勾选）
APP_LIFECYCLE_EVENTS = {
    "APP_STARTED",
    "APP_STOPPED",
    "APP_UPDATED",
    "APP_INSTALLED",
    "APP_AUTO_STARTED",
    "APP_UNINSTALLED",
}

# 后端认可的静态事件 ID（与 valid_event_ids.MONITOR_EVENT_IDS 同源；完整可选集合还包含动态发现的 VM eventId）
VALID_EVENT_IDS = MONITOR_EVENT_IDS

# 默认勾选的事件（不含应用生命周期 6 项；不含任务计划 SCHEDULER_*；
# 应用启动/自启动失败、UPS 开启/关闭 默认不勾选）
DEFAULT_SELECTED_EVENTS = [
    "LoginSucc",
    "LoginSucc2FA1",
    "LoginFail",
    "Logout",
    "FoundDisk",
    "InsertDisk",
    "EjectDisk",
    "StorageBroken",
    "STORAGE_DEGRADED",
    "APP_CRASH",
    "APP_UPDATE_FAILED",
    "CPU_USAGE_ALARM",
    "CPU_USAGE_RESTORED",
    "CPU_TEMPERATURE_ALARM",
    "MEMORY_USAGE_ALARM",
    "MEMORY_USAGE_RESTORED",
    "UPS_ONBATT",
    "UPS_ONBATT_LOWBATT",
    "UPS_ONLINE",
    "DiskWakeup",
    "DiskSpindown",
    "SSH_INVALID_USER",
    "SSH_AUTH_FAILED",
    "SSH_LOGIN_SUCCESS",
    "SSH_DISCONNECTED",
    "DISK_IO_ERR",
]

# 旧版默认勾选（含应用启动失败、自启动失败、UPS 开启/关闭），用于迁移：若当前配置等于此集合则改为新默认
OLD_DEFAULT_SELECTED_EVENTS_WITH_EXTRA = {
    "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
    "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
    "UPS_ENABLE",
    "UPS_DISABLE",
}


def discover_vm_event_ids(db_path: str) -> List[str]:
    """从 logger_data.db3 读取所有包含 VM 的 eventId，按字母序返回。"""
    if not db_path:
        return []
    conn = None
    try:
        conn = connect_readonly_with_fallback(db_path, timeout=3.0)
        cur = conn.execute(
            "SELECT DISTINCT eventId FROM log "
            "WHERE eventId IS NOT NULL AND UPPER(eventId) LIKE '%VM%' "
            "ORDER BY eventId ASC"
        )
        rows = [str(r[0]).strip() for r in cur.fetchall() if r and str(r[0]).strip()]
        return rows
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sort_vm_event_ids(event_ids: List[str]) -> List[str]:
    """按预设顺序优先，其余事件按字母序追加。"""
    uniq: List[str] = []
    seen = set()
    for e in event_ids:
        s = (e or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    preferred = [e for e in VM_EVENT_PREFERRED_ORDER if e in seen]
    rest = sorted([e for e in uniq if e not in preferred])
    return preferred + rest


def _build_events_by_category_list(
    category_defs: List[Tuple[str, str, List[str]]],
    discovered_vm_event_ids: List[str],
    titles: Dict[str, str],
    notes: Dict[str, str],
) -> List[Dict[str, Any]]:
    events_by_category: List[Dict[str, Any]] = []
    for cat_id, cat_name, event_ids in category_defs:
        ids = list(event_ids)
        if cat_id == "vm":
            ids = discovered_vm_event_ids
        events = []
        for key in ids:
            if key in EVENT_IDS_HIDDEN_IN_UI:
                continue
            raw_title = titles.get(key)
            if raw_title:
                display_title = raw_title.replace("飞牛NAS-", "").replace("飞牛NAS", "")
                display_title = re.sub(r"\s+", " ", display_title).strip()
            else:
                display_title = f"虚拟机事件：{key}" if "VM" in key.upper() else key
            base_note = notes.get(key, "")
            events.append({
                "id": key,
                "title": display_title,
                "note": base_note,
            })
        if events:
            events_by_category.append({"id": cat_id, "name": cat_name, "events": events})
    return events_by_category


def build_events_for_ui(
    logger_db_path: str,
    backup_db_path: str,
    trim_media_db_path: str,
    trim_activity_db_path: str,
    photo_db_path: str,
    scheduler_db_path: str = "",
    titles: Dict[str, str] = None,
    notes: Dict[str, str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], set, List[str]]:
    """构建配置页事件分类（系统事件 + 三方应用事件）与可选事件集合。"""
    if titles is None:
        titles = {}
    if notes is None:
        notes = {}
    discovered_vm_event_ids = discover_vm_event_ids(logger_db_path)
    if not discovered_vm_event_ids:
        discovered_vm_event_ids = list(VM_EVENT_PREFERRED_ORDER)
    discovered_vm_event_ids = sort_vm_event_ids(discovered_vm_event_ids)

    valid_event_ids = set(VALID_EVENT_IDS) | set(discovered_vm_event_ids)

    events_by_category = _build_events_by_category_list(
        EVENT_CATEGORIES, discovered_vm_event_ids, titles, notes
    )
    third_party_events_by_category = _build_events_by_category_list(
        THIRD_PARTY_EVENT_CATEGORIES, discovered_vm_event_ids, titles, notes
    )
    return events_by_category, third_party_events_by_category, valid_event_ids, discovered_vm_event_ids
