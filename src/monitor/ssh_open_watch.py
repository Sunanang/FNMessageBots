"""
SSH 服务开启超时提醒。

从检测到 ``SSH_ENABLED``（Started ssh.service）起算，按配置间隔反复推送
``SSH_OPEN_TIMEOUT``，直到 ``SSH_DISABLED``。
开关由 ``monitor_events`` 勾选 ``SSH_OPEN_TIMEOUT`` 控制；间隔分钟由
``ssh_open_remind_minutes`` 自定义（``get_remind_minutes`` 返回 0 表示关闭）。
状态落盘，便于进程重启后按剩余时间续计。
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .models import JournalEntry
from .ssh_journal_poller import SSH_DISABLED, SSH_ENABLED

SSH_OPEN_TIMEOUT = "SSH_OPEN_TIMEOUT"
_STATE_FILENAME = "ssh_open_watch_state.json"


def _ssh_service_active() -> Optional[bool]:
    """探测 ssh/sshd 是否 active；无法探测时返回 None。"""
    saw_result = False
    for unit in ("ssh", "sshd"):
        try:
            p = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            saw_result = True
            if (p.stdout or "").strip() == "active":
                return True
        except FileNotFoundError:
            return None
        except Exception:
            continue
    return False if saw_result else None


class SshOpenWatch:
    def __init__(
        self,
        cursor_dir: str,
        *,
        get_remind_minutes: Callable[[], int],
        on_timeout: Callable[[Dict[str, Any], JournalEntry], None],
    ):
        self.cursor_dir = Path(cursor_dir)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.cursor_dir / _STATE_FILENAME
        self._get_remind_minutes = get_remind_minutes
        self._on_timeout = on_timeout
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._opened_at: Optional[float] = None
        self._next_due_at: Optional[float] = None
        # 推送瞬间防重入（同一次 fire 的竞态），不是「整段会话只提醒一次」
        self._firing: bool = False
        self.logger = logging.getLogger(__name__)

    def start(self) -> None:
        """启动时恢复未关闭的会话计时。"""
        with self._lock:
            self._load_state()
            if self._opened_at is not None:
                active = _ssh_service_active()
                if active is False:
                    self.logger.info("SSH 开启超时状态存在但服务已关闭，清除计时")
                    self._opened_at = None
                    self._next_due_at = None
                    self._firing = False
                    self._save_state_locked()
            self._reschedule_locked(reason="start")

    def stop(self) -> None:
        with self._lock:
            self._cancel_timer_locked()

    def update_config(self) -> None:
        """热更新间隔后重算下次提醒时间。"""
        with self._lock:
            if self._opened_at is not None:
                minutes = self._effective_minutes()
                if minutes <= 0:
                    self._cancel_timer_locked()
                    self._next_due_at = None
                    self._save_state_locked()
                    return
                # 已排程的下次时间若晚于「现在+新间隔」，收紧到新间隔；已逾期则尽快触发
                now = time.time()
                due = now + minutes * 60.0
                if self._next_due_at is None or float(self._next_due_at) > due:
                    self._next_due_at = due
                self._save_state_locked()
            self._reschedule_locked(reason="config")

    def on_service_event(self, event_type: str, event_data: Optional[Dict[str, Any]] = None) -> None:
        if event_type == SSH_ENABLED:
            self.on_enabled()
        elif event_type == SSH_DISABLED:
            self.on_disabled()

    def on_enabled(self) -> None:
        with self._lock:
            now = time.time()
            minutes = self._effective_minutes()
            self._opened_at = now
            self._firing = False
            self._next_due_at = (now + minutes * 60.0) if minutes > 0 else None
            self._save_state_locked()
            self._reschedule_locked(reason="enabled")
            self.logger.info(
                "SSH 已开启，超时提醒间隔=%s 分钟（0=关闭）",
                minutes,
            )

    def on_disabled(self) -> None:
        with self._lock:
            self._cancel_timer_locked()
            self._opened_at = None
            self._next_due_at = None
            self._firing = False
            self._save_state_locked()
            self.logger.info("SSH 已关闭，清除开启超时提醒")

    def _effective_minutes(self) -> int:
        try:
            minutes = int(self._get_remind_minutes() or 0)
        except (TypeError, ValueError):
            minutes = 0
        return max(0, min(minutes, 10080))

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def _reschedule_locked(self, reason: str) -> None:
        self._cancel_timer_locked()
        minutes = self._effective_minutes()
        if minutes <= 0 or self._opened_at is None:
            return
        now = time.time()
        if self._next_due_at is None:
            self._next_due_at = float(self._opened_at) + minutes * 60.0
            self._save_state_locked()
        remain = max(0.0, float(self._next_due_at) - now)
        opened_at = float(self._opened_at)
        due_at = float(self._next_due_at)

        def _fire() -> None:
            self._fire_timeout(opened_at=opened_at, due_at=due_at, minutes=minutes)

        self._timer = threading.Timer(remain, _fire)
        self._timer.daemon = True
        self._timer.start()
        self.logger.debug(
            "SSH 超时提醒已调度 reason=%s remain=%.1fs interval=%s",
            reason,
            remain,
            minutes,
        )

    def _fire_timeout(self, *, opened_at: float, due_at: float, minutes: int) -> None:
        event_data: Optional[Dict[str, Any]] = None
        entry: Optional[JournalEntry] = None
        elapsed_min = 0
        with self._lock:
            if not self._session_still_valid_locked(opened_at):
                return
            if self._next_due_at is None or abs(float(self._next_due_at) - float(due_at)) > 0.5:
                return
            if self._firing:
                return
            # 服务已关则清状态，避免误推
            active = _ssh_service_active()
            if active is False:
                self._opened_at = None
                self._next_due_at = None
                self._firing = False
                self._save_state_locked()
                self.logger.info("SSH 超时触发时服务已关闭，跳过推送")
                return
            self._firing = True
            elapsed_min = max(0, int((time.time() - float(self._opened_at)) / 60.0))
            opened_display = datetime.fromtimestamp(float(self._opened_at)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            remind_seq = max(1, int(elapsed_min / max(minutes, 1)) if minutes else 1)
            fire_token = f"{opened_at:.3f}-{due_at:.3f}-{time.time_ns()}"
            event_data = {
                "from": "ssh_open_watch",
                "remind_minutes": minutes,
                "elapsed_minutes": elapsed_min,
                "opened_at": opened_display,
                "remind_seq": remind_seq,
                "_source_cursor": f"ssh-open-timeout-{fire_token}",
            }
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = JournalEntry(
                cursor=f"ssh-open-timeout-{fire_token}",
                timestamp=ts,
                hostname="sshd",
                syslog_identifier="ssh_open_watch",
                message=f"SSH 已开启超过 {elapsed_min} 分钟仍未关闭（间隔 {minutes} 分钟）",
                priority=4,
                pid=0,
                raw_data=json.dumps(event_data, ensure_ascii=False),
            )

        # 锁外推送前再确认：避免与 on_disabled / 热加载关提醒竞态误推
        with self._lock:
            if not self._session_still_valid_locked(opened_at):
                self._firing = False
                return
            if self._effective_minutes() <= 0:
                self._firing = False
                return
        try:
            self.logger.warning(
                "SSH 开启超时提醒: 已开启 %s 分钟（间隔 %s）",
                elapsed_min,
                minutes,
            )
            if event_data is not None and entry is not None:
                self._on_timeout(event_data, entry)
        except Exception as exc:
            self.logger.error("推送 SSH 开启超时提醒失败: %s", exc, exc_info=True)
        finally:
            with self._lock:
                if not self._session_still_valid_locked(opened_at):
                    self._firing = False
                    return
                # 下次：从本次 due 起再隔一个间隔；若已严重滞后，从现在起算，避免补推风暴
                interval = self._effective_minutes() * 60.0
                now = time.time()
                nxt = float(due_at) + interval
                if nxt < now:
                    nxt = now + interval
                self._next_due_at = nxt
                self._firing = False
                self._save_state_locked()
                self._reschedule_locked(reason="repeat")

    def _session_still_valid_locked(self, opened_at: float) -> bool:
        if self._opened_at is None:
            return False
        if abs(float(self._opened_at) - float(opened_at)) > 0.5:
            return False
        if self._effective_minutes() <= 0:
            return False
        return True

    def _load_state(self) -> None:
        try:
            if not self._state_file.exists():
                self._opened_at = None
                self._next_due_at = None
                self._firing = False
                return
            obj = json.loads(self._state_file.read_text() or "{}")
            if not isinstance(obj, dict):
                return
            opened = obj.get("opened_at")
            self._opened_at = float(opened) if opened is not None else None
            due = obj.get("next_due_at")
            if due is not None:
                self._next_due_at = float(due)
            elif self._opened_at is not None:
                minutes = self._effective_minutes()
                # 兼容旧状态：reminded=true 表示已提醒过，下次从现在起再隔一个间隔
                if bool(obj.get("reminded", False)) and minutes > 0:
                    self._next_due_at = time.time() + minutes * 60.0
                elif minutes > 0:
                    self._next_due_at = float(self._opened_at) + minutes * 60.0
                else:
                    self._next_due_at = None
            else:
                self._next_due_at = None
            self._firing = False
        except Exception as exc:
            self.logger.warning("读取 SSH 开启超时状态失败: %s", exc)
            self._opened_at = None
            self._next_due_at = None
            self._firing = False

    def _save_state_locked(self) -> None:
        payload = {
            "version": 2,
            "opened_at": self._opened_at,
            "next_due_at": self._next_due_at,
        }
        try:
            self._state_file.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            self.logger.warning("写入 SSH 开启超时状态失败: %s", exc)
