"""
NAS 定时巡检：按配置间隔（分钟）采集本机 CPU/内存/磁盘状态，经已配置的推送渠道发送。
首次启用仅锚定周期起点不立即推送；推送失败时按指数退避重试。
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat as stat_mod
import socket
import subprocess
import threading
import time
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import ipaddress
import urllib.request

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

from .sqlite_uri import connect_readonly_with_fallback

_STATE_FILENAME = "nas_patrol_state.json"

RETRY_BACKOFF_BASE_SEC = 30
RETRY_BACKOFF_MAX_SEC = 3600

# NUT upsd 默认端口（仅从系统自动探测连接，不提供应用层配置项）
_NUT_UPSD_DEFAULT_PORT = 3493
# 巡检 UPS：compose 挂载 /etc/nut 目录；仅当其中存在可读 ups.conf 时才采集
_NUT_UPS_CONF_MOUNT_PATH = "/etc/nut/ups.conf"

# psutil disk_partitions(all=True) 时按 fstype 排除的伪/容器文件系统（路径上仍会再挡 Docker）
_PATROL_PSUTIL_NOISE_FSTYPES: frozenset = frozenset(
    {
        "tmpfs",
        "devtmpfs",
        "proc",
        "sysfs",
        "cgroup2",
        "overlay",
        "squashfs",
        "fuse.portal",
        "rpc_pipefs",
        "securityfs",
        "debugfs",
        "pstore",
        "bpf",
        "tracefs",
        "autofs",
        "binfmt_misc",
        "nsfs",
    }
)

_PATROL_CONTAINER_FILE_BIND_MOUNTS: frozenset = frozenset(
    {
        "/etc/hostname",
        "/etc/hosts",
        "/etc/localtime",
        "/etc/resolv.conf",
        "/etc/timezone",
    }
)

_PATROL_CONTAINER_APP_MOUNT_PREFIXES: Tuple[str, ...] = (
    "/app",
    "/workspace",
    "/config",
)

_PATROL_HOST_STORAGE_PATH_CANDIDATES: Tuple[str, ...] = (
    "/vol1",
    "/vol2",
    "/vol3",
    "/vol4",
    "/volume1",
    "/volume2",
    "/volume3",
    "/mnt/vol1",
    "/mnt/volume1",
    "/usr/trim/var/eventlogger_service",
    "/usr/trim/var/backup_service",
    "/usr/local/apps/@appdata/trim.media/database",
    "/usr/local/apps/@appdata/trim.photos/db",
    "/usr/local/apps/@appdata/fn-scheduler",
)


class _PatrolCfgEmpty:
    """探测脚本无 Config 时仅提供 logger_db_path（可由 LOGGER_DB_PATH 推断）。"""

    logger_db_path: str = ""


def _state_path(cursor_dir: str) -> Path:
    p = Path(cursor_dir or "./data/cursor")
    p.mkdir(parents=True, exist_ok=True)
    return p / _STATE_FILENAME


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _save_state(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.getLogger(__name__).warning("写入巡检状态失败: %s", e)


def _normalize_patrol_state(state: Dict[str, Any]) -> None:
    if "last_success_ts" not in state and "last_report_ts" in state:
        try:
            state["last_success_ts"] = float(state["last_report_ts"])
        except (TypeError, ValueError):
            state["last_success_ts"] = 0.0
    if "last_success_ts" not in state:
        state["last_success_ts"] = 0.0
    state.pop("last_report_ts", None)


def _cpu_mem_disk() -> Tuple[str, str, str]:
    """返回 (cpu%, mem%, 根分区剩余 GB) 的字符串，不可用时为 —"""
    if not psutil:
        return "—", "—", "—"
    try:
        cpu = f"{psutil.cpu_percent(interval=1.0):.1f}"
    except Exception:
        cpu = "—"
    try:
        mem = f"{psutil.virtual_memory().percent:.1f}"
    except Exception:
        mem = "—"
    try:
        free_gb = psutil.disk_usage("/").free / (1024**3)
        disk = f"{free_gb:.1f}"
    except Exception:
        disk = "—"
    return cpu, mem, disk


def _cpu_disk_temp_c() -> Tuple[str, str]:
    """返回 CPU 温度、磁盘/存储相关温度（摄氏度数值字符串，无时为 —）。"""
    cpu_t, disk_t = "—", "—"
    if not psutil:
        return cpu_t, disk_t
    try:
        t = psutil.sensors_temperatures()
        if isinstance(t, dict):
            if "coretemp" in t and t["coretemp"]:
                cpu_t = f"{t['coretemp'][0].current:.1f}"
            elif "cpu_thermal" in t and t["cpu_thermal"]:
                cpu_t = f"{t['cpu_thermal'][0].current:.1f}"
            elif "cpu-thermal" in t and t["cpu-thermal"]:
                cpu_t = f"{t['cpu-thermal'][0].current:.1f}"
            if "nvme" in t and t["nvme"]:
                disk_t = f"{t['nvme'][0].current:.1f}"
            elif "sata" in t and t["sata"]:
                disk_t = f"{t['sata'][0].current:.1f}"
    except Exception:
        pass
    if disk_t == "—":
        for hw in ("/sys/class/hwmon/hwmon1/temp1_input", "/sys/class/hwmon/hwmon0/temp1_input"):
            try:
                v = int(open(hw, "r", encoding="utf-8").read().strip())
                disk_t = f"{v / 1000.0:.1f}"
                break
            except Exception:
                continue
    return cpu_t, disk_t


def _resolve_cmd(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    for prefix in ("/usr/sbin", "/usr/bin", "/sbin", "/bin", "/usr/local/bin", "/usr/local/sbin"):
        c = str(Path(prefix) / name)
        if Path(c).exists():
            return c
    return name


def _run_cmd(args: List[str], timeout: float = 2.0) -> str:
    try:
        cmd = list(args)
        if cmd:
            cmd[0] = _resolve_cmd(cmd[0])
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    out = (p.stdout or "").strip()
    if out:
        return out
    return (p.stderr or "").strip()


def _run_cmd_any(candidates: List[List[str]], timeout: float = 2.0) -> str:
    for c in candidates:
        out = _run_cmd(c, timeout=timeout)
        if out:
            return out
    return ""


def _run_upsc_cmd(args: List[str], timeout: float = 2.0) -> str:
    """仅采纳 upsc 成功时的 stdout，避免把帮助/版本 stderr 当成 UPS 数据。"""
    try:
        cmd = list(args)
        if cmd:
            cmd[0] = _resolve_cmd(cmd[0])
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


_UPSC_KNOWN_VAR_PREFIXES: Tuple[str, ...] = (
    "ups.",
    "battery.",
    "input.",
    "output.",
    "device.",
    "driver.",
    "upsmon.",
)


def _upsc_text_is_error_or_help(text: str) -> bool:
    if not (text or "").strip():
        return True
    low = text.lower()
    if any(
        m in low
        for m in (
            "network ups tools",
            "display this help",
            "usage:",
            "error:",
            "connection refused",
            "driver not connected",
            "unknown argument",
            "can't connect",
            "cannot connect",
            "no such host",
            "timed out",
        )
    ):
        return True
    first = text.splitlines()[0].strip()
    return bool(first.startswith("-") and " " in first)


def _upsc_is_plausible_device_name(name: str) -> bool:
    s = (name or "").strip()
    if not s or len(s) > 128 or _upsc_text_is_error_or_help(s):
        return False
    if " " in s or s.startswith("-"):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", s))


def _upsc_kv_has_ups_vars(kv: Dict[str, str]) -> bool:
    for k in kv:
        kl = k.lower()
        if any(kl.startswith(p) for p in _UPSC_KNOWN_VAR_PREFIXES):
            return True
    return False


def _safe_path_exists(p: Path) -> bool:
    try:
        return p.exists()
    except Exception:
        return False


def _safe_glob(base: Path, pattern: str) -> List[Path]:
    try:
        return list(base.glob(pattern))
    except Exception:
        return []


def _valid_sysfs_block_token(s: str) -> bool:
    """内核块设备名（PKNAME/slave 名等），排除 lsblk 报错行被误解析。"""
    t = (s or "").strip()
    if not t or len(t) > 80:
        return False
    low = t.lower()
    if low.startswith("lsblk"):
        return False
    if any(ch in t for ch in " \t:/\\"):
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9_+-]+", t))


def _fmt_uptime(sec: float) -> str:
    total = max(0, int(sec))
    days = total // 86400
    remain = total % 86400
    hours = remain // 3600
    remain %= 3600
    mins = remain // 60
    secs = remain % 60
    return f"{days}天{hours}时{mins}分{secs}秒"


def _fmt_boot_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "--"


def _pick_lan_ip() -> str:
    if not psutil:
        return "--"
    try:
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if getattr(a, "family", None) != socket.AF_INET:
                    continue
                ip = str(getattr(a, "address", "") or "").strip()
                if not ip or ip.startswith("127."):
                    continue
                try:
                    obj = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if obj.is_private:
                    return ip
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if getattr(a, "family", None) == socket.AF_INET:
                    ip = str(getattr(a, "address", "") or "").strip()
                    if ip and not ip.startswith("127."):
                        return ip
    except Exception:
        return "--"
    return "--"


def _pick_wan_ip() -> str:
    urls = (
        "https://api4.ipify.org",  # 优先 IPv4
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    )
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=2.0) as r:  # nosec B310
                text = (r.read().decode("utf-8", errors="ignore") or "").strip()
            ip_obj = ipaddress.ip_address(text)
            if ip_obj.version == 4:
                return text
        except Exception:
            continue
    # 回退允许 IPv6
    for u in ("https://api64.ipify.org", "https://api.ipify.org"):
        try:
            with urllib.request.urlopen(u, timeout=2.0) as r:  # nosec B310
                text = (r.read().decode("utf-8", errors="ignore") or "").strip()
            ipaddress.ip_address(text)
            return text
        except Exception:
            continue
    return "--"


def _read_system_version() -> str:
    # 系统版本（操作系统）
    out = _run_cmd(["sh", "-lc", "grep PRETTY_NAME /etc/os-release | cut -d '\"' -f2"], timeout=3.0)
    ver = str(out or "").strip().splitlines()
    if ver and ver[0].strip():
        return ver[0].strip()
    return "--"


def _looks_like_container_hostname(s: str) -> bool:
    """Docker 短 ID 等：12/64 位十六进制，容器内常被当作 hostname。"""
    t = (s or "").strip().lower()
    if not t:
        return True
    if re.fullmatch(r"[0-9a-f]{12}", t):
        return True
    if re.fullmatch(r"[0-9a-f]{64}", t):
        return True
    return False


def _default_ipv4_gateway() -> str:
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dest, gw_hex = parts[1], parts[2]
                if dest != "00000000" or gw_hex == "00000000":
                    continue
                gw_bytes = bytes.fromhex(gw_hex)
                if len(gw_bytes) != 4:
                    continue
                return socket.inet_ntoa(gw_bytes[::-1])
    except Exception:
        return ""
    return ""


_JSON_HOST_KEYS = (
    "nasName",
    "hostname",
    "hostName",
    "machineName",
    "deviceName",
    "stationName",
    "devName",
    "serverName",
    "nas_hostname",
)


def _deep_find_strings_by_keys(obj: Any, keys: Tuple[str, ...]) -> str:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                s = v.strip()
                if s and not _looks_like_container_hostname(s):
                    return s[:128]
        for v in obj.values():
            s = _deep_find_strings_by_keys(v, keys)
            if s:
                return s
    elif isinstance(obj, list):
        for it in obj:
            s = _deep_find_strings_by_keys(it, keys)
            if s:
                return s
    return ""


_FNOS_VER_KEYS = (
    "fnosVersion",
    "fnos_version",
    "fnOsVersion",
    "sysVersion",
    "systemVersion",
    "trimVersion",
    "miniOsVersion",
)


def _sanitize_fnos_version_candidate(raw: str) -> str:
    s = (raw or "").strip()
    if not s or len(s) > 160:
        return ""
    low = s.lower()
    if "error" in low or "unknown" in low:
        return ""
    if "fnos" in low:
        return s[:160]
    if re.search(r"\d+\.\d+\.\d+", s) or re.match(r"^v?\d+\.\d+(\.\d+)?$", s.strip(), re.I):
        ver = re.sub(r"^v", "", s, flags=re.I).strip()
        return f"FnOS {ver}"[:160] if not ver.lower().startswith("fnos") else s[:160]
    return ""


def _deep_find_fnos_version_json(obj: Any) -> str:
    if isinstance(obj, dict):
        for k in _FNOS_VER_KEYS:
            v = obj.get(k)
            if v is None:
                continue
            s = _sanitize_fnos_version_candidate(str(v))
            if s:
                return s
        for v in obj.values():
            s = _deep_find_fnos_version_json(v)
            if s:
                return s
    elif isinstance(obj, list):
        for it in obj:
            s = _deep_find_fnos_version_json(it)
            if s:
                return s
    return ""


def _patrol_read_hostname_fnos_from_logger_db(cfg: Any) -> Tuple[str, str]:
    """从已挂载的 eventlogger SQLite 的 parameter JSON 推断 NAS 展示名与系统版本（Docker 内常用）。"""
    path = (os.getenv("LOGGER_DB_PATH") or str(getattr(cfg, "logger_db_path", "") or "")).strip()
    if not path or not os.path.isfile(path):
        return "", ""
    conn: Optional[sqlite3.Connection] = None
    rows: List[Any] = []
    try:
        conn = connect_readonly_with_fallback(path, timeout=3.0)
        rows = conn.execute("SELECT parameter FROM log ORDER BY rowid DESC LIMIT 500").fetchall()
    except Exception:
        return "", ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    best_h, best_v = "", ""
    for row in rows:
        param = row[0] if row else None
        if not param or not str(param).strip():
            continue
        try:
            obj = json.loads(param)
        except Exception:
            continue
        if not best_h:
            cand = _deep_find_strings_by_keys(obj, _JSON_HOST_KEYS)
            if cand:
                best_h = cand
        if not best_v:
            candv = _deep_find_fnos_version_json(obj)
            if candv:
                best_v = candv
        if best_h and best_v:
            break
    return best_h, best_v


def _hostname_from_etc_hosts() -> str:
    try:
        text = Path("/etc/hosts").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] != "127.0.1.1":
            continue
        for name in parts[1:]:
            if name.lower() in ("localhost",):
                continue
            if _looks_like_container_hostname(name):
                continue
            return name.strip()[:128]
    return ""


def _hostname_from_mounted_host_paths() -> str:
    for fp in ("/host/etc/hostname", "/rootfs/etc/hostname", "/mnt/host/etc/hostname"):
        try:
            p = Path(fp)
            if not p.is_file():
                continue
            lines = p.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if lines and lines[0].strip():
                cand = lines[0].strip()
                if not _looks_like_container_hostname(cand):
                    return cand[:128]
        except Exception:
            continue
    return ""


def _read_hostname(logger_db_hostname: str = "") -> str:
    hint = (logger_db_hostname or "").strip()
    if hint and not _looks_like_container_hostname(hint):
        return hint[:128]
    h2 = _hostname_from_etc_hosts()
    if h2:
        return h2
    h3 = _hostname_from_mounted_host_paths()
    if h3:
        return h3
    sock = (socket.gethostname() or "").strip()
    if sock and not _looks_like_container_hostname(sock):
        return sock[:128]
    return sock or "--"


def _read_fnos_version(logger_db_fnos: str = "") -> str:
    """飞牛版本：优先开放 API / TRIM_SYS_VERSION，再回退本地探测（手装 Docker 等环境）。"""
    try:
        from utils.fnos_platform import resolve_fnos_version_via_api

        via_api = resolve_fnos_version_via_api()
        if via_api:
            return via_api[:160]
    except Exception:
        pass

    vlog = (logger_db_fnos or "").strip()
    if vlog:
        return vlog[:160]
    for rel in (
        "/etc/fnos-release",
        "/etc/fnos_version",
        "/usr/trim/etc/version",
        "/usr/trim/etc/fnos_version",
        "/usr/trim/VERSION",
        "/run/fnos/version",
    ):
        try:
            p = Path(rel)
            if not p.is_file():
                continue
            lines = p.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if lines and lines[0].strip():
                return lines[0].strip()[:160]
        except Exception:
            continue

    # 容器内通常没有飞牛的 trim 包；若把「宿主的 dpkg status」只读挂进容器，可解析出与宿主机 dpkg 一致的版本
    ver_host = _fnos_trim_version_from_host_dpkg_status()
    if ver_host:
        return _format_fnos_version_display(ver_host)

    # 容器自身 dpkg（仅当镜像/环境内确实安装了 trim 时有效）
    for sh_line in (
        "dpkg -s trim 2>/dev/null | grep Version | awk '{print $2}' || true",
        "dpkg-query -W -f='${Version}\\n' trim 2>/dev/null || true",
    ):
        out = _run_cmd(["sh", "-lc", sh_line], timeout=3.0)
        raw_lines = (out or "").strip().splitlines()
        if not raw_lines or not raw_lines[0].strip():
            continue
        raw = raw_lines[0].strip()
        low = raw.lower()
        if "dpkg-query" in low or "not installed" in low or "no information" in low or "no packages" in low:
            continue
        if low.startswith("e:"):
            continue
        return _format_fnos_version_display(raw)
    return "--"


def _format_fnos_version_display(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "--"
    if raw.lower().startswith("fnos"):
        return raw[:160]
    return f"FnOS {raw}"[:160]


def _fnos_trim_version_from_host_dpkg_status() -> str:
    """从 Debian dpkg status 流式解析 Package: trim 的 Version（避免整文件读入内存）。"""
    candidates = (
        "/host/dpkg/status",
        "/host/var/lib/dpkg/status",
        "/rootfs/var/lib/dpkg/status",
        "/mnt/host/var/lib/dpkg/status",
    )
    for rel in candidates:
        p = Path(rel)
        if not p.is_file():
            continue
        v = _fnos_trim_version_stream_dpkg_status(p)
        if v:
            return v
    return ""


def _fnos_trim_version_stream_dpkg_status(path: Path, max_read_bytes: int = 64 * 1024 * 1024) -> str:
    in_trim = False
    nread = 0
    try:
        with open(path, "rb") as fb:
            for raw in fb:
                nread += len(raw)
                if nread > max_read_bytes:
                    break
                line = raw.decode("utf-8", errors="ignore")
                if line.startswith("Package:"):
                    in_trim = line.split(":", 1)[1].strip() == "trim"
                elif in_trim and line.startswith("Version:"):
                    ver = line.split(":", 1)[1].strip()
                    if ver:
                        return ver
    except Exception:
        return ""
    return ""


def _read_update_status() -> str:
    """按用户要求关闭更新检查。"""
    return "不检查"


def _nut_ups_conf_is_mounted() -> bool:
    """是否已挂载 ups.conf（容器内 /etc/nut/ups.conf 存在且可读）。"""
    p = Path(_NUT_UPS_CONF_MOUNT_PATH)
    try:
        return p.is_file() and os.access(str(p), os.R_OK)
    except OSError:
        return False


def _read_nut_ups_conf_text() -> str:
    """读取已挂载的 ups.conf（未挂载时返回空，不通过 sudo/cat 探测宿主机）。"""
    if not _nut_ups_conf_is_mounted():
        return ""
    try:
        return Path(_NUT_UPS_CONF_MOUNT_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _upsc_query_var(upsc_bin: str, nutpfx: List[str], device_id: str, var: str) -> str:
    """查询单个 upsc 变量（如 device.product）。"""
    out = _run_upsc_cmd([upsc_bin] + list(nutpfx) + [device_id, var], timeout=2.0)
    if not out or _upsc_text_is_error_or_help(out):
        return ""
    return out.strip().splitlines()[-1].strip()


def _parse_nut_ups_conf_sections(text: str) -> Dict[str, Dict[str, str]]:
    """解析 ups.conf：{节名(与 upsc -l 一致): {键: 值}}。"""
    sections: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m_sec = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m_sec:
            current = m_sec.group(1).strip()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, val = line.split("=", 1)
        k = key.strip().lower()
        v = val.strip().strip('"').strip("'")
        if k:
            sections[current][k] = v
    return sections


def _ups_device_display_name(
    device_id: str,
    conf_sections: Dict[str, Dict[str, str]],
    upsc_kv: Dict[str, str],
    upsc_bin: str = "",
    nutpfx: Optional[List[str]] = None,
) -> str:
    """设备名优先 ups.conf 的 product，其次 upsc device.product，最后节名。"""
    sec = conf_sections.get(device_id) or {}
    product = (sec.get("product") or "").strip()
    if product:
        return product
    from_upsc = (upsc_kv.get("device.product") or "").strip()
    if from_upsc:
        return from_upsc
    if upsc_bin and device_id:
        nutpfx_list = list(nutpfx or [])
        fetched = _upsc_query_var(upsc_bin, nutpfx_list, device_id, "device.product")
        if fetched:
            return fetched
    return device_id


def _parse_ups_status_text(v: str) -> str:
    raw = (v or "").strip().upper()
    if not raw:
        return "--"
    parts: List[str] = []
    if "OB" in raw:
        parts.append("电池供电")
    if "OL" in raw:
        parts.append("市电供电")
    if "LB" in raw:
        parts.append("低电量")
    return "/".join(parts) if parts else raw


def _upsc_list_device_names(upsc_bin: str, nut_host: Optional[str], port: int) -> List[str]:
    if not nut_host:
        raw = _run_upsc_cmd([upsc_bin, "-l"], timeout=1.8) or _run_upsc_cmd([upsc_bin, "-L"], timeout=1.8)
    else:
        raw = _run_upsc_cmd([upsc_bin, "-h", nut_host, "-p", str(port), "-l"], timeout=1.8) or _run_upsc_cmd(
            [upsc_bin, "-h", nut_host, "-p", str(port), "-L"], timeout=1.8
        )
    if _upsc_text_is_error_or_help(raw):
        return []
    names: List[str] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.lower().startswith("error"):
            continue
        if _upsc_is_plausible_device_name(s):
            names.append(s)
    return names


def _collect_ups_info() -> Dict[str, Any]:
    if not _nut_ups_conf_is_mounted():
        return {"present": False}

    upsc_bin = _resolve_cmd("upsc")
    port = _NUT_UPSD_DEFAULT_PORT

    hosts_to_try: List[Optional[str]] = []
    hosts_to_try.append(None)
    gw = _default_ipv4_gateway()
    for h in (gw, "172.17.0.1", "192.168.65.254"):
        if h and h not in hosts_to_try:
            hosts_to_try.append(h)
    if not any(x == "host.docker.internal" for x in hosts_to_try if x):
        hosts_to_try.append("host.docker.internal")

    seen: Set[str] = set()
    names: List[str] = []
    chosen_host: Optional[str] = None
    for h in hosts_to_try:
        key = h or "__local__"
        if key in seen:
            continue
        seen.add(key)
        cand = _upsc_list_device_names(upsc_bin, h, port)
        if cand:
            names = cand
            chosen_host = h
            break

    if not names:
        return {"present": False}

    nutpfx: List[str] = []
    if chosen_host:
        nutpfx = ["-h", chosen_host, "-p", str(port)]

    dev = names[0]
    out = _run_upsc_cmd([upsc_bin] + nutpfx + [dev], timeout=2.0)
    if _upsc_text_is_error_or_help(out):
        return {"present": False}

    kv: Dict[str, str] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        kv[k.strip().lower()] = v.strip()

    if not _upsc_kv_has_ups_vars(kv):
        return {"present": False}

    def _pick_key(keys: List[str]) -> str:
        for k in keys:
            if kv.get(k):
                return str(kv.get(k))
        for k in keys:
            one = _run_upsc_cmd([upsc_bin] + nutpfx + [dev, k], timeout=2.0)
            if one and not _upsc_text_is_error_or_help(one):
                return one.strip().splitlines()[-1].strip()
        return "--"

    power_raw = _pick_key(["ups.status", "input.status", "output.status"])
    if power_raw == "--" or _upsc_text_is_error_or_help(power_raw):
        return {"present": False}

    power_status = _parse_ups_status_text(power_raw)
    conf_sections = _parse_nut_ups_conf_sections(_read_nut_ups_conf_text())
    display_name = _ups_device_display_name(dev, conf_sections, kv, upsc_bin, nutpfx)
    if _upsc_text_is_error_or_help(display_name):
        return {"present": False}
    return {
        "present": True,
        "device": display_name,
        "device_id": dev,
        "power_status": power_status,
    }


def _normalize_block_name(name: str) -> str:
    n = str(name or "").strip()
    if not n or any(ch in n for ch in ":/\\"):
        return ""
    # trim_/luks- 逻辑名不做末尾去数字，避免把 UUID 段误削坏；解析应走 mapper/realpath
    if n.startswith(("trim_", "luks-")):
        return n
    # device mapper: dm-2 不能去掉末尾数字
    if re.fullmatch(r"dm-\d+", n):
        return n
    # nvme0n1p2 -> nvme0n1；nvme0n1 必须保持（禁止误削成 nvme0n）
    m_nv = re.fullmatch(r"(nvme\d+n\d+)(p\d+)?", n, re.I)
    if m_nv:
        return m_nv.group(1)
    # mmcblk0p1 -> mmcblk0
    if n.startswith("mmcblk") and "p" in n:
        return n.split("p")[0]
    # md RAID 设备保留编号（例如 md0）
    if re.fullmatch(r"md\d+", n):
        return n
    # sda1 -> sda
    return re.sub(r"\d+$", "", n)


def _patrol_block_dev_for_inspection(dev_path: str) -> str:
    """把 ``/dev/trim_*`` 等规范成 ``/dev/mapper/…`` 或 ``dm-*`` 节点，便于 lsblk/sysfs 解析到 sda/nvme。"""
    dp = str(dev_path or "").strip()
    if not dp.startswith("/dev/"):
        return dp
    candidates: List[str] = []
    seen: Set[str] = set()

    def _add(p: str) -> None:
        p = str(p).strip()
        if p.startswith("/dev/") and p not in seen:
            seen.add(p)
            candidates.append(p)

    _add(dp)
    try:
        _add(os.path.realpath(dp))
    except OSError:
        pass
    base = os.path.basename(dp)
    if base.startswith(("trim_", "luks-")):
        mp = f"/dev/mapper/{base}"
        if mp != dp:
            _add(mp)
            try:
                _add(os.path.realpath(mp))
            except OSError:
                pass
    for c in candidates:
        try:
            bn = os.path.basename(os.path.realpath(c))
        except OSError:
            bn = os.path.basename(c)
        if re.fullmatch(r"dm-\d+", bn):
            try:
                return os.path.realpath(c)
            except OSError:
                return c
    for c in candidates:
        if c.startswith("/dev/"):
            try:
                return os.path.realpath(c)
            except OSError:
                return c
    return dp


def _lsblk_pkname_devpath(dev_path: str) -> str:
    """对真实块设备路径查询 lsblk PKNAME（必须用 /dev/mapper/trim_… 等完整路径，不能拼成 /dev/trim_…）。"""
    dp = str(dev_path or "").strip()
    if not dp.startswith("/dev/"):
        return ""
    lsblk = _resolve_cmd("lsblk")
    try:
        proc = subprocess.run(
            [lsblk, "-no", "PKNAME", dp],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return ""
    tok = line[0].strip()
    if tok and tok not in ("-", "--") and _valid_sysfs_block_token(tok):
        return tok
    base = os.path.basename(dp)
    if base.startswith(("trim_", "luks-")):
        mp = f"/dev/mapper/{base}"
        if mp != dp and mp.startswith("/dev/"):
            return _lsblk_pkname_devpath(mp)
    return ""


def _patrol_is_whole_disk_name(nn: str) -> bool:
    """已规范化到整盘名（非 mapper/trim 逻辑名）。"""
    if not nn or not _valid_sysfs_block_token(nn):
        return False
    if re.fullmatch(r"nvme\d+n\d+", nn, re.I):
        return True
    if re.fullmatch(r"sd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"vd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"xvd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"hd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"mmcblk\d+", nn, re.I):
        return True
    return False


def _walk_lsblk_to_physical_base(dev_path: str) -> str:
    """沿 PKNAME 从挂载点对应设备解析到 nvme*/sd* 等整盘名（解决 trim_* 显示名问题）。"""
    try:
        cur = _patrol_block_dev_for_inspection(dev_path)
    except Exception:
        return ""
    if not str(cur).startswith("/dev/"):
        return ""
    seen: Set[str] = set()
    for _ in range(40):
        if cur in seen:
            break
        seen.add(cur)
        nm = os.path.basename(cur)
        nn = _normalize_block_name(nm) or nm
        if not nn:
            break
        if not _valid_sysfs_block_token(nn):
            break
        if _patrol_is_whole_disk_name(nn):
            return nn
        pk = _lsblk_pkname_devpath(cur)
        if not pk:
            break
        cur = pk if pk.startswith("/dev/") else f"/dev/{pk}"
    return ""


def _lsblk_parent(name_or_path: str) -> str:
    if not name_or_path:
        return ""
    if str(name_or_path).startswith("/dev/"):
        return _lsblk_pkname_devpath(name_or_path)
    token = name_or_path.removeprefix("/dev/").strip()
    if not _valid_sysfs_block_token(token):
        return ""
    for dev in (f"/dev/{token}", f"/dev/mapper/{token}"):
        pk = _lsblk_pkname_devpath(dev)
        if pk:
            return pk
    return ""


def _list_sysfs_slaves(block_name: str) -> List[str]:
    if not block_name:
        return []
    slaves_dir = Path(f"/sys/block/{block_name}/slaves")
    try:
        names = sorted([p.name for p in slaves_dir.iterdir()])
    except Exception:
        return []
    return [n for n in names if n]


def _dedupe_block_names(names: List[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in names:
        name = _normalize_block_name(raw)
        if not name or not _valid_sysfs_block_token(name):
            continue
        if not _patrol_is_whole_disk_name(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _resolve_block_to_physical_names(block_name: str, visited: Optional[set] = None) -> List[str]:
    """递归将 dm/md/分区设备解析为所有底层物理盘，避免多盘存储池只展示第一块。"""
    name = _normalize_block_name(block_name)
    if not name or not _valid_sysfs_block_token(name):
        return []
    if _patrol_is_whole_disk_name(name):
        return [name]

    if visited is None:
        visited = set()
    if name in visited:
        return []
    visited.add(name)

    names: List[str] = []
    for slave in _list_sysfs_slaves(name):
        names.extend(_resolve_block_to_physical_names(slave, visited))
    if names:
        return _dedupe_block_names(names)

    parent = _lsblk_parent(name)
    if parent:
        names.extend(_resolve_block_to_physical_names(parent, visited))
    return _dedupe_block_names(names)


def _resolve_block_to_physical(block_name: str, visited: Optional[set] = None) -> str:
    """递归将 dm/md/分区设备解析到底层物理盘（sd/nvme/mmcblk）。"""
    names = _resolve_block_to_physical_names(block_name, visited)
    if names:
        return names[0]
    name = _normalize_block_name(block_name)
    if not name:
        return ""

    if visited is None:
        visited = set()
    if name in visited:
        return name
    visited.add(name)

    # 先走 sysfs slaves（适合 dm/md）
    slaves = _list_sysfs_slaves(name)
    for s in slaves:
        resolved = _resolve_block_to_physical(s, visited)
        if resolved.startswith(("sd", "hd", "vd", "xvd", "nvme", "mmcblk")):
            return resolved

    # 再走 lsblk 父链
    parent = _lsblk_parent(name)
    if parent:
        return _resolve_block_to_physical(parent, visited)
    return name


def _sysfs_block_basename_from_devpath(dev_path: str) -> str:
    """通过 /sys/dev/block/M:m 解析块设备在 sysfs 下的名字（如 dm-2、sda1），不依赖路径是否带 mapper/trim 前缀。"""
    dp0 = str(dev_path or "").strip()
    if not dp0.startswith("/dev/"):
        return ""
    cand_list: List[str] = []
    seen_c: Set[str] = set()
    for c in (
        _patrol_block_dev_for_inspection(dp0),
        dp0,
    ):
        if not c.startswith("/dev/") or c in seen_c:
            continue
        seen_c.add(c)
        cand_list.append(c)
        try:
            rp = os.path.realpath(c)
            if rp.startswith("/dev/") and rp not in seen_c:
                seen_c.add(rp)
                cand_list.append(rp)
        except OSError:
            pass
    for cand in cand_list:
        if not str(cand).startswith("/dev/"):
            continue
        try:
            st = os.stat(cand)
        except OSError:
            continue
        if not stat_mod.S_ISBLK(st.st_mode):
            continue
        maj = os.major(st.st_rdev)
        mi = os.minor(st.st_rdev)
        link = Path(f"/sys/dev/block/{maj}:{mi}")
        try:
            if not link.exists():
                continue
            name = link.resolve().name
            if _valid_sysfs_block_token(name):
                return name
        except OSError:
            continue
    return ""


def _resolve_physical_disk_name(dev_path: str) -> str:
    names = _resolve_physical_disk_names(dev_path)
    if names:
        return names[0]
    return ""


def _resolve_physical_disk_names(dev_path: str) -> List[str]:
    if not dev_path or not str(dev_path).startswith("/dev/"):
        return []
    found: List[str] = []
    walked = _walk_lsblk_to_physical_base(dev_path)
    if walked:
        found.append(walked)
    sb = _sysfs_block_basename_from_devpath(dev_path)
    if sb:
        found.extend(_resolve_block_to_physical_names(sb))
    insp = _patrol_block_dev_for_inspection(dev_path)
    try:
        base = os.path.basename(os.path.realpath(insp))
    except OSError:
        base = os.path.basename(insp)
    base = _normalize_block_name(base) or base
    found.extend(_resolve_block_to_physical_names(base))
    return _dedupe_block_names(found)


def _patrol_mount_is_file_bind_noise(mount: str) -> bool:
    """容器内常见文件级 bind mount 不是数据卷，不能作为硬盘行展示。"""
    m = str(mount or "").strip()
    if not m:
        return False
    if m != "/":
        m = m.rstrip("/")
    if m in _PATROL_CONTAINER_FILE_BIND_MOUNTS:
        return True
    if any(m == p or m.startswith(f"{p}/") for p in _PATROL_CONTAINER_APP_MOUNT_PREFIXES):
        return True
    if m.startswith(("/run/secrets/", "/var/run/secrets/")):
        return True
    try:
        st = os.stat(m)
    except OSError:
        return False
    return stat_mod.S_ISREG(st.st_mode)


def _patrol_readable_unresolved_device_label(dev_path: str, mount: str) -> str:
    """物理盘解析失败时仍避免展示 trim_/luks- 这类内部映射 ID。"""
    m = str(mount or "").strip().rstrip("/")
    if _patrol_mount_is_file_bind_noise(m):
        return "存储空间"
    if m:
        tail = os.path.basename(m) or m.strip("/")
        if tail:
            return f"存储空间 {tail}"
    raw = os.path.basename(str(dev_path or "").strip())
    if raw.startswith(("trim_", "luks-")):
        return "存储空间"
    return raw or "unknown"


def _smart_health_for_block_path(block_path: str) -> str:
    """对 ``/dev/...`` 绝对路径做 SMART 健康判断（含 mapper、分区）。"""
    bp = str(block_path or "").strip()
    if not bp.startswith("/dev/"):
        return "--"
    base = os.path.basename(bp)
    nb = _normalize_block_name(base) or base
    if not _valid_sysfs_block_token(nb) and not bp.startswith("/dev/mapper/"):
        return "--"
    out = _run_cmd(["smartctl", "-H", bp], timeout=2.5)
    if out:
        low = out.lower()
        if "test result: passed" in low or "smart overall-health self-assessment test result: passed" in low:
            return "健康"
        fail_markers = (
            "test result: failed",
            "overall-health self-assessment test result: failed",
            "smart overall-health self-assessment test result: failed",
            "prefail",
        )
        if any(k in low for k in fail_markers):
            return "异常"
        m = re.search(r"critical_warning\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)", out)
        if m:
            raw = m.group(1).strip().lower()
            try:
                val = int(raw, 16) if raw.startswith("0x") else int(raw)
                return "健康" if val == 0 else "异常"
            except Exception:
                pass

    if "nvme" in nb.lower():
        out_nvme_json = _run_cmd(["nvme", "smart-log", "-o", "json", bp], timeout=2.5)
        if out_nvme_json:
            try:
                obj = json.loads(out_nvme_json)
                if isinstance(obj, dict):
                    cw = obj.get("critical_warning")
                    if cw is not None:
                        v = int(cw, 16) if isinstance(cw, str) and cw.lower().startswith("0x") else int(cw)
                        return "健康" if v == 0 else "异常"
            except Exception:
                pass
        out_nvme = _run_cmd(["nvme", "smart-log", bp], timeout=2.5)
        if out_nvme:
            m = re.search(r"critical_warning\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)", out_nvme)
            if m:
                raw = m.group(1).strip().lower()
                try:
                    val = int(raw, 16) if raw.startswith("0x") else int(raw)
                    return "健康" if val == 0 else "异常"
                except Exception:
                    pass
        ctrl = nb.split("n")[0] if "n" in nb else nb
        smart_candidates = [
            Path(f"/sys/class/nvme/{ctrl}/smart_log/critical_warning"),
            Path(f"/sys/class/nvme/{ctrl}/device/critical_warning"),
        ]
        for p in smart_candidates:
            try:
                raw = p.read_text(encoding="utf-8", errors="ignore").strip().lower()
                if not raw:
                    continue
                val = int(raw, 16) if raw.startswith("0x") else int(raw)
                return "健康" if val == 0 else "异常"
            except Exception:
                continue

    sys_key = nb if _valid_sysfs_block_token(nb) else ""
    if sys_key:
        sys_state = Path(f"/sys/block/{sys_key}/device/state")
        try:
            s = sys_state.read_text(encoding="utf-8", errors="ignore").strip().lower()
            if s in {"running", "active", "live"}:
                return "健康"
        except Exception:
            pass
    return "--"


def _smart_health_for_disk(dev_base: str) -> str:
    if not dev_base:
        return "--"
    if str(dev_base).startswith("/dev/"):
        return _smart_health_for_block_path(dev_base)
    if not _valid_sysfs_block_token(dev_base):
        return "--"
    return _smart_health_for_block_path(f"/dev/{dev_base}")


def _patrol_sysfs_disk_running(dev_base: str) -> bool:
    """内核块设备 ``running/active/live`` 视为在线可用（无 smartctl 时的弱信号）。"""
    db = str(dev_base or "").strip()
    if not db or not _valid_sysfs_block_token(db):
        return False
    try:
        s = Path(f"/sys/block/{db}/device/state").read_text(encoding="utf-8", errors="ignore").strip().lower()
        return s in {"running", "active", "live"}
    except OSError:
        return False


def _smart_health_for_patrol_partition(dev_path: str, physical: str) -> str:
    """先对挂载点对应块设备路径探测，再对解析出的整盘名（sda/nvme0n1）探测。"""
    try:
        real = os.path.realpath(dev_path)
    except Exception:
        real = dev_path
    if str(real).startswith("/dev/"):
        h = _smart_health_for_block_path(real)
        if h not in {"--", "—"}:
            return h
    if physical and _patrol_is_whole_disk_name(physical):
        h2 = _smart_health_for_block_path(f"/dev/{physical}")
        if h2 not in {"--", "—"}:
            return h2
    if physical and _valid_sysfs_block_token(physical):
        return _smart_health_for_disk(physical)
    return "--"


def _smart_temp_for_block_path(block_path: str) -> str:
    bp = str(block_path or "").strip()
    if not bp.startswith("/dev/"):
        return "--"
    base = os.path.basename(bp)
    nb = _normalize_block_name(base) or base
    out = _run_cmd(["smartctl", "-A", bp], timeout=2.5)
    if out:
        for line in out.splitlines():
            low = line.lower()
            if "temperature_celsius" in low or "temperature:" in low:
                m = re.search(r"(-?\d+)\s*(?:c|celsius)?\b", line, flags=re.IGNORECASE)
                if m:
                    return f"{m.group(1)}.0"
            if "current_drive_temperature" in low:
                m = re.search(r"(-?\d+)\b", line)
                if m:
                    return f"{m.group(1)}.0"

    if "nvme" in nb.lower():
        out_nvme_json = _run_cmd(["nvme", "smart-log", "-o", "json", bp], timeout=2.5)
        if out_nvme_json:
            try:
                obj = json.loads(out_nvme_json)
                if isinstance(obj, dict):
                    for k in ("temperature", "temperature_sensor_1", "temp", "composite_temperature"):
                        if obj.get(k) is not None:
                            raw = str(obj.get(k)).strip()
                            m = re.search(r"(-?\d+)", raw)
                            if m:
                                v = int(m.group(1))
                                if v > 200:
                                    v = v - 273
                                return f"{float(v):.1f}"
            except Exception:
                pass
        out_nvme = _run_cmd(["nvme", "smart-log", bp], timeout=2.5)
        if out_nvme:
            m = re.search(r"temperature\s*[:=]\s*(-?\d+)", out_nvme, flags=re.IGNORECASE)
            if m:
                v = int(m.group(1))
                if v > 200:
                    v = v - 273
                return f"{float(v):.1f}"

    sys_paths: List[Path] = []
    if _valid_sysfs_block_token(nb):
        block_hwmon = Path(f"/sys/block/{nb}/device/hwmon")
        if _safe_path_exists(block_hwmon):
            for p in _safe_glob(block_hwmon, "hwmon*/temp1_input"):
                sys_paths.append(p)
        if nb.startswith("nvme"):
            ctrl = nb.split("n")[0] if "n" in nb else nb
            nvme_hwmon = Path(f"/sys/class/nvme/{ctrl}/device/hwmon")
            if _safe_path_exists(nvme_hwmon):
                for p in _safe_glob(nvme_hwmon, "hwmon*/temp1_input"):
                    sys_paths.append(p)
            sys_paths.append(Path(f"/sys/class/nvme/{ctrl}/smart_log/temperature"))
        sys_paths.append(Path(f"/sys/class/hwmon/{nb}/temp1_input"))

    for temp_path in sys_paths:
        try:
            raw = temp_path.read_text(encoding="utf-8", errors="ignore").strip()
            if not raw:
                continue
            v = int(raw)
            if abs(v) > 1000:
                return f"{v / 1000.0:.1f}"
            return f"{float(v):.1f}"
        except Exception:
            continue
    return "--"


def _smart_temp_for_disk(dev_base: str) -> str:
    if not dev_base:
        return "--"
    if str(dev_base).startswith("/dev/"):
        return _smart_temp_for_block_path(dev_base)
    if not _valid_sysfs_block_token(dev_base):
        return "--"
    return _smart_temp_for_block_path(f"/dev/{dev_base}")


def _smart_temp_for_patrol_partition(dev_path: str, physical: str) -> str:
    try:
        real = os.path.realpath(dev_path)
    except Exception:
        real = dev_path
    if str(real).startswith("/dev/"):
        t = _smart_temp_for_block_path(real)
        if t not in {"--", "—"}:
            return t
    if physical and _patrol_is_whole_disk_name(physical):
        t2 = _smart_temp_for_block_path(f"/dev/{physical}")
        if t2 not in {"--", "—"}:
            return t2
    if physical and _valid_sysfs_block_token(physical):
        return _smart_temp_for_disk(physical)
    return "--"


def _patrol_resolve_psutil_device(raw_dev: str) -> str:
    """把 psutil 的 device 字段解析成真实 ``/dev/...``（支持 UUID=/LABEL=/PARTUUID=）。"""
    d = str(raw_dev or "").strip()
    if not d:
        return ""
    if d.startswith("/dev/"):
        try:
            if os.path.exists(d):
                return os.path.realpath(d)
        except OSError:
            pass
        return d
    key, _, rest = d.partition("=")
    key = key.strip().lower()
    rest = rest.strip()
    if not key or not rest:
        return ""
    try:
        if key == "uuid":
            for u in (rest.lower(), rest):
                p = Path(f"/dev/disk/by-uuid/{u}")
                if p.exists():
                    return os.path.realpath(str(p))
        if key == "partuuid":
            for u in (rest.lower(), rest):
                p = Path(f"/dev/disk/by-partuuid/{u}")
                if p.exists():
                    return os.path.realpath(str(p))
        if key == "partlabel":
            base = Path("/dev/disk/by-partlabel")
            if base.is_dir():
                cand = base / rest
                if cand.exists():
                    return os.path.realpath(str(cand))
                for child in base.iterdir():
                    if child.name == rest:
                        return os.path.realpath(str(child))
        if key == "label":
            base = Path("/dev/disk/by-label")
            if base.is_dir():
                cand = base / rest
                if cand.exists():
                    return os.path.realpath(str(cand))
                for child in base.iterdir():
                    if child.name == rest:
                        return os.path.realpath(str(child))
    except OSError:
        return ""
    return ""


def _patrol_mount_is_data_volish(mnt: str) -> bool:
    """飞牛多块存储空间典型挂载：/vol1、/vol2、/vol3（大小写不敏感）；亦兼容 /volumeN。"""
    m = str(mnt or "").strip()
    ml = m.lower()
    # 必须 /vol 后紧跟数字，避免误把 /volumes、/voluntary 等当成数据卷
    if re.match(r"^/vol\d+", ml):
        return True
    return bool(re.match(r"^/volume\d+(?:/|$)", m, re.I))


def _patrol_is_fn_vol_mount(mnt: str) -> bool:
    """飞牛「存储空间」标准挂载点：仅为 ``/vol`` + 数字（不含子路径）。"""
    return bool(re.fullmatch(r"/vol\d+", str(mnt or "").strip(), re.I))


# compose 预挂不存在的 /volN 时，Docker 会在宿主建空目录再 bind；内容上不像真实存储空间
_FN_VOL_REAL_NAME_MARKERS = frozenset(
    {
        "@appdata",
        "@homes",
        "@home",
        "@share",
        "@shares",
        "@thumbnail",
        "@tmp",
        "@database",
        "homes",
        "Users",
    }
)


def _patrol_fn_vol_looks_like_real_storage(mnt: str) -> bool:
    """真实飞牛存储卷应含 @appdata 等目录；排除 Docker 为缺失 /volN 创建的空/假 bind。"""
    p = Path(str(mnt or "").strip())
    try:
        if not p.is_dir():
            return False
        for entry in p.iterdir():
            name = entry.name
            if name in _FN_VOL_REAL_NAME_MARKERS or name.startswith("@"):
                return True
        return False
    except OSError:
        return False


def _patrol_findmnt_block_mounts() -> List[Tuple[str, str]]:
    """用 findmnt 补全 psutil 未列出的块设备挂载（Linux 飞牛宿主机常见）。"""
    fm = _resolve_cmd("findmnt")
    try:
        proc = subprocess.run(
            [fm, "-rno", "SOURCE,TARGET,FSTYPE"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    rows: List[Tuple[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            parts = line.split("\t")
        else:
            parts = re.split(r"\s+", line, maxsplit=2)
        if len(parts) < 2:
            continue
        src, tgt = parts[0].strip(), parts[1].strip()
        fst = parts[2].strip().lower() if len(parts) > 2 else ""
        if not tgt or tgt == "/":
            continue
        if fst in _PATROL_PSUTIL_NOISE_FSTYPES:
            continue
        low = tgt.lower()
        if "docker" in low or "containerd" in low or "/kub" in low:
            continue
        if tgt.startswith(("/proc/", "/sys/", "/run/credentials", "/snap/")):
            continue
        if _patrol_mount_is_file_bind_noise(tgt):
            continue
        if src.startswith("["):
            continue
        rdev = _patrol_resolve_psutil_device(src) if not str(src).startswith("/dev/") else str(src).strip()
        if not rdev.startswith("/dev/"):
            continue
        try:
            rdev = os.path.realpath(rdev)
        except OSError:
            pass
        if not rdev.startswith("/dev/"):
            continue
        try:
            st = os.stat(rdev)
        except OSError:
            continue
        if not stat_mod.S_ISBLK(st.st_mode):
            continue
        rows.append((rdev, tgt))
    return rows


def _patrol_collect_disk_partitions() -> List[Tuple[str, str]]:
    """存在 /vol1、/vol2、/vol3 等数据卷挂载时只统计这些卷，避免 Docker 噪声；否则统计全部块设备挂载。

    合并 ``psutil.disk_partitions(all=True)`` 与 ``findmnt``，并解析 UUID=/LABEL=，减少漏盘。
    """
    out_ps: List[Tuple[str, str]] = []
    if psutil:
        try:
            for p in psutil.disk_partitions(all=True):
                dev = str(p.device or "").strip()
                mnt = str(p.mountpoint or "").strip()
                fst = str(p.fstype or "").lower()
                if not mnt or mnt == "/":
                    continue
                if fst in _PATROL_PSUTIL_NOISE_FSTYPES:
                    continue
                low = mnt.lower()
                if "docker" in low or "containerd" in low or "/kub" in low:
                    continue
                if mnt.startswith(("/proc/", "/sys/", "/run/credentials", "/snap/")):
                    continue
                if _patrol_mount_is_file_bind_noise(mnt):
                    continue
                rdev = _patrol_resolve_psutil_device(dev)
                if not rdev.startswith("/dev/"):
                    continue
                out_ps.append((rdev, mnt))
        except Exception:
            out_ps = []
    by_mount: Dict[str, Tuple[str, str]] = {}
    for d, m in out_ps:
        by_mount[m] = (d, m)
    for d, m in _patrol_findmnt_block_mounts():
        if m not in by_mount:
            by_mount[m] = (d, m)
    out = list(by_mount.values())
    vol = [(d, m) for d, m in out if _patrol_mount_is_data_volish(m)]
    return vol if vol else out


def _patrol_list_visible_whole_disks() -> List[str]:
    """列出系统可见整盘，作为挂载点反推失败/漏挂载时的兜底。"""
    names: List[str] = []
    sys_block = Path("/sys/block")
    try:
        for p in sorted(sys_block.iterdir(), key=lambda x: x.name):
            name = _normalize_block_name(p.name)
            if name and _patrol_is_whole_disk_name(name):
                names.append(name)
    except Exception:
        pass

    out = _run_cmd(["lsblk", "-dnro", "NAME,TYPE"], timeout=3.0)
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1].lower() != "disk":
            continue
        name = _normalize_block_name(parts[0])
        if name and _patrol_is_whole_disk_name(name):
            names.append(name)
    return _dedupe_block_names(names)


def _disk_size_gb_for_disk(dev_base: str) -> str:
    dev = str(dev_base or "").strip()
    if not dev or not _valid_sysfs_block_token(dev):
        return "--"
    sys_size = Path(f"/sys/block/{dev}/size")
    try:
        sectors = int(sys_size.read_text(encoding="utf-8", errors="ignore").strip())
        if sectors > 0:
            return f"{sectors * 512 / (1024**3):.1f}"
    except Exception:
        pass
    out = _run_cmd(["lsblk", "-b", "-dnro", "SIZE", f"/dev/{dev}"], timeout=2.0)
    for line in (out or "").splitlines():
        raw = line.strip()
        if not raw or not raw.isdigit():
            continue
        size = int(raw)
        if size > 0:
            return f"{size / (1024**3):.1f}"
    return "--"


def _disk_total_gb_for_mount(mount: str) -> str:
    if not psutil:
        return "--"
    try:
        total = psutil.disk_usage(mount).total
        if total > 0:
            return f"{total / (1024**3):.1f}"
    except Exception:
        pass
    return "--"


def _patrol_mounts_to_probe_for_physical() -> List[str]:
    """按顺序探测挂载点，用于将整盘名（sda）反查到 /vol* 等数据卷。"""
    ordered: List[str] = []
    seen: Set[str] = set()
    for p in (
        *_patrol_visible_fn_vol_mount_roots(),
        "/volume1",
        "/volume2",
        "/volume3",
        "/volume4",
    ) + tuple(_PATROL_HOST_STORAGE_PATH_CANDIDATES):
        s = str(p).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        ordered.append(s)
    return ordered


def _patrol_best_mount_for_physical(dev_base: str) -> str:
    """根据块设备反查其数据卷挂载路径（典型 /vol1–/vol3），供剩余空间统计。"""
    want = str(dev_base or "").strip().lower()
    if not want:
        return ""
    for base in _patrol_mounts_to_probe_for_physical():
        if _patrol_mount_is_file_bind_noise(base):
            continue
        try:
            if not Path(base).exists():
                continue
        except OSError:
            continue
        src = _patrol_mount_source_for_path(base)
        if not src:
            continue
        rdev = src if str(src).startswith("/dev/") else _patrol_resolve_psutil_device(src)
        if not rdev.startswith("/dev/"):
            continue
        try:
            rdev = os.path.realpath(rdev)
        except OSError:
            pass
        phy = [x.lower() for x in _resolve_physical_disk_names(rdev)]
        if want in phy:
            return base
    return ""


def _patrol_df_space_gb_pair(path: str) -> Tuple[str, str]:
    """用 ``df`` 得到 ``(剩余GB, 总容量GB)`` 字符串；优先 GNU ``--output``，否则 ``-Pk``。"""
    p = str(path or "").strip()
    if not p:
        return "--", "--"
    out = _run_cmd(["df", "-B1", "--output=size,avail", "--", p], timeout=2.0)
    lines = [ln for ln in (out or "").splitlines() if ln.strip() and not ln.lstrip().lower().startswith("df:")]
    if len(lines) >= 2:
        parts = lines[-1].split()
        if len(parts) >= 2:
            try:
                sz_b = int(parts[0])
                av_b = int(parts[1])
                if sz_b > 0 and av_b >= 0:
                    return f"{av_b / (1024**3):.1f}", f"{sz_b / (1024**3):.1f}"
            except (TypeError, ValueError, IndexError):
                pass
    out2 = _run_cmd(["df", "-Pk", "--", p], timeout=2.0)
    lines2 = [ln for ln in (out2 or "").splitlines() if ln.strip() and not ln.lstrip().lower().startswith("df:")]
    if len(lines2) < 2:
        return "--", "--"
    parts = lines2[-1].split()
    if len(parts) < 4:
        return "--", "--"
    try:
        blocks_1k = int(parts[1])
        avail_1k = int(parts[3])
        if blocks_1k <= 0 or avail_1k < 0:
            return "--", "--"
        total_gb = f"{blocks_1k / (1024**2):.1f}"
        free_gb = f"{avail_1k / (1024**2):.1f}"
        return free_gb, total_gb
    except (TypeError, ValueError):
        return "--", "--"


def _patrol_df_free_gb_for_path(path: str) -> str:
    """df 解析可用空间（GB），与 ``_patrol_df_space_gb_pair`` 一致。"""
    f, _ = _patrol_df_space_gb_pair(path)
    return f


def _patrol_free_gb_str_for_mount(mnt: str) -> str:
    """挂载点剩余空间（GB）；优先 ``df``（与系统一致），再 psutil、statvfs。"""
    m = str(mnt or "").strip()
    if not m:
        return "--"
    try:
        if not Path(m).exists():
            return "--"
    except OSError:
        return "--"
    df_f = _patrol_df_free_gb_for_path(m)
    if df_f not in {"--", "—"}:
        return df_f
    if psutil:
        try:
            u = psutil.disk_usage(m)
            if u.total > 0 and u.free >= 0:
                return f"{u.free / (1024**3):.1f}"
        except Exception:
            pass
    try:
        stv = os.statvfs(m)
        fr = int(stv.f_frsize)
        bavail = int(stv.f_bavail) * fr
        bfree = int(stv.f_bfree) * fr
        use_b = bavail if bavail > 0 else bfree
        if use_b >= 0:
            return f"{use_b / (1024**3):.1f}"
    except OSError:
        pass
    return "--"


def _patrol_partition_dev_paths(dev_base: str) -> List[str]:
    """整盘下的分区块设备路径（如 /dev/sda1、/dev/nvme0n1p1），供 findmnt -S 反查挂载点。"""
    bd = (dev_base or "").strip()
    if not bd or not _valid_sysfs_block_token(bd):
        return []
    bpath = Path(f"/sys/block/{bd}")
    if not bpath.is_dir():
        try:
            core = Path(f"/dev/{bd}")
            return [str(core)] if core.exists() else []
        except OSError:
            return []
    out: List[str] = []
    try:
        for ch in sorted(bpath.iterdir()):
            if not ch.is_dir():
                continue
            name = ch.name
            if name == bd or not name.startswith(bd):
                continue
            devn = Path(f"/dev/{name}")
            try:
                if devn.exists():
                    out.append(str(devn))
            except OSError:
                continue
    except OSError:
        pass
    if not out:
        try:
            core = Path(f"/dev/{bd}")
            if core.exists():
                out.append(str(core))
        except OSError:
            pass
    return out


def _patrol_free_gb_via_findmnt_for_disk(dev_base: str) -> str:
    """对整盘各分区执行 ``findmnt -S``，在挂载点上再读剩余空间（不依赖 /vol 反查）。"""
    best: Optional[float] = None
    for part in _patrol_partition_dev_paths(dev_base):
        out = _run_cmd(["findmnt", "-n", "-r", "-S", part, "-o", "TARGET"], timeout=2.5)
        if not out:
            continue
        for line in (out or "").splitlines():
            tgt = (line or "").strip()
            if not tgt:
                continue
            tgt = tgt.split()[0]
            if _patrol_mount_is_file_bind_noise(tgt):
                continue
            try:
                if not Path(tgt).exists():
                    continue
            except OSError:
                continue
            g = _patrol_free_gb_str_for_mount(tgt)
            if g in {"--", "—"}:
                continue
            try:
                fv = float(g)
            except (TypeError, ValueError):
                continue
            if fv < 0:
                continue
            if best is None or fv > best:
                best = fv
    return f"{best:.1f}" if best is not None else "--"


def _patrol_scan_vol_space_by_physical() -> Dict[str, Dict[str, str]]:
    """正向扫描容器内可见的 /volN：每卷的 df/用量映射到底层整盘，含剩余与文件系统总容量。"""
    out: Dict[str, Dict[str, float]] = {}
    for vol in _patrol_visible_fn_vol_mount_roots():
        try:
            if not Path(vol).exists():
                continue
        except OSError:
            continue
        if _patrol_mount_is_file_bind_noise(vol):
            continue
        free_str, tot_str = _patrol_df_space_gb_pair(vol)
        if free_str in {"--", "—"} or tot_str in {"--", "—"}:
            if psutil:
                try:
                    u = psutil.disk_usage(vol)
                    if u.total > 0:
                        tot_str = f"{u.total / (1024**3):.1f}"
                        free_str = f"{u.free / (1024**3):.1f}"
                except Exception:
                    pass
        if free_str in {"--", "—"} or tot_str in {"--", "—"}:
            continue
        try:
            free_val = float(free_str)
            tot_val = float(tot_str)
        except (TypeError, ValueError):
            continue
        if free_val < 0 or tot_val <= 0:
            continue
        src = _patrol_mount_source_for_path(vol)
        if not src:
            continue
        rdev = src.strip() if str(src).startswith("/dev/") else _patrol_resolve_psutil_device(src)
        if not rdev.startswith("/dev/"):
            continue
        dev_open = _patrol_block_dev_for_inspection(rdev)
        try:
            dev_open = os.path.realpath(dev_open)
        except OSError:
            pass
        names = _resolve_physical_disk_names(dev_open)
        if not names:
            try:
                base = os.path.basename(os.path.realpath(_patrol_block_dev_for_inspection(rdev)))
            except OSError:
                base = os.path.basename(rdev)
            nb = _normalize_block_name(base) or base
            if nb and _patrol_is_whole_disk_name(nb):
                names = [nb]
        sing = _resolve_physical_disk_name(dev_open)
        if sing and _patrol_is_whole_disk_name(sing):
            names = list(dict.fromkeys(list(names or []) + [sing]))
        for raw_phy in names or []:
            key = str(raw_phy or "").strip().lower()
            if not key:
                continue
            prev = out.get(key)
            if prev is None or free_val > prev["free"]:
                out[key] = {"free": free_val, "total": tot_val}
    return {
        k: {"free_gb": f"{v['free']:.1f}", "total_gb": f"{v['total']:.1f}"}
        for k, v in out.items()
    }


def _patrol_disk_free_gb_for_row(
    mount: str,
    physical: str,
    vol_space_by_physical: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """巡检单行剩余空间：优先 /vol 正向映射（与总容量同源），再挂载点、反查卷、findmnt、lsblk。"""
    vol_space = vol_space_by_physical or {}
    m0 = str(mount or "").strip()
    phy = str(physical or "").strip()
    pl = phy.strip().lower()
    if pl:
        bag = vol_space.get(pl) or {}
        hit = bag.get("free_gb")
        if hit and str(hit).strip() and hit not in {"--", "—"}:
            return str(hit).strip()

    if m0 and not _patrol_mount_is_file_bind_noise(m0):
        got = _patrol_free_gb_str_for_mount(m0)
        if got not in {"--", "—"}:
            return got
    if phy:
        m_alt = _patrol_best_mount_for_physical(phy)
        if m_alt and m_alt != m0:
            got2 = _patrol_free_gb_str_for_mount(m_alt)
            if got2 not in {"--", "—"}:
                return got2
        fm = _patrol_free_gb_via_findmnt_for_disk(phy)
        if fm not in {"--", "—"}:
            return fm
        fb = _disk_free_gb_for_disk(phy)
        if fb not in {"--", "—"}:
            return fb
    if m0 and not _patrol_mount_is_file_bind_noise(m0):
        return _patrol_free_gb_str_for_mount(m0)
    return "--"


def _disk_free_gb_for_disk(dev_base: str) -> str:
    """从 lsblk 的文件系统字段读取整盘或其子设备的真实可用空间。"""
    dev = str(dev_base or "").strip()
    if not dev or not _valid_sysfs_block_token(dev):
        return "--"

    out = _run_cmd(
        ["lsblk", "-b", "-J", "-o", "NAME,TYPE,FSAVAIL,MOUNTPOINT,MOUNTPOINTS", f"/dev/{dev}"],
        timeout=3.0,
    )
    if not out:
        return _disk_free_gb_from_mounted_paths_for_disk(dev)
    try:
        obj = json.loads(out)
    except Exception:
        return _disk_free_gb_from_mounted_paths_for_disk(dev)

    def _mounts_for_node(node: Dict[str, Any]) -> List[str]:
        raw = node.get("mountpoints")
        mounts: List[str] = []
        if isinstance(raw, list):
            mounts.extend(str(x).strip() for x in raw if str(x or "").strip())
        elif isinstance(raw, str) and raw.strip():
            mounts.extend(x.strip() for x in raw.splitlines() if x.strip())
        raw_one = node.get("mountpoint")
        if isinstance(raw_one, str) and raw_one.strip():
            mounts.append(raw_one.strip())
        return mounts

    def _walk(node: Dict[str, Any]) -> List[int]:
        vals: List[int] = []
        mounts = _mounts_for_node(node)
        if mounts and not all(_patrol_mount_is_file_bind_noise(m) for m in mounts):
            raw = node.get("fsavail")
            try:
                if raw is None or raw is False or raw == "":
                    raise ValueError
                avail = int(raw)
                if avail >= 0:
                    vals.append(avail)
            except (TypeError, ValueError):
                pass
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    vals.extend(_walk(child))
        return vals

    values: List[int] = []
    for block in obj.get("blockdevices") or []:
        if isinstance(block, dict):
            values.extend(_walk(block))
    if not values:
        return _disk_free_gb_from_mounted_paths_for_disk(dev)
    return f"{max(values) / (1024**3):.1f}"


def _patrol_mount_source_for_path(path: str) -> str:
    p = str(path or "").strip()
    if not p:
        return ""
    out = _run_cmd(["findmnt", "-T", p, "-rno", "SOURCE"], timeout=2.0)
    if out:
        src = out.strip().splitlines()[0].strip()
        if src:
            return src
    out = _run_cmd(["df", "-P", p], timeout=2.0)
    lines = [x for x in (out or "").splitlines() if x.strip()]
    if len(lines) >= 2:
        return lines[-1].split()[0].strip()
    return ""


def _disk_free_gb_from_mounted_paths_for_disk(dev_base: str) -> str:
    dev = str(dev_base or "").strip()
    if not dev:
        return "--"
    candidates: List[Tuple[str, str]] = []

    if psutil:
        try:
            for p in psutil.disk_partitions(all=True):
                mnt = str(p.mountpoint or "").strip()
                src = str(p.device or "").strip()
                if not mnt or not src or _patrol_mount_is_file_bind_noise(mnt):
                    continue
                candidates.append((src, mnt))
        except Exception:
            pass

    for path in _PATROL_HOST_STORAGE_PATH_CANDIDATES:
        if _patrol_mount_is_file_bind_noise(path):
            continue
        try:
            if not Path(path).exists():
                continue
        except OSError:
            continue
        src = _patrol_mount_source_for_path(path)
        if src:
            candidates.append((src, path))

    best: Optional[float] = None
    for src, mount in candidates:
        rdev = _patrol_resolve_psutil_device(src) if not str(src).startswith("/dev/") else str(src).strip()
        if not rdev.startswith("/dev/"):
            continue
        if dev not in _resolve_physical_disk_names(rdev):
            continue
        try:
            free = psutil.disk_usage(mount).free / (1024**3) if psutil else 0.0
        except Exception:
            continue
        if free < 0:
            continue
        if best is None or free > best:
            best = free
    return f"{best:.1f}" if best is not None else "--"


def _patrol_disk_row_merge_key(dev_path: str, physical: str, display_dev: str, mount: str) -> str:
    """同一整盘（nvme0n1/sda）多挂载点合并；dm 设备单独成键；其余按 realpath 或 设备@挂载 区分。"""
    p = (physical or "").strip().rstrip("-")
    if p and _patrol_is_whole_disk_name(p):
        mnv = re.match(r"^(nvme\d+n\d+)", p, re.I)
        if mnv:
            return mnv.group(1).lower()
        return p
    if p and _valid_sysfs_block_token(p) and re.fullmatch(r"dm-\d+", p):
        return p
    try:
        rp = os.path.realpath(dev_path)
        if str(rp).startswith("/dev/"):
            return rp
    except Exception:
        pass
    d = (display_dev or "").strip().rstrip("-")
    return f"{d}@{mount}" if d else f"unknown@{mount}"


def _psutil_disk_temp_fallback(device_name: str) -> str:
    """从 psutil 传感器中按设备名提取温度（如 nvme0n1/sda/sdb）。"""
    if not psutil:
        return "--"
    dev = str(device_name or "").strip().lower()
    if not dev:
        return "--"
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return "--"
    if not isinstance(temps, dict):
        return "--"

    def _to_temp(v: Any) -> str:
        try:
            fv = float(v)
            # 少数来源可能返回 Kelvin
            if fv > 200:
                fv = fv - 273.15
            return f"{fv:.1f}"
        except Exception:
            return "--"

    # 1) 先按 label/chip 中出现设备名精确匹配
    for chip, entries in temps.items():
        chip_s = str(chip or "").lower()
        for e in entries or []:
            label = str(getattr(e, "label", "") or "").lower()
            blob = f"{chip_s} {label}"
            if dev in blob:
                t = _to_temp(getattr(e, "current", None))
                if t != "--":
                    return t

    # 2) nvme/sata/drivetemp 常见通用回退
    preferred_chips: List[str] = []
    if dev.startswith("nvme"):
        preferred_chips = ["nvme", "drivetemp", "sata"]
    else:
        preferred_chips = ["drivetemp", "sata", "nvme"]
    for pref in preferred_chips:
        for chip, entries in temps.items():
            chip_s = str(chip or "").lower()
            if pref not in chip_s:
                continue
            for e in entries or []:
                t = _to_temp(getattr(e, "current", None))
                if t != "--":
                    return t
    return "--"


def _patrol_vol_row_health_temp(dev_open: str, physicals: List[str]) -> Tuple[str, str, str, str]:
    """存储空间 volN 行：聚合 SMART/sysfs；展示与飞牛界面一致的「正常」而非内部「健康」。"""
    hr_fail = "未读到健康状态（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
    tr_fail = "未读到温度（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
    phys = [p for p in (physicals or []) if (p or "").strip()]
    hs: List[str] = []
    if phys:
        for p in phys:
            h = _smart_health_for_patrol_partition(dev_open, p)
            if h not in {"--", "—"}:
                hs.append(h)
            for part in _patrol_partition_dev_paths(p):
                hp = _smart_health_for_block_path(part)
                if hp not in {"--", "—"}:
                    hs.append(hp)
    else:
        h0 = _smart_health_for_patrol_partition(dev_open, "")
        if h0 not in {"--", "—"}:
            hs.append(h0)
        try:
            hp0 = _smart_health_for_block_path(os.path.realpath(dev_open))
        except OSError:
            hp0 = _smart_health_for_block_path(dev_open)
        if hp0 not in {"--", "—"}:
            hs.append(hp0)

    for p in phys:
        if _patrol_sysfs_disk_running(p):
            hs.append("健康")

    if any(h == "异常" for h in hs):
        health = "异常"
    elif any(h == "健康" for h in hs):
        health = "正常"
    else:
        health = "--"

    temp = "--"
    if phys:
        for p in phys:
            t = _smart_temp_for_patrol_partition(dev_open, p)
            if t not in {"--", "—"}:
                temp = t
                break
        if temp in {"--", "—"}:
            temp = _psutil_disk_temp_fallback(str(phys[0]))
    else:
        temp = _smart_temp_for_patrol_partition(dev_open, "")
        if temp in {"--", "—"}:
            try:
                bn = os.path.basename(os.path.realpath(dev_open))
            except OSError:
                bn = os.path.basename(dev_open)
            temp = _psutil_disk_temp_fallback(bn)
    temp_reason = "" if temp not in {"--", "—"} else tr_fail

    health_reason = ""
    if health in {"--", "—"}:
        if any(_patrol_sysfs_disk_running(p) for p in phys) or temp not in {"--", "—"}:
            health = "正常"
            health_reason = ""
        else:
            health_reason = hr_fail
    return health, temp, health_reason, temp_reason


def _patrol_vol_mount_sort_key(mpath: str) -> int:
    m = re.search(r"/vol(\d+)$", str(mpath).strip(), re.I)
    return int(m.group(1)) if m else 0


def _patrol_discover_fn_vol_mount_root_paths() -> List[str]:
    """扫描容器根目录下所有 /volN（N 为任意正整数，不限于 vol1～vol6）。"""
    found: List[str] = []
    try:
        for entry in Path("/").iterdir():
            if not entry.is_dir():
                continue
            if re.fullmatch(r"vol\d+", entry.name, re.I):
                found.append(f"/{entry.name}")
    except OSError:
        pass
    return sorted(found, key=_patrol_vol_mount_sort_key)


def _patrol_visible_fn_vol_mount_roots() -> List[str]:
    """容器内可见且看起来像真实存储的 /volN（排除 Docker 空 bind 伪卷）。"""
    visible: List[str] = []
    for p in _patrol_discover_fn_vol_mount_root_paths():
        try:
            if Path(p).is_dir() and _patrol_fn_vol_looks_like_real_storage(p):
                visible.append(p)
        except OSError:
            continue
    return visible


def _patrol_has_any_fn_vol_mount_in_container() -> bool:
    return bool(_patrol_visible_fn_vol_mount_roots())


def _patrol_fn_compose_vol_mounts_ready() -> bool:
    """兼容旧名：任一 /volN 在容器内可见即视为已挂载存储卷。"""
    return _patrol_has_any_fn_vol_mount_in_container()


def _collect_disk_items() -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    if not psutil:
        return items
    if not _patrol_has_any_fn_vol_mount_in_container():
        return items
    mappings = _patrol_collect_disk_partitions()

    vol_pairs: List[Tuple[str, str]] = []
    for dev_path, mount in mappings:
        if not _patrol_is_fn_vol_mount(mount):
            continue
        if _patrol_mount_is_file_bind_noise(mount):
            continue
        if not _patrol_fn_vol_looks_like_real_storage(mount):
            continue
        dp = str(dev_path or "").strip()
        if not dp.startswith("/dev/"):
            src = _patrol_mount_source_for_path(mount)
            r = (
                str(src).strip()
                if str(src or "").startswith("/dev/")
                else _patrol_resolve_psutil_device(str(src or ""))
            )
            if not r.startswith("/dev/"):
                continue
            dp = r
        vol_pairs.append((dp, mount))

    if vol_pairs:
        by_mount: Dict[str, str] = {}
        for d, m in vol_pairs:
            mk = str(m).strip()
            if mk and mk not in by_mount:
                by_mount[mk] = d

        mounts_sorted = sorted(by_mount.keys(), key=_patrol_vol_mount_sort_key)
        for mnt in mounts_sorted:
            dev_raw = by_mount[mnt]
            dev_open = _patrol_block_dev_for_inspection(dev_raw)
            display_dev = _patrol_readable_unresolved_device_label(dev_open, mnt).rstrip("-")
            phys = _resolve_physical_disk_names(dev_open)
            if not phys:
                sing = _resolve_physical_disk_name(dev_open)
                if sing and _patrol_is_whole_disk_name(sing):
                    phys = [sing]
            health, temp, health_reason, temp_reason = _patrol_vol_row_health_temp(dev_open, phys)
            free_s, tot_s = _patrol_df_space_gb_pair(mnt)
            if tot_s in {"--", "—"} or free_s in {"--", "—"}:
                try:
                    u = psutil.disk_usage(mnt)
                    if u.total > 0:
                        tot_s = f"{u.total / (1024**3):.1f}"
                        free_s = f"{u.free / (1024**3):.1f}"
                except Exception:
                    pass
            items.append(
                {
                    "name": "",
                    "device": display_dev,
                    "mount_point": mnt,
                    "free_gb": free_s,
                    "size_gb": tot_s,
                    "temp_c": temp,
                    "status": health,
                    "temp_reason": temp_reason,
                    "status_reason": health_reason,
                }
            )
        for idx, row in enumerate(items, start=1):
            row["name"] = f"硬盘{idx}"
        return items

    vol_space_by_physical = _patrol_scan_vol_space_by_physical()

    merged: Dict[str, Dict[str, str]] = {}
    pending_unresolved: List[Dict[str, Any]] = []

    def _upsert_disk_row(
        key: str,
        display_dev: str,
        mount: str,
        free_gb: str,
        size_gb: str,
        health: str,
        temp: str,
        health_reason: str,
        temp_reason: str,
    ) -> None:
        if key not in merged:
            merged[key] = {
                "name": "",
                "device": display_dev,
                "mount_point": mount,
                "free_gb": free_gb,
                "size_gb": size_gb,
                "temp_c": temp,
                "status": health,
                "temp_reason": temp_reason,
                "status_reason": health_reason,
            }
            return

        ex = merged[key]
        cur_d = str(ex.get("device") or "")
        if _patrol_is_whole_disk_name(display_dev) and not _patrol_is_whole_disk_name(cur_d):
            ex["device"] = display_dev
        try:
            if float(free_gb) > float(ex["free_gb"]):
                ex["free_gb"] = free_gb
                ex["mount_point"] = mount
        except (TypeError, ValueError):
            if str(ex.get("free_gb") or "--") in {"--", "—"} and free_gb not in {"--", "—"}:
                ex["free_gb"] = free_gb
                ex["mount_point"] = mount
        if str(ex.get("size_gb") or "--") in {"--", "—"} and size_gb not in {"--", "—"}:
            ex["size_gb"] = size_gb
        if ex.get("status") in {"--", "—"} and health not in {"--", "—"}:
            ex["status"] = health
            ex["status_reason"] = health_reason
        if ex.get("temp_c") in {"--", "—"} and temp not in {"--", "—"}:
            ex["temp_c"] = temp
            ex["temp_reason"] = temp_reason

    for dev_path, mount in mappings:
        if not str(dev_path).startswith("/dev/"):
            continue
        if _patrol_is_fn_vol_mount(mount):
            continue
        hidden_mount = _patrol_mount_is_file_bind_noise(mount)
        dev_open = _patrol_block_dev_for_inspection(dev_path)
        physical_names = _resolve_physical_disk_names(dev_path)
        try:
            raw_base = os.path.basename(os.path.realpath(dev_open))
        except OSError:
            raw_base = os.path.basename(dev_open)
        raw_nb = _normalize_block_name(raw_base) or raw_base
        if not physical_names and raw_nb and _patrol_is_whole_disk_name(raw_nb):
            physical_names = [raw_nb]

        if hidden_mount:
            continue

        if not physical_names:
            display_dev = _patrol_readable_unresolved_device_label(dev_open, mount)
            if not display_dev or display_dev.startswith(("trim_", "luks-")):
                display_dev = _patrol_readable_unresolved_device_label(dev_open, mount)
            display_dev = display_dev.rstrip("-")

            health = _smart_health_for_patrol_partition(dev_open, "")
            temp = _smart_temp_for_patrol_partition(dev_open, "")
            if temp in {"--", "—"}:
                temp = _psutil_disk_temp_fallback(display_dev)
            pending_unresolved.append(
                {
                    "key": _patrol_disk_row_merge_key(dev_open, "", display_dev, mount),
                    "display_dev": display_dev,
                    "mount": mount,
                    "free_gb": _patrol_free_gb_str_for_mount(mount),
                    "size_gb": _disk_total_gb_for_mount(mount),
                    "health": health,
                    "temp": temp,
                    "health_reason": ""
                    if health not in {"--", "—"}
                    else "未读到健康状态（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）",
                    "temp_reason": ""
                    if temp not in {"--", "—"}
                    else "未读到温度（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）",
                    "hidden": hidden_mount,
                }
            )
            continue

        row_physicals = physical_names
        for physical in row_physicals:
            display_dev = physical if physical else _patrol_readable_unresolved_device_label(dev_open, mount)
            if not display_dev or display_dev.startswith(("trim_", "luks-")):
                display_dev = _patrol_readable_unresolved_device_label(dev_open, mount)
            display_dev = display_dev.rstrip("-")

            health = _smart_health_for_patrol_partition(dev_open, physical)
            temp = _smart_temp_for_patrol_partition(dev_open, physical)
            if temp in {"--", "—"}:
                temp = _psutil_disk_temp_fallback(physical or display_dev)
            health_reason = (
                ""
                if health not in {"--", "—"}
                else "未读到健康状态（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
            )
            temp_reason = (
                ""
                if temp not in {"--", "—"}
                else "未读到温度（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
            )
            key = _patrol_disk_row_merge_key(dev_open, physical, display_dev, mount)
            size_gb = _disk_size_gb_for_disk(physical) if physical else _disk_total_gb_for_mount(mount)
            free_gb = _patrol_disk_free_gb_for_row(mount, physical, vol_space_by_physical)
            m_row = (_patrol_best_mount_for_physical(physical) or mount).strip() or mount
            _upsert_disk_row(key, display_dev, m_row, free_gb, size_gb, health, temp, health_reason, temp_reason)

    for physical in _patrol_list_visible_whole_disks():
        key = physical.lower()
        if key in merged:
            continue
        health = _smart_health_for_disk(physical)
        temp = _smart_temp_for_disk(physical)
        if temp in {"--", "—"}:
            temp = _psutil_disk_temp_fallback(physical)
        health_reason = (
            ""
            if health not in {"--", "—"}
            else "未读到健康状态（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
        )
        temp_reason = (
            ""
            if temp not in {"--", "—"}
            else "未读到温度（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
        )
        m_pick = _patrol_best_mount_for_physical(physical)
        free_s = _patrol_disk_free_gb_for_row(m_pick, physical, vol_space_by_physical)
        _upsert_disk_row(
            key,
            physical,
            m_pick,
            free_s,
            _disk_size_gb_for_disk(physical),
            health,
            temp,
            health_reason,
            temp_reason,
        )

    for row in pending_unresolved:
        if row.get("hidden"):
            continue
        _upsert_disk_row(
            str(row.get("key") or ""),
            str(row.get("display_dev") or ""),
            str(row.get("mount") or ""),
            str(row.get("free_gb") or "--"),
            str(row.get("size_gb") or "--"),
            str(row.get("health") or "--"),
            str(row.get("temp") or "--"),
            str(row.get("health_reason") or ""),
            str(row.get("temp_reason") or ""),
        )

    for row in merged.values():
        devk = str(row.get("device") or "").strip().lower()
        if not devk:
            continue
        bag = vol_space_by_physical.get(devk)
        if not bag:
            continue
        fg = bag.get("free_gb")
        tg = bag.get("total_gb")
        if fg and str(fg).strip() and fg not in {"--", "—"}:
            row["free_gb"] = fg
        if tg and str(tg).strip() and tg not in {"--", "—"}:
            row["size_gb"] = tg

    for idx, row in enumerate(merged.values(), start=1):
        row["name"] = f"硬盘{idx}"
        items.append(row)
    return items


def _collect_patrol_payload(_cfg: Any, _state: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _cfg if _cfg is not None else _PatrolCfgEmpty()
    cpu_pct, mem_pct, disk_free_gb = _cpu_mem_disk()
    cpu_temp, disk_temp = _cpu_disk_temp_c()
    missing: List[str] = []

    log_hint_h, log_hint_v = _patrol_read_hostname_fnos_from_logger_db(cfg)
    hostname = _read_hostname(log_hint_h)
    lan_ip = _pick_lan_ip()
    wan_ip = _pick_wan_ip()
    system_version = _read_system_version()
    fnos_version = _read_fnos_version(log_hint_v)
    has_update = _read_update_status()
    boot_ts = psutil.boot_time() if psutil else 0.0
    uptime = _fmt_uptime(time.time() - boot_ts) if boot_ts > 0 else "--"
    startup_time = _fmt_boot_time(boot_ts) if boot_ts > 0 else "--"
    ups = _collect_ups_info()
    disks = _collect_disk_items()

    if hostname == "--":
        missing.append("主机名称")
    if lan_ip == "--":
        missing.append("内网IP")
    if wan_ip == "--":
        missing.append("外网IP")
    if system_version == "--":
        missing.append("系统版本")
    if fnos_version == "--":
        missing.append("飞牛版本")
    if uptime == "--":
        missing.append("运行时间")
    if startup_time == "--":
        missing.append("启动时间")
    if str(cpu_pct) == "—":
        missing.append("CPU使用率")
    if str(cpu_temp) == "—":
        missing.append("CPU温度")
    if str(mem_pct) == "—":
        missing.append("内存使用率")
    if not disks and _patrol_has_any_fn_vol_mount_in_container():
        missing.append("硬盘状态")
    if disks:
        if all(str(d.get("temp_c") or "--") in {"--", "—"} for d in disks):
            missing.append("硬盘温度")
        if all(str(d.get("status") or "--") in {"--", "—"} for d in disks):
            missing.append("硬盘健康状态")

    return {
        "hostname": hostname,
        "lan_ip": lan_ip,
        "wan_ip": wan_ip,
        "system_version": system_version,
        "fnos_version": fnos_version,
        "has_update": has_update,
        "uptime_text": uptime,
        "startup_time": startup_time,
        "ups": ups,
        "disks": disks,
        "cpu_percent": cpu_pct,
        "cpu_temp_c": cpu_temp,
        "mem_percent": mem_pct,
        "disk_free_gb": disk_free_gb,
        "disk_temp_c": disk_temp,
        "missing_fields": missing,
    }


def _send_patrol_notification(app: Any, logger: Optional[logging.Logger]) -> bool:
    cfg = app.config
    if not cfg or not getattr(cfg, "nas_patrol_enabled", False) or not app.notifier:
        return False
    payload = _collect_patrol_payload(cfg, {})
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw_log = json.dumps(payload, ensure_ascii=False)
    try:
        result = app.notifier.send_notification(
            event_type="NAS_PATROL_REPORT",
            event_data=payload,
            raw_log=raw_log,
            timestamp=ts,
        )
    except Exception as e:
        if logger:
            logger.error("NAS 巡检推送异常: %s", e, exc_info=True)
        return False

    if not getattr(result, "success", False):
        if logger:
            logger.warning("NAS 巡检推送未成功（渠道失败或未配置等）")
        return False
    return True


def nas_patrol_worker_loop(app: Any) -> None:
    log = logging.getLogger(__name__)
    log.info("NAS 定时巡检线程已启动")
    while app.running:
        try:
            cfg = app.config
            if not cfg or not getattr(cfg, "nas_patrol_enabled", False) or not app.notifier:
                time.sleep(30)
                continue
            try:
                minutes = int(getattr(cfg, "nas_patrol_interval_minutes", 720) or 720)
            except (TypeError, ValueError):
                minutes = 720
            minutes = max(5, min(10080, minutes))
            interval = minutes * 60
            sp = _state_path(getattr(cfg, "cursor_dir", "./data/cursor") or "./data/cursor")
            state = _load_state(sp)
            _normalize_patrol_state(state)
            if bool(state.get("patrol_anchor_done")) and float(state.get("last_success_ts") or 0) <= 0:
                state["patrol_anchor_done"] = False
                _save_state(sp, state)
            now = time.time()

            if not state.get("patrol_anchor_done"):
                state["patrol_anchor_done"] = True
                last_s = float(state.get("last_success_ts") or 0)
                if last_s <= 0:
                    state["last_success_ts"] = now
                    state["retry_until_ts"] = 0.0
                    state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                    _save_state(sp, state)
                    log.info(
                        "NAS 巡检已锚定首次周期，第一次采集将在约 %s 分钟后执行（不立即推送）",
                        minutes,
                    )
                    time.sleep(5)
                    continue
                _save_state(sp, state)

            retry_until = float(state.get("retry_until_ts") or 0)
            if now < retry_until:
                time.sleep(min(60.0, max(5.0, retry_until - now)))
                continue

            last_success = float(state.get("last_success_ts") or 0)
            if last_success <= 0 or (now - last_success) < interval:
                sleep_s = min(60.0, max(5.0, last_success + interval - now))
                time.sleep(sleep_s)
                continue

            ok = _send_patrol_notification(app, log)
            now_after = time.time()
            if ok:
                state["last_success_ts"] = now_after
                state["retry_until_ts"] = 0.0
                state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                log.info("NAS 巡检推送成功，下次约在 %s 分钟后", minutes)
            else:
                cur = int(state.get("retry_backoff_sec") or RETRY_BACKOFF_BASE_SEC)
                cur = max(RETRY_BACKOFF_BASE_SEC, min(cur, RETRY_BACKOFF_MAX_SEC))
                wait_sec = cur
                next_bo = min(cur * 2, RETRY_BACKOFF_MAX_SEC)
                state["retry_until_ts"] = now_after + float(wait_sec)
                state["retry_backoff_sec"] = next_bo
                log.warning(
                    "NAS 巡检将在 %s 秒后重试（指数退避，下次失败等待上限 %s 秒）",
                    wait_sec,
                    next_bo,
                )
            _save_state(sp, state)

        except Exception as e:
            log.error("NAS 巡检线程异常: %s", e, exc_info=True)
        time.sleep(30)


def start_nas_patrol_thread(app: Any) -> Optional[threading.Thread]:
    t = threading.Thread(target=nas_patrol_worker_loop, args=(app,), name="NasPatrol", daemon=True)
    t.start()
    return t
