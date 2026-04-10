"""
数据库日志轮询器
从 eventlogger 的 SQLite 数据库 log 表轮询新记录（默认 /usr/trim/var/eventlogger_service/logger_data.db3）。
表结构: id, serviceId, uid, uname, logtime(10位时间戳), loglevel, eventId, parameter(JSON), category
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable, Dict, List, Optional, Any

# 推送时间显示偏移（秒）。若 NAS 存库的 logtime 比 UTC 少 8 小时，设 LOGTIME_DISPLAY_OFFSET_SECONDS=28800
_LOGTIME_OFFSET_SECONDS = int(os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS", "28800"))

from .models import JournalEntry


# 数据库 eventId -> 项目内 event_type（通知/处理器使用的类型）
# 数据库与项目一致的直接同名字符串；不一致的映射到项目已有类型
DB_EVENT_ID_TO_PROJECT: Dict[str, str] = {
    "LoginSucc": "LoginSucc",
    "LoginSucc2FA1": "LoginSucc2FA1",
    "LoginFail": "LoginFail",
    "Logout": "Logout",
    "FoundDisk": "FoundDisk",
    "APP_CRASH": "APP_CRASH",
    "SshdLoginSucc": "SSH_LOGIN_SUCCESS",
    "SshdLoginAuthFail": "SSH_AUTH_FAILED",
    "SshdLogonout": "SSH_DISCONNECTED",
    "APP_INSTALL_FAILED_INIT_DOCKER_EXCEPTION": "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
    "UPS_ONLINE": "UPS_ONLINE",
    "UPS_ONBATT_LOWBATT": "UPS_ONBATT_LOWBATT",
    "UPS_DISCONNET": "UPS_ONBATT",
    "UPS_CONNET_OL": "UPS_ONLINE",
    "UPS_ENABLE": "UPS_ENABLE",
    "UPS_DISABLE": "UPS_DISABLE",
    "APP_UPDATE_FAILED": "APP_UPDATE_FAILED",
    "APP_STARTED": "APP_STARTED",
    "APP_STOPPED": "APP_STOPPED",
    "APP_UPDATED": "APP_UPDATED",
    "APP_INSTALLED": "APP_INSTALLED",
    "APP_AUTO_STARTED": "APP_AUTO_STARTED",
    "APP_UNINSTALLED": "APP_UNINSTALLED",
    "DISK_IO_ERR": "DISK_IO_ERR",
    "DiskWakeup": "DiskWakeup",
    "DiskSpindown": "DiskSpindown",
    "CPU_USAGE_ALARM": "CPU_USAGE_ALARM",
    "CPU_USAGE_RESTORED": "CPU_USAGE_RESTORED",
    "MEMORY_USAGE_ALARM": "MEMORY_USAGE_ALARM",
    "MEMORY_USAGE_RESTORED": "MEMORY_USAGE_RESTORED",
    # 可选事件（默认不推送，需用户在配置中勾选）
    "ARCHIVING_SUCCESS": "ARCHIVING_SUCCESS",
    "DeleteFile": "DeleteFile",
    "MovetoTrashbin": "MovetoTrashbin",
    "SHARE_EVENTID_DEL": "SHARE_EVENTID_DEL",
    "SHARE_EVENTID_PUT": "SHARE_EVENTID_PUT",
    "WEBDAV_ENABLED": "WEBDAV_ENABLED",
    "WEBDAV_DISABLED": "WEBDAV_DISABLED",
    "SAMBA_ENABLED": "SAMBA_ENABLED",
    "SAMBA_DISABLED": "SAMBA_DISABLED",
    "DLNA_ENABLED": "DLNA_ENABLED",
    "DLNA_DISABLED": "DLNA_DISABLED",
    "FTP_ENABLED": "FTP_ENABLED",
    "FTP_DISABLED": "FTP_DISABLED",
    "NFS_ENABLED": "NFS_ENABLED",
    "NFS_DISABLED": "NFS_DISABLED",
    "FW_ENABLE": "FW_ENABLE",
    "FW_DISABLE": "FW_DISABLE",
    "SECURITY_PORTCHANGED": "SECURITY_PORTCHANGED",
    "SHUTDOWN_VM": "SHUTDOWN_VM",
    "STATUS_RUNNING_VM": "STATUS_RUNNING_VM",
    "DESTROY_VM": "DESTROY_VM",
}

def _logtime_to_datetime(logtime: int) -> str:
    """10 位 Unix 时间戳转 YYYY-MM-DD HH:MM:SS（Asia/Shanghai）。可设 LOGTIME_DISPLAY_OFFSET_SECONDS 修正存库偏差（如 28800=+8h）。"""
    try:
        ts = int(logtime) + _LOGTIME_OFFSET_SECONDS
        dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Shanghai"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def _parse_parameter(parameter: Optional[str], uname: Optional[str], uid: Optional[int]) -> Dict[str, Any]:
    """解析 parameter JSON，并合并 uname/uid。
    常见 eventId 的 parameter 结构示例：
    - APP_CRASH: {"data":{...}, "datetime", "eventId", "from", "level"}
    - SshdLogonout / SshdLoginAuthFail / SshdLoginSucc: {"user":"xxx", "from":"192.168.1.155"}，from 为 IP
    """
    data: Dict[str, Any] = {}
    if parameter and parameter.strip():
        try:
            data = json.loads(parameter)
        except json.JSONDecodeError:
            data = {"raw": parameter}
    if uname is not None and "user" not in data:
        data["user"] = uname
    if uid is not None and "uid" not in data:
        data["uid"] = uid
    if "IP" not in data and "ip" not in data:
        # 飞牛 SSH 事件等使用 "from" 表示来源 IP，统一映射到 IP 供通知展示
        data["IP"] = data.get("from") or data.get("FROM") or ""
    return data


def _row_to_entry(row: Dict[str, Any]) -> JournalEntry:
    """将数据库一行转为 JournalEntry（供现有 event_processor 使用）。"""
    logtime = row.get("logtime") or 0
    ts = _logtime_to_datetime(logtime)
    parameter = row.get("parameter") or "{}"
    return JournalEntry(
        cursor=str(row.get("id", "")),
        timestamp=ts,
        hostname=str(row.get("serviceId") or "db"),
        syslog_identifier=str(row.get("eventId") or "unknown"),
        message=parameter,
        priority=int(row.get("loglevel") or 0),
        pid=int(row.get("uid") or 0),
        raw_data=parameter,
        original_line=parameter,
    )


class DBLogPoller:
    """从 logger_data.db3 的 log 表轮询新记录并分发到已注册的事件处理器。"""

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
    ):
        self.db_path = db_path
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, poll_interval)
        self.monitor_events = set(monitor_events or [])
        self.event_handlers: Dict[str, Callable] = {}
        self.batch_handler: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._cursor_file = self.cursor_dir / "db_poller_cursor.txt"
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)

    def add_handler(self, event_type: str, handler: Callable):
        """注册事件类型对应的处理函数（event_type 为项目内类型，如 LoginSucc、SSH_LOGIN_SUCCESS）。"""
        self.event_handlers[event_type] = handler
        self.logger.info("注册事件处理器: %s", event_type)

    def clear_handlers(self) -> None:
        """清空已注册的事件处理器（热加载配置前调用）。"""
        self.event_handlers.clear()

    def set_batch_handler(self, handler: Optional[Callable[[List[Dict[str, Any]]], None]]) -> None:
        """注册按轮询批量处理函数（同一轮仅调用一次）。"""
        self.batch_handler = handler

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        db_path: Optional[str] = None,
    ) -> None:
        """热加载时更新监控事件、轮询间隔、数据库路径。"""
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, poll_interval)
        if db_path is not None:
            self.db_path = db_path
        self.logger.info("DBLogPoller 配置已更新: events=%s, interval=%s, db=%s", len(self.monitor_events), self.poll_interval, self.db_path)

    def _read_last_id(self) -> int:
        try:
            if self._cursor_file.exists():
                raw = self._cursor_file.read_text().strip()
                if raw.isdigit():
                    return int(raw)
        except Exception as e:
            self.logger.warning("读取游标失败: %s", e)
        return 0

    def _write_last_id(self, last_id: int) -> None:
        try:
            self._cursor_file.write_text(str(last_id))
        except Exception as e:
            self.logger.warning("写入游标失败: %s", e)

    def _get_max_log_id(self) -> int:
        """获取 log 表当前最大 id；启动时用此值作为游标，只处理此后新写入的记录。"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM log").fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception as e:
            self.logger.warning("获取 log 表最大 id 失败: %s，将从头轮询", e)
            return 0

    def _fetch_new_rows(self, after_id: int) -> List[Dict[str, Any]]:
        """查询 id > after_id 的记录，按 id 升序。"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT id, serviceId, uid, uname, logtime, loglevel, eventId, parameter, category FROM log WHERE id > ? ORDER BY id ASC",
                (after_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            self.logger.error("查询数据库失败: %s", e)
            return []

    def _poll_once(self, last_id: int) -> int:
        rows = self._fetch_new_rows(last_id)
        batch_events: List[Dict[str, Any]] = []
        for row in rows:
            row_id = row.get("id", 0)
            db_event_id = (row.get("eventId") or "").strip()
            if not db_event_id:
                continue
            project_type = DB_EVENT_ID_TO_PROJECT.get(db_event_id, db_event_id)
            # SshdLoginAuthFail 且 uname=invalid 才是无效用户尝试，按 SSH_INVALID_USER 处理
            uname_raw = (row.get("uname") or "").strip()
            if db_event_id == "SshdLoginAuthFail" and uname_raw.lower() == "invalid":
                project_type = "SSH_INVALID_USER"
            if self.monitor_events and project_type not in self.monitor_events:
                continue
            handler = self.event_handlers.get(project_type)
            if not handler:
                continue
            event_data = _parse_parameter(
                row.get("parameter"),
                row.get("uname"),
                row.get("uid"),
            )
            entry = _row_to_entry(row)
            batch_events.append({
                "row_id": row_id,
                "db_event_id": db_event_id,
                "event_type": project_type,
                "event_data": event_data,
                "entry": entry,
                "handler": handler,
            })
        if batch_events:
            if self.batch_handler:
                try:
                    self.batch_handler(batch_events)
                except Exception as e:
                    self.logger.error("批量处理事件失败（count=%s）: %s", len(batch_events), e, exc_info=True)
            else:
                # 兼容旧逻辑：未注册批处理时按条处理
                for item in batch_events:
                    try:
                        item["handler"](item["event_data"], item["entry"])
                    except Exception as e:
                        self.logger.error("处理事件失败 eventId=%s: %s", item["db_event_id"], e)
        if rows:
            self._write_last_id(rows[-1].get("id", last_id))
        return last_id if not rows else rows[-1].get("id", last_id)

    def _run_loop(self) -> None:
        # 启动时以当前库中最大 id 为游标，不处理历史；只推送此后新写入的记录
        last_id = self._get_max_log_id()
        self._write_last_id(last_id)
        self.logger.info("数据库轮询启动，仅处理 id > %s 的新记录，间隔 %s 秒", last_id, self.poll_interval)
        while self.running:
            try:
                last_id = self._poll_once(last_id)
            except Exception as e:
                self.logger.error("轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="DBLogPoller", daemon=False)
        self._thread.start()
        self.logger.info("DBLogPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("DBLogPoller 已停止")
