"""
从宿主机 systemd journal 轮询 SSH 登录相关日志。

飞牛新版本 eventlogger 往往不再写入 Sshd*，且 /var/log/auth.log 已取消；
改用 ``journalctl -u ssh -u sshd`` 增量采集，映射到现有 SSH_* 事件。
"""

from __future__ import annotations

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

SSH_LOGIN_SUCCESS = "SSH_LOGIN_SUCCESS"
SSH_AUTH_FAILED = "SSH_AUTH_FAILED"
SSH_INVALID_USER = "SSH_INVALID_USER"
SSH_DISCONNECTED = "SSH_DISCONNECTED"

SSH_JOURNAL_EVENTS: Set[str] = {
    SSH_LOGIN_SUCCESS,
    SSH_AUTH_FAILED,
    SSH_INVALID_USER,
    SSH_DISCONNECTED,
}

_STATE_FILENAME = "ssh_journal_poller_state.json"

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
_RE_SESSION_CLOSED = re.compile(
    r"pam_unix\(sshd:session\):\s+session\s+closed\s+for\s+user\s+(\S+)",
    re.IGNORECASE,
)


def journalctl_available() -> bool:
    return bool(shutil.which("journalctl"))


def parse_ssh_journal_message(message: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    解析单行 sshd MESSAGE，返回 (event_type, event_data) 或 None（忽略噪声/无关行）。
    """
    msg = (message or "").strip()
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

    m = _RE_SESSION_CLOSED.search(msg)
    if m:
        return SSH_DISCONNECTED, {"user": m.group(1), "IP": ""}

    return None


class SshJournalPoller:
    """轮询 journalctl 增量，推送 SSH_* 事件。"""

    def __init__(
        self,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
        journal_units: Optional[List[str]] = None,
    ):
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, int(poll_interval or 5))
        self.monitor_events = set(monitor_events or [])
        self.journal_units = list(journal_units or ["ssh", "sshd"])
        self.event_handlers: Dict[str, Callable] = {}
        self.poll_batch_summary_enabled = False
        self.summary_batch_enqueue: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._state_file = self.cursor_dir / _STATE_FILENAME
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._available: Optional[bool] = None

    def add_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))

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
        if code != 0:
            self.logger.warning(
                "journalctl 不可用或无权读取 SSH journal（请挂载 /var/log/journal 与 /etc/machine-id）: %s",
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
        code, out, err = self._run_journalctl(["-n", "0", "--show-cursor"])
        if code != 0:
            self.logger.warning("SSH journal 对齐失败: %s", (err or out)[:240])
            return
        cur = self._extract_show_cursor(out)
        if cur:
            state["cursor"] = cur
            state["aligned"] = True
            self._save_state(state)
            self.logger.info("SSH journal 已对齐当前游标（不推送历史）")
        else:
            # 无日志时也可能没有 cursor；标记 aligned，下次用 --since now 思路仍靠 after-cursor 空
            state["aligned"] = True
            self._save_state(state)
            self.logger.info("SSH journal 对齐完成（暂无 cursor，等待新日志）")

    def _fetch_entries(self, cursor: str) -> Tuple[List[Dict[str, Any]], str]:
        """返回 (entries, new_cursor)。entries 为 journal -o json 对象列表。

        必须带 ``__CURSOR``（不是 CURSOR），否则游标不前进会每轮重复推送。
        无游标时不回退 ``-n 50``，避免把历史当增量刷屏。
        """
        if not cursor:
            return [], cursor
        extra = [
            "-o",
            "json",
            "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP",
            "--after-cursor",
            cursor,
            "--show-cursor",
        ]
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
        if self.monitor_events and event_type not in self.monitor_events:
            return
        handler = self.event_handlers.get(event_type)
        if not handler:
            return
        event_data = dict(event_data)
        event_data.setdefault("_source", "ssh_journal")
        event_data.setdefault("_source_event_id", event_type)
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
        if not cursor:
            # 对齐时未拿到 cursor：再试一次，绝不无游标扫历史
            self._align_cursor(state)
            cursor = str(state.get("cursor") or "")
            if not cursor:
                return
        entries, new_cursor = self._fetch_entries(cursor)
        for obj in entries:
            msg = obj.get("MESSAGE")
            if isinstance(msg, list):
                msg = "".join(str(x) for x in msg)
            msg = str(msg or "")
            parsed = parse_ssh_journal_message(msg)
            if not parsed:
                continue
            event_type, event_data = parsed
            self._emit(event_type, event_data, msg, self._ts_from_entry(obj))
        if new_cursor and new_cursor != cursor:
            state["cursor"] = new_cursor
            self._save_state(state)

    def _run_loop(self) -> None:
        state = self._load_state()
        if not state.get("aligned") or not state.get("cursor"):
            self._align_cursor(state)
        self.logger.info(
            "SSH journal 轮询启动 units=%s interval=%ss",
            ",".join(self.journal_units),
            self.poll_interval,
        )
        print(
            f"SSH journal 轮询已启动（units={','.join(self.journal_units)}，间隔 {self.poll_interval}s）",
            flush=True,
        )
        while self.running:
            try:
                if SSH_JOURNAL_EVENTS & self.monitor_events:
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
        if not (SSH_JOURNAL_EVENTS & self.monitor_events):
            self.logger.info("monitor_events 未包含 SSH 事件，跳过 SshJournalPoller")
            return
        if not self.is_available():
            self.logger.warning("SSH journal 不可用，跳过 SshJournalPoller（将回退 logger_data 中的 Sshd*）")
            print(
                "SSH journal 不可用：请确认镜像含 journalctl，并挂载 /var/log/journal 与 /etc/machine-id",
                flush=True,
            )
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
