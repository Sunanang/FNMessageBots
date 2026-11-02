"""通知待发状态：SQLite 事务提交后才确认接收，发送成功后再删除。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from monitor.batch_dedupe import fingerprint_poll_batch_item
from monitor.models import JournalEntry

RETRY_SECONDS = 60


class OutboxWriteError(RuntimeError):
    """待发状态尚未持久化，上游不得确认或越过该记录。"""


class NotificationOutbox:
    def __init__(self, cursor_dir: str):
        directory = Path(cursor_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "notification_outbox.db"
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS poll_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _read(conn: sqlite3.Connection, name: str) -> Dict[str, Any]:
        row = conn.execute("SELECT payload FROM state WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def _write(conn: sqlite3.Connection, name: str, value: Dict[str, Any]) -> None:
        conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)",
                     (name, json.dumps(value, ensure_ascii=False)))

    def dnd_state(self) -> Dict[str, Any]:
        with self._connect() as conn:
            return self._read(conn, "dnd")

    def update_dnd(self, update: Callable[[Dict[str, Any]], Any]) -> Any:
        with self._connect() as conn:
            state = self._read(conn, "dnd")
            result = update(state)
            self._write(conn, "dnd", state)
            return result

    def enqueue_poll(self, items: List[Dict[str, Any]]) -> None:
        with self._connect() as conn:
            for item in items:
                entry = item.get("entry")
                payload = {key: value for key, value in item.items() if key not in {"handler", "entry"}}
                payload["entry"] = entry.to_dict() if isinstance(entry, JournalEntry) else {
                    key: getattr(entry, key, default)
                    for key, default in {
                        "cursor": "", "timestamp": "", "hostname": "", "syslog_identifier": "",
                        "message": "", "priority": 0, "pid": 0, "raw_data": "{}", "original_line": "",
                    }.items()
                }
                conn.execute("INSERT OR IGNORE INTO poll_events VALUES (?, ?)", (
                    fingerprint_poll_batch_item({**item, "row_id": None})
                    if (item.get("event_data") or {}).get("_source_cursor") else fingerprint_poll_batch_item(item),
                    json.dumps(payload, ensure_ascii=False),
                ))

    def poll_items(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM poll_events ORDER BY rowid").fetchall()
        return [self._decode_poll(row[0]) for row in rows]

    @staticmethod
    def _decode_poll(raw: str) -> Dict[str, Any]:
        item = json.loads(raw)
        item["entry"] = JournalEntry(**item["entry"])
        return item

    def claim_poll(self, now: float) -> Optional[Dict[str, Any]]:
        """失败批次固定成员和顺序，新增事件另排；重启后也保持 60 秒节流。"""
        with self._connect() as conn:
            job: Dict[str, Any] = self._read(conn, "poll_job")
            if job and 0 <= now - job["last_attempt"] < RETRY_SECONDS:
                return None
            if not job:
                rows = conn.execute("SELECT id FROM poll_events ORDER BY rowid LIMIT 500").fetchall()
                if not rows:
                    return None
                job = dict(ids=[row[0] for row in rows])
            job["last_attempt"] = now
            self._write(conn, "poll_job", job)
            items = []
            for key in job["ids"]:
                row = conn.execute("SELECT payload FROM poll_events WHERE id=?", (key,)).fetchone()
                if row:
                    items.append(self._decode_poll(row[0]))
            return {**job, "items": items}

    def ack_poll(self, ids: List[str]) -> None:
        with self._connect() as conn:
            conn.executemany("DELETE FROM poll_events WHERE id=?", [(key,) for key in ids])
            self._write(conn, "poll_job", {})
