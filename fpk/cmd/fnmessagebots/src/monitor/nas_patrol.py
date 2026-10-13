"""
NAS 定时巡检：按 Cron 表达式调度，采集本机 CPU/内存/磁盘状态并经已配置渠道推送。
首次启用仅锚定周期起点不立即推送；推送失败时按指数退避重试。
"""

from __future__ import annotations

import http.client
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
import ssl
import urllib.parse

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

from .sqlite_uri import connect_readonly_with_fallback

_STATE_FILENAME = "nas_patrol_state.json"

RETRY_BACKOFF_BASE_SEC = 300
RETRY_BACKOFF_MAX_SEC = 3600
# 单个巡检周期推送失败后的最大重试次数；用尽后放弃本周期，等下次计划时间
RETRY_MAX_ATTEMPTS = 2

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
    """优先使用默认路由上的活动网卡，桥接容器可显式指定 NAS_LAN_IP。"""
    override = (os.getenv("NAS_LAN_IP") or "").strip()
    if override:
        try:
            addr = ipaddress.IPv4Address(override)
            if not (addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast):
                return str(addr)
        except ValueError:
            pass
        logging.getLogger(__name__).warning("NAS_LAN_IP 不是有效的内网 IPv4，已忽略")
    if not psutil:
        return "--"
    try:
        interfaces = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        default_interface = _default_ipv4_interface()
        candidates: List[Tuple[int, int, str, str]] = []
        for name, addrs in interfaces.items():
            if re.match(r"^(lo$|docker|br-|virbr|veth|cni|flannel|tun|tap|wg|tailscale|zt)", name):
                continue
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if getattr(a, "family", None) != socket.AF_INET:
                    continue
                ip = str(getattr(a, "address", "") or "").strip()
                try:
                    obj = ipaddress.IPv4Address(ip)
                except ValueError:
                    continue
                if obj.is_loopback or obj.is_link_local or obj.is_unspecified or obj.is_multicast:
                    continue
                candidates.append((0 if name == default_interface else 1, 0 if obj.is_private else 1, name, ip))
        if candidates:
            return min(candidates)[3]
    except Exception as e:
        logging.getLogger(__name__).warning("内网 IP 检测失败: %s", e)
        return "--"
    return "--"


def _default_ipv4_interface() -> str:
    """读取 Linux 默认路由，多个出口时选 UP 且 metric 最低的一项。"""
    routes: List[Tuple[int, str]] = []
    try:
        for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 8 and parts[1] == "00000000" and int(parts[3], 16) & 1:
                routes.append((int(parts[6]), parts[0]))
    except (OSError, ValueError):
        pass
    return min(routes)[1] if routes else ""


def _http_get_ip(url: str, *, family: int, timeout: float = 3.0, _redirects: int = 2) -> str:
    """GET 公网 IP 接口，强制使用指定地址族（避免双栈误走另一族）。"""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host or parsed.scheme not in {"http", "https"}:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    last_err: Optional[BaseException] = None
    for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
        af, socktype, proto, _canon, sa = res
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(sa)
            if parsed.scheme == "https":
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {parsed.netloc}\r\n"
                f"User-Agent: FNMessageBots\r\n"
                f"Accept: text/plain\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(req)
            # 标准 HTTP 解析处理状态码和 chunked，避免把错误页/分块长度当成 IP。
            with http.client.HTTPResponse(sock) as response:
                response.begin()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if location and _redirects > 0:
                        return _http_get_ip(
                            urllib.parse.urljoin(url, location), family=family,
                            timeout=timeout, _redirects=_redirects - 1,
                        )
                    raise ValueError("公网 IP 接口重定向无效或次数过多")
                if response.status != 200:
                    raise ValueError(f"公网 IP 接口 HTTP {response.status}")
                body = response.read(4097)
                if len(body) > 4096:
                    raise ValueError("公网 IP 接口响应过长")
                return body.decode("utf-8", errors="strict").strip()
        except Exception as e:
            last_err = e
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    if last_err:
        raise last_err
    return ""


def _pick_wan_ip() -> str:
    """优先拿到 IPv4；仅当 IPv4 全部失败时才用 IPv6。

    避免偶发 IPv4 超时回退到 IPv6、下次又拿到 IPv4，导致通知在 v4/v6 间来回刷。
    """
    v4_urls = (
        "https://api4.ipify.org",
        "https://ipv4.icanhazip.com",
        "https://v4.ident.me",
        "https://ifconfig.me/ip",
        "https://ipinfo.io/ip",
    )
    # 两轮穷尽 IPv4，降低偶发超时误落到 IPv6 的概率
    for round_i in range(2):
        for u in v4_urls:
            try:
                text = _http_get_ip(u, family=socket.AF_INET, timeout=3.0)
                ip_obj = ipaddress.ip_address(text)
                if ip_obj.version == 4 and ip_obj.is_global and not ip_obj.is_multicast:
                    return str(ip_obj)
            except Exception as e:
                logging.getLogger(__name__).debug("公网 IPv4 查询失败 (%s): %s", u, e)
                continue
        if round_i == 0:
            time.sleep(0.5)

    v6_urls = (
        "https://api6.ipify.org",
        "https://ipv6.icanhazip.com",
        "https://v6.ident.me",
        "https://api64.ipify.org",
    )
    for u in v6_urls:
        try:
            text = _http_get_ip(u, family=socket.AF_INET6, timeout=3.0)
            ip_obj = ipaddress.ip_address(text)
            if ip_obj.version == 6 and ip_obj.is_global and not ip_obj.is_multicast:
                return str(ip_obj)
        except Exception as e:
            logging.getLogger(__name__).debug("公网 IPv6 查询失败 (%s): %s", u, e)
            continue
    logging.getLogger(__name__).warning("外网 IP 检测失败：所有 IPv4/IPv6 查询源均未返回有效公网地址")
    return "--"


def _read_system_version() -> str:
    """读取宿主机 OS；容器中缺少宿主挂载时不使用镜像自身的 Debian 版本。"""
    paths = ["/host/etc/os-release", "/rootfs/etc/os-release", "/mnt/host/etc/os-release"]
    if not _in_container():
        paths.extend(["/etc/os-release", "/usr/lib/os-release"])
    for filename in paths:
        try:
            values = {}
            for line in Path(filename).read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip().strip("\"'")
            version = values.get("PRETTY_NAME") or " ".join(
                v for v in (values.get("NAME"), values.get("VERSION_ID")) if v
            )
            if version:
                return version[:160]
        except (OSError, UnicodeError):
            continue
    logging.getLogger(__name__).warning("未读到宿主系统版本：Docker 请只读挂载 /etc/os-release 到 /host/etc/os-release")
    return "--"


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


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

    for rel in (
        "/etc/fnos-release", "/etc/fnos_version", "/usr/trim/etc/version",
        "/usr/trim/etc/fnos_version", "/usr/trim/VERSION", "/run/fnos/version",
    ):
        try:
            lines = Path(rel).read_text(encoding="utf-8").strip().splitlines()
            if lines:
                version = _sanitize_fnos_version_candidate(lines[0])
                if version:
                    return version
        except (OSError, UnicodeError):
            continue

    # 当前宿主的版本文件/包信息优先于日志；旧事件中的版本号可能已经过期。
    ver_host = _fnos_trim_version_from_host_dpkg_status()
    if ver_host:
        return _format_fnos_version_display(ver_host)
    if not _in_container():
        try:
            proc = subprocess.run(
                [_resolve_cmd("dpkg-query"), "-W", "-f=${Version}", "trim"],
                capture_output=True, text=True, timeout=3.0, check=False,
            )
            if proc.returncode == 0:
                version = _sanitize_fnos_version_candidate(proc.stdout.strip())
                if version:
                    return version
        except (OSError, subprocess.SubprocessError):
            pass

    version = _sanitize_fnos_version_candidate(logger_db_fnos)
    if version:
        logging.getLogger(__name__).warning("忽略历史日志中的飞牛版本，无法确认是否为当前版本: %s", version)
    logging.getLogger(__name__).warning("未读到飞牛版本：请检查开放 API、TRIM_SYS_VERSION 或宿主 dpkg status 挂载")
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
    if not _in_container():
        candidates += ("/var/lib/dpkg/status",)
    for rel in candidates:
        p = Path(rel)
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


def _smartctl_sat_retry_needed(block_path: str, output: str) -> bool:
    """仅 USB 桥接识别/透传失败时尝试 SAT，不把权限不足当成设备类型错误。"""
    low = (output or "").lower()
    if any(marker in low for marker in ("permission denied", "operation not permitted", "access denied")):
        return False
    if "unknown usb bridge" in low:
        return True
    if not any(marker in low for marker in (
        "please specify device type", "unable to detect device type",
        "unsupported field in scsi command", "unsupported scsi opcode",
        "read device identity failed",
    )):
        return False
    nb = _normalize_block_name(os.path.basename(block_path))
    if not _valid_sysfs_block_token(nb):
        return False
    try:
        device = Path(f"/sys/class/block/{nb}/device").resolve(strict=True)
        return any(re.fullmatch(r"usb\d+", part) for part in device.parts)
    except (OSError, RuntimeError):
        return False


def _run_smartctl(block_path: str, options: List[str], timeout: float) -> str:
    out = _run_cmd(["smartctl", *options, block_path], timeout=timeout)
    if _smartctl_sat_retry_needed(block_path, out):
        return _run_cmd(["smartctl", *options, "-d", "sat", block_path], timeout=timeout) or out
    return out


def _parse_smart_health_output(out: str) -> str:
    try:
        obj = json.loads(out)
        if isinstance(obj, dict):
            passed = (obj.get("smart_status") or {}).get("passed")
            if isinstance(passed, bool):
                return "健康" if passed else "异常"
            warning = (obj.get("nvme_smart_health_information_log") or {}).get("critical_warning")
            if warning is not None:
                value = int(warning, 0) if isinstance(warning, str) else int(warning)
                return "健康" if value == 0 else "异常"
    except (ValueError, TypeError, AttributeError):
        pass
    low = (out or "").lower()
    if re.search(r"(?:test result:\s*passed\b|smart health status:\s*ok\b)", low):
        return "健康"
    if re.search(r"(?:test result:\s*failed\b|smart health status:\s*(?:failed|bad)\b)", low):
        return "异常"
    match = re.search(r"critical_warning\s*[:=]\s*(0x[0-9a-f]+|\d+)", low)
    if match:
        raw = match.group(1)
        return "健康" if int(raw, 16 if raw.startswith("0x") else 10) == 0 else "异常"
    return "--"


def _smart_health_for_block_path(block_path: str) -> str:
    """对 ``/dev/...`` 绝对路径做 SMART 健康判断（含 mapper、分区）。"""
    bp = str(block_path or "").strip()
    if not bp.startswith("/dev/"):
        return "--"
    base = os.path.basename(bp)
    nb = _normalize_block_name(base) or base
    if not _valid_sysfs_block_token(nb) and not bp.startswith("/dev/mapper/"):
        return "--"
    out = _run_smartctl(bp, ["-H"], timeout=2.5)
    health = _parse_smart_health_output(out)
    if health != "--":
        return health

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
        ctrl = _nvme_controller_name(nb)
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

    return "--"


def _smart_health_for_disk(dev_base: str) -> str:
    if not dev_base:
        return "--"
    if str(dev_base).startswith("/dev/"):
        return _smart_health_for_block_path(dev_base)
    if not _valid_sysfs_block_token(dev_base):
        return "--"
    return _smart_health_for_block_path(f"/dev/{dev_base}")


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


def _celsius_ok(v: float) -> bool:
    return -40.0 <= float(v) <= 120.0


def _fmt_celsius(v: float) -> str:
    return f"{float(v):.1f}"


def _nvme_kelvin_or_celsius(v: int) -> Optional[float]:
    """NVMe 日志温度可能是摄氏或开尔文。"""
    try:
        x = int(v)
    except (TypeError, ValueError):
        return None
    if x > 200:
        x = x - 273
    if _celsius_ok(x):
        return float(x)
    return None


def _parse_celsius_from_smartctl_json(obj: Any) -> str:
    """与飞牛界面一致：优先复合温度 / Sensor1 / ATA-194 RAW，不用 Sensor2。"""
    if not isinstance(obj, dict):
        return "--"

    # 1) smartctl 顶层 temperature.current（通常等于飞牛主温度）
    temp_obj = obj.get("temperature")
    if isinstance(temp_obj, dict):
        if temp_obj.get("current") is not None:
            try:
                c = float(temp_obj.get("current"))
                if _celsius_ok(c):
                    return _fmt_celsius(c)
            except (TypeError, ValueError):
                pass
        # 若有 sensors 列表，只用第一个（Sensor 1）
        sensors = temp_obj.get("sensors") or temp_obj.get("temperature_sensors")
        if isinstance(sensors, list) and sensors:
            try:
                c = float(sensors[0])
                if c > 200:
                    c = c - 273.0
                if _celsius_ok(c):
                    return _fmt_celsius(c)
            except (TypeError, ValueError):
                pass
    elif temp_obj is not None:
        try:
            c = float(temp_obj)
            if _celsius_ok(c):
                return _fmt_celsius(c)
        except (TypeError, ValueError):
            pass

    # 2) NVMe SMART 健康日志：composite / temperature，再 sensor 1；明确跳过 sensor 2+
    nvme = obj.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        for k in (
            "temperature",
            "composite_temperature",
            "temperature_sensor_1",
            "Temperature Sensor 1",
        ):
            if nvme.get(k) is None:
                continue
            c = _nvme_kelvin_or_celsius(nvme.get(k))
            if c is not None:
                return _fmt_celsius(c)
        # 数组形式：仅取下标 0
        for k in ("temperature_sensors", "sensors"):
            arr = nvme.get(k)
            if isinstance(arr, list) and arr:
                c = _nvme_kelvin_or_celsius(arr[0])
                if c is not None:
                    return _fmt_celsius(c)

    # 3) ATA：优先 194 Temperature_Celsius 的 RAW，其次 190；绝不用归一化 value
    table = (obj.get("ata_smart_attributes") or {}).get("table")
    if isinstance(table, list):
        by_id: Dict[int, Any] = {}
        for attr in table:
            if not isinstance(attr, dict):
                continue
            try:
                aid = int(attr.get("id"))
            except (TypeError, ValueError):
                continue
            by_id[aid] = attr
        for aid in (194, 190):
            attr = by_id.get(aid)
            if not attr:
                continue
            raw = attr.get("raw")
            val = raw.get("value") if isinstance(raw, dict) else raw
            if val is None:
                continue
            try:
                v = int(str(val).split()[0])
                if v > 200:
                    v = v & 0xFF
                if _celsius_ok(v):
                    return _fmt_celsius(v)
            except (TypeError, ValueError):
                continue
    return "--"


def _parse_celsius_from_smartctl_text(out: str) -> str:
    """文本解析：先主 Temperature:，再 Sensor 1；忽略 Sensor 2+；ATA 用 RAW。"""
    main_temp: Optional[str] = None
    sensor1: Optional[str] = None
    ata194: Optional[str] = None
    ata190: Optional[str] = None

    for line in (out or "").splitlines():
        low = line.lower().strip()
        if not low:
            continue

        # 跳过统计/告警时间行
        if "comp. temperature time" in low or "temperature time:" in low:
            continue
        # 明确忽略 Sensor 2/3/...
        if re.match(r"temperature\s+sensor\s+[2-9]\d*\s*:", low):
            continue

        # 主温度：Temperature: 41 Celsius（不能写成 Temperature Sensor）
        if re.match(r"^temperature\s*:", low) or low.startswith("current drive temperature"):
            m = re.search(
                r"(?:^temperature|current drive temperature)\s*[:=]\s*(-?\d+)",
                low,
                flags=re.IGNORECASE,
            )
            if m:
                try:
                    v = int(m.group(1))
                    if _celsius_ok(v):
                        main_temp = _fmt_celsius(v)
                except (TypeError, ValueError):
                    pass
            continue

        # Sensor 1
        if re.match(r"temperature\s+sensor\s+1\s*:", low):
            m = re.search(r":\s*(-?\d+)", low)
            if m:
                try:
                    v = int(m.group(1))
                    if _celsius_ok(v):
                        sensor1 = _fmt_celsius(v)
                except (TypeError, ValueError):
                    pass
            continue

        # ATA 194 / 190：RAW 是第十列；WHEN_FAILED 可能是 In_the_past，不能只匹配 "-"。
        if "temperature_celsius" in low or re.match(r"^190\s+", low) or re.match(r"^194\s+", low):
            fields = line.split(None, 9)
            m = re.match(r"(-?\d+)(?:\s|\(|$)", fields[9]) if len(fields) == 10 else None
            if m:
                try:
                    v = int(m.group(1))
                    if _celsius_ok(v):
                        if "temperature_celsius" in low or re.match(r"^194\s+", low):
                            ata194 = _fmt_celsius(v)
                        else:
                            ata190 = _fmt_celsius(v)
                except (TypeError, ValueError):
                    pass
            continue

    if main_temp:
        return main_temp
    if sensor1:
        return sensor1
    if ata194:
        return ata194
    if ata190:
        return ata190
    return "--"


def _parse_smart_temperature_output(out: str) -> str:
    """兼容 JSON 和文本结果；采集与保存检查使用同样的温度解析。"""
    try:
        temp = _parse_celsius_from_smartctl_json(json.loads(out))
        if temp not in {"--", "—"}:
            return temp
    except (ValueError, TypeError):
        pass
    return _parse_celsius_from_smartctl_text(out)


def _sysfs_temp_path_sort_key(p: Path) -> Tuple[int, str]:
    """优先 temp1_input（对应 Sensor1/主温），再 temp2+。"""
    name = p.name.lower()
    m = re.match(r"temp(\d+)_input$", name)
    idx = int(m.group(1)) if m else 99
    # smart_log/temperature 视作主温度
    if name == "temperature":
        idx = 0
    return (idx, str(p))


def _nvme_controller_name(block_name: str) -> str:
    match = re.fullmatch(r"(nvme\d+)(?:n\d+)?(?:p\d+)?", block_name)
    return match.group(1) if match else ""


def _disk_hwmon_dirs(nb: str) -> List[Path]:
    """用 sysfs 设备链接绑定传感器，禁止仅按 nvme/drivetemp 的通用名称匹配。"""
    if not _valid_sysfs_block_token(nb):
        return []
    device_paths = [Path(f"/sys/class/block/{nb}"), Path(f"/sys/class/block/{nb}/device")]
    ctrl = _nvme_controller_name(nb)
    if ctrl:
        device_paths.extend([Path(f"/sys/class/nvme/{ctrl}"), Path(f"/sys/class/nvme/{ctrl}/device")])
    roots: Set[Path] = set()
    for device in device_paths:
        try:
            roots.add(device.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    matches: Set[Path] = set()
    for root in roots:
        for parent in (root, root / "hwmon"):
            for hw in _safe_glob(parent, "hwmon*"):
                if re.fullmatch(r"hwmon\d+", hw.name):
                    matches.add(hw)
    for hw in _safe_glob(Path("/sys/class/hwmon"), "hwmon*"):
        try:
            device = (hw / "device").resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if any(device == root or root in device.parents for root in roots):
            matches.add(hw)
    return sorted(matches, key=str)


def _hwmon_temp1_for_block(nb: str) -> str:
    for hw in _disk_hwmon_dirs(nb):
        for path in (hw / "temp1_input", hw / "device" / "temp1_input"):
            try:
                c = int(path.read_text(encoding="utf-8").strip()) / 1000.0
                if _celsius_ok(c):
                    # 保留飞牛主温度取整的展示方式。
                    return str(int(c))
            except (OSError, ValueError):
                continue
    return ""


def _smart_temp_for_block_path(block_path: str) -> str:
    bp = str(block_path or "").strip()
    if not bp.startswith("/dev/"):
        return "--"
    base = os.path.basename(bp)
    nb = _normalize_block_name(base) or base

    # 0) 与飞牛系统显示一致：设备所属控制器 hwmon 的 temp1_input（毫摄氏度取整）
    hw = _hwmon_temp1_for_block(nb)
    if hw:
        return hw

    # 1) smartctl JSON（避免文本列对齐误解析）
    out_json = _run_smartctl(bp, ["-A", "-j"], timeout=3.0)
    if out_json:
        parsed = _parse_smart_temperature_output(out_json)
        if parsed not in {"--", "—"}:
            return parsed

    # 2) smartctl 文本
    out = _run_smartctl(bp, ["-A"], timeout=2.5)
    if out:
        parsed = _parse_celsius_from_smartctl_text(out)
        if parsed not in {"--", "—"}:
            return parsed

    # 3) nvme-cli：主温度 / sensor1，不用 sensor2
    if "nvme" in nb.lower():
        out_nvme_json = _run_cmd(["nvme", "smart-log", "-o", "json", bp], timeout=2.5)
        if out_nvme_json:
            try:
                obj = json.loads(out_nvme_json)
                if isinstance(obj, dict):
                    for k in (
                        "temperature",
                        "composite_temperature",
                        "temperature_sensor_1",
                        "temp",
                    ):
                        if obj.get(k) is None:
                            continue
                        c = _nvme_kelvin_or_celsius(obj.get(k))
                        if c is not None:
                            return _fmt_celsius(c)
                    for k in ("temperature_sensors", "sensors"):
                        arr = obj.get(k)
                        if isinstance(arr, list) and arr:
                            c = _nvme_kelvin_or_celsius(arr[0])
                            if c is not None:
                                return _fmt_celsius(c)
            except Exception:
                pass
        out_nvme = _run_cmd(["nvme", "smart-log", bp], timeout=2.5)
        if out_nvme:
            # 只要第一行 temperature:，不要 Temperature Sensor 2
            for line in out_nvme.splitlines():
                low = line.lower().strip()
                if re.match(r"temperature\s+sensor\s+[2-9]", low):
                    continue
                if re.match(r"^temperature\s*:", low) or re.match(r"temperature\s+sensor\s+1\s*:", low):
                    m = re.search(r":\s*(-?\d+)", low)
                    if m:
                        c = _nvme_kelvin_or_celsius(int(m.group(1)))
                        if c is not None:
                            return _fmt_celsius(c)

    # hwmon 已在最前面按设备映射读取；这里只尝试该 NVMe 控制器的专用节点。
    ctrl = _nvme_controller_name(nb)
    if ctrl:
        try:
            raw = Path(f"/sys/class/nvme/{ctrl}/smart_log/temperature").read_text(encoding="utf-8")
            c = _nvme_kelvin_or_celsius(int(raw.strip()))
            if c is not None:
                return _fmt_celsius(c)
        except (OSError, ValueError):
            pass
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
    """列出系统可见物理整盘（sda / nvme0n1 等），用于按盘而非按卷统计。"""
    names: List[str] = []
    # lsblk TYPE=disk 优先（更贴近 smartctl 枚举）
    out = _run_cmd(["lsblk", "-dnro", "NAME,TYPE"], timeout=3.0)
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1].lower() != "disk":
            continue
        name = _normalize_block_name(parts[0])
        if name and _patrol_is_whole_disk_name(name):
            names.append(name)

    sys_block = Path("/sys/block")
    try:
        for p in sorted(sys_block.iterdir(), key=lambda x: x.name):
            name = _normalize_block_name(p.name)
            if name and _patrol_is_whole_disk_name(name):
                names.append(name)
    except Exception:
        pass
    return _dedupe_block_names(names)


def _disk_size_gb_for_disk(dev_base: str) -> str:
    """读取整盘容量，不使用分区或文件系统容量代替；保留两位小数。"""
    dev = str(dev_base or "").strip()
    if not dev or not _valid_sysfs_block_token(dev):
        return "--"
    sys_size = Path(f"/sys/block/{dev}/size")
    try:
        sectors = int(sys_size.read_text(encoding="utf-8", errors="ignore").strip())
        if sectors > 0:
            return f"{sectors * 512 / (1024**3):.2f}"
    except Exception:
        pass
    out = _run_cmd(["lsblk", "-b", "-dnro", "SIZE", f"/dev/{dev}"], timeout=2.0)
    for line in (out or "").splitlines():
        raw = line.strip()
        if not raw or not raw.isdigit():
            continue
        size = int(raw)
        if size > 0:
            return f"{size / (1024**3):.2f}"
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
    free_s, _ = _patrol_space_gb_via_findmnt_for_disk(dev_base)
    return free_s


def _patrol_space_gb_via_findmnt_for_disk(dev_base: str) -> Tuple[str, str]:
    """对整盘各分区 findmnt，返回 (剩余GB, 文件系统总容量GB)。"""
    best_free: Optional[float] = None
    best_total: Optional[float] = None
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
            free_s, tot_s = _patrol_df_space_gb_pair(tgt)
            if free_s in {"--", "—"} or tot_s in {"--", "—"}:
                continue
            try:
                fv = float(free_s)
                tv = float(tot_s)
            except (TypeError, ValueError):
                continue
            if fv < 0 or tv <= 0:
                continue
            if best_free is None or fv > best_free:
                best_free = fv
                best_total = tv
    if best_free is not None and best_total is not None:
        return f"{best_free:.1f}", f"{best_total:.1f}"
    return "--", "--"


def _patrol_vol_bag_for_physical(
    physical: str,
    vol_space_by_physical: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[Dict[str, str]]:
    """按物理整盘名取 /vol 空间；扫描表 miss 时反向遍历可见 /volN。"""
    pl = str(physical or "").strip().lower()
    if not pl:
        return None
    vol_space = vol_space_by_physical or {}
    hit = vol_space.get(pl)
    if hit:
        fg = str(hit.get("free_gb") or "").strip()
        tg = str(hit.get("total_gb") or "").strip()
        if fg and tg and fg not in {"--", "—"} and tg not in {"--", "—"}:
            return hit

    for vol in _patrol_visible_fn_vol_mount_roots():
        if _patrol_mount_is_file_bind_noise(vol):
            continue
        try:
            if not Path(vol).exists():
                continue
        except OSError:
            continue
        src = _patrol_mount_source_for_path(vol)
        if not src:
            continue
        rdev = src.strip() if str(src).startswith("/dev/") else _patrol_resolve_psutil_device(src)
        if not rdev.startswith("/dev/"):
            continue
        try:
            rdev_open = os.path.realpath(_patrol_block_dev_for_inspection(rdev))
        except OSError:
            rdev_open = _patrol_block_dev_for_inspection(rdev)
        names = {x.lower() for x in _resolve_physical_disk_names(rdev_open)}
        walked = _walk_lsblk_to_physical_base(rdev_open)
        if walked:
            names.add(walked.lower())
        if pl not in names:
            continue
        free_s, tot_s = _patrol_df_space_gb_pair(vol)
        if free_s in {"--", "—"} or tot_s in {"--", "—"}:
            if psutil:
                try:
                    u = psutil.disk_usage(vol)
                    if u.total > 0:
                        tot_s = f"{u.total / (1024**3):.1f}"
                        free_s = f"{u.free / (1024**3):.1f}"
                except Exception:
                    pass
        if free_s not in {"--", "—"} and tot_s not in {"--", "—"}:
            return {"free_gb": free_s, "total_gb": tot_s}
    return None


def _patrol_disk_space_gb_for_row(
    mount: str,
    physical: str,
    vol_space_by_physical: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[str, str]:
    """关联存储卷的 (剩余GB, 文件系统总容量GB)，不混入物理整盘容量。"""
    phy = str(physical or "").strip()
    m0 = str(mount or "").strip()

    bag = _patrol_vol_bag_for_physical(phy, vol_space_by_physical)
    if bag:
        return str(bag.get("free_gb") or "--"), str(bag.get("total_gb") or "--")

    if not m0 and phy:
        m0 = _patrol_best_mount_for_physical(phy)
    if m0 and not _patrol_mount_is_file_bind_noise(m0):
        free_s, tot_s = _patrol_df_space_gb_pair(m0)
        if free_s not in {"--", "—"} and tot_s not in {"--", "—"}:
            return free_s, tot_s

    if phy:
        fm_free, fm_tot = _patrol_space_gb_via_findmnt_for_disk(phy)
        if fm_free not in {"--", "—"} and fm_tot not in {"--", "—"}:
            return fm_free, fm_tot

        free_s = _patrol_disk_free_gb_for_row(m0, phy, vol_space_by_physical)
        if free_s not in {"--", "—"}:
            return free_s, "--"

    return "--", "--"


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
        walked = _walk_lsblk_to_physical_base(dev_open)
        if walked:
            names = list(dict.fromkeys(list(names or []) + [walked]))
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
    """从 psutil 传感器中按设备名提取温度（如 nvme0n1/sda/sdb）。

    与飞牛一致：优先 Composite / Sensor 1 / temp1，跳过 Sensor 2+。
    """
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
            if fv > 200:
                fv = fv - 273.15
            if not _celsius_ok(fv):
                return "--"
            return _fmt_celsius(fv)
        except Exception:
            return "--"

    def _label_rank(label: str) -> int:
        low = (label or "").lower()
        if re.search(r"sensor\s*[2-9]", low) or re.search(r"temp\s*[2-9]", low):
            return 99
        if "composite" in low or low in {"", "temp1", "temp", "temperature"}:
            return 0
        if "sensor 1" in low or "sensor1" in low or "temp1" in low:
            return 1
        return 5

    # psutil 会把多个盘的同名 chip 合并；仅接收带明确设备/控制器标识的条目。
    tokens = [dev]
    ctrl = _nvme_controller_name(dev)
    if ctrl:
        tokens.append(ctrl)
    candidates: List[Tuple[int, str]] = []
    for chip, entries in temps.items():
        for entry in entries or []:
            label = str(getattr(entry, "label", "") or "")
            identity = f"{chip} {label}".lower()
            if not any(re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", identity) for token in tokens):
                continue
            rank = _label_rank(label)
            if rank >= 99:
                continue
            value = _to_temp(getattr(entry, "current", None))
            if value != "--":
                candidates.append((rank, value))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
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

    health_reason = hr_fail if health in {"--", "—"} else ""
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
    """按物理整盘（sda / nvme0n1…）生成巡检行，不再按 /volN 卷数分行。

    容量/温度/健康对整盘路径探测；存储卷的剩余空间及总容量从关联挂载反查。
    """
    items: List[Dict[str, str]] = []
    vol_space_by_physical = _patrol_scan_vol_space_by_physical()
    whole_disks = _patrol_list_visible_whole_disks()

    def _sort_disk_key(name: str) -> Tuple[int, str]:
        n = str(name or "").lower()
        if n.startswith("nvme"):
            return (0, n)
        if n.startswith("sd"):
            return (1, n)
        return (2, n)

    for physical in sorted(whole_disks, key=_sort_disk_key):
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
        free_s, filesystem_size = _patrol_disk_space_gb_for_row(m_pick, physical, vol_space_by_physical)
        disk_size = _disk_size_gb_for_disk(physical)
        items.append(
            {
                "name": "",
                "device": physical,
                "mount_point": m_pick,
                "free_gb": free_s if free_s else "--",
                "size_gb": disk_size if disk_size else "--",
                "filesystem_size_gb": filesystem_size if filesystem_size else "--",
                "temp_c": temp,
                "status": health,
                "temp_reason": temp_reason,
                "status_reason": health_reason,
            }
        )

    for idx, row in enumerate(items, start=1):
        row["name"] = f"硬盘{idx}"
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
    # 汇总盘温：取已采集物理盘温度的最高值（比单一传感器回退更贴近实况）
    disk_temps: List[float] = []
    for d in disks:
        raw_t = str(d.get("temp_c") or "").strip()
        if raw_t in {"", "--", "—"}:
            continue
        try:
            disk_temps.append(float(raw_t))
        except ValueError:
            continue
    if disk_temps:
        mx = max(disk_temps)
        disk_temp = str(int(mx)) if mx.is_integer() else f"{mx:.1f}"

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
    if not disks and Path("/sys/block").is_dir():
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


def _send_patrol_notification(
    app: Any,
    logger: Optional[logging.Logger],
    *,
    require_enabled: bool = True,
) -> bool:
    cfg = app.config
    if not cfg:
        if logger:
            logger.warning("NAS 巡检跳过：配置未加载")
        return False
    if require_enabled and not getattr(cfg, "nas_patrol_enabled", False):
        if logger:
            logger.warning("NAS 巡检跳过：未开启巡检任务")
        return False
    if not app.notifier:
        if logger:
            logger.warning("NAS 巡检跳过：未配置推送渠道（notifier 为空）")
        return False
    payload = _collect_patrol_payload(cfg, {})
    # 巡检触发时顺带检测外网 IP（仅用户勾选 WAN_IP_CHANGED 时生效）
    try:
        from monitor.wan_ip_monitor import check_and_notify_wan_ip_change

        reported_ip = check_and_notify_wan_ip_change(
            app,
            source="patrol",
            known_ip=str(payload.get("wan_ip") or ""),
        )
        if reported_ip:
            payload["wan_ip"] = reported_ip
            if "外网IP" in payload.get("missing_fields", []):
                payload["missing_fields"].remove("外网IP")
    except Exception as e:
        if logger:
            logger.warning("巡检触发外网 IP 检测失败: %s", e)
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


def run_nas_patrol_now(app: Any) -> tuple[bool, str]:
    """立即执行一次巡检推送（供 Web 手动触发；不要求已到 Cron 点）。"""
    log = logging.getLogger(__name__)
    if not app:
        return False, "运行时未就绪"
    if not getattr(app, "notifier", None):
        return False, "未配置推送渠道，请先保存至少一个推送渠道。"
    cfg = getattr(app, "config", None)
    enabled = bool(cfg and getattr(cfg, "nas_patrol_enabled", False))
    print("NAS 巡检：收到立即执行请求", flush=True)
    ok = _send_patrol_notification(app, log, require_enabled=False)
    if ok:
        try:
            if cfg:
                sp = _state_path(getattr(cfg, "cursor_dir", "./data/cursor") or "./data/cursor")
                state = _load_state(sp)
                _normalize_patrol_state(state)
                state["patrol_anchor_done"] = True
                state["last_success_ts"] = time.time()
                state["retry_until_ts"] = 0.0
                state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                state["retry_count"] = 0
                _save_state(sp, state)
        except Exception as e:
            log.warning("手动巡检成功但更新状态失败: %s", e)
        if enabled:
            msg = "巡检推送已发送。定时任务将按 Cron 在下次触发点继续执行。"
        else:
            msg = (
                "巡检推送已发送，但未勾选「巡检任务」：定时 Cron 不会自动执行。"
                "请勾选并保存配置。"
            )
        print(f"NAS 巡检：{msg}", flush=True)
        return True, msg
    return False, "巡检推送失败，请查看容器日志与推送记录。"


def nas_patrol_worker_loop(app: Any) -> None:
    log = logging.getLogger(__name__)
    log.info("NAS 定时巡检线程已启动")
    try:
        from utils.cron_util import croniter_available

        if not croniter_available():
            print(
                "NAS 巡检：未检测到 croniter，将使用内置 Cron 回退实现（*/10 等仍可调度）",
                flush=True,
            )
    except Exception:
        pass
    _last_wait_due = 0.0
    _last_idle_msg = ""
    while app.running:
        try:
            cfg = app.config
            if not cfg or not getattr(cfg, "nas_patrol_enabled", False) or not app.notifier:
                reason = []
                if not cfg:
                    reason.append("配置未加载")
                elif not getattr(cfg, "nas_patrol_enabled", False):
                    reason.append("巡检未开启")
                if not getattr(app, "notifier", None):
                    reason.append("无推送渠道")
                msg = "NAS 巡检空闲：" + ("、".join(reason) if reason else "条件未满足")
                # 状态不变只记一次
                if msg != _last_idle_msg:
                    log.info(msg)
                    _last_idle_msg = msg
                time.sleep(30)
                continue
            _last_idle_msg = ""

            from utils.cron_util import next_cron_timestamp, resolve_nas_patrol_cron

            try:
                cron_expr = resolve_nas_patrol_cron(
                    getattr(cfg, "nas_patrol_cron", "") or "",
                    getattr(cfg, "nas_patrol_interval_minutes", 720),
                )
            except Exception as e:
                msg = f"NAS 巡检 Cron 配置无效: {e}"
                log.error(msg)
                time.sleep(60)
                continue

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
                    try:
                        nxt = next_cron_timestamp(cron_expr, now)
                        wait_min = max(0.0, (nxt - now) / 60.0)
                        nxt_str = datetime.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:
                        wait_min = 0.0
                        nxt_str = f"（计算失败: {e}）"
                    msg = (
                        f"NAS 巡检已锚定首次周期（Cron={cron_expr}），"
                        f"第一次采集约在 {wait_min:.1f} 分钟后（{nxt_str}，不立即推送）；"
                        f"可在 Web 点「立即巡检一次」马上验证"
                    )
                    log.info(msg)
                    time.sleep(5)
                    continue
                _save_state(sp, state)

            retry_until = float(state.get("retry_until_ts") or 0)
            if now < retry_until:
                time.sleep(min(60.0, max(5.0, retry_until - now)))
                continue

            last_success = float(state.get("last_success_ts") or 0)
            # 状态异常：上次成功落在未来 → 重置，避免干等数小时
            if last_success > now + 60:
                msg = (
                    "NAS 巡检状态异常：last_success_ts 在未来（"
                    f"{datetime.fromtimestamp(last_success).strftime('%Y-%m-%d %H:%M:%S')}），"
                    "已重置为当前时间"
                )
                log.warning(msg)
                last_success = now
                state["last_success_ts"] = now
                _save_state(sp, state)

            try:
                next_ts = next_cron_timestamp(
                    cron_expr, last_success if last_success > 0 else now
                )
            except Exception as e:
                msg = f"NAS 巡检 Cron 无效 ({cron_expr}): {e}"
                log.error(msg)
                time.sleep(60)
                continue

            due = now >= next_ts
            if not due:
                due_at = next_ts
                sleep_s = min(60.0, max(5.0, due_at - now))
                # 下次触发时间不变只记一次
                if abs(due_at - _last_wait_due) > 1:
                    nxt_str = datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M:%S")
                    log.info("NAS 巡检下次触发：%s（Cron=%s）", nxt_str, cron_expr)
                    _last_wait_due = due_at
                time.sleep(sleep_s)
                continue

            log.info("NAS 巡检开始采集并推送（Cron=%s）", cron_expr)
            ok = _send_patrol_notification(app, log)
            now_after = time.time()
            if ok:
                state["last_success_ts"] = now_after
                state["retry_until_ts"] = 0.0
                state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                state["retry_count"] = 0
                log.info("NAS 巡检推送成功")
            else:
                attempts = int(state.get("retry_count") or 0) + 1
                if attempts > RETRY_MAX_ATTEMPTS:
                    state["last_success_ts"] = now_after
                    state["retry_until_ts"] = 0.0
                    state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                    state["retry_count"] = 0
                    log.warning("NAS 巡检推送连续失败 %s 次，放弃本周期，等待下次计划时间", attempts)
                else:
                    cur = int(state.get("retry_backoff_sec") or RETRY_BACKOFF_BASE_SEC)
                    cur = max(RETRY_BACKOFF_BASE_SEC, min(cur, RETRY_BACKOFF_MAX_SEC))
                    state["retry_until_ts"] = now_after + float(cur)
                    state["retry_backoff_sec"] = min(cur * 2, RETRY_BACKOFF_MAX_SEC)
                    state["retry_count"] = attempts
                    log.warning(
                        "NAS 巡检推送失败，%s 秒后重试（%s/%s）", cur, attempts, RETRY_MAX_ATTEMPTS
                    )
            _save_state(sp, state)

        except Exception as e:
            log.error("NAS 巡检线程异常: %s", e, exc_info=True)
        time.sleep(30)


def start_nas_patrol_thread(app: Any) -> Optional[threading.Thread]:
    t = threading.Thread(target=nas_patrol_worker_loop, args=(app,), name="NasPatrol", daemon=True)
    t.start()
    return t
