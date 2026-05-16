"""
trimactivity.db 轮询：根据 user_token 表推断影视类应用的登录（新会话）与登出（token 失效）。
可与 logger 中按 serviceId 过滤的「影视库登录」同时使用；若重复请在配置中只启用一种来源。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .models import JournalEntry
from .media_db_poller import _ms_to_str
from .sqlite_uri import connect_readonly_with_fallback

_ACTIVITY_EVENTS: Set[str] = {"MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT"}

_DEDUP_TTL = 24 * 3600


def _norm_patterns(patterns: Optional[List[str]]) -> List[str]:
    if not patterns:
        return []
    out: List[str] = []
    for p in patterns:
        s = (p or "").strip()
        if s:
            out.append(s)
    return out


def _match_app(name: Optional[str], patterns: List[str]) -> bool:
    # 兜底关键词：仅保留更明确标识，避免 "media" 过宽误匹配
    fallback_patterns = ["影视", "trim"]
    hay = (name or "").strip().lower()
    if not hay:
        return False
    all_patterns = list(patterns or []) + fallback_patterns
    return any((p or "").strip().lower() in hay for p in all_patterns if (p or "").strip())


class TrimActivityPoller:
    """轮询 trimactivity.db 的 user_token。"""

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        app_name_patterns: Optional[List[str]] = None,
        poll_interval: int = 10,
        monitor_events: Optional[List[str]] = None,
    ):
        self.db_path = (db_path or "").strip()
        self.cursor_dir = Path(cursor_dir)
        self.app_name_patterns = _norm_patterns(app_name_patterns)
        self.poll_interval = max(1, int(poll_interval or 10))
        self.monitor_events = set(monitor_events or [])
        self.poll_batch_summary_enabled = False
        self.summary_batch_enqueue: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self.event_handlers: Dict[str, Callable] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._state_file = self.cursor_dir / "trim_activity_poller_state.json"
        self._dedup_file = self.cursor_dir / "trim_activity_poller_dedup.json"
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._dedup_seen: Dict[str, int] = {}
        self._open_db_error_count = 0

    def add_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        db_path: Optional[str] = None,
        app_name_patterns: Optional[List[str]] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if db_path is not None:
            self.db_path = (db_path or "").strip()
        if app_name_patterns is not None:
            self.app_name_patterns = _norm_patterns(app_name_patterns)

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
            "last_create_ms": 0,
            "last_update_ms": 0,
            "token_status": {},
        }
        try:
            if self._state_file.exists():
                raw = self._state_file.read_text().strip()
                if raw:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        default.update(obj)
                        if not isinstance(default.get("token_status"), dict):
                            default["token_status"] = {}
        except Exception as e:
            self.logger.warning("读取 trimactivity 轮询状态失败: %s", e)
        return default

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            token_status = state.get("token_status")
            if isinstance(token_status, dict) and len(token_status) > 100000:
                items = list(token_status.items())[-80000:]
                state["token_status"] = dict(items)
            self._state_file.write_text(json.dumps(state, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入 trimactivity 轮询状态失败: %s", e)

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
            self.logger.warning("读取 trimactivity 去重缓存失败: %s", e)
        self._dedup_seen = {}

    def _save_dedup(self) -> None:
        try:
            self._dedup_file.write_text(json.dumps(self._dedup_seen, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入 trimactivity 去重缓存失败: %s", e)

    def _prune_dedup(self) -> None:
        now = int(time.time())
        self._dedup_seen = {k: v for k, v in self._dedup_seen.items() if int(v) >= now - _DEDUP_TTL}

    def _connect(self) -> sqlite3.Connection:
        conn = connect_readonly_with_fallback(
            self.db_path,
            timeout=10.0,
            table_probe_sql="SELECT 1 FROM user_token LIMIT 1",
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _status_to_int(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _snapshot_tokens(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        """抓取当前 token 快照，用于检测“退出时直接删行”的场景。"""
        snap: Dict[str, Dict[str, Any]] = {}
        q = """
        SELECT token, user_guid, ip, app_name, status, create_time, update_time
        FROM user_token
        WHERE token IS NOT NULL
        """
        for r in conn.execute(q):
            d = dict(r)
            token = str(d.get("token") or "").strip()
            if not token:
                continue
            app = d.get("app_name")
            if not _match_app(str(app) if app is not None else "", self.app_name_patterns):
                continue
            snap[token] = {
                "status": self._status_to_int(d.get("status")),
                "user_guid": d.get("user_guid"),
                "ip": d.get("ip"),
                "app_name": app,
                "update_time": int(d.get("update_time") or 0),
            }
        return snap

    def _emit(self, event_type: str, event_data: Dict[str, Any], raw: Dict[str, Any], ts_ms: Optional[int]) -> None:
        if self.monitor_events and event_type not in self.monitor_events:
            return
        fp = f"{event_type}|{json.dumps(raw, sort_keys=True, ensure_ascii=False)[:400]}"
        now = int(time.time())
        if fp in self._dedup_seen:
            return
        handler = self.event_handlers.get(event_type)
        if not handler:
            return
        event_data = dict(event_data)
        event_data.setdefault("_source", "trimactivity_db")
        event_data.setdefault("_source_cursor", fp)
        event_data.setdefault("_source_event_id", event_type)
        raw_log = json.dumps(raw, ensure_ascii=False)
        ts = _ms_to_str(ts_ms)
        entry = JournalEntry(
            cursor=f"trimactivity-{event_type}-{now}",
            timestamp=ts,
            hostname="trimactivity.db",
            syslog_identifier=event_type,
            message=raw_log,
            priority=0,
            pid=0,
            raw_data=raw_log,
            original_line=raw_log,
        )
        try:
            if self.poll_batch_summary_enabled and self.summary_batch_enqueue:
                rid = abs(hash(fp)) % (2**31 - 1)
                self.summary_batch_enqueue(
                    [
                        {
                            "row_id": rid,
                            "db_event_id": event_type,
                            "event_type": event_type,
                            "event_data": event_data,
                            "entry": entry,
                            "handler": handler,
                            "source": "trimactivity_db",
                        }
                    ]
                )
            else:
                handler(event_data, entry)
            self._dedup_seen[fp] = now
        except Exception as e:
            self.logger.error("处理 trimactivity 事件失败 %s: %s", event_type, e, exc_info=True)

    def _baseline(self, conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(create_time),0), COALESCE(MAX(update_time),0) FROM user_token"
        ).fetchone()
        state["last_create_ms"] = int(row[0] or 0)
        state["last_update_ms"] = int(row[1] or 0)
        token_status: Dict[str, Dict[str, Any]] = {}
        try:
            for token, user_guid, ip, app_name, status, update_time in conn.execute(
                "SELECT token, user_guid, ip, app_name, status, update_time FROM user_token WHERE token IS NOT NULL"
            ):
                tk = str(token or "").strip()
                if not tk:
                    continue
                token_status[tk] = {
                    "status": self._status_to_int(status),
                    "user_guid": user_guid,
                    "ip": ip,
                    "app_name": app_name,
                    "update_time": int(update_time or 0),
                }
        except Exception as e:
            self.logger.warning("构建 trimactivity token 快照失败: %s", e)
        state["token_status"] = token_status
        state["initialized"] = True
        self.logger.info(
            "trimactivity 轮询已对齐水位 create=%s update=%s（不推送历史）",
            state["last_create_ms"],
            state["last_update_ms"],
        )

    def _poll_once(self, state: Dict[str, Any]) -> None:
        if not self.db_path or not os.path.exists(self.db_path):
            return
        try:
            conn = self._connect()
            self._open_db_error_count = 0
        except Exception as e:
            msg = str(e).lower()
            if "unable to open" in msg or "readonly" in msg:
                self._open_db_error_count += 1
                if self._open_db_error_count == 1 or self._open_db_error_count % 12 == 0:
                    self.logger.warning(
                        "连接 trimactivity.db 失败: %s（已连续 %s 次，期间同类错误已节流）",
                        e,
                        self._open_db_error_count,
                    )
            else:
                self._open_db_error_count = 0
                self.logger.warning("连接 trimactivity.db 失败: %s", e)
            return
        try:
            if not state.get("initialized"):
                self._baseline(conn, state)
                return

            lc = int(state.get("last_create_ms") or 0)
            lu = int(state.get("last_update_ms") or 0)
            max_c, max_u = lc, lu
            token_status: Dict[str, Dict[str, Any]] = state.get("token_status") or {}
            if not isinstance(token_status, dict):
                token_status = {}

            # 新会话（登录）
            q_new = """
            SELECT token, user_guid, ip, device, app_name, status, create_time, update_time
            FROM user_token
            WHERE create_time > ?
            ORDER BY create_time ASC
            LIMIT 200
            """
            for r in conn.execute(q_new, (lc,)):
                d = dict(r)
                ct = int(d.get("create_time") or 0)
                max_c = max(max_c, ct)
                app = d.get("app_name")
                if not _match_app(str(app) if app is not None else "", self.app_name_patterns):
                    self.logger.debug(
                        "trimactivity 登录事件被过滤: app_name=%r patterns=%s",
                        app,
                        self.app_name_patterns,
                    )
                    continue
                ev = {
                    "user": d.get("user_guid") or "",
                    "IP": d.get("ip") or "",
                    "user_guid": d.get("user_guid"),
                    "app_name": app,
                    "device": d.get("device"),
                    "message": f"影视应用新会话（app={app}）",
                }
                token = str(d.get("token") or "").strip()
                if token:
                    token_status[token] = {
                        "status": self._status_to_int(d.get("status")),
                        "user_guid": d.get("user_guid"),
                        "ip": d.get("ip"),
                        "app_name": app,
                        "update_time": int(d.get("update_time") or ct or 0),
                    }
                self._emit("MEDIA_LOGIN_SUCC", ev, d, ct)

            # 登出：会话状态从“活跃(1)”变为其他值时视为退出；不同版本状态码可能不止 0
            q_out = """
            SELECT token, user_guid, ip, device, app_name, status, create_time, update_time
            FROM user_token
            WHERE update_time > ? AND update_time > COALESCE(create_time, 0)
            ORDER BY update_time ASC
            LIMIT 200
            """
            for r in conn.execute(q_out, (lu,)):
                d = dict(r)
                ut = int(d.get("update_time") or 0)
                max_u = max(max_u, ut)
                app = d.get("app_name")
                if not _match_app(str(app) if app is not None else "", self.app_name_patterns):
                    self.logger.debug(
                        "trimactivity 登出事件被过滤: app_name=%r patterns=%s",
                        app,
                        self.app_name_patterns,
                    )
                    continue
                status_raw = d.get("status")
                try:
                    status_num = int(status_raw)
                except (TypeError, ValueError):
                    status_num = None
                token = str(d.get("token") or "").strip()
                old_info = token_status.get(token) if token else None
                old_status = old_info.get("status") if isinstance(old_info, dict) else None
                # 仅状态发生迁移且从活跃态(1)转出时判定为退出，避免误报
                if old_status is None:
                    if token:
                        token_status[token] = {
                            "status": status_num if status_num is not None else -1,
                            "user_guid": d.get("user_guid"),
                            "ip": d.get("ip"),
                            "app_name": app,
                            "update_time": ut,
                        }
                    continue
                if status_num is None:
                    if token:
                        token_status[token] = {
                            "status": -1,
                            "user_guid": d.get("user_guid"),
                            "ip": d.get("ip"),
                            "app_name": app,
                            "update_time": ut,
                        }
                    continue
                if old_status == status_num:
                    continue
                if token:
                    token_status[token] = {
                        "status": status_num,
                        "user_guid": d.get("user_guid"),
                        "ip": d.get("ip"),
                        "app_name": app,
                        "update_time": ut,
                    }
                if old_status != 1 or status_num == 1:
                    continue
                ev = {
                    "user": d.get("user_guid") or "",
                    "IP": d.get("ip") or "",
                    "user_guid": d.get("user_guid"),
                    "app_name": app,
                    "status": status_raw,
                    "message": f"影视应用会话结束（app={app}）",
                }
                self.logger.info(
                    "捕获影视登出会话: user=%s app=%s status=%s",
                    ev.get("user_guid") or "",
                    app,
                    status_raw,
                )
                self._emit("MEDIA_LOGOUT", ev, d, ut)

            # 补充场景：某些版本退出会直接删除 token 行，无 status 变更可见。
            current_snapshot = self._snapshot_tokens(conn)
            for tk, old in list(token_status.items()):
                if tk in current_snapshot:
                    continue
                if not isinstance(old, dict):
                    continue
                if int(old.get("status", -1)) != 1:
                    continue
                app_old = old.get("app_name")
                if not _match_app(str(app_old) if app_old is not None else "", self.app_name_patterns):
                    continue
                logout_data = {
                    "user": old.get("user_guid") or "",
                    "IP": old.get("ip") or "",
                    "user_guid": old.get("user_guid"),
                    "app_name": old.get("app_name"),
                    "status": "deleted",
                    "message": f"影视应用会话结束（token删除，app={old.get('app_name')}）",
                }
                self.logger.info(
                    "捕获影视登出会话(token删除): user=%s app=%s token=%s",
                    logout_data.get("user_guid") or "",
                    old.get("app_name"),
                    tk[:8],
                )
                self._emit("MEDIA_LOGOUT", logout_data, {"token": tk, **old}, int(old.get("update_time") or 0))

            token_status = current_snapshot

            state["last_create_ms"] = max_c
            state["last_update_ms"] = max(lu, max_u)
            state["token_status"] = token_status
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        self._load_dedup()
        state = self._load_state()
        self.logger.info("TrimActivityPoller 启动 db=%s patterns=%s", self.db_path or "(未配置)", self.app_name_patterns)
        while self.running:
            try:
                if (
                    self.db_path
                    and self.app_name_patterns
                    and (_ACTIVITY_EVENTS & self.monitor_events)
                ):
                    self._poll_once(state)
                    self._save_state(state)
                    self._prune_dedup()
                    self._save_dedup()
            except Exception as e:
                msg = str(e).lower()
                if "unable to open database file" in msg:
                    self._open_db_error_count += 1
                    if self._open_db_error_count == 1 or self._open_db_error_count % 12 == 0:
                        self.logger.error(
                            "trimactivity 轮询异常: %s（已连续 %s 次，期间同类错误已节流）",
                            e,
                            self._open_db_error_count,
                            exc_info=True,
                        )
                else:
                    self._open_db_error_count = 0
                    self.logger.error("trimactivity 轮询异常: %s", e, exc_info=True)
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
            self.logger.warning("TrimActivityPoller 启动对齐失败（连接数据库失败）: %s", e)
            return
        try:
            state = self._load_state()
            try:
                self._baseline(conn, state)
                self._save_state(state)
            except sqlite3.Error as e:
                self.logger.warning(
                    "TrimActivityPoller 启动对齐失败（将在线程内重试 baseline）: %s",
                    e,
                    exc_info=True,
                )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def start(self) -> None:
        if self.running:
            return
        if not self.db_path:
            self.logger.info("未配置 trim_activity_db_path，跳过 TrimActivityPoller")
            return
        if not self.app_name_patterns:
            self.logger.info("未配置 media_lib_app_name_patterns，跳过 TrimActivityPoller")
            return
        if not (_ACTIVITY_EVENTS & self.monitor_events):
            self.logger.info("monitor_events 未包含 MEDIA_LOGIN_SUCC/MEDIA_LOGOUT，跳过 TrimActivityPoller")
            return
        self._align_state_to_latest()
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="TrimActivityPoller", daemon=False)
        self._thread.start()
        self.logger.info("TrimActivityPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("TrimActivityPoller 已停止")
