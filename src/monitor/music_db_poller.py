"""
音乐库 music.db 轮询：
- track 入库 / 软删除移出 → 连续 60 秒静默后汇总（MUSIC_LIBRARY_SUMMARY）
- favorite_track 新增 → 即时 MUSIC_FAVORITED
- playlist_track 新增 → 即时 MUSIC_PLAYLIST_TRACK_ADDED
不做取消收藏 / 移出歌单 / 播放次数。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from .models import JournalEntry
from .music_summary import (
    MUSIC_DEBOUNCE_SEC,
    MUSIC_SUMMARY_EVENT,
    build_music_summary,
)
from .sqlite_uri import connect_readonly_with_fallback

MUSIC_TRACK_ADDED = "MUSIC_TRACK_ADDED"
MUSIC_TRACK_REMOVED = "MUSIC_TRACK_REMOVED"
MUSIC_FAVORITED = "MUSIC_FAVORITED"
MUSIC_PLAYLIST_TRACK_ADDED = "MUSIC_PLAYLIST_TRACK_ADDED"

MUSIC_POLL_EVENTS: Set[str] = {
    MUSIC_TRACK_ADDED,
    MUSIC_TRACK_REMOVED,
    MUSIC_FAVORITED,
    MUSIC_PLAYLIST_TRACK_ADDED,
}
_MUSIC_LIBRARY_STAGES: Set[str] = {MUSIC_TRACK_ADDED, MUSIC_TRACK_REMOVED}

_DEDUP_TTL = 24 * 3600
_ALIVE_ID_CAP = 120_000
_PREVIEW_TITLE_CAP = 80
# 静默批次只保留有限明细做预览；总数走 stage_counts，避免万人曲库扫描撑爆状态文件
_PENDING_DETAIL_CAP = 80
_SQL_IN_CHUNK = 400


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


def _ts_to_local_str(ts: Any) -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    if ts is None:
        return now
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return now
    if v <= 0:
        return now
    if v > 10_000_000_000:
        v = int(v / 1000)
    try:
        return datetime.fromtimestamp(v, tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return now


def _track_deleted_expr(cols: Set[str], alias: str = "t") -> str:
    parts: List[str] = []
    if "is_audio_file_deleted" in cols:
        parts.append(f"COALESCE({alias}.is_audio_file_deleted, 0) != 0")
    if "is_admin_deleted" in cols:
        parts.append(f"COALESCE({alias}.is_admin_deleted, 0) != 0")
    if not parts:
        return "0"
    return "(" + " OR ".join(parts) + ")"


class MusicDBPoller:
    """轮询 music.db：曲目入库/移出汇总、收藏、加入歌单。"""

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
        self._state_file = self.cursor_dir / "music_db_poller_state.json"
        self._dedup_file = self.cursor_dir / "music_db_poller_dedup.json"
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._dedup_seen: Dict[str, int] = {}
        self._open_db_error_count = 0
        self._state_lock = threading.RLock()
        self._active_state: Optional[Dict[str, Any]] = None
        self._schema_cache: Dict[str, Set[str]] = {}
        self._schema_warned: Set[str] = set()

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
            if not (_MUSIC_LIBRARY_STAGES & self.monitor_events):
                with self._state_lock:
                    if self._active_state is not None:
                        self._active_state["music_pending"] = {}
                    saved = self._load_state()
                    saved["music_pending"] = {}
                    self._save_state(saved)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if db_path is not None:
            self.db_path = (db_path or "").strip()
            self._schema_cache.clear()

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
            "last_track_id": 0,
            "last_favorite_id": 0,
            "last_playlist_track_id": 0,
            "alive_track_ids": [],
            "music_pending": {},
        }
        try:
            if self._state_file.exists():
                raw = self._state_file.read_text().strip()
                if raw:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        default.update(obj)
        except Exception as e:
            self.logger.warning("读取音乐库轮询状态失败: %s", e)
        if not isinstance(default.get("alive_track_ids"), list):
            default["alive_track_ids"] = []
        if not isinstance(default.get("music_pending"), dict):
            default["music_pending"] = {}
        return default

    def _save_state(self, state: Dict[str, Any]) -> bool:
        with self._state_lock:
            if not (_MUSIC_LIBRARY_STAGES & self.monitor_events):
                state["music_pending"] = {}
            return self._write_state(state)

    def _write_state(self, state: Dict[str, Any]) -> bool:
        temporary = None
        try:
            state["version"] = 1
            alive = state.get("alive_track_ids")
            if isinstance(alive, list) and len(alive) > _ALIVE_ID_CAP:
                state["alive_track_ids"] = alive[-_ALIVE_ID_CAP:]
            fd, temporary = tempfile.mkstemp(prefix=".music-state-", dir=str(self.cursor_dir))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_file)
            return True
        except Exception as e:
            self.logger.warning("写入音乐库轮询状态失败: %s", e)
            return False
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

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
            self.logger.warning("读取音乐库去重缓存失败: %s", e)
        self._dedup_seen = {}

    def _save_dedup(self) -> None:
        try:
            self._dedup_file.write_text(json.dumps(self._dedup_seen, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入音乐库去重缓存失败: %s", e)

    def _prune_dedup(self) -> None:
        now = int(time.time())
        self._dedup_seen = {k: v for k, v in self._dedup_seen.items() if int(v) >= now - _DEDUP_TTL}

    def _connect(self) -> sqlite3.Connection:
        conn = connect_readonly_with_fallback(
            self.db_path,
            timeout=10.0,
            table_probe_sql="SELECT 1 FROM track LIMIT 1",
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _table_cols(self, conn: sqlite3.Connection, table: str) -> Set[str]:
        key = table.strip().lower()
        cached = self._schema_cache.get(key)
        if cached is not None:
            return cached
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            cols = {str(r[1]) for r in rows if r and r[1]}
        except Exception as e:
            self.logger.warning("读取表结构失败 %s: %s", table, e)
            cols = set()
        self._schema_cache[key] = cols
        return cols

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        key = f"exists:{table.strip().lower()}"
        cached_flag = self._schema_cache.get(key)
        if cached_flag is not None:
            return bool(cached_flag)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone()
            exists = bool(row)
        except Exception:
            exists = False
        # 复用 schema_cache：用单元素 set 表示 True，空 set 表示 False
        self._schema_cache[key] = {"1"} if exists else set()
        return exists

    def _fingerprint(self, kind: str, key: str) -> str:
        return f"{kind}|{key}"

    def _emit(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        raw_obj: Dict[str, Any],
        ts_field: Any,
    ) -> bool:
        if event_type != MUSIC_SUMMARY_EVENT and self.monitor_events and event_type not in self.monitor_events:
            return False
        key = event_data.get("batch_id") or json.dumps(raw_obj, sort_keys=True, ensure_ascii=False)
        fp = self._fingerprint(event_type, hashlib.sha256(str(key).encode()).hexdigest())
        now = int(time.time())
        if fp in self._dedup_seen:
            return True
        handler = self.event_handlers.get(event_type)
        if not handler:
            return False
        event_data = dict(event_data)
        event_data.setdefault("_source", "music_db")
        event_data.setdefault("_source_cursor", fp)
        event_data.setdefault("_source_event_id", event_type)
        raw_log = json.dumps(raw_obj, ensure_ascii=False)
        ts = (
            datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            if event_type == MUSIC_SUMMARY_EVENT
            else _ts_to_local_str(ts_field)
        )
        entry = JournalEntry(
            cursor=f"music-{event_type}-{now}-{hash(fp) & 0xFFFFFFFF:x}",
            timestamp=ts,
            hostname="music.db",
            syslog_identifier=event_type,
            message=raw_log,
            priority=0,
            pid=0,
            raw_data=raw_log,
            original_line=raw_log,
        )
        try:
            if event_type != MUSIC_SUMMARY_EVENT and self.poll_batch_summary_enabled and self.summary_batch_enqueue:
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
                            "source": "music_db",
                        }
                    ]
                )
            else:
                result = handler(event_data, entry)
                if result is False or getattr(result, "success", None) is False:
                    return False
            self._dedup_seen[fp] = now
            return True
        except Exception as e:
            self.logger.error("处理音乐库事件失败 %s: %s", event_type, e, exc_info=True)
            return False

    def _artist_subquery(self, conn: sqlite3.Connection, track_cols: Set[str]) -> str:
        # track_artist / artist 是官方库常见结构；表缺失时退化为空串，避免整轮 SQL 失败。
        if "id" not in track_cols:
            return "''"
        if not (
            self._table_exists(conn, "track_artist")
            and self._table_exists(conn, "artist")
        ):
            return "''"
        return (
            "(SELECT GROUP_CONCAT(a.name, ' / ') FROM track_artist ta "
            "JOIN artist a ON a.id = ta.artist_id WHERE ta.track_id = t.id)"
        )

    def _album_expr(self, conn: sqlite3.Connection, track_cols: Set[str]) -> str:
        if "album_id" not in track_cols or not self._table_exists(conn, "album"):
            return "''"
        album_cols = self._table_cols(conn, "album")
        if "name" not in album_cols:
            return "''"
        return "(SELECT al.name FROM album al WHERE al.id = t.album_id)"

    def _fetch_track_meta(
        self, conn: sqlite3.Connection, track_id: int
    ) -> Dict[str, Any]:
        return self._fetch_track_meta_many(conn, [track_id]).get(
            track_id,
            {"track_id": track_id, "title": "", "artist": "", "album": "", "guid": ""},
        )

    def _fetch_track_meta_many(
        self, conn: sqlite3.Connection, track_ids: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        out: Dict[int, Dict[str, Any]] = {}
        ids = [int(x) for x in track_ids if _maybe_int(x) is not None]
        if not ids:
            return out
        track_cols = self._table_cols(conn, "track")
        if "id" not in track_cols:
            for tid in ids:
                out[tid] = {"track_id": tid, "title": "", "artist": "", "album": "", "guid": ""}
            return out
        title_col = "t.title" if "title" in track_cols else "NULL"
        guid_col = "t.guid" if "guid" in track_cols else "NULL"
        artist_sql = self._artist_subquery(conn, track_cols)
        album_sql = self._album_expr(conn, track_cols)
        for i in range(0, len(ids), _SQL_IN_CHUNK):
            chunk = ids[i : i + _SQL_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            try:
                rows = conn.execute(
                    f"""
                    SELECT t.id AS track_id,
                           {title_col} AS title,
                           {guid_col} AS guid,
                           {artist_sql} AS artist,
                           {album_sql} AS album
                    FROM track t
                    WHERE t.id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
            except Exception as e:
                self.logger.debug("批量查询曲目元数据失败: %s", e)
                for tid in chunk:
                    out.setdefault(
                        tid,
                        {"track_id": tid, "title": "", "artist": "", "album": "", "guid": ""},
                    )
                continue
            seen = set()
            for row in rows:
                d = dict(row)
                tid = int(d.get("track_id") or 0)
                if tid <= 0:
                    continue
                seen.add(tid)
                out[tid] = {
                    "track_id": tid,
                    "title": (d.get("title") or "").strip()[:_PREVIEW_TITLE_CAP],
                    "artist": (d.get("artist") or "").strip()[:_PREVIEW_TITLE_CAP],
                    "album": (d.get("album") or "").strip()[:_PREVIEW_TITLE_CAP],
                    "guid": (d.get("guid") or "").strip(),
                }
            for tid in chunk:
                if tid not in seen:
                    out[tid] = {
                        "track_id": tid,
                        "title": "",
                        "artist": "",
                        "album": "",
                        "guid": "",
                    }
        return out

    def _unavailable_expr(self, conn: sqlite3.Connection, track_cols: Set[str]) -> str:
        """曲目视为「不在库」：软删除标志，或关联 audio_file 已物理失效。"""
        deleted = _track_deleted_expr(track_cols, "t")
        physical = ""
        if (
            "audio_file_id" in track_cols
            and self._table_exists(conn, "audio_file")
            and "is_physical_file_deleted" in self._table_cols(conn, "audio_file")
        ):
            physical = (
                " OR EXISTS (SELECT 1 FROM audio_file af "
                "WHERE af.id = t.audio_file_id AND COALESCE(af.is_physical_file_deleted, 0) != 0)"
            )
        return f"({deleted}{physical})"

    def _alive_sql(self, conn: sqlite3.Connection, track_cols: Set[str]) -> str:
        return (
            "SELECT t.id FROM track t "
            f"WHERE NOT {self._unavailable_expr(conn, track_cols)} "
            "ORDER BY t.id ASC"
        )

    def _load_alive_ids(self, conn: sqlite3.Connection) -> List[int]:
        track_cols = self._table_cols(conn, "track")
        if "id" not in track_cols:
            return []
        try:
            rows = conn.execute(self._alive_sql(conn, track_cols)).fetchall()
        except Exception as e:
            self.logger.warning("读取有效曲目 id 失败: %s", e)
            return []
        ids = [int(r[0]) for r in rows if r and r[0] is not None]
        if len(ids) > _ALIVE_ID_CAP:
            self.logger.warning(
                "有效曲目过多（%s），移出库差分仅保留 id 最大的 %s 个",
                len(ids),
                _ALIVE_ID_CAP,
            )
            # 已按 id ASC：截取末尾 = 稳定保留最大 id，避免无序切片导致假移出
            ids = ids[-_ALIVE_ID_CAP:]
        return ids

    def _still_alive_among(self, conn: sqlite3.Connection, candidate_ids: Set[int]) -> Set[int]:
        """在已知候选集中查出仍有效的 id（增量移出检测，避免每轮全表扫描）。"""
        if not candidate_ids:
            return set()
        track_cols = self._table_cols(conn, "track")
        if "id" not in track_cols:
            return set()
        unavailable = self._unavailable_expr(conn, track_cols)
        still: Set[int] = set()
        ordered = list(candidate_ids)
        for i in range(0, len(ordered), _SQL_IN_CHUNK):
            chunk = ordered[i : i + _SQL_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            try:
                rows = conn.execute(
                    f"SELECT t.id FROM track t WHERE t.id IN ({placeholders}) AND NOT {unavailable}",
                    chunk,
                ).fetchall()
            except Exception as e:
                self.logger.warning("增量检查有效曲目失败: %s", e)
                return set(candidate_ids)
            still.update(int(r[0]) for r in rows if r and r[0] is not None)
        return still

    def _empty_pending(self) -> Dict[str, Any]:
        return {
            "batch_id": uuid.uuid4().hex,
            "first_activity_ts": time.time(),
            "last_activity_ts": time.time(),
            "tracks": {},
            "stage_counts": {MUSIC_TRACK_ADDED: 0, MUSIC_TRACK_REMOVED: 0},
            "added_ids": [],
        }

    def _accumulate_library(
        self,
        state: Dict[str, Any],
        event_type: str,
        data: Dict[str, Any],
        raw: Dict[str, Any],
    ) -> None:
        if event_type not in self.monitor_events:
            return
        fingerprint = self._fingerprint(
            event_type,
            hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        )
        if fingerprint in self._dedup_seen:
            return
        now = time.time()
        pending = state.setdefault("music_pending", {})
        if not pending or not isinstance(pending.get("tracks"), dict):
            pending = self._empty_pending()
            pending["first_activity_ts"] = now
            state["music_pending"] = pending
        stage_counts = pending.setdefault(
            "stage_counts", {MUSIC_TRACK_ADDED: 0, MUSIC_TRACK_REMOVED: 0}
        )
        added_ids = {
            int(x)
            for x in (pending.get("added_ids") or [])
            if _maybe_int(x) is not None
        }
        pending["last_activity_ts"] = now
        tid = data.get("track_id")
        tid_i = _maybe_int(tid)
        key = f"track:{tid}" if tid is not None else f"raw:{fingerprint}"
        tracks = pending["tracks"]
        info = tracks.get(key) or {}
        stages = set(info.get("stages", []))

        # 同一静默窗口内入库后又移出：净变化为 0（即使明细因 cap 未保留也要抵消）
        if event_type == MUSIC_TRACK_REMOVED and tid_i is not None and tid_i in added_ids:
            added_ids.discard(tid_i)
            pending["added_ids"] = list(added_ids)
            stage_counts[MUSIC_TRACK_ADDED] = max(0, int(stage_counts.get(MUSIC_TRACK_ADDED) or 0) - 1)
            tracks.pop(key, None)
            self._dedup_seen[fingerprint] = int(now)
            if (
                not tracks
                and int(stage_counts.get(MUSIC_TRACK_ADDED) or 0) == 0
                and int(stage_counts.get(MUSIC_TRACK_REMOVED) or 0) == 0
            ):
                state["music_pending"] = {}
            return

        if event_type not in stages:
            stage_counts[event_type] = int(stage_counts.get(event_type) or 0) + 1
        stages.add(event_type)
        if event_type == MUSIC_TRACK_ADDED and tid_i is not None:
            added_ids.add(tid_i)
            pending["added_ids"] = list(added_ids)
        else:
            pending["added_ids"] = list(added_ids)

        # 明细只保留有限条供预览；超出后仍计入 stage_counts
        if key not in tracks and len(tracks) >= _PENDING_DETAIL_CAP:
            self._dedup_seen[fingerprint] = int(now)
            return

        info.update(
            track_key=key,
            stages=sorted(stages),
            track_id=tid,
            title=data.get("title") or info.get("title") or "",
            artist=data.get("artist") or info.get("artist") or "",
            album=data.get("album") or info.get("album") or "",
            guid=data.get("guid") or info.get("guid") or "",
        )
        tracks[key] = info
        self._dedup_seen[fingerprint] = int(now)

    def _flush_music_pending(self, state: Dict[str, Any]) -> None:
        pending = state.get("music_pending") or {}
        if not isinstance(pending, dict):
            state["music_pending"] = {}
            return
        stage_counts = pending.get("stage_counts")
        if not isinstance(stage_counts, dict):
            stage_counts = {MUSIC_TRACK_ADDED: 0, MUSIC_TRACK_REMOVED: 0}
            pending["stage_counts"] = stage_counts
        tracks = pending.get("tracks")
        if not isinstance(tracks, dict):
            tracks = {}
            pending["tracks"] = tracks
        for key, row in list(tracks.items()):
            row["stages"] = [stage for stage in row.get("stages", []) if stage in self.monitor_events]
            if not row["stages"]:
                del tracks[key]
        for stage in (MUSIC_TRACK_ADDED, MUSIC_TRACK_REMOVED):
            if stage not in self.monitor_events:
                stage_counts[stage] = 0
        has_work = bool(tracks) or any(int(stage_counts.get(s) or 0) > 0 for s in _MUSIC_LIBRARY_STAGES)
        if not has_work:
            state["music_pending"] = {}
            self._save_state(state)
            return
        if time.time() - pending["last_activity_ts"] < MUSIC_DEBOUNCE_SEC:
            return
        if pending.get("last_attempt_ts") and time.time() - pending["last_attempt_ts"] < MUSIC_DEBOUNCE_SEC:
            return
        data = build_music_summary(pending)
        pending["last_attempt_ts"] = time.time()
        if self._emit(MUSIC_SUMMARY_EVENT, data, data, None):
            self._save_dedup()
            state["music_pending"] = {}
            self._save_state(state)
            self.logger.info(
                "音乐库本轮汇总已处理：入库 %s 首，移出 %s 首",
                data["by_type"].get(MUSIC_TRACK_ADDED, 0),
                data["by_type"].get(MUSIC_TRACK_REMOVED, 0),
            )
        else:
            self.logger.warning(
                "音乐汇总尚未投递，保留批次 %s，至少 %s 秒后重试",
                pending["batch_id"],
                MUSIC_DEBOUNCE_SEC,
            )

    def _baseline(self, conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
        track_cols = self._table_cols(conn, "track")
        fav_cols = self._table_cols(conn, "favorite_track")
        plt_cols = self._table_cols(conn, "playlist_track")

        last_track = 0
        if "id" in track_cols:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM track").fetchone()
            last_track = int(row[0] or 0)
        last_fav = 0
        if "id" in fav_cols:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM favorite_track").fetchone()
            last_fav = int(row[0] or 0)
        last_plt = 0
        if "id" in plt_cols:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM playlist_track").fetchone()
            last_plt = int(row[0] or 0)

        state["last_track_id"] = last_track
        state["last_favorite_id"] = last_fav
        state["last_playlist_track_id"] = last_plt
        if MUSIC_TRACK_REMOVED in self.monitor_events:
            state["alive_track_ids"] = self._load_alive_ids(conn)
        else:
            state["alive_track_ids"] = []
        state["initialized"] = True
        state["music_pending"] = {}
        if MUSIC_FAVORITED in self.monitor_events and "id" not in fav_cols:
            self._warn_schema_once(
                "favorite_track.id",
                "favorite_track 缺少 id 列，无法增量检测收藏（MUSIC_FAVORITED 将不会推送）",
            )
        if MUSIC_PLAYLIST_TRACK_ADDED in self.monitor_events and "id" not in plt_cols:
            self._warn_schema_once(
                "playlist_track.id",
                "playlist_track 缺少 id 列，无法增量检测加入歌单（MUSIC_PLAYLIST_TRACK_ADDED 将不会推送）",
            )
        self.logger.info(
            "音乐库数据库轮询已对齐当前水位（不推送历史）: track_id=%s favorite_id=%s playlist_track_id=%s alive=%s",
            last_track,
            last_fav,
            last_plt,
            len(state["alive_track_ids"]),
        )

    def _warn_schema_once(self, key: str, message: str) -> None:
        if key in self._schema_warned:
            return
        self._schema_warned.add(key)
        self.logger.warning(message)

    def _poll_library_changes(self, conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
        track_cols = self._table_cols(conn, "track")
        if "id" not in track_cols:
            return
        unavailable = self._unavailable_expr(conn, track_cols)
        title_col = "t.title" if "title" in track_cols else "NULL"
        guid_col = "t.guid" if "guid" in track_cols else "NULL"
        created_col = "t.created_at" if "created_at" in track_cols else "NULL"
        artist_sql = self._artist_subquery(conn, track_cols)
        album_sql = self._album_expr(conn, track_cols)

        last_tid = int(state.get("last_track_id") or 0)
        max_tid = last_tid
        newly_added_ids: List[int] = []

        if MUSIC_TRACK_ADDED in self.monitor_events:
            q = f"""
            SELECT t.id AS track_id, {title_col} AS title, {guid_col} AS guid,
                   {created_col} AS created_at,
                   {artist_sql} AS artist, {album_sql} AS album
            FROM track t
            WHERE t.id > ? AND NOT {unavailable}
            ORDER BY t.id ASC
            """
            for row in conn.execute(q, (last_tid,)):
                d = dict(row)
                tid = int(d.get("track_id") or 0)
                max_tid = max(max_tid, tid)
                newly_added_ids.append(tid)
                ev = {
                    "track_id": tid,
                    "title": (d.get("title") or "").strip()[:_PREVIEW_TITLE_CAP],
                    "artist": (d.get("artist") or "").strip()[:_PREVIEW_TITLE_CAP],
                    "album": (d.get("album") or "").strip()[:_PREVIEW_TITLE_CAP],
                    "guid": (d.get("guid") or "").strip(),
                    "message": f"新曲入库: {(d.get('title') or '').strip() or tid}",
                }
                self._accumulate_library(state, MUSIC_TRACK_ADDED, ev, d)

        # 即使未勾选入库，也推进水位，避免日后勾选时回放历史
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM track").fetchone()
            max_tid = max(max_tid, int(row[0] or 0))
        except Exception:
            pass
        state["last_track_id"] = max_tid

        if MUSIC_TRACK_REMOVED in self.monitor_events:
            prev_alive = {
                int(x) for x in (state.get("alive_track_ids") or []) if _maybe_int(x) is not None
            }
            if not prev_alive:
                # 首次启用移出监控：只种子快照，不回放历史失效
                state["alive_track_ids"] = self._load_alive_ids(conn)
            else:
                still_alive = self._still_alive_among(conn, prev_alive)
                removed_ids = sorted(prev_alive - still_alive)
                meta_by_id = self._fetch_track_meta_many(conn, removed_ids)
                for tid in removed_ids:
                    meta = meta_by_id.get(tid) or {
                        "track_id": tid,
                        "title": "",
                        "artist": "",
                        "album": "",
                        "guid": "",
                    }
                    ev = {
                        **meta,
                        "message": f"曲目移出库: {meta.get('title') or tid}",
                    }
                    raw = {"track_id": tid, "removed": True}
                    self._accumulate_library(state, MUSIC_TRACK_REMOVED, ev, raw)
                current_alive = still_alive | set(newly_added_ids)
                state["alive_track_ids"] = sorted(current_alive)[-_ALIVE_ID_CAP:]
        else:
            state["alive_track_ids"] = []

    def _user_label(self, conn: sqlite3.Connection, user_id: Any) -> str:
        uid = _maybe_int(user_id)
        if uid is None:
            return ""
        user_cols = self._table_cols(conn, "user")
        if "id" not in user_cols:
            return f"用户#{uid}"
        fields = []
        if "guid" in user_cols:
            fields.append("guid")
        if "nas_uid" in user_cols:
            fields.append("nas_uid")
        if "name" in user_cols:
            fields.append("name")
        if not fields:
            return f"用户#{uid}"
        try:
            row = conn.execute(
                f"SELECT {', '.join(fields)} FROM user WHERE id = ?", (uid,)
            ).fetchone()
        except Exception:
            return f"用户#{uid}"
        if not row:
            return f"用户#{uid}"
        d = dict(row)
        name = (d.get("name") or "").strip()
        nas = d.get("nas_uid")
        guid = (d.get("guid") or "").strip()
        if name:
            return name
        if nas is not None and str(nas).strip():
            return f"NAS UID {nas}"
        if guid:
            return guid[:16]
        return f"用户#{uid}"

    def _playlist_name(self, conn: sqlite3.Connection, playlist_id: Any) -> str:
        pid = _maybe_int(playlist_id)
        if pid is None:
            return ""
        cols = self._table_cols(conn, "playlist")
        if "id" not in cols:
            return f"歌单#{pid}"
        name_col = "name" if "name" in cols else ("title" if "title" in cols else None)
        if not name_col:
            return f"歌单#{pid}"
        try:
            row = conn.execute(
                f"SELECT {name_col} AS name FROM playlist WHERE id = ?", (pid,)
            ).fetchone()
        except Exception:
            return f"歌单#{pid}"
        if not row:
            return f"歌单#{pid}"
        return (dict(row).get("name") or "").strip() or f"歌单#{pid}"

    def _poll_favorites(self, conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
        if MUSIC_FAVORITED not in self.monitor_events:
            # 仍推进水位
            fav_cols = self._table_cols(conn, "favorite_track")
            if "id" in fav_cols:
                try:
                    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM favorite_track").fetchone()
                    state["last_favorite_id"] = max(
                        int(state.get("last_favorite_id") or 0), int(row[0] or 0)
                    )
                except Exception:
                    pass
            return
        fav_cols = self._table_cols(conn, "favorite_track")
        if "id" not in fav_cols or "track_id" not in fav_cols:
            self.logger.debug("favorite_track 缺少 id/track_id，跳过收藏轮询")
            return
        last_id = int(state.get("last_favorite_id") or 0)
        created_col = "created_at" if "created_at" in fav_cols else ("NULL")
        user_col = "user_id" if "user_id" in fav_cols else "NULL"
        q = f"""
        SELECT id, track_id, {user_col} AS user_id, {created_col} AS created_at
        FROM favorite_track
        WHERE id > ?
        ORDER BY id ASC
        """
        max_id = last_id
        for row in conn.execute(q, (last_id,)):
            d = dict(row)
            fid = int(d.get("id") or 0)
            tid = int(d.get("track_id") or 0)
            meta = self._fetch_track_meta(conn, tid) if tid else {}
            user_label = self._user_label(conn, d.get("user_id"))
            ev = {
                **meta,
                "favorite_id": fid,
                "user_id": _maybe_int(d.get("user_id")),
                "user_label": user_label,
                "message": f"收藏: {meta.get('title') or tid}",
            }
            if not self._emit(MUSIC_FAVORITED, ev, d, d.get("created_at")):
                # 投递失败则停在此 id，下轮重试，避免丢收藏通知
                break
            max_id = max(max_id, fid)
        state["last_favorite_id"] = max_id

    def _poll_playlist_tracks(self, conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
        plt_cols = self._table_cols(conn, "playlist_track")
        if MUSIC_PLAYLIST_TRACK_ADDED not in self.monitor_events:
            if "id" in plt_cols:
                try:
                    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM playlist_track").fetchone()
                    state["last_playlist_track_id"] = max(
                        int(state.get("last_playlist_track_id") or 0), int(row[0] or 0)
                    )
                except Exception:
                    pass
            return
        if "id" not in plt_cols or "track_id" not in plt_cols:
            self.logger.debug("playlist_track 缺少 id/track_id，跳过歌单轮询")
            return
        last_id = int(state.get("last_playlist_track_id") or 0)
        added_col = "added_at" if "added_at" in plt_cols else (
            "created_at" if "created_at" in plt_cols else "NULL"
        )
        playlist_col = "playlist_id" if "playlist_id" in plt_cols else "NULL"
        user_col = "user_id" if "user_id" in plt_cols else "NULL"
        q = f"""
        SELECT id, track_id, {playlist_col} AS playlist_id, {user_col} AS user_id,
               {added_col} AS added_at
        FROM playlist_track
        WHERE id > ?
        ORDER BY id ASC
        """
        max_id = last_id
        for row in conn.execute(q, (last_id,)):
            d = dict(row)
            rid = int(d.get("id") or 0)
            tid = int(d.get("track_id") or 0)
            meta = self._fetch_track_meta(conn, tid) if tid else {}
            pl_name = self._playlist_name(conn, d.get("playlist_id"))
            user_label = self._user_label(conn, d.get("user_id"))
            ev = {
                **meta,
                "playlist_track_id": rid,
                "playlist_id": _maybe_int(d.get("playlist_id")),
                "playlist_name": pl_name,
                "user_id": _maybe_int(d.get("user_id")),
                "user_label": user_label,
                "message": f"加入歌单「{pl_name or '?'}」: {meta.get('title') or tid}",
            }
            if not self._emit(MUSIC_PLAYLIST_TRACK_ADDED, ev, d, d.get("added_at")):
                break
            max_id = max(max_id, rid)
        state["last_playlist_track_id"] = max_id

    def _poll_once(self, state: Dict[str, Any]) -> None:
        self._active_state = state
        if not self.db_path or not os.path.exists(self.db_path):
            return
        try:
            conn = self._connect()
            self._open_db_error_count = 0
        except Exception as e:
            msg = str(e).lower()
            if "unable to open" in msg or "readonly" in msg or "permission" in msg:
                self._open_db_error_count += 1
                if self._open_db_error_count == 1 or self._open_db_error_count % 12 == 0:
                    self.logger.warning(
                        "连接 music.db 失败: %s（已连续 %s 次，期间同类错误已节流）",
                        e,
                        self._open_db_error_count,
                    )
            else:
                self._open_db_error_count = 0
                self.logger.warning("连接 music.db 失败: %s", e)
            return

        try:
            db_identity = os.path.realpath(self.db_path)
            if state.get("db_path") and state["db_path"] != db_identity:
                state.update(initialized=False, music_pending={}, alive_track_ids=[])
                self._dedup_seen.clear()
                self._schema_cache.clear()
            state["db_path"] = db_identity

            if not state.get("initialized"):
                self._baseline(conn, state)
                if self._save_state(state):
                    self._flush_music_pending(state)
                return

            if _MUSIC_LIBRARY_STAGES & self.monitor_events:
                self._poll_library_changes(conn, state)
            else:
                # 未勾选入库/移出时仍推进 track 水位
                track_cols = self._table_cols(conn, "track")
                if "id" in track_cols:
                    try:
                        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM track").fetchone()
                        state["last_track_id"] = max(
                            int(state.get("last_track_id") or 0), int(row[0] or 0)
                        )
                    except Exception:
                        pass
                state["alive_track_ids"] = []
                state["music_pending"] = {}

            self._poll_favorites(conn, state)
            self._poll_playlist_tracks(conn, state)

            if self._save_state(state):
                self._flush_music_pending(state)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        self._load_dedup()
        state = self._load_state()
        # 每次启动重新对齐水位，不补推历史；进行中的静默批次一并丢弃
        state["initialized"] = False
        state["music_pending"] = {}
        self.logger.info(
            "MusicDBPoller 启动 db=%s（曲目变更冷却汇总 %ss）",
            self.db_path or "(未配置)",
            MUSIC_DEBOUNCE_SEC,
        )
        while self.running:
            try:
                if self.db_path and (MUSIC_POLL_EVENTS & self.monitor_events):
                    self._poll_once(state)
                    self._prune_dedup()
                    self._save_dedup()
            except Exception as e:
                self.logger.error("音乐库轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    try:
                        if self.db_path and (_MUSIC_LIBRARY_STAGES & self.monitor_events):
                            pending = state.get("music_pending") or {}
                            if pending.get("tracks"):
                                last_act = float(pending.get("last_activity_ts") or 0)
                                if last_act > 0 and (time.time() - last_act) >= float(MUSIC_DEBOUNCE_SEC):
                                    self._flush_music_pending(state)
                    except Exception as e:
                        self.logger.warning("停止时刷新音乐汇总失败: %s", e)
                    return
                time.sleep(1)

    def start(self) -> None:
        if self.running:
            return
        if not self.db_path:
            self.logger.info("未配置 music_db_path，跳过 MusicDBPoller")
            return
        if not (MUSIC_POLL_EVENTS & self.monitor_events):
            self.logger.info("monitor_events 未包含音乐库事件，跳过 MusicDBPoller")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="MusicDBPoller", daemon=False)
        self._thread.start()
        self.logger.info("MusicDBPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("MusicDBPoller 已停止")
