"""
用户可配置的监控事件 ID 白名单。

供 Config._validate、Web 配置页保存过滤、event_catalog 等与「可选事件」相关的逻辑共用。
动态发现的虚拟机 eventId（logger 中）通过名称含 VM 另行允许，不在此 frozenset 内。
"""

from __future__ import annotations

from typing import Any, Iterable, List

# 与 MultiPlatformNotifier / EventProcessor 中已实现推送的事件类型保持一致
MONITOR_EVENT_IDS = frozenset({
    "LoginSucc",
    "LoginSucc2FA1",
    "LoginFail",
    "Logout",
    "FoundDisk",
    "InsertDisk",
    "EjectDisk",
    "StorageBroken",
    "STORAGE_DEGRADED",
    "SSH_INVALID_USER",
    "SSH_AUTH_FAILED",
    "SSH_LOGIN_SUCCESS",
    "SSH_DISCONNECTED",
    "APP_CRASH",
    "APP_UPDATE_FAILED",
    "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
    "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
    "APP_STARTED",
    "APP_STOPPED",
    "APP_UPDATED",
    "APP_INSTALLED",
    "APP_AUTO_STARTED",
    "APP_UNINSTALLED",
    "CPU_USAGE_ALARM",
    "CPU_USAGE_RESTORED",
    "CPU_TEMPERATURE_ALARM",
    "MEMORY_USAGE_ALARM",
    "MEMORY_USAGE_RESTORED",
    "UPS_ONBATT",
    "UPS_ONBATT_LOWBATT",
    "UPS_ONLINE",
    "UPS_ENABLE",
    "UPS_DISABLE",
    "DiskWakeup",
    "DiskSpindown",
    "DISK_IO_ERR",
    "ARCHIVING_SUCCESS",
    "DeleteFile",
    "MovetoTrashbin",
    "SHARE_EVENTID_DEL",
    "SHARE_EVENTID_PUT",
    "WEBDAV_ENABLED",
    "WEBDAV_DISABLED",
    "SAMBA_ENABLED",
    "SAMBA_DISABLED",
    "DLNA_ENABLED",
    "DLNA_DISABLED",
    "FTP_ENABLED",
    "FTP_DISABLED",
    "NFS_ENABLED",
    "NFS_DISABLED",
    "FW_ENABLE",
    "FW_DISABLE",
    "SECURITY_PORTCHANGED",
    "SHUTDOWN_VM",
    "STATUS_RUNNING_VM",
    "STATUS_REBOOTED_VM",
    "DESTROY_VM",
    "BACKUP_TASK_SUCCESS",
    "BACKUP_TASK_FAILED",
    "BACKUP_TASK_PARTIAL_SUCCESS",
    "SCHEDULER_TASK_SUCCESS",
    "SCHEDULER_TASK_FAILED",
    "SCHEDULER_TASK_CONDITION_FAILED",
    "MEDIA_LOGIN_SUCC",
    "MEDIA_LOGOUT",
    "MEDIA_USER_CREATED",
    "TRIM_RESOURCE_ADDED",
    "TRIM_SCRAPE_SUCCESS",
    "PHOTO_SHARE_CREATED",
    "PHOTO_SHARE_EXPIRED",
    "PHOTO_DEVICE_REGISTERED",
    "FACE_RECOGNITION_UPDATED",
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
})


def is_allowed_monitor_event(event_id: Any) -> bool:
    """是否为允许写入 monitor_events 的事件 ID（含白名单或动态 VM 类 eventId）。"""
    s = str(event_id).strip()
    if not s:
        return False
    if s in MONITOR_EVENT_IDS:
        return True
    return "VM" in s.upper()


def filter_monitor_events(events: Iterable[Any]) -> List[Any]:
    """过滤掉未知事件 ID，保留元素顺序与原类型（与旧版 list 推导行为一致）。"""
    return [e for e in events if is_allowed_monitor_event(e)]
