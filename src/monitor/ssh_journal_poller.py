"""
从宿主机 systemd journal 轮询 SSH 登录相关日志。

飞牛新版本 eventlogger 往往不再写入 Sshd*，且 /var/log/auth.log 已取消；
改用 ``journalctl -u ssh -u sshd`` 增量采集，映射到现有 SSH_* 事件。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .models import JournalEntry
from utils.access_guidance import journal_permission_action

SSH_LOGIN_SUCCESS = "SSH_LOGIN_SUCCESS"
SSH_AUTH_FAILED = "SSH_AUTH_FAILED"
SSH_INVALID_USER = "SSH_INVALID_USER"
SSH_DISCONNECTED = "SSH_DISCONNECTED"
# systemd 启停 ssh/sshd.service（飞牛控制台开关 SSH 服务）
SSH_ENABLED = "SSH_ENABLED"
SSH_DISABLED = "SSH_DISABLED"
# 启/停成对：勾选任一即同时推送开启与关闭
SSH_SERVICE_SWITCH_EVENTS: Set[str] = {SSH_ENABLED, SSH_DISABLED}

SSH_JOURNAL_EVENTS: Set[str] = {
    SSH_LOGIN_SUCCESS,
    SSH_AUTH_FAILED,
    SSH_INVALID_USER,
    SSH_DISCONNECTED,
    SSH_ENABLED,
    SSH_DISABLED,
}

# systemd catalog MESSAGE_ID（与文案语言无关）
_SYSTEMD_UNIT_STARTED_MSG_ID = "39f53479d3a045ac8e11786248231fbf"
_SYSTEMD_UNIT_STOPPED_MSG_ID = "9d1aaa27d60140bd96365438aad20286"

_STATE_FILENAME = "ssh_journal_poller_state.json"

# 「忽略本机回环 / 忽略局域网」只静默这两类；失败 / 无效用户仍推送
SSH_SOURCE_QUIET_EVENTS = frozenset({SSH_LOGIN_SUCCESS, SSH_DISCONNECTED})
# 兼容旧名
SSH_LOOPBACK_QUIET_EVENTS = SSH_SOURCE_QUIET_EVENTS


def _parse_ip(ip: str):
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped if mapped is not None else addr


def is_loopback_ip(ip: str) -> bool:
    addr = _parse_ip(ip)
    return bool(addr is not None and addr.is_loopback)


def is_lan_ip(ip: str) -> bool:
    """
    局域网/私网地址（不含回环）。
    覆盖 10/8、172.16/12、192.168/16 及 IPv6 ULA 等；回环仍由 ignore_loopback 单独控制。
    """
    addr = _parse_ip(ip)
    if addr is None or addr.is_loopback:
        return False
    return bool(addr.is_private)


def should_ignore_loopback_ssh(event_type: str, event_data: Dict[str, Any]) -> bool:
    if event_type not in SSH_SOURCE_QUIET_EVENTS:
        return False
    return is_loopback_ip(str(event_data.get("IP") or event_data.get("ip") or ""))


def should_ignore_lan_ssh(event_type: str, event_data: Dict[str, Any]) -> bool:
    if event_type not in SSH_SOURCE_QUIET_EVENTS:
        return False
    return is_lan_ip(str(event_data.get("IP") or event_data.get("ip") or ""))


def should_ignore_ssh_by_source(
    event_type: str,
    event_data: Dict[str, Any],
    *,
    ignore_loopback: bool = False,
    ignore_lan: bool = False,
) -> bool:
    if ignore_loopback and should_ignore_loopback_ssh(event_type, event_data):
        return True
    if ignore_lan and should_ignore_lan_ssh(event_type, event_data):
        return True
    return False

# Accepted publickey/password/keyboard-interactive for user from ip port N ssh2
_RE_ACCEPTED = re.compile(
    r"Accepted\s+\S+\s+for\s+(\S+)\s+from\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)
_RE_FAILED = re.compile(
    r"Failed\s+password\s+for\s+(?:invalid\s+user\s+)?(\S+)\s+from\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)
_RE_INVALID = re.compile(
    r"Invalid\s+user\s+(\S+)\s+from\s+(\S+)(?:\s+port\s+\d+)?",
    re.IGNORECASE,
)
_RE_DISCONNECT = re.compile(
    r"Disconnected\s+from(?:\s+user)?\s+(\S+)\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)
# Disconnected from authenticating user X IP port N
_RE_DISCONNECT_AUTH = re.compile(
    r"Disconnected\s+from\s+authenticating\s+user\s+(\S+)\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)


def journalctl_available() -> bool:
    return bool(shutil.which("journalctl"))


# systemd：匹配 Started/Stopped（忽略 Starting/Stopping 过渡态）
# 兼容：unit 名形式、Description 形式、中文「已启动/已停止」
_RE_SSH_UNIT_STARTED = re.compile(
    r"(?:^|\b)(?:Started|已启动)\s+ssh(?:d)?\.service\b",
    re.IGNORECASE,
)
_RE_SSH_UNIT_STOPPED = re.compile(
    r"(?:^|\b)(?:Stopped|已停止)\s+ssh(?:d)?\.service\b",
    re.IGNORECASE,
)
_RE_SSH_DESC_STARTED = re.compile(
    r"(?:^|\b)(?:Started|已启动)\s+OpenBSD\s+Secure\s+Shell\s+server\b",
    re.IGNORECASE,
)
_RE_SSH_DESC_STOPPED = re.compile(
    r"(?:^|\b)(?:Stopped|已停止)\s+OpenBSD\s+Secure\s+Shell\s+server\b",
    re.IGNORECASE,
)
# 过渡态 Stopping：部分环境只见到 Stopping / signal 15，没有最终 Stopped 行
_RE_SSH_UNIT_STOPPING = re.compile(
    r"(?:^|\b)(?:Stopping|正在停止)\s+ssh(?:d)?\.service\b",
    re.IGNORECASE,
)
_RE_SSH_DESC_STOPPING = re.compile(
    r"(?:^|\b)(?:Stopping|正在停止)\s+OpenBSD\s+Secure\s+Shell\s+server\b",
    re.IGNORECASE,
)
_RE_SSH_SIGNAL_TERM = re.compile(
    r"Received\s+signal\s+15;\s*terminating",
    re.IGNORECASE,
)
# systemd Stopping 作业开始
_SYSTEMD_UNIT_STOPPING_MSG_ID = "de2e677fb0254e519c4d6fbfb0c6b8bd"


def _ssh_unit_from_text(*parts: str) -> str:
    blob = " ".join(p for p in parts if p).lower()
    return "sshd" if "sshd" in blob else "ssh"


def _coerce_journal_message(message: Any) -> str:
    """journalctl JSON 的 MESSAGE 可能是 str，或字节数组 list[int]。"""
    if message is None:
        return ""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        if not message:
            return ""
        if all(isinstance(x, int) for x in message):
            try:
                return bytes(message).decode("utf-8", errors="replace").strip()
            except Exception:
                return "".join(chr(x) for x in message if isinstance(x, int) and 0 <= x < 256).strip()
        return "".join(str(x) for x in message).strip()
    return str(message).strip()


def parse_ssh_journal_message(message: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    解析单行 sshd/ssh.service MESSAGE，返回 (event_type, event_data) 或 None（忽略噪声/无关行）。
    """
    msg = _coerce_journal_message(message)
    if not msg:
        return None
    # 扫端口 / 非 SSH 协议探测，不推送
    low = msg.lower()
    if "kex_exchange_identification" in low or "invalid protocol identifier" in low:
        return None
    if "banner exchange" in low and "invalid format" in low:
        return None
    # pam 细节交给 Failed password / Accepted 行，避免双推
    if "pam_unix(sshd:auth)" in low or "pam_winbind(sshd:auth)" in low:
        return None
    if "pam_unix(sshd:session): session opened" in low:
        return None

    # SSH 服务开关（控制台启停 / systemctl start|stop ssh）
    if _RE_SSH_UNIT_STARTED.search(msg) or _RE_SSH_DESC_STARTED.search(msg):
        return SSH_ENABLED, {"from": "systemd", "unit": _ssh_unit_from_text(msg)}
    if (
        _RE_SSH_UNIT_STOPPED.search(msg)
        or _RE_SSH_DESC_STOPPED.search(msg)
        or _RE_SSH_UNIT_STOPPING.search(msg)
        or _RE_SSH_DESC_STOPPING.search(msg)
        or _RE_SSH_SIGNAL_TERM.search(msg)
    ):
        return SSH_DISABLED, {"from": "systemd", "unit": _ssh_unit_from_text(msg)}

    m = _RE_ACCEPTED.search(msg)
    if m:
        return SSH_LOGIN_SUCCESS, {"user": m.group(1), "IP": m.group(2)}

    m = _RE_INVALID.search(msg)
    if m:
        return SSH_INVALID_USER, {"user": m.group(1), "IP": m.group(2)}

    m = _RE_FAILED.search(msg)
    if m:
        user = m.group(1)
        ip = m.group(2)
        # "Failed password for invalid user xxx" 已由 Invalid user 覆盖；此处 invalid 前缀再判一次
        if re.search(r"Failed\s+password\s+for\s+invalid\s+user\s+", msg, re.I):
            return SSH_INVALID_USER, {"user": user, "IP": ip}
        return SSH_AUTH_FAILED, {"user": user, "IP": ip}

    m = _RE_DISCONNECT_AUTH.search(msg) or _RE_DISCONNECT.search(msg)
    if m:
        return SSH_DISCONNECTED, {"user": m.group(1), "IP": m.group(2)}

    # pam「session closed」行不带 IP，同一次断开已由上方 Disconnected 行推送，不再单独解析
    return None


def parse_ssh_journal_entry(obj: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """解析 journal JSON 条目：先 MESSAGE，再回退 systemd MESSAGE_ID（与语言无关）。"""
    msg = _coerce_journal_message(obj.get("MESSAGE"))
    parsed = parse_ssh_journal_message(msg)
    if parsed:
        return parsed

    mid = str(obj.get("MESSAGE_ID") or "").strip().lower()
    unit_field = str(obj.get("UNIT") or obj.get("_SYSTEMD_UNIT") or "").strip()
    # 本 poller 仅查 -u ssh/sshd；若带 UNIT 则再确认，避免误伤
    if unit_field and "ssh" not in unit_field.lower():
        return None
    unit = _ssh_unit_from_text(unit_field, msg)
    if mid == _SYSTEMD_UNIT_STARTED_MSG_ID:
        return SSH_ENABLED, {"from": "systemd", "unit": unit, "via": "message_id"}
    if mid in (_SYSTEMD_UNIT_STOPPED_MSG_ID, _SYSTEMD_UNIT_STOPPING_MSG_ID):
        return SSH_DISABLED, {"from": "systemd", "unit": unit, "via": "message_id"}
    return None


class SshJournalPoller:
    """轮询 journalctl 增量，推送 SSH_* 事件。"""

    def __init__(
        self,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
        journal_units: Optional[List[str]] = None,
        ignore_loopback: bool = False,
        ignore_lan: bool = False,
    ):
        self.cursor_dir = Path(cursor_dir)
        self.ignore_loopback = bool(ignore_loopback)
        self.ignore_lan = bool(ignore_lan)
        self.poll_interval = max(1, int(poll_interval or 5))
        self.monitor_events = set(monitor_events or [])
        self.journal_units = list(journal_units or ["ssh", "sshd"])
        self.event_handlers: Dict[str, Callable] = {}
        # SSH 服务启停钩子：不论是否勾选 SSH_ENABLED/DISABLED 都会回调（供开启超时提醒等）
        self._service_hooks: List[Callable[[str, Dict[str, Any]], None]] = []
        self.poll_batch_summary_enabled = False
        self.summary_batch_enqueue: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._state_file = self.cursor_dir / _STATE_FILENAME
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._available: Optional[bool] = None
        # Stopping + signal15 + Stopped 常在同一秒内连发，合并为一次关闭推送
        self._last_disabled_push_at = 0.0
        self._disabled_coalesce_seconds = 5.0

    def add_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def add_service_hook(self, hook: Callable[[str, Dict[str, Any]], None]) -> None:
        if hook and hook not in self._service_hooks:
            self._service_hooks.append(hook)

    def clear_service_hooks(self) -> None:
        self._service_hooks.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        ignore_loopback: Optional[bool] = None,
        ignore_lan: Optional[bool] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if ignore_loopback is not None:
            self.ignore_loopback = bool(ignore_loopback)
        if ignore_lan is not None:
            self.ignore_lan = bool(ignore_lan)

    def set_poll_batch_summary(
        self,
        enabled: bool,
        enqueue: Optional[Callable[[List[Dict[str, Any]]], None]],
    ) -> None:
        self.poll_batch_summary_enabled = bool(enabled)
        self.summary_batch_enqueue = enqueue

    def is_available(self, *, force_reprobe: bool = False) -> bool:
        if force_reprobe or self._available is None:
            self._available = self._probe()
        return bool(self._available)

    def _probe(self) -> bool:
        if not journalctl_available():
            self.logger.warning("未找到 journalctl，无法采集 SSH journal")
            return False
        # 试读一条（可能无权限）；-n 0 只拿 cursor
        code, out, err = self._run_journalctl(["-n", "0", "--show-cursor"])
        denied = any(marker in err.lower() for marker in (
            "permission denied", "access denied", "insufficient permissions",
            "not seeing messages from other users",
        ))
        if code != 0 or denied:
            self.logger.warning(
                "journalctl 不可用或无权读取 SSH journal。%s 详情: %s",
                journal_permission_action(),
                (err or out or "").strip()[:240],
            )
            return False
        return True

    def _unit_args(self) -> List[str]:
        args: List[str] = []
        for u in self.journal_units:
            u = (u or "").strip()
            if u:
                args.extend(["-u", u])
        return args or ["-u", "ssh", "-u", "sshd"]

    def _run_journalctl(self, extra: List[str]) -> Tuple[int, str, str]:
        cmd: List[str] = ["journalctl"]
        # 容器内读宿主机 journal 时，若挂载了目录可显式指定
        journal_dir = (os.environ.get("SSH_JOURNAL_DIRECTORY") or "").strip()
        if journal_dir and Path(journal_dir).is_dir():
            cmd.append(f"--directory={journal_dir}")
        cmd.extend(self._unit_args())
        cmd.extend(["--no-pager", *extra])
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return p.returncode, p.stdout or "", p.stderr or ""
        except FileNotFoundError:
            return 127, "", "journalctl not found"
        except subprocess.TimeoutExpired:
            return 124, "", "journalctl timeout"
        except Exception as e:
            return 1, "", str(e)

    def _load_state(self) -> Dict[str, Any]:
        default = {"version": 1, "cursor": "", "aligned": False}
        try:
            if self._state_file.exists():
                obj = json.loads(self._state_file.read_text() or "{}")
                if isinstance(obj, dict):
                    default.update(obj)
        except Exception as e:
            self.logger.warning("读取 SSH journal 状态失败: %s", e)
        return default

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            self._state_file.write_text(json.dumps(state, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入 SSH journal 状态失败: %s", e)

    def _extract_show_cursor(self, text: str) -> str:
        # 末行形如: -- cursor: s=...
        for line in reversed((text or "").splitlines()):
            line = line.strip()
            if line.startswith("-- cursor:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _align_cursor(self, state: Dict[str, Any]) -> None:
        """对齐到当前 journal 末尾，不补推历史。"""
        since = f"@{time.time():.6f}"
        code, out, err = self._run_journalctl(["-n", "0", "--show-cursor"])
        if code != 0:
            self.logger.warning("SSH journal 对齐失败: %s", (err or out)[:240])
            return
        cur = self._extract_show_cursor(out)
        if cur:
            state["cursor"] = cur
            state.pop("since", None)
            state["aligned"] = True
            self._save_state(state)
            self.logger.info("SSH journal 已对齐当前游标（不推送历史）")
        else:
            # 空 journal 没有游标；固定起始时间，后续轮询接收此后首条新日志。
            state["cursor"] = ""
            state["since"] = since
            state["aligned"] = True
            self._save_state(state)
            self.logger.info("SSH journal 对齐完成（暂无 cursor，等待新日志）")

    def _fetch_entries(self, cursor: str, since: str = "") -> Tuple[List[Dict[str, Any]], str]:
        """返回 (entries, new_cursor)。entries 为 journal -o json 对象列表。

        必须带 ``__CURSOR``（不是 CURSOR），否则游标不前进会每轮重复推送。
        无游标时使用固定起始时间，避免遗漏首条事件或把历史当增量刷屏。
        """
        if not cursor and not since:
            return [], cursor
        extra = [
            "-o",
            "json",
            # MESSAGE_ID/UNIT：关闭文案不含 ssh.service 时仍可识别启停
            "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP,MESSAGE_ID,UNIT,_SYSTEMD_UNIT",
            "--show-cursor",
        ]
        extra.extend(["--after-cursor", cursor] if cursor else ["--since", since])
        code, out, err = self._run_journalctl(extra)
        if code != 0:
            self.logger.warning("读取 SSH journal 失败: %s", (err or out)[:240])
            return [], cursor
        entries: List[Dict[str, Any]] = []
        new_cursor = cursor
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("-- cursor:"):
                c = line.split(":", 1)[1].strip()
                if c:
                    new_cursor = c
                continue
            if line.startswith("--"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            entries.append(obj)
            c = (obj.get("__CURSOR") or "").strip()
            if c:
                new_cursor = c
        # 本轮无新条目时，--show-cursor 仍可能给出当前位置
        if not entries:
            tail_cur = self._extract_show_cursor(out)
            if tail_cur:
                new_cursor = tail_cur
        return entries, new_cursor

    @staticmethod
    def _ts_from_entry(obj: Dict[str, Any]) -> str:
        raw = obj.get("__REALTIME_TIMESTAMP")
        try:
            # 微秒
            us = int(raw)
            sec = us / 1_000_000.0
            try:
                from zoneinfo import ZoneInfo

                return datetime.fromtimestamp(sec, tz=ZoneInfo("Asia/Shanghai")).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except Exception:
                return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _emit(self, event_type: str, event_data: Dict[str, Any], raw_msg: str, ts: str) -> None:
        event_data = dict(event_data)
        event_data.setdefault("_source", "ssh_journal")
        event_data.setdefault("_source_event_id", event_type)

        if event_type in SSH_SERVICE_SWITCH_EVENTS and self._service_hooks:
            for hook in list(self._service_hooks):
                try:
                    hook(event_type, event_data)
                except Exception as e:
                    self.logger.error("SSH 服务钩子失败 %s: %s", event_type, e, exc_info=True)

        # 启/停成对：勾选「SSH已启用」或「SSH已关闭」任一，两者都推
        if event_type in SSH_SERVICE_SWITCH_EVENTS:
            if self.monitor_events and not (self.monitor_events & SSH_SERVICE_SWITCH_EVENTS):
                return
        elif self.monitor_events and event_type not in self.monitor_events:
            return
        if should_ignore_ssh_by_source(
            event_type,
            event_data,
            ignore_loopback=self.ignore_loopback,
            ignore_lan=self.ignore_lan,
        ):
            return
        handler = self.event_handlers.get(event_type)
        if not handler:
            return
        # Stopping / signal15 / Stopped 常连发：钩子已触发，推送只留一次
        if event_type == SSH_DISABLED:
            now = time.time()
            if (now - float(self._last_disabled_push_at or 0.0)) < float(self._disabled_coalesce_seconds):
                self.logger.debug("合并短时间内重复的 SSH 关闭事件")
                return
            self._last_disabled_push_at = now
        entry = JournalEntry(
            cursor=f"ssh-journal-{event_type}-{int(time.time())}",
            timestamp=ts,
            hostname="sshd",
            syslog_identifier="sshd",
            message=raw_msg,
            priority=0,
            pid=0,
            raw_data=raw_msg,
            original_line=raw_msg,
        )
        try:
            if self.poll_batch_summary_enabled and self.summary_batch_enqueue:
                rid = hash(raw_msg) & 0x7FFFFFFF
                self.summary_batch_enqueue(
                    [
                        {
                            "row_id": rid,
                            "db_event_id": event_type,
                            "event_type": event_type,
                            "event_data": event_data,
                            "entry": entry,
                            "handler": handler,
                            "source": "ssh_journal",
                        }
                    ]
                )
            else:
                handler(event_data, entry)
        except Exception as e:
            self.logger.error("处理 SSH journal 事件失败 %s: %s", event_type, e, exc_info=True)

    def _poll_once(self, state: Dict[str, Any]) -> None:
        if not state.get("aligned"):
            self._align_cursor(state)
            return
        cursor = str(state.get("cursor") or "")
        if not cursor and not state.get("since"):
            # 兼容旧版保存的空游标状态；时间只记录一次，空闲轮询不再重新对齐。
            state["since"] = f"@{time.time():.6f}"
            self._save_state(state)
        entries, new_cursor = self._fetch_entries(cursor, str(state.get("since") or ""))
        for obj in entries:
            parsed = parse_ssh_journal_entry(obj)
            if not parsed:
                continue
            event_type, event_data = parsed
            event_data = dict(event_data)
            msg = _coerce_journal_message(obj.get("MESSAGE")) or event_type
            # 每条 journal 记录唯一游标，避免 SSH 启停被「同分钟去重」吞掉
            jcursor = str(obj.get("__CURSOR") or "").strip()
            if jcursor:
                event_data["_source_cursor"] = jcursor
            else:
                event_data["_source_cursor"] = f"{event_type}-{time.time_ns()}"
            self._emit(event_type, event_data, msg, self._ts_from_entry(obj))
        if new_cursor and new_cursor != cursor:
            state["cursor"] = new_cursor
            self._save_state(state)

    def _run_loop(self) -> None:
        # 每次启动重新对齐；停机期间的日志不补推，对齐失败时也不能沿用旧游标。
        state: Dict[str, Any] = {"version": 1, "cursor": "", "aligned": False}
        self.logger.info(
            "SSH journal 轮询启动 units=%s interval=%ss",
            ",".join(self.journal_units),
            self.poll_interval,
        )
        while self.running:
            try:
                if (SSH_JOURNAL_EVENTS & self.monitor_events) or self._service_hooks:
                    if not state.get("aligned"):
                        self._align_cursor(state)
                    if state.get("aligned"):
                        self._poll_once(state)
            except Exception as e:
                self.logger.error("SSH journal 轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def start(self) -> None:
        if self.running:
            return
        if not (SSH_JOURNAL_EVENTS & self.monitor_events) and not self._service_hooks:
            self.logger.info("monitor_events 未包含 SSH 事件且无服务钩子，跳过 SshJournalPoller")
            return
        if not self.is_available():
            self.logger.warning("SSH journal 不可用，跳过 SshJournalPoller（将回退 logger_data 中的 Sshd*）")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="SshJournalPoller", daemon=False)
        self._thread.start()
        self.logger.info("SshJournalPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("SshJournalPoller 已停止")
