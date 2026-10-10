"""配置保存/加载时的只读诊断：区别权限、缺少数据源与不兼容结构。"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, TypedDict

from monitor.docker_events_poller import DOCKER_POLL_EVENTS
from monitor.docker_socket_access import check_docker_socket_access
from monitor.ssh_journal_poller import SSH_JOURNAL_EVENTS
from monitor.sqlite_uri import connect_readonly_with_fallback
from utils.access_guidance import db_acl_commands, docker_permission_commands, in_container, journal_permission_action, service_user
from utils.value_parser import as_bool


class AccessIssue(TypedDict):
    title: str
    message: str
    code: str
    action: str
    commands: str
    faq: str
    risk: str


def issue(title: str, message: str, *, code: str = "unavailable", action: str = "",
          commands: str = "", faq: str = "faq-external-db", risk: str = "") -> AccessIssue:
    return {"title": title, "message": message, "code": code, "action": action,
            "commands": commands, "faq": faq, "risk": risk}


def warning_messages(details: list[AccessIssue]) -> list[str]:
    return [f'{item["title"]}: {item["message"]} {item["action"]}'.strip() for item in details]


def _db_permission_issue(label: str, path: str, message: str) -> AccessIssue:
    if in_container():
        return issue(label, message, code="permission", action=(
            "在 NAS 核对挂载源和容器进程的 UID/GID，给该账号增加目录进入及数据库读取 ACL；"
            "不要给宿主数据库递归 chown 或全局 chmod。具体操作见 FAQ。"))
    return issue(label, message, code="permission", commands=db_acl_commands(path), action=(
        "在 NAS SSH 用管理员账号执行下面的只读 ACL 授权，再保存配置。系统需有 setfacl；"
        "库文件或 WAL/SHM 被重建后可能需要重新授权。"))


def database_access_issue(label: str, db_path: str, probe_sql: str = "SELECT 1",
                          sqlite_error: Optional[Exception] = None) -> Optional[AccessIssue]:
    path = (db_path or "").strip()
    if not path:
        action = "确认对应应用已安装，填写实际数据库路径。"
        if in_container():
            action += "Docker 还需只读挂载其数据目录。"
        return issue(label, "未找到数据库路径。", code="missing", action=action)
    p = Path(os.path.abspath(path))
    # 打开已知文件只需父目录 x，不要求每层目录都能列出内容。
    for directory in reversed(p.parents):
        try:
            st = directory.stat()
            if not stat.S_ISDIR(st.st_mode):
                return issue(label, f"{directory} 不是目录。", code="missing", action="核对数据库路径。")
            if not os.access(directory, os.X_OK):
                return _db_permission_issue(label, str(p), f"无法进入目录 {directory}。")
        except PermissionError:
            return _db_permission_issue(label, str(p), f"无法访问目录 {directory}。")
        except FileNotFoundError:
            return issue(label, f"目录不存在：{directory}。", code="missing", action=(
                "核对实际路径和对应应用是否安装；缺失文件不能通过提权补出。" +
                ("Docker 请核对宿主目录挂载。" if in_container() else "")))
        except OSError as exc:
            return issue(label, f"目录检查失败：{exc}。", action="检查存储是否已挂载或发生 I/O 错误。")
    try:
        st = p.stat()
        if not stat.S_ISREG(st.st_mode):
            return issue(label, f"{p} 不是数据库文件。", code="missing", action="填写数据库文件路径，不能填写目录。")
        if not os.access(p, os.R_OK):
            return _db_permission_issue(label, str(p), f"数据库不可读：{p}。")
    except PermissionError:
        return _db_permission_issue(label, str(p), f"无法访问数据库：{p}。")
    except FileNotFoundError:
        return issue(label, f"数据库不存在：{p}。", code="missing", action=(
            "核对路径和应用是否安装。" + ("Docker 请挂载对应宿主数据目录。" if in_container() else "")))
    except OSError as exc:
        return issue(label, f"数据库检查失败：{exc}。", action="检查存储状态。")
    # immutable 回退可能只读到主库快照，不能因探测成功就忽略不可读的 WAL/SHM。
    for suffix in ("-wal", "-shm"):
        side = Path(str(p) + suffix)
        try:
            if side.exists() and not os.access(side, os.R_OK):
                return _db_permission_issue(label, str(p), f"SQLite 附属文件不可读：{side}。")
        except OSError:
            pass
    error = sqlite_error
    if error is None:
        try:
            conn = connect_readonly_with_fallback(str(p), timeout=1.0, table_probe_sql=probe_sql)
            conn.close()
            return None
        except Exception as exc:
            error = exc
    text = str(error)
    if "no such table" in text.lower() or "no such column" in text.lower():
        return issue(label, f"数据库可访问，但表结构不兼容：{text}。", code="schema", action=(
            "核对是否选择了正确的库；litevideo.sqlite / litegallery.sqlite 与当前适配的 "
            "trimmedia.db / photo.db 不能直接互换，需要核对表结构并适配程序。增加权限不能解决此错误。"))
    if "malformed" in text.lower() or "not a database" in text.lower():
        return issue(label, f"数据库格式异常：{text}。", code="database", action=(
            "请通过对应应用检查、备份及恢复数据库；不要直接删除系统数据库或卸载本应用。"))
    if "locked" in text.lower() or "busy" in text.lower():
        return issue(label, f"数据库正在被占用：{text}。", code="busy", action="稍后再保存检查，无需提升权限。")
    return issue(label, f"路径可访问，但 SQLite 读取失败：{text}。", action=(
        "检查 WAL/SHM 文件权限、数据库格式和存储状态；不要直接放宽整个目录权限。"))


def _run_readonly(args: list[str], timeout: float = 2.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _permission_error(text: str) -> bool:
    return any(marker in text.lower() for marker in (
        "permission denied", "operation not permitted", "access denied", "insufficient permissions",
        "must be root", "root privileges", "not seeing messages from other users",
    ))


def ssh_access_issues() -> list[AccessIssue]:
    from monitor.nas_patrol import _resolve_cmd
    journal = _resolve_cmd("journalctl")
    try:
        result = _run_readonly([journal, "--system", "-n", "0", "--no-pager"])
    except FileNotFoundError:
        return [issue("SSH 日志", "未找到 journalctl。", code="missing", faq="faq-ssh-journal", action=(
            "使用包含 journalctl 的 Docker 镜像；原生 FPK 请核对系统组件。加组不能补出该命令。"))]
    except (OSError, subprocess.SubprocessError) as exc:
        return [issue("SSH 日志", f"journal 检查未完成：{exc}。", faq="faq-ssh-journal", action="稍后重新保存检查。")]
    text = result.stdout + result.stderr
    # journalctl 可能以 0 退出，同时在 stderr 提示仅能看到自己的日志。
    if _permission_error(text):
        command = "" if in_container() else f"sudo usermod -aG systemd-journal {shlex.quote(service_user())}"
        return [issue("SSH 日志", "当前应用进程无权读取系统 journal。", code="permission",
                      faq="faq-ssh-journal", action=journal_permission_action(), commands=command)]
    if result.returncode != 0:
        return [issue("SSH 日志", f"系统 journal 不可读：{text.strip()[:240]}。", faq="faq-ssh-journal",
                      action="核对 journal 数据源；Docker 检查日志目录及 machine-id 挂载。")]
    if "no journal files" in text.lower():
        return [issue("SSH 日志", "没有可读取的系统 journal 文件。", code="source", faq="faq-ssh-journal",
                      action="检查系统日志服务及日志目录；Docker 检查宿主 journal 挂载。加组不会补出不存在的日志。")]
    # SSH 未启动或暂时没有登录事件均是正常空闲状态，只检查 journal 是否可读。
    return []


def docker_access_issue(sock_path: str) -> Optional[AccessIssue]:
    message = check_docker_socket_access(sock_path)
    if not message:
        return None
    commands = ""
    risk = ""
    code = "unavailable"
    if "无法读写" in message or "stat 被拒绝" in message:
        code = "permission"
        risk = "Docker socket 访问等同于宿主 root 级控制能力。"
        if in_container():
            action = "按 socket 的宿主 GID 设置 Compose group_add，并重建容器。"
        else:
            action = "执行对应授权命令后，在应用中心停止并重新启动应用，再保存检查。socket 重建后 ACL 可能需重新设置。"
            commands = docker_permission_commands(sock_path)
    else:
        action = "核对 Docker 是否已安装、服务是否运行和 socket 路径。"
        if in_container():
            action += "容器还需挂载宿主 socket。"
    return issue("Docker 事件", message, code=code, faq="faq-docker-sock", action=action,
                 commands=commands, risk=risk)


def _smart_permission_issue(device: str, smartctl: str, needs_nvme: bool = False,
                            devices: Optional[list[str]] = None,
                            temperature_devices: Optional[list[str]] = None) -> AccessIssue:
    denied_devices = devices or [device]
    device_text = "、".join(denied_devices)
    quoted_devices = " ".join(shlex.quote(d) for d in denied_devices)
    message = f"当前应用账号无权读取 {device_text} 的 SMART 数据，巡检健康状态将显示 --。"
    if temperature_devices:
        message += (f"其中 {'、'.join(temperature_devices)} 没有可读的硬盘 hwmon 温度，"
                    "需通过 SMART 读取温度；授权前温度也会显示 --。")
    # 同一工具采集多个盘时，不让后续 SATA 授权覆盖掉 NVMe 所需的 capability。
    nvme = needs_nvme or any(Path(d).name.startswith("nvme") for d in denied_devices)
    if in_container():
        caps = "SYS_RAWIO, SYS_ADMIN" if nvme else "SYS_RAWIO"
        return issue("硬盘 SMART", message, code="permission", faq="faq-patrol",
                     action=f"在 Compose 配置上述设备的 devices 映射及 cap_add: [{caps}]，再重建容器。",
                     risk="SYS_ADMIN 是强权限，仅在需要 NVMe SMART 且确认接受权限范围时添加。" if nvme else "")
    caps = "cap_sys_rawio,cap_sys_admin+ep" if nvme else "cap_sys_rawio+ep"
    user = shlex.quote(service_user())
    from monitor.nas_patrol import _resolve_cmd
    # NAS 普通 SSH 账号的 PATH 可能不包含 sbin，使用完整路径避免授权命令未执行。
    setcap = _resolve_cmd("setcap")
    setfacl = _resolve_cmd("setfacl")
    setcap = shlex.quote("/usr/sbin/setcap" if setcap == "setcap" else setcap)
    setfacl = shlex.quote("/usr/bin/setfacl" if setfacl == "setfacl" else setfacl)
    return issue("硬盘 SMART", message, code="permission", faq="faq-patrol",
                 action=("在 NAS SSH 用管理员账号执行下方命令，给 smartctl 增加 capability，并开放上述设备的读取权限。"
                         "两步均需成功；仅设置 capability 不会开放设备读取权限。粘贴整段后按回车执行。"
                         "通常无需重启应用；授权后重新保存检查，并点击「立即巡检一次」验证。"
                         "只添加 disk 组或配置免密 sudo 无法保证当前程序取得 SMART。"),
                 commands=(f"sudo {setcap} {shlex.quote(caps)} {shlex.quote(smartctl)} &&\n"
                           f"sudo {setfacl} -m u:{user}:r -- {quoted_devices} &&\n"
                           f"for disk in {quoted_devices}; do\n"
                           '  echo "检查 $disk";\n'
                           f'  sudo -u {user} {shlex.quote(smartctl)} -H -A "$disk";\n'
                           '  echo "退出码: $?";\n'
                           "done\n"),
                 risk=("capability 作用于所有能执行该 smartctl 的用户，并非只允许读取健康数据。"
                       + ("CAP_SYS_ADMIN 权限范围较大。" if nvme else "")
                       + "工具升级、设备重建后可能需重新授权；系统禁止 file capability 时该方式不会生效。"))


def patrol_access_issues() -> list[AccessIssue]:
    from monitor import nas_patrol as patrol
    warnings: list[AccessIssue] = []
    if patrol._read_system_version() == "--":
        warnings.append(issue("系统版本", "无法读取宿主操作系统版本。", faq="faq-patrol", action=(
            "在 Compose 只读挂载 /etc/os-release:/host/etc/os-release:ro 并重建容器。" if in_container()
            else "检查 /etc/os-release 是否存在且应用账号可读；无需将整个应用改成 root。")))
    if patrol._read_fnos_version() == "--":
        warnings.append(issue("飞牛版本", "开放 API、版本文件和包信息均未取得当前版本。", faq="faq-patrol", action=(
            "在 Compose 只读挂载 /var/lib/dpkg/status:/host/dpkg/status:ro 并重建容器，或配置开放 API。" if in_container()
            else "检查开放 API 配置或 /var/lib/dpkg/status 的可读性；增加权限不能替代有效的版本来源。")))
    disks = patrol._patrol_list_visible_whole_disks()
    if not disks:
        warnings.append(issue("硬盘巡检", "未发现可采集的物理硬盘。", code="source", faq="faq-patrol",
                              action="检查系统设备可见性；Docker 需要访问宿主 sysfs，不能用提权补出不存在的设备。"))
        return warnings
    smartctl = patrol._resolve_cmd("smartctl")
    denied_devices: list[str] = []
    denied_temperature_devices: list[str] = []
    unchecked_devices: list[str] = []
    deadline = time.monotonic() + 4.0
    for disk in disks:
        device = f"/dev/{disk}"
        temp = patrol._hwmon_temp1_for_block(disk)
        # 先识别所有打不开的设备，避免某块盘超时让后续盘的权限提示被漏掉。
        if Path(smartctl).exists() and Path(device).exists() and not os.access(device, os.R_OK):
            denied_devices.append(device)
            if not temp:
                denied_temperature_devices.append(device)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            unchecked_devices.append(device)
            continue
        try:
            result = _run_readonly([smartctl, "-H", "-A", "-j", device], timeout=min(1.5, remaining))
            if patrol._smartctl_sat_retry_needed(device, result.stdout + result.stderr):
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    result = _run_readonly([smartctl, "-H", "-A", "-j", "-d", "sat", device],
                                           timeout=min(1.5, remaining))
        except FileNotFoundError:
            warnings.append(issue("硬盘 SMART", "未安装 smartctl，无法通过该工具读取健康状态。", code="missing",
                                  faq="faq-patrol", action="安装 smartmontools 后重新保存检查；hwmon 温度读取不依赖它。",
                                  commands="" if in_container() else "sudo apt-get install smartmontools"))
            break
        except subprocess.TimeoutExpired:
            warnings.append(issue("硬盘 SMART", f"{device} 查询超时，未判定为权限问题。", code="timeout",
                                  faq="faq-patrol", action="用「立即巡检」或 FAQ 的手动命令进一步检查设备响应。"))
            continue
        except PermissionError:
            denied_devices.append(device)
            if not temp:
                denied_temperature_devices.append(device)
            continue
        except OSError as exc:
            warnings.append(issue("硬盘 SMART", f"{device} 查询失败：{exc}。", faq="faq-patrol", action="核对设备与工具路径。"))
            continue
        text = result.stdout + result.stderr
        if _permission_error(text) or (not os.access(device, os.R_OK) and Path(device).exists()):
            denied_devices.append(device)
            if not temp:
                denied_temperature_devices.append(device)
            continue
        elif not Path(device).exists():
            warnings.append(issue("硬盘 SMART", f"设备节点不存在：{device}。", code="missing", faq="faq-patrol", action=(
                f"在 Compose 用 devices 映射 {device}，再重建容器。" if in_container() else "核对系统设备节点和硬盘是否连接。")))
        else:
            if patrol._parse_smart_health_output(result.stdout) == "--":
                warnings.append(issue("硬盘 SMART", f"{device} 未返回健康数据，不能据此显示正常。", faq="faq-patrol", action=(
                    "按 FAQ 对比应用账号与管理员的查询结果，核对权限及 USB/RAID 控制器支持；即使 root 也不能保证设备提供此数据。")))
        if not temp and patrol._parse_smart_temperature_output(result.stdout) in {"--", ""}:
            warnings.append(issue("硬盘温度", f"{device} 未读到可归属于此盘的温度。", faq="faq-patrol", action=(
                "此盘没有可读的硬盘 hwmon 温度，SMART 也未返回温度。对比应用账号与管理员查询结果，"
                "检查 USB/RAID 桥接支持；不要把 CPU/GPU 温度当作硬盘温度。"),
                commands=(f"sudo -u {shlex.quote(service_user())} {shlex.quote(smartctl)} -H -A {shlex.quote(device)};\n"
                          'echo "应用账号退出码: $?";\n'
                          f"sudo {shlex.quote(smartctl)} -H -A {shlex.quote(device)};\n"
                          'echo "管理员退出码: $?"\n')))
    if denied_devices:
        warnings.append(_smart_permission_issue(denied_devices[0], smartctl,
                        needs_nvme=any(d.startswith("nvme") for d in disks), devices=denied_devices,
                        temperature_devices=denied_temperature_devices))
    if unchecked_devices:
        warnings.append(issue("硬盘巡检", f"{'、'.join(unchecked_devices)} 的 SMART 检查未完成。", code="timeout",
                              faq="faq-patrol", action="保存检查有时间上限，可用「立即巡检」查看各盘实际采集结果。"))
    return warnings


def collect_access_warnings(raw_cfg: dict[str, Any], events: list[str], *,
                            include_patrol: bool = False) -> list[AccessIssue]:
    selected = set(events or [])
    warnings: list[AccessIssue] = []
    sources = (
        ({"BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS"}, "备份库", "backup_db_path", "SELECT id FROM operations LIMIT 1"),
        ({"TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS"}, "影视库", "trim_media_db_path", "SELECT guid FROM item LIMIT 1"),
        ({"MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT"}, "影视登录库", "trim_activity_db_path", "SELECT token FROM user_token LIMIT 1"),
        ({"PHOTO_SHARE_CREATED", "PHOTO_SHARE_EXPIRED", "PHOTO_DEVICE_REGISTERED", "FACE_RECOGNITION_UPDATED"}, "相册库", "photo_db_path", "SELECT id FROM share_link LIMIT 1"),
        ({"SCHEDULER_TASK_SUCCESS", "SCHEDULER_TASK_FAILED", "SCHEDULER_TASK_CONDITION_FAILED"}, "任务计划库", "scheduler_db_path", "SELECT id FROM task_results LIMIT 1"),
    )
    # 主日志是一般系统事件的数据源；单独勾选外部源/巡检时不要求它存在。
    external = set().union(*(source[0] for source in sources), DOCKER_POLL_EVENTS, SSH_JOURNAL_EVENTS,
                           {"NAS_PATROL_REPORT", "WAN_IP_CHANGED", "APP_START", "APP_STOP"})
    if selected - external or as_bool(raw_cfg.get("media_lib_logger_enabled", False), False):
        result = database_access_issue("主日志库", raw_cfg.get("logger_db_path", ""), "SELECT id FROM log LIMIT 1")
        if result:
            warnings.append(result)
    for event_ids, label, key, sql in sources:
        if selected & event_ids:
            result = database_access_issue(label, raw_cfg.get(key, ""), sql)
            if result:
                warnings.append(result)
    if selected & DOCKER_POLL_EVENTS:
        result = docker_access_issue((raw_cfg.get("docker_socket_path") or "").strip() or "/var/run/docker.sock")
        if result:
            warnings.append(result)
    if selected & SSH_JOURNAL_EVENTS:
        warnings.extend(ssh_access_issues())
    # 巡检是独立开关，不在页面可勾选事件目录中；手动触发也需检查。
    if include_patrol or as_bool(raw_cfg.get("nas_patrol_enabled", False), False) or "NAS_PATROL_REPORT" in selected:
        warnings.extend(patrol_access_issues())
    return warnings
