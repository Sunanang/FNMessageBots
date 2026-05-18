"""
相册 photo.db 轮询：
- share_link.id 新增 → 照片/相册分享创建
- share_link.valid_to 到期（定时检查）→ 分享过期
- device.id 新增 → 照片同步设备注册
- face_task_log.id 新增 → 人脸识别任务记录（作为识别更新信号）
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from .models import JournalEntry
from .sqlite_uri import connect_readonly_with_fallback

PHOTO_SHARE_CREATED = "PHOTO_SHARE_CREATED"
PHOTO_SHARE_EXPIRED = "PHOTO_SHARE_EXPIRED"
PHOTO_DEVICE_REGISTERED = "PHOTO_DEVICE_REGISTERED"
FACE_RECOGNITION_UPDATED = "FACE_RECOGNITION_UPDATED"

PHOTO_POLL_EVENTS: Set[str] = {
    PHOTO_SHARE_CREATED,
    PHOTO_SHARE_EXPIRED,
    PHOTO_DEVICE_REGISTERED,
    FACE_RECOGNITION_UPDATED,
}

_DEDUP_TTL = 30 * 24 * 3600


def _maybe_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _sec_to_local_str(sec: Optional[int]) -> str:
    if sec is None:
        return ""
    try:
        v = int(sec)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return "永久有效"
    try:
        return datetime.fromtimestamp(v, tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return str(v)


class PhotoDBPoller:
    """轮询 photo.db：share_link / device / face_task_log。"""

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        poll_interval: int = 10,
        monitor_events: Optional[List[str]] = None,
    ):
        self.db_path = (db_path or "").strip()
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, int(poll_interval or 10))
        self.monitor_events = set(monitor_events or [])
        self.poll_batch_summary_enabled = False
        self.summary_batch_enqueue: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self.event_handlers: Dict[str, Callable] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._state_file = self.cursor_dir / "photo_db_poller_state.json"
        self._dedup_file = self.cursor_dir / "photo_db_poller_dedup.json"
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._dedup_seen: Dict[str, int] = {}

    def add_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        db_path: Optional[str] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if db_path is not None:
            self.db_path = (db_path or "").strip()

    def set_poll_batch_summary(
        self,
        enabled: bool,
        enqueue: Optional[Callable[[List[Dict[str, Any]]], None]],
    ) -> None:
        self.poll_batch_summary_enabled = bool(enabled)
        self.summary_batch_enqueue = enqueue

    def _load_state(self) -> Dict[str, Any]:
        default: Dict[str, Any] = {
            "version": 1,
            "initialized": False,
            "last_share_link_id": 0,
            "last_device_id": 0,
            "last_face_task_log_id": 0,
        }
        try:
            if self._state_file.exists():
                raw = self._state_file.read_text().strip()
                if raw:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        default.update(obj)
        except Exception as e:
            self.logger.warning("读取相册轮询状态失败: %s", e)
        return default

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            self._state_file.write_text(json.dumps(state, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入相册轮询状态失败: %s", e)

    def _load_dedup(self) -> None:
        try:
            if self._dedup_file.exists():
                obj = json.loads(self._dedup_file.read_text() or "{}")
                if isinstance(obj, dict):
                    now = int(time.time())
                    self._dedup_seen = {
                        str(k): int(v)
                        for k, v in obj.items()
                        if isinstance(v, (int, float)) and int(v) >= now - _DEDUP_TTL
                    }
                    return
        except Exception as e:
            self.logger.warning("读取相册去重缓存失败: %s", e)
        self._dedup_seen = {}

    def _save_dedup(self) -> None:
        try:
            self._dedup_file.write_text(json.dumps(self._dedup_seen, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入相册去重缓存失败: %s", e)

    def _prune_dedup(self) -> None:
        now = int(time.time())
        self._dedup_seen = {k: v for k, v in self._dedup_seen.items() if int(v) >= now - _DEDUP_TTL}

    def _connect(self) -> sqlite3.Connection:
        # 仅做轮询读取，使用只读连接避免要求数据库目录可写（WAL/journal 权限）
        conn = connect_readonly_with_fallback(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _fingerprint(self, kind: str, key: str) -> str:
        return f"{kind}|{key}"

    def _emit(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        raw_obj: Dict[str, Any],
        ts_sec: Optional[int],
    ) -> None:
        if self.monitor_events and event_type not in self.monitor_events:
            return
        fp = self._fingerprint(event_type, json.dumps(raw_obj, sort_keys=True, ensure_ascii=False)[:400])
        now = int(time.time())
        if fp in self._dedup_seen:
            return
        handler = self.event_handlers.get(event_type)
        if not handler:
            return
        event_data = dict(event_data)
        event_data.setdefault("_source", "photo_db")
        event_data.setdefault("_source_cursor", fp)
        event_data.setdefault("_source_event_id", event_type)
        raw_log = json.dumps(raw_obj, ensure_ascii=False)
        ts = _sec_to_local_str(ts_sec) if ts_sec else datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        entry = JournalEntry(
            cursor=f"photo-{event_type}-{now}-{hash(fp) & 0xFFFFFFFF:x}",
            timestamp=ts,
            hostname="photo.db",
            syslog_identifier=event_type,
            message=raw_log,
            priority=0,
            pid=0,
            raw_data=raw_log,
            original_line=raw_log,
        )
        try:
            if self.poll_batch_summary_enabled and self.summary_batch_enqueue:
                rid = hash(fp) & 0x7FFFFFFF
                self.summary_batch_enqueue(
                    [
                        {
                            "row_id": rid,
                            "db_event_id": event_type,
                            "event_type": event_type,
                            "event_data": event_data,
                            "entry": entry,
                            "handler": handler,
                            "source": "photo_db",
                        }
                    ]
                )
            else:
                handler(event_data, entry)
            self._dedup_seen[fp] = now
        except Exception as e:
            self.logger.error("处理相册事件失败 %s: %s", event_type, e, exc_info=True)

    def _owner_label(self, owner_id: Any, nas_uid: Any) -> str:
        parts: List[str] = []
        if nas_uid is not None and str(nas_uid).strip():
            parts.append(f"NAS 用户 UID {nas_uid}")
        if owner_id is not None and str(owner_id).strip():
            parts.append(f"相册用户 id {owner_id}")
        return "，".join(parts) if parts else "未知"

    def _baseline(self, conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM share_link").fetchone()
        state["last_share_link_id"] = int(row[0] or 0)
        row_d = conn.execute("SELECT COALESCE(MAX(id), 0) FROM device").fetchone()
        state["last_device_id"] = int(row_d[0] or 0)
        row_f = conn.execute("SELECT COALESCE(MAX(id), 0) FROM face_task_log").fetchone()
        state["last_face_task_log_id"] = int(row_f[0] or 0)
        now = int(time.time())
        try:
            for r in conn.execute(
                "SELECT share_id, valid_to FROM share_link WHERE valid_to > 0 AND valid_to <= ?", (now,)
            ):
                sid = str(r[0] or "").strip()
                if not sid:
                    continue
                raw = {"share_id": sid, "valid_to": r[1]}
                self._dedup_seen[
                    self._fingerprint(PHOTO_SHARE_EXPIRED, json.dumps(raw, sort_keys=True, ensure_ascii=False)[:400])
                ] = now
        except Exception as e:
            self.logger.warning("相册过期基线去重失败: %s", e)
        state["initialized"] = True
        self.logger.info(
            "相册数据库轮询已对齐当前水位（不推送历史）: share_link_id=%s device_id=%s face_task_log_id=%s",
            state["last_share_link_id"],
            state["last_device_id"],
            state["last_face_task_log_id"],
        )

    def _poll_once(self, state: Dict[str, Any]) -> None:
        if not self.db_path or not os.path.exists(self.db_path):
            return
        try:
            conn = self._connect()
        except Exception as e:
            self.logger.warning("连接 photo.db 失败: %s", e)
            return
        try:
            if not state.get("initialized"):
                self._baseline(conn, state)
                self._save_state(state)
                self._save_dedup()
                return

            now = int(time.time())
            last_s = int(state.get("last_share_link_id") or 0)
            if PHOTO_SHARE_CREATED in self.monitor_events:
                q = (
                    "SELECT sl.id, sl.name, sl.share_id, sl.owner, sl.album_id, sl.valid_to, sl.create_time, u.nas_uid "
                    "FROM share_link sl LEFT JOIN user u ON u.id = sl.owner "
                    "WHERE sl.id > ? ORDER BY sl.id ASC"
                )
                max_s = last_s
                for row in conn.execute(q, (last_s,)):
                    d = dict(row)
                    max_s = max(max_s, int(d.get("id") or 0))
                    oid = d.get("owner")
                    nas = d.get("nas_uid")
                    ev = {
                        "share_name": (d.get("name") or "").strip(),
                        "share_id": (d.get("share_id") or "").strip(),
                        "share_link_id": int(d.get("id") or 0),
                        "owner_nas_uid": _maybe_int(nas),
                        "owner_photo_user_id": _maybe_int(oid),
                        "owner_label": self._owner_label(oid, nas),
                        "valid_to_str": _sec_to_local_str(d.get("valid_to")),
                        "album_id": d.get("album_id"),
                    }
                    self._emit(PHOTO_SHARE_CREATED, ev, d, d.get("create_time"))
                state["last_share_link_id"] = max_s

            if PHOTO_SHARE_EXPIRED in self.monitor_events:
                q = (
                    "SELECT sl.id, sl.name, sl.share_id, sl.valid_to, sl.owner, u.nas_uid "
                    "FROM share_link sl LEFT JOIN user u ON u.id = sl.owner "
                    "WHERE sl.valid_to > 0 AND sl.valid_to <= ?"
                )
                for row in conn.execute(q, (now,)):
                    d = dict(row)
                    sid = (d.get("share_id") or "").strip()
                    if not sid:
                        continue
                    oid = d.get("owner")
                    nas = d.get("nas_uid")
                    raw = {"share_id": sid, "valid_to": d.get("valid_to")}
                    ev = {
                        "share_name": (d.get("name") or "").strip(),
                        "share_id": sid,
                        "expired_at_str": _sec_to_local_str(d.get("valid_to")),
                        "owner_nas_uid": _maybe_int(nas),
                        "owner_photo_user_id": _maybe_int(oid),
                        "owner_label": self._owner_label(oid, nas),
                    }
                    self._emit(PHOTO_SHARE_EXPIRED, ev, raw, d.get("valid_to"))

            last_d = int(state.get("last_device_id") or 0)
            if PHOTO_DEVICE_REGISTERED in self.monitor_events:
                q = (
                    "SELECT d.id, d.user_id, d.device_id, d.device_name, d.device_uniq_name, "
                    "d.created_at, u.nas_uid FROM device d "
                    "LEFT JOIN user u ON u.id = d.user_id WHERE d.id > ? ORDER BY d.id ASC"
                )
                max_d = last_d
                for row in conn.execute(q, (last_d,)):
                    d = dict(row)
                    max_d = max(max_d, int(d.get("id") or 0))
                    name = (d.get("device_name") or d.get("device_uniq_name") or d.get("device_id") or "").strip()
                    uid = d.get("user_id")
                    nas = d.get("nas_uid")
                    ev = {
                        "device_display": name or f"设备#{d.get('id')}",
                        "device_id": (d.get("device_id") or "").strip(),
                        "device_uniq_name": (d.get("device_uniq_name") or "").strip(),
                        "owner_nas_uid": _maybe_int(nas),
                        "owner_photo_user_id": _maybe_int(uid),
                        "owner_label": self._owner_label(uid, nas),
                    }
                    self._emit(PHOTO_DEVICE_REGISTERED, ev, d, d.get("created_at"))
                state["last_device_id"] = max_d

            last_f = int(state.get("last_face_task_log_id") or 0)
            if FACE_RECOGNITION_UPDATED in self.monitor_events:
                q = (
                    "SELECT ftl.id, ftl.user_photo_id, ftl.user_id, up.photo_id "
                    "FROM face_task_log ftl "
                    "LEFT JOIN user_photo up ON up.id = ftl.user_photo_id "
                    "WHERE ftl.id > ? ORDER BY ftl.id ASC"
                )
                rows_f = [dict(r) for r in conn.execute(q, (last_f,)).fetchall()]
                max_f = last_f
                if rows_f:
                    max_f = max(max_f, max(int(r.get("id") or 0) for r in rows_f))
                    latest = rows_f[-1]
                    ev = {
                        "task_log_id": int(latest.get("id") or 0),
                        "user_photo_id": int(latest.get("user_photo_id") or 0),
                        "photo_id": latest.get("photo_id"),
                        "user_id": latest.get("user_id"),
                        "record_count": len(rows_f),
                    }
                    raw = {
                        "first_task_log_id": int(rows_f[0].get("id") or 0),
                        "last_task_log_id": int(latest.get("id") or 0),
                        "record_count": len(rows_f),
                    }
                    self._emit(FACE_RECOGNITION_UPDATED, ev, raw, None)
                state["last_face_task_log_id"] = max_f
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        self._load_dedup()
        state = self._load_state()
        self.logger.info("PhotoDBPoller 启动 db=%s", self.db_path or "(未配置)")
        while self.running:
            try:
                if self.db_path and (PHOTO_POLL_EVENTS & self.monitor_events):
                    self._poll_once(state)
                    self._save_state(state)
                    self._prune_dedup()
                    self._save_dedup()
            except Exception as e:
                self.logger.error("相册轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def _align_state_to_latest(self) -> None:
        """每次启用时对齐到当前数据库水位，避免补发停用期间的存量记录。"""
        if not self.db_path or not os.path.exists(self.db_path):
            return
        try:
            conn = self._connect()
        except Exception as e:
            self.logger.warning("PhotoDBPoller 启动对齐失败（连接数据库失败）: %s", e)
            return
        try:
            state = self._load_state()
            self._baseline(conn, state)
            self._save_state(state)
            self._save_dedup()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def start(self) -> None:
        if self.running:
            return
        if not self.db_path:
            self.logger.info("未配置 photo_db_path，跳过 PhotoDBPoller")
            return
        if not (PHOTO_POLL_EVENTS & self.monitor_events):
            self.logger.info("monitor_events 未包含相册事件，跳过 PhotoDBPoller")
            return
        self._align_state_to_latest()
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="PhotoDBPoller", daemon=False)
        self._thread.start()
        self.logger.info("PhotoDBPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("PhotoDBPoller 已停止")
