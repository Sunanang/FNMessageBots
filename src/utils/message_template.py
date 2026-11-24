"""消息标题/正文自定义模板：事件族 + 单事件，渲染与配置规范化。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

TEMPLATE_VERSION = 1
TITLE_SOFT_WARN_CHARS = 80
LOG_PREVIEW_MAX_CHARS = 500
MAX_TEMPLATE_CHARS = 8000

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# 覆盖 EVENT_TITLES 里实际用到的前缀：emoji、杂项技术符（⏰⏸️⏹️）、几何图形（▶️）等
_EMOJI_PREFIX_RE = re.compile(
    r"^["
    r"\U0001F300-\U0001F5FF"
    r"\U0001F600-\U0001F64F"
    r"\U0001F680-\U0001F6FF"
    r"\U0001F700-\U0001F77F"
    r"\U0001F780-\U0001F7FF"
    r"\U0001F800-\U0001F8FF"
    r"\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FAFF"
    r"\u2300-\u23FF"
    r"\u25A0-\u25FF"
    r"\u2600-\u27BF"
    r"\u200d"
    r"\ufe0f"
    r"]+",
    flags=re.UNICODE,
)

TEMPLATE_BLACKLIST = frozenset({
    "APP_START",
    "APP_STOP",
    "POLL_BATCH_SUMMARY",
    "MEDIA_LIBRARY_SUMMARY",
    "MUSIC_LIBRARY_SUMMARY",
    "STARTUP_QUIET_SUMMARY",
    "DND_SUMMARY",
    "NAS_PATROL_REPORT",
    "TEST_PUSH",
})

GLOBAL_VARS = (
    "prefix",
    "event_type",
    "event_title",
    "event_icon",
    "time",
    "note",
)

# 各族业务变量（并集白名单）
FAMILY_VARS: Dict[str, Tuple[str, ...]] = {
    "login": ("user", "ip", "via"),
    "user_manage": ("user", "uid", "target_user", "target_uid"),
    "ssh": ("user", "ip", "port", "reason", "elapsed_minutes", "opened_at", "remind_minutes"),
    "security": ("user", "from", "old_wan_ip", "new_wan_ip"),
    "hardware": ("threshold", "top_processes"),
    "disk": ("name", "model", "serial", "volume", "disk", "err_cnt"),
    "ups": ("status_text",),
    "share_protocol": ("user", "from"),
    "app_manage": ("app_name", "app_id", "display_name", "from"),
    "file_ops": ("user", "from"),
    "vm": ("user", "vm_title", "from"),
    "vm_backup": (
        "task_name", "status", "error_message", "storage_name", "target_path",
        "files_count", "actual_size", "start_time", "finished_time",
    ),
    "vm_media": ("user", "ip", "via", "title", "path", "item_type", "app_name", "email", "role"),
    "vm_photo": (
        "share_name", "share_id", "device_display", "person_names_text",
        "photos_with_face", "faces_detected", "record_count", "expired_at_str",
    ),
    "vm_music": ("title", "artist", "album", "playlist_name", "user_label"),
    "docker": ("container_name", "container_id", "image", "docker_action", "exit_code"),
    "scheduler": (
        "task_name", "status", "account", "trigger_reason_label",
        "started_at", "finished_at", "duration", "log_preview",
    ),
}

# 与 event_catalog 分类对齐（utils 不依赖 web，避免循环导入）
_FAMILY_DEFS: List[Tuple[str, str, List[str]]] = [
    ("login", "登录与认证", ["LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout"]),
    ("user_manage", "用户管理", ["AddUser", "DelUser"]),
    ("ssh", "SSH", [
        "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
        "SSH_ENABLED", "SSH_DISABLED", "SSH_OPEN_TIMEOUT",
    ]),
    ("security", "安全", ["FW_ENABLE", "FW_DISABLE", "SECURITY_PORTCHANGED", "WAN_IP_CHANGED"]),
    ("hardware", "硬件与告警", [
        "CPU_USAGE_ALARM", "CPU_USAGE_RESTORED", "CPU_TEMPERATURE_ALARM",
        "MEMORY_USAGE_ALARM", "MEMORY_USAGE_RESTORED",
    ]),
    ("disk", "磁盘与存储", [
        "FoundDisk", "InsertDisk", "EjectDisk", "StorageBroken", "STORAGE_DEGRADED",
        "DiskWakeup", "DiskSpindown", "DISK_IO_ERR",
    ]),
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
        "CREATE_VM", "DELETE_VM", "EDIT_VM", "OVA_EXPORT_VM", "START_VM",
        "PAUSE_VM", "RESUME_VM", "REBOOT_VM",
        "STATUS_SHUTOFF_VM", "STATUS_PAUSED_VM", "STATUS_RESUMED_VM", "STATUS_REBOOTED_VM",
    ]),
    ("vm_backup", "备份任务", [
        "BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS",
    ]),
    ("vm_media", "影视库", [
        "MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT", "MEDIA_USER_CREATED",
        "TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS",
    ]),
    ("vm_photo", "相册", [
        "PHOTO_SHARE_CREATED", "PHOTO_SHARE_EXPIRED",
        "PHOTO_DEVICE_REGISTERED", "FACE_RECOGNITION_UPDATED",
    ]),
    ("vm_music", "音乐库", [
        "MUSIC_TRACK_ADDED", "MUSIC_TRACK_REMOVED", "MUSIC_FAVORITED", "MUSIC_PLAYLIST_TRACK_ADDED",
    ]),
    ("docker", "Docker 容器", [
        "DOCKER_CONTAINER_CREATE", "DOCKER_CONTAINER_START", "DOCKER_CONTAINER_STOP",
        "DOCKER_CONTAINER_DIE", "DOCKER_CONTAINER_OOM", "DOCKER_CONTAINER_KILL",
        "DOCKER_CONTAINER_PAUSE", "DOCKER_CONTAINER_UNPAUSE",
        "DOCKER_CONTAINER_RESTART", "DOCKER_CONTAINER_DESTROY",
    ]),
    ("scheduler", "任务计划", [
        "SCHEDULER_TASK_SUCCESS", "SCHEDULER_TASK_FAILED", "SCHEDULER_TASK_CONDITION_FAILED",
    ]),
]

# event_data 别名 → 白名单名
_FIELD_ALIASES: Dict[str, str] = {
    "IP": "ip",
    "ip": "ip",
    "USER_NAME": "user",
    "uname": "user",
    "user_guid": "user",
    "THRESHOLD": "threshold",
    "threshold": "threshold",
    "VOL": "volume",
    "vol": "volume",
    "volume": "volume",
    "APP_NAME": "app_name",
    "APP_ID": "app_id",
    "DISPLAY_NAME": "display_name",
    "VM_TITLE": "vm_title",
    "vm_title": "vm_title",
    "top_cpu_processes": "top_processes",
    "previous_wan_ip": "old_wan_ip",
    "wan_ip": "new_wan_ip",
    "EMAIL": "email",
    "email": "email",
    "NEW_USER": "user",
    "ROLE": "role",
    "role": "role",
}

_FAMILY_NAME: Dict[str, str] = {}
_EVENT_TO_FAMILY: Dict[str, str] = {}
for _fid, _fname, _eids in _FAMILY_DEFS:
    _FAMILY_NAME[_fid] = _fname
    for _eid in _eids:
        _EVENT_TO_FAMILY[_eid] = _fid

_DEFAULT_TITLE = "{{event_icon}} {{prefix}}-{{event_title}}"

FAMILY_DEFAULT_BODIES: Dict[str, str] = {
    "login": "{{time}}\n用户名: {{user}}\nIP地址: {{ip}}\n认证方式: {{via}}\n{{note}}",
    "user_manage": (
        "{{time}}\n操作用户: {{user}}\n操作用户 UID: {{uid}}\n"
        "目标用户: {{target_user}}\n目标用户 UID: {{target_uid}}\n{{note}}"
    ),
    "ssh": (
        "{{time}}\n用户名: {{user}}\nIP地址: {{ip}}\n端口: {{port}}\n失败原因: {{reason}}\n"
        "已开启时长(分): {{elapsed_minutes}}\n开启时间: {{opened_at}}\n"
        "提醒间隔(分): {{remind_minutes}}\n{{note}}"
    ),
    "security": (
        "{{time}}\n用户: {{user}}\n来源: {{from}}\n"
        "原外网IP: {{old_wan_ip}}\n新外网IP: {{new_wan_ip}}\n{{note}}"
    ),
    "hardware": "{{time}}\n阈值: {{threshold}}\n占用进程: {{top_processes}}\n{{note}}",
    "disk": (
        "{{time}}\n名称: {{name}}\n型号: {{model}}\n序列号: {{serial}}\n"
        "卷: {{volume}}\n磁盘: {{disk}}\n错误次数: {{err_cnt}}\n{{note}}"
    ),
    "ups": "{{time}}\n{{status_text}}\n{{note}}",
    "share_protocol": "{{time}}\n用户: {{user}}\n来源: {{from}}\n{{note}}",
    "app_manage": (
        "{{time}}\n应用: {{display_name}}\n应用ID: {{app_id}}\n"
        "应用名: {{app_name}}\n来源: {{from}}\n{{note}}"
    ),
    "file_ops": "{{time}}\n用户: {{user}}\n来源: {{from}}\n{{note}}",
    "vm": "{{time}}\n用户: {{user}}\n虚拟机: {{vm_title}}\n来源: {{from}}\n{{note}}",
    "vm_backup": (
        "{{time}}\n任务名称: {{task_name}}\n状态: {{status}}\n存储: {{storage_name}}\n"
        "备份路径: {{target_path}}\n传输文件数: {{files_count}}\n传输大小: {{actual_size}}\n"
        "开始: {{start_time}}\n结束: {{finished_time}}\n错误: {{error_message}}\n{{note}}"
    ),
    "vm_media": (
        "{{time}}\n用户: {{user}}\nIP: {{ip}}\n认证: {{via}}\n标题: {{title}}\n"
        "路径: {{path}}\n类型: {{item_type}}\n应用: {{app_name}}\n邮箱: {{email}}\n"
        "角色: {{role}}\n{{note}}"
    ),
    "vm_photo": (
        "{{time}}\n分享名: {{share_name}}\n分享ID: {{share_id}}\n设备: {{device_display}}\n"
        "人物: {{person_names_text}}\n有人脸照片数: {{photos_with_face}}\n"
        "人脸数: {{faces_detected}}\n记录数: {{record_count}}\n过期: {{expired_at_str}}\n{{note}}"
    ),
    "vm_music": (
        "{{time}}\n标题: {{title}}\n艺术家: {{artist}}\n专辑: {{album}}\n"
        "歌单: {{playlist_name}}\n用户: {{user_label}}\n{{note}}"
    ),
    "docker": (
        "{{time}}\n容器名称: {{container_name}}\n容器ID: {{container_id}}\n"
        "镜像: {{image}}\n引擎动作: {{docker_action}}\n退出码: {{exit_code}}\n{{note}}"
    ),
    "scheduler": (
        "{{time}}\n任务名称: {{task_name}}\n状态: {{status}}\n执行账户: {{account}}\n"
        "触发方式: {{trigger_reason_label}}\n开始: {{started_at}}\n结束: {{finished_at}}\n"
        "耗时: {{duration}}\n日志: {{log_preview}}\n{{note}}"
    ),
}


def empty_templates() -> Dict[str, Any]:
    return {"version": TEMPLATE_VERSION, "families": {}, "events": {}}


def family_id_for_event(event_type: str) -> Optional[str]:
    et = str(event_type or "")
    if not et or et in TEMPLATE_BLACKLIST:
        return None
    fid = _EVENT_TO_FAMILY.get(et)
    if fid:
        return fid
    if "VM" in et.upper():
        return "vm"
    return None


def family_display_name(family_id: str) -> str:
    return _FAMILY_NAME.get(family_id, family_id)


def is_template_allowed(event_type: str) -> bool:
    return family_id_for_event(event_type) is not None


def _valid_family_ids() -> set:
    return set(FAMILY_VARS.keys())


def _strip_entry(obj: Any) -> Optional[Dict[str, str]]:
    if not isinstance(obj, dict):
        return None
    title = str(obj.get("title") or "")
    body = str(obj.get("body") or "")
    if len(title) > MAX_TEMPLATE_CHARS or len(body) > MAX_TEMPLATE_CHARS:
        return None
    if not title.strip() and not body.strip():
        return None
    out: Dict[str, str] = {"title": title, "body": body}
    updated = obj.get("updated_at")
    if isinstance(updated, str) and updated.strip():
        out["updated_at"] = updated.strip()
    return out


def normalize_message_templates(raw: Any) -> Dict[str, Any]:
    """规范化配置；未知 key / 坏结构丢弃。"""
    base = empty_templates()
    if not isinstance(raw, dict):
        return base
    families_in = raw.get("families") if isinstance(raw.get("families"), dict) else {}
    events_in = raw.get("events") if isinstance(raw.get("events"), dict) else {}
    valid_f = _valid_family_ids()
    for fid, entry in families_in.items():
        if str(fid) not in valid_f:
            continue
        cleaned = _strip_entry(entry)
        if cleaned:
            base["families"][str(fid)] = cleaned
    for eid, entry in events_in.items():
        et = str(eid)
        if not is_template_allowed(et):
            continue
        cleaned = _strip_entry(entry)
        if cleaned:
            base["events"][et] = cleaned
    return base


def builtin_template(family_id: str) -> Tuple[str, str]:
    body = FAMILY_DEFAULT_BODIES.get(family_id, "{{time}}\n{{note}}")
    return _DEFAULT_TITLE, body


def resolve_template(
    event_type: str,
    templates: Optional[Dict[str, Any]],
) -> Tuple[str, str, str]:
    """返回 (scope, title, body)；scope 为 event|family|builtin。黑名单返回 builtin 空由调用方走官方。"""
    et = str(event_type or "")
    fid = family_id_for_event(et)
    if fid is None:
        return "builtin", "", ""
    store = normalize_message_templates(templates or {})
    ev = store["events"].get(et)
    if isinstance(ev, dict) and (ev.get("title") or ev.get("body")):
        return "event", str(ev.get("title") or ""), str(ev.get("body") or "")
    fam = store["families"].get(fid)
    if isinstance(fam, dict) and (fam.get("title") or fam.get("body")):
        return "family", str(fam.get("title") or ""), str(fam.get("body") or "")
    title, body = builtin_template(fid)
    return "builtin", title, body


def parse_official_title(raw_title: str) -> Tuple[str, str]:
    """从官方 EVENT_TITLES 拆出 (icon, short_title)。"""
    text = str(raw_title or "").strip()
    icon = ""
    m = _EMOJI_PREFIX_RE.match(text)
    if m:
        icon = m.group(0).strip()
        text = text[m.end():].strip()
    for token in ("飞牛NAS-", "飞牛NAS"):
        text = text.replace(token, "")
    text = re.sub(r"\s+", " ", text).strip(" -")
    return icon, text


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_coerce_str(v) for v in value if v not in (None, "")]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def build_context(
    event_type: str,
    event_data: Optional[Dict[str, Any]],
    *,
    prefix: str = "",
    timestamp: str = "",
    official_title: str = "",
    official_note: str = "",
) -> Dict[str, str]:
    et = str(event_type or "")
    data = event_data if isinstance(event_data, dict) else {}
    icon, short = parse_official_title(official_title)
    if not short:
        short = et or "未知事件"
    pfx = (prefix or "").strip() or "飞牛NAS"
    ctx: Dict[str, str] = {
        "prefix": pfx,
        "event_type": et,
        "event_title": short,
        "event_icon": icon,
        "time": str(timestamp or ""),
        "note": str(official_note or "").strip(),
    }
    # 扁平化常用字段 + 别名
    flat: Dict[str, Any] = dict(data)
    nested = data.get("data")
    if isinstance(nested, dict):
        for k, v in nested.items():
            flat.setdefault(k, v)
    for key, value in flat.items():
        sk = str(key)
        canon = _FIELD_ALIASES.get(sk, sk if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sk) else "")
        if not canon:
            continue
        if canon == "log_preview":
            text = _coerce_str(value)
            if len(text) > LOG_PREVIEW_MAX_CHARS:
                text = text[: LOG_PREVIEW_MAX_CHARS - 3] + "..."
            ctx[canon] = text
        elif canon == "threshold":
            ctx[canon] = _coerce_str(value)
        else:
            if canon not in ctx or not ctx[canon]:
                ctx[canon] = _coerce_str(value)
    # 常用直取
    for src, dst in (
        ("user", "user"),
        ("IP", "ip"),
        ("via", "via"),
        ("from", "from"),
        ("container_name", "container_name"),
        ("container_id", "container_id"),
        ("image", "image"),
        ("docker_action", "docker_action"),
        ("exit_code", "exit_code"),
        ("task_name", "task_name"),
        ("status", "status"),
        ("error_message", "error_message"),
        ("storage_name", "storage_name"),
        ("target_path", "target_path"),
        ("files_count", "files_count"),
        ("actual_size", "actual_size"),
        ("start_time", "start_time"),
        ("finished_time", "finished_time"),
        ("title", "title"),
        ("artist", "artist"),
        ("album", "album"),
        ("playlist_name", "playlist_name"),
        ("user_label", "user_label"),
        ("share_name", "share_name"),
        ("share_id", "share_id"),
        ("device_display", "device_display"),
        ("person_names_text", "person_names_text"),
        ("photos_with_face", "photos_with_face"),
        ("faces_detected", "faces_detected"),
        ("record_count", "record_count"),
        ("expired_at_str", "expired_at_str"),
        ("path", "path"),
        ("item_type", "item_type"),
        ("app_name", "app_name"),
        ("email", "email"),
        ("role", "role"),
        ("account", "account"),
        ("trigger_reason_label", "trigger_reason_label"),
        ("started_at", "started_at"),
        ("finished_at", "finished_at"),
        ("duration", "duration"),
        ("log_preview", "log_preview"),
        ("name", "name"),
        ("model", "model"),
        ("serial", "serial"),
        ("disk", "disk"),
        ("port", "port"),
        ("reason", "reason"),
        ("elapsed_minutes", "elapsed_minutes"),
        ("opened_at", "opened_at"),
        ("remind_minutes", "remind_minutes"),
        ("uid", "uid"),
        ("target_user", "target_user"),
        ("target_uid", "target_uid"),
        ("old_wan_ip", "old_wan_ip"),
        ("new_wan_ip", "new_wan_ip"),
        ("status_text", "status_text"),
        ("display_name", "display_name"),
        ("app_id", "app_id"),
        ("vm_title", "vm_title"),
        ("err_cnt", "err_cnt"),
        ("volume", "volume"),
        ("top_processes", "top_processes"),
    ):
        if dst not in ctx or not ctx[dst]:
            if src in flat:
                val = _coerce_str(flat.get(src))
                if dst == "log_preview" and len(val) > LOG_PREVIEW_MAX_CHARS:
                    val = val[: LOG_PREVIEW_MAX_CHARS - 3] + "..."
                ctx[dst] = val
    # UPS 说明
    if not ctx.get("status_text"):
        ups = flat.get("ups")
        if isinstance(ups, dict):
            ctx["status_text"] = _coerce_str(ups.get("status") or ups.get("message") or "")
    return ctx


def render_template(text: str, ctx: Dict[str, str]) -> str:
    raw = str(text or "")
    if not raw:
        return ""

    def repl(match: re.Match) -> str:
        key = match.group(1)
        return str(ctx.get(key, "") or "")

    lines_out: List[str] = []
    for line in raw.splitlines():
        rendered = _VAR_RE.sub(repl, line)
        # 整行在替换后仅空白 → 删除（含「标签: 」后变量为空的行）
        if not rendered.strip():
            continue
        # 「标签: {{x}}」替换后变成「标签: 」或「标签:」→ 删除
        if re.fullmatch(r"[^:\n]{1,40}:\s*", rendered):
            continue
        lines_out.append(rendered.rstrip())
    # 压缩连续空行（上面已跳过空行，这里再保险）
    compact: List[str] = []
    prev_blank = False
    for line in lines_out:
        blank = not line.strip()
        if blank and prev_blank:
            continue
        compact.append(line)
        prev_blank = blank
    return "\n".join(compact).strip()


def render_message(
    event_type: str,
    event_data: Optional[Dict[str, Any]],
    *,
    timestamp: str = "",
    prefix: str = "",
    templates: Optional[Dict[str, Any]] = None,
    official_title: str = "",
    official_note: str = "",
    title_override: Optional[str] = None,
    body_override: Optional[str] = None,
) -> Optional[Tuple[str, str, str]]:
    """渲染自定义消息。

    返回 (scope, title, body)；若不可用/双空则 None（调用方走官方）。
    传入 title_override/body_override 时按草稿渲染（预览/试发）。
    """
    et = str(event_type or "")
    if not is_template_allowed(et):
        return None
    if title_override is not None or body_override is not None:
        scope = "draft"
        title_tpl = str(title_override or "")
        body_tpl = str(body_override or "")
        if not title_tpl.strip() and not body_tpl.strip():
            return None
    else:
        scope, title_tpl, body_tpl = resolve_template(et, templates)
        # 无用户自定义时回退官方 _build_message；builtin 仅作抽屉预填
        if scope == "builtin":
            return None
        if not title_tpl.strip() and not body_tpl.strip():
            return None
    ctx = build_context(
        et,
        event_data,
        prefix=prefix,
        timestamp=timestamp,
        official_title=official_title,
        official_note=official_note,
    )
    title = render_template(title_tpl, ctx)
    # 压缩 icon 与前缀间多余空格
    title = re.sub(r"\s{2,}", " ", title).strip()
    body = render_template(body_tpl, ctx)
    if not title and not body:
        return None
    # 标题渲染为空时回退官方标题文案（仍保留自定义正文），避免空 title 推送
    if not title and official_title:
        icon, short = parse_official_title(official_title)
        pfx = (prefix or "").strip() or "飞牛NAS"
        if icon:
            title = f"{icon} {pfx}-{short}".strip() if short else icon
        elif short:
            title = f"{pfx}-{short}"
        else:
            title = str(official_title).strip()
        title = re.sub(r"\s{2,}", " ", title).strip()
    if not title and not body:
        return None
    return scope, title, body


def sample_event_data(event_type: str) -> Dict[str, Any]:
    """抽屉预览/试发用的样例数据。"""
    fid = family_id_for_event(event_type) or ""
    base: Dict[str, Any] = {
        "user": "demo",
        "IP": "192.168.1.10",
        "via": "password",
        "from": "web",
        "uid": "1000",
        "target_user": "alice",
        "target_uid": "1001",
        "port": "22",
        "reason": "invalid credentials",
        "elapsed_minutes": "45",
        "opened_at": "2026-11-23 12:00:00",
        "remind_minutes": "30",
        "old_wan_ip": "1.2.3.4",
        "new_wan_ip": "5.6.7.8",
        "THRESHOLD": "90",
        "top_cpu_processes": "python(12%), dockerd(8%)",
        "name": "sdb",
        "model": "DemoDisk",
        "serial": "SN123",
        "volume": "vol1",
        "disk": "sdb",
        "err_cnt": "3",
        "status_text": "UPS 切换到电池供电",
        "APP_NAME": "DemoApp",
        "APP_ID": "demo.app",
        "DISPLAY_NAME": "演示应用",
        "VM_TITLE": "demo-vm",
        "task_name": "每日备份",
        "status": "failed",
        "error_message": "连接超时",
        "storage_name": "远程盘",
        "target_path": "/backup",
        "files_count": "12",
        "actual_size": "1.2 GB",
        "start_time": "1700000000",
        "finished_time": "1700003600",
        "title": "示例影片",
        "path": "/media/demo.mkv",
        "item_type": "movie",
        "app_name": "影视",
        "email": "a@example.com",
        "role": "user",
        "share_name": "家庭相册",
        "share_id": "abc",
        "device_display": "iPhone",
        "person_names_text": "小明, 小红",
        "photos_with_face": "8",
        "faces_detected": "12",
        "record_count": "5",
        "expired_at_str": "2026-12-01",
        "artist": "Demo Artist",
        "album": "Demo Album",
        "playlist_name": "收藏",
        "user_label": "demo",
        "container_name": "nginx",
        "container_id": "abcd1234",
        "image": "nginx:latest",
        "docker_action": "start",
        "exit_code": "0",
        "account": "admin",
        "trigger_reason_label": "定时触发",
        "started_at": "2026-11-23 12:00:00",
        "finished_at": "2026-11-23 12:01:00",
        "duration": "60 秒",
        "log_preview": "task finished with warnings",
    }
    _ = fid
    return base


def ui_catalog_meta() -> Dict[str, Any]:
    """供 GET /api/config：各族变量与官方默认。"""
    families = []
    for fid, fname, _ in _FAMILY_DEFS:
        title, body = builtin_template(fid)
        families.append({
            "id": fid,
            "name": fname,
            "vars_global": list(GLOBAL_VARS),
            "vars_family": list(FAMILY_VARS.get(fid, ())),
            "default_title": title,
            "default_body": body,
        })
    return {
        "version": TEMPLATE_VERSION,
        "blacklist": sorted(TEMPLATE_BLACKLIST),
        "families": families,
        "event_family": dict(_EVENT_TO_FAMILY),
        "title_soft_warn_chars": TITLE_SOFT_WARN_CHARS,
    }


def template_status_for_event(event_type: str, templates: Optional[Dict[str, Any]]) -> str:
    """default | family | event"""
    if not is_template_allowed(event_type):
        return "default"
    store = normalize_message_templates(templates or {})
    if event_type in store["events"]:
        return "event"
    fid = family_id_for_event(event_type)
    if fid and fid in store["families"]:
        return "family"
    return "default"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
