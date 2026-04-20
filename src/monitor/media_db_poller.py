"""
影视库 trimmedia.db 轮询：资源入库、资源删除、刮削完成（fetch_status 变为 1）。
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

TRIM_RESOURCE_ADDED = "TRIM_RESOURCE_ADDED"
TRIM_SCRAPE_SUCCESS = "TRIM_SCRAPE_SUCCESS"

_TRIM_EVENTS: Set[str] = {TRIM_RESOURCE_ADDED, TRIM_SCRAPE_SUCCESS}

# 认为「刮削完成」的条目类型（Season 等容器常为 0，不推刮削）
_SCRAPE_TYPES = ("Movie", "TV", "Episode")

# 新条目「入库」通知覆盖的类型
_ADD_TYPES = ("Movie", "TV", "Season", "Episode")

_DEDUP_TTL = 24 * 3600


def _ms_to_str(ms: Optional[int]) -> str:
    if ms is None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    if v > 10_000_000_000:
        v = int(v / 1000)
    try:
        return datetime.fromtimestamp(v, tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


class MediaDBPoller:
    """轮询 trimmedia.db：item / item_media / media_delete 变更。"""

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
        self.event_handlers: Dict[str, Callable] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._state_file = self.cursor_dir / "media_db_poller_state.json"
        self._dedup_file = self.cursor_dir / "media_db_poller_dedup.json"
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
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if db_path is not None:
            self.db_path = (db_path or "").strip()

    def _load_state(self) -> Dict[str, Any]:
        default: Dict[str, Any] = {
            "version": 1,
            "initialized": False,
            "last_item_create_ms": 0,
            "last_item_update_ms": 0,
            "last_media_ts_ms": 0,
            "last_media_delete_ms": 0,
            "fetch_by_guid": {},
        }
        try:
            if self._state_file.exists():
                raw = self._state_file.read_text().strip()
                if raw:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        default.update(obj)
                        if not isinstance(default.get("fetch_by_guid"), dict):
                            default["fetch_by_guid"] = {}
        except Exception as e:
            self.logger.warning("读取影视库轮询状态失败: %s", e)
        return default

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            fb = state.get("fetch_by_guid")
            if isinstance(fb, dict) and len(fb) > 80000:
                # 防止状态无限膨胀：仅保留最近有变更的条目由 fetch 快照自然收缩困难，故截断
                items = list(fb.items())[-60000:]
                state["fetch_by_guid"] = dict(items)
            self._state_file.write_text(json.dumps(state, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入影视库轮询状态失败: %s", e)

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
            self.logger.warning("读取影视库去重缓存失败: %s", e)
        self._dedup_seen = {}

    def _save_dedup(self) -> None:
        try:
            self._dedup_file.write_text(json.dumps(self._dedup_seen, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入影视库去重缓存失败: %s", e)

    def _prune_dedup(self) -> None:
        now = int(time.time())
        self._dedup_seen = {k: v for k, v in self._dedup_seen.items() if int(v) >= now - _DEDUP_TTL}

    def _connect(self) -> sqlite3.Connection:
        # 仅做轮询读取：immutable=1 可避免部分 NAS/WAL 场景下的只读打开失败
        conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro&immutable=1",
            uri=True,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1").fetchone()
        return conn

    def _fingerprint(self, kind: str, key: str) -> str:
        return f"{kind}|{key}"

    def _emit(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        raw_obj: Dict[str, Any],
        ts_field_ms: Optional[int],
    ) -> None:
        if self.monitor_events and event_type not in self.monitor_events:
            return
        fp = self._fingerprint(event_type, json.dumps(raw_obj, sort_keys=True, ensure_ascii=False)[:500])
        now = int(time.time())
        if fp in self._dedup_seen:
            return
        handler = self.event_handlers.get(event_type)
        if not handler:
            return
        raw_log = json.dumps(raw_obj, ensure_ascii=False)
        ts = _ms_to_str(ts_field_ms)
        entry = JournalEntry(
            cursor=f"trimmedia-{event_type}-{now}-{hash(fp) & 0xFFFFFFFF:x}",
            timestamp=ts,
            hostname="trimmedia.db",
            syslog_identifier=event_type,
            message=raw_log,
            priority=0,
            pid=0,
            raw_data=raw_log,
            original_line=raw_log,
        )
        try:
            handler(event_data, entry)
            self._dedup_seen[fp] = now
        except Exception as e:
            self.logger.error("处理影视库事件失败 %s: %s", event_type, e, exc_info=True)

    def _baseline(self, conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(create_time),0), COALESCE(MAX(update_time),0) FROM item"
        ).fetchone()
        mcr, mur = int(row[0] or 0), int(row[1] or 0)
        row_m = conn.execute(
            "SELECT COALESCE(MAX(COALESCE(update_time,create_time)),0) FROM item_media"
        ).fetchone()
        mmt = int(row_m[0] or 0)
        row_d = conn.execute("SELECT COALESCE(MAX(update_time),0) FROM media_delete").fetchone()
        mdd = int(row_d[0] or 0)
        state["last_item_create_ms"] = mcr
        state["last_item_update_ms"] = mur
        state["last_media_ts_ms"] = mmt
        state["last_media_delete_ms"] = mdd
        fb: Dict[str, int] = {}
        try:
            for g, fs in conn.execute("SELECT guid, fetch_status FROM item"):
                if g:
                    try:
                        fb[str(g)] = int(fs if fs is not None else 0)
                    except (TypeError, ValueError):
                        fb[str(g)] = 0
        except Exception as e:
            self.logger.warning("构建 fetch 快照失败: %s", e)
        state["fetch_by_guid"] = fb
        state["initialized"] = True
        self.logger.info(
            "影视库数据库轮询已对齐当前水位（不推送历史），item(create,update)=(%s,%s)",
            mcr,
            mur,
        )

    def _poll_once(self, state: Dict[str, Any]) -> None:
        if not self.db_path or not os.path.exists(self.db_path):
            return
        try:
            conn = self._connect()
        except Exception as e:
            self.logger.warning("连接 trimmedia.db 失败: %s", e)
            return

        try:
            if not state.get("initialized"):
                self._baseline(conn, state)
                self._save_state(state)
                return

            lic = int(state.get("last_item_create_ms") or 0)
            liu = int(state.get("last_item_update_ms") or 0)
            lmt = int(state.get("last_media_ts_ms") or 0)
            lmd = int(state.get("last_media_delete_ms") or 0)
            fb: Dict[str, int] = state.get("fetch_by_guid") or {}
            if not isinstance(fb, dict):
                fb = {}

            max_ic = lic
            max_iu = liu

            # 1) 新条目（入库）
            q_add = """
            SELECT guid, type, title, path, parent_guid, season_number, episode_number,
                   tmdb_id, fetch_status, create_time, update_time
            FROM item
            WHERE create_time > ?
            ORDER BY create_time ASC
            """
            for r in conn.execute(q_add, (lic,)):
                d = dict(r)
                itype = (d.get("type") or "").strip()
                if itype not in _ADD_TYPES:
                    continue
                ct = int(d.get("create_time") or 0)
                max_ic = max(max_ic, ct)
                ev = {
                    "title": (d.get("title") or "").strip(),
                    "item_type": itype,
                    "path": (d.get("path") or "").strip(),
                    "guid": d.get("guid"),
                    "parent_guid": d.get("parent_guid"),
                    "season_number": d.get("season_number"),
                    "episode_number": d.get("episode_number"),
                    "tmdb_id": d.get("tmdb_id"),
                    "fetch_status": d.get("fetch_status"),
                    "message": f"新条目入库: {itype}「{(d.get('title') or '').strip() or d.get('guid')}」",
                }
                g = d.get("guid")
                if g:
                    fb[str(g)] = int(d.get("fetch_status") or 0)
                self._emit(TRIM_RESOURCE_ADDED, ev, d, ct)

            # 2) fetch_status 0 -> 1（刮削成功）
            q_up = """
            SELECT guid, type, title, path, season_number, episode_number, tmdb_id,
                   fetch_status, create_time, update_time, release_date, first_air_date, air_date, runtime, overview
            FROM item
            WHERE update_time > ?
            ORDER BY update_time ASC
            """
            for r in conn.execute(q_up, (liu,)):
                d = dict(r)
                ut = int(d.get("update_time") or 0)
                max_iu = max(max_iu, ut)
                g = d.get("guid")
                if not g:
                    continue
                sg = str(g)
                itype = (d.get("type") or "").strip()
                new_fs = int(d.get("fetch_status") or 0)
                old_fs = fb.get(sg)
                if old_fs is None:
                    old_fs = new_fs
                fb[sg] = new_fs
                if itype not in _SCRAPE_TYPES:
                    continue
                if old_fs != 1 and new_fs == 1:
                    release_date = (
                        (d.get("release_date") or "").strip()
                        or (d.get("first_air_date") or "").strip()
                        or (d.get("air_date") or "").strip()
                    )
                    runtime = int(d.get("runtime") or 0)
                    ev = {
                        "title": (d.get("title") or "").strip(),
                        "item_type": itype,
                        "guid": g,
                        "season_number": d.get("season_number"),
                        "episode_number": d.get("episode_number"),
                        "tmdb_id": d.get("tmdb_id"),
                        "runtime": runtime,
                        "release_date": release_date,
                        "overview": (d.get("overview") or "").strip(),
                        "message": f"刮削完成: {itype}「{(d.get('title') or '').strip() or g}」",
                    }
                    self._emit(TRIM_SCRAPE_SUCCESS, ev, d, ut)

            # 3) 媒体文件关联（入库）
            q_media = """
            SELECT m.guid AS media_guid, m.item_guid, m.path, m.size, m.create_time, m.update_time,
                   i.type AS item_type, i.title AS item_title, i.season_number, i.episode_number
            FROM item_media m
            LEFT JOIN item i ON i.guid = m.item_guid
            WHERE COALESCE(m.update_time, m.create_time, 0) > ?
            ORDER BY COALESCE(m.update_time, m.create_time) ASC
            LIMIT 500
            """
            max_mt = lmt
            for r in conn.execute(q_media, (lmt,)):
                d = dict(r)
                ts = int(d.get("update_time") or d.get("create_time") or 0)
                max_mt = max(max_mt, ts)
                path = (d.get("path") or "").strip()
                ev = {
                    "title": (d.get("item_title") or "").strip(),
                    "item_type": d.get("item_type"),
                    "path": path,
                    "media_guid": d.get("media_guid"),
                    "item_guid": d.get("item_guid"),
                    "size": d.get("size"),
                    "season_number": d.get("season_number"),
                    "episode_number": d.get("episode_number"),
                    "message": f"媒体文件入库: {path or d.get('media_guid')}",
                }
                self._emit(TRIM_RESOURCE_ADDED, ev, d, ts)

            state["last_item_create_ms"] = max_ic
            state["last_item_update_ms"] = max_iu
            state["last_media_ts_ms"] = max_mt
            state["last_media_delete_ms"] = lmd
            state["fetch_by_guid"] = fb
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        self._load_dedup()
        state = self._load_state()
        self.logger.info("MediaDBPoller 启动 db=%s", self.db_path or "(未配置)")
        while self.running:
            try:
                if self.db_path and _TRIM_EVENTS & self.monitor_events:
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
                            "影视库轮询异常: %s（已连续 %s 次，期间同类错误已节流）",
                            e,
                            self._open_db_error_count,
                            exc_info=True,
                        )
                else:
                    self._open_db_error_count = 0
                    self.logger.error("影视库轮询异常: %s", e, exc_info=True)
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
            self.logger.warning("MediaDBPoller 启动对齐失败（连接数据库失败）: %s", e)
            return
        try:
            state = self._load_state()
            self._baseline(conn, state)
            self._save_state(state)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def start(self) -> None:
        if self.running:
            return
        if not self.db_path:
            self.logger.info("未配置 trim_media_db_path，跳过 MediaDBPoller")
            return
        if not (_TRIM_EVENTS & self.monitor_events):
            self.logger.info("monitor_events 未包含影视库文件事件，跳过 MediaDBPoller")
            return
        self._align_state_to_latest()
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="MediaDBPoller", daemon=False)
        self._thread.start()
        self.logger.info("MediaDBPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("MediaDBPoller 已停止")
