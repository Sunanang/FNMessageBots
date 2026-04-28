"""
fn-scheduler 任务执行结果轮询器
轮询 scheduler.db 的 task_results 表，仅处理已完成（finished_at 不为空）的记录。
含回看窗口，应对执行结果写入延迟问题。
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import JournalEntry


SCHEDULER_TASK_SUCCESS_EVENT = "SCHEDULER_TASK_SUCCESS"
SCHEDULER_TASK_FAILED_EVENT = "SCHEDULER_TASK_FAILED"
SCHEDULER_TASK_CONDITION_FAILED_EVENT = "SCHEDULER_TASK_CONDITION_FAILED"

SCHEDULER_POLL_EVENTS = frozenset({
    SCHEDULER_TASK_SUCCESS_EVENT,
    SCHEDULER_TASK_FAILED_EVENT,
    SCHEDULER_TASK_CONDITION_FAILED_EVENT,
})

# 回看窗口（秒）：处理写入延迟，避免漏报
SCHEDULER_LOOKBACK_SECONDS = 600
# 去重缓存 TTL（秒）：3 天
SCHEDULER_DEDUP_TTL_SECONDS = 3 * 24 * 3600
# 日志预览长度（字符）
SCHEDULER_LOG_PREVIEW_LIMIT = 500


def _iso_to_timestamp(iso_str: Optional[str]) -> float:
    """将 ISO 8601 字符串转换为 UTC 时间戳（秒）。失败返回 0.0。"""
    if not iso_str:
        return 0.0
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(iso_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return 0.0


def _fmt_duration(started_at: Optional[str], finished_at: Optional[str]) -> str:
    """计算并格式化执行耗时。"""
    s = _iso_to_timestamp(started_at)
    f = _iso_to_timestamp(finished_at)
    if s <= 0 or f <= 0 or f < s:
        return ""
    sec = int(f - s)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"


def _iso_to_display(iso_str: Optional[str]) -> str:
    """将 ISO 8601 字符串转为本地时间显示格式（YYYY-MM-DD HH:MM:SS）。"""
    if not iso_str:
        return ""
    ts = _iso_to_timestamp(iso_str)
    if ts <= 0:
        return iso_str.strip()
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str.strip()


def _status_to_event_type(status: str) -> Optional[str]:
    if status == "success":
        return SCHEDULER_TASK_SUCCESS_EVENT
    if status == "failed":
        return SCHEDULER_TASK_FAILED_EVENT
    if status == "condition_failed":
        return SCHEDULER_TASK_CONDITION_FAILED_EVENT
    return None


TRIGGER_REASON_LABELS: Dict[str, str] = {
    "schedule": "计划触发",
    "condition": "条件触发",
    "condition_check": "条件检查",
    "system_boot": "系统启动",
    "system_shutdown": "系统关闭",
    "manual": "手动触发",
}


class SchedulerDBPoller:
    """轮询 fn-scheduler 的 scheduler.db，检测任务执行结果并分发事件。"""

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
    ):
        self.db_path = db_path
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, int(poll_interval or 5))
        self.monitor_events = set(monitor_events or [])
        self.event_handlers: Dict[str, Callable] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._cursor_file = self.cursor_dir / "scheduler_db_poller_cursor.json"
        self._dedup_file = self.cursor_dir / "scheduler_db_poller_dedup.json"
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
            self.db_path = db_path

    # ── 游标 ──────────────────────────────────────────────────────────────────

    def _read_cursor(self) -> Dict[str, Any]:
        default: Dict[str, Any] = {"last_finished_at": "", "last_id": 0}
        try:
            if self._cursor_file.exists():
                obj = json.loads(self._cursor_file.read_text() or "{}")
                if isinstance(obj, dict):
                    return {
                        "last_finished_at": str(obj.get("last_finished_at") or ""),
                        "last_id": int(obj.get("last_id") or 0),
                    }
        except Exception as e:
            self.logger.warning("读取任务计划游标失败: %s", e)
        return default

    def _write_cursor(self, last_finished_at: str, last_id: int) -> None:
        try:
            payload = {"last_finished_at": last_finished_at, "last_id": int(last_id or 0)}
            self._cursor_file.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入任务计划游标失败: %s", e)

    # ── 去重缓存 ───────────────────────────────────────────────────────────────

    def _load_dedup(self) -> None:
        try:
            if self._dedup_file.exists():
                obj = json.loads(self._dedup_file.read_text() or "{}")
                if isinstance(obj, dict):
                    now = int(time.time())
                    self._dedup_seen = {
                        str(k): int(v)
                        for k, v in obj.items()
                        if isinstance(v, (int, float)) and int(v) >= now - SCHEDULER_DEDUP_TTL_SECONDS
                    }
                    return
        except Exception as e:
            self.logger.warning("读取任务计划去重缓存失败: %s", e)
        self._dedup_seen = {}

    def _save_dedup(self) -> None:
        try:
            self._dedup_file.write_text(json.dumps(self._dedup_seen, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入任务计划去重缓存失败: %s", e)

    def _prune_dedup(self) -> None:
        now = int(time.time())
        cutoff = now - SCHEDULER_DEDUP_TTL_SECONDS
        self._dedup_seen = {k: v for k, v in self._dedup_seen.items() if v >= cutoff}

    # ── 数据库 ─────────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_latest_watermark(self) -> Dict[str, Any]:
        """获取当前数据库最新已完成记录的水位，用于首次启动时初始化游标。"""
        sql = """
        SELECT id, finished_at
        FROM task_results
        WHERE finished_at IS NOT NULL
          AND status IN ('success', 'failed', 'condition_failed')
        ORDER BY datetime(finished_at) DESC, id DESC
        LIMIT 1
        """
        try:
            conn = self._connect()
            row = conn.execute(sql).fetchone()
            conn.close()
            if not row:
                return {"last_finished_at": "", "last_id": 0}
            return {
                "last_finished_at": str(row["finished_at"] or ""),
                "last_id": int(row["id"] or 0),
            }
        except Exception as e:
            self.logger.warning("获取任务计划数据库最新水位失败: %s", e)
            return {"last_finished_at": "", "last_id": 0}

    def _fetch_rows(self, from_finished_at_ts: float) -> List[Dict[str, Any]]:
        """查询 finished_at >= from_finished_at_ts 的已完成记录，联表获取任务名。

        使用 datetime() 函数做归一化比较，兼容 fn-scheduler 存储的空格分隔格式
        ("2026-04-26 10:23:07") 和标准 ISO T 分隔格式，避免直接字符串比较出错。
        """
        try:
            from_dt = datetime.fromtimestamp(from_finished_at_ts, tz=timezone.utc)
            # 用空格格式传参，与 fn-scheduler 存储格式一致；datetime() 两侧均可归一化
            from_str = from_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            from_str = "1970-01-01 00:00:00"

        sql = """
        SELECT
            r.id,
            r.task_id,
            r.status,
            r.trigger_reason,
            r.started_at,
            r.finished_at,
            r.log,
            t.name AS task_name,
            t.account,
            t.trigger_type,
            t.schedule_expression
        FROM task_results r
        LEFT JOIN tasks t ON t.id = r.task_id
        WHERE r.finished_at IS NOT NULL
          AND datetime(r.finished_at) >= datetime(?)
          AND r.status IN ('success', 'failed', 'condition_failed')
        ORDER BY datetime(r.finished_at) ASC, r.id ASC
        """
        try:
            conn = self._connect()
            rows = [dict(r) for r in conn.execute(sql, (from_str,)).fetchall()]
            conn.close()
            return rows
        except Exception as e:
            self.logger.error("查询任务计划数据库失败: %s", e)
            return []

    # ── 事件构建 ───────────────────────────────────────────────────────────────

    def _to_event(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status = str(row.get("status") or "")
        event_type = _status_to_event_type(status)
        if not event_type:
            return None

        finished_at_str = str(row.get("finished_at") or "")
        started_at_str = str(row.get("started_at") or "")
        log_full = str(row.get("log") or "")
        log_preview = log_full[:SCHEDULER_LOG_PREVIEW_LIMIT]
        if len(log_full) > SCHEDULER_LOG_PREVIEW_LIMIT:
            log_preview += "…"

        trigger_reason_raw = str(row.get("trigger_reason") or "")
        event_data = {
            "result_id": row.get("id"),
            "task_id": row.get("task_id"),
            "task_name": str(row.get("task_name") or f"任务-{row.get('task_id')}"),
            "account": str(row.get("account") or ""),
            "status": status,
            "trigger_reason": trigger_reason_raw,
            "trigger_reason_label": TRIGGER_REASON_LABELS.get(trigger_reason_raw, trigger_reason_raw),
            "started_at": _iso_to_display(started_at_str),
            "finished_at": _iso_to_display(finished_at_str),
            "duration": _fmt_duration(started_at_str, finished_at_str),
            "schedule_expression": str(row.get("schedule_expression") or ""),
            "log_preview": log_preview,
            "log_size": len(log_full),
        }

        timestamp = _iso_to_display(finished_at_str) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = JournalEntry(
            cursor=str(row.get("id") or ""),
            timestamp=timestamp,
            hostname="scheduler.db",
            syslog_identifier=event_type,
            message=json.dumps(event_data, ensure_ascii=False),
            priority=0,
            pid=int(row.get("task_id") or 0),
            raw_data=json.dumps(row, ensure_ascii=False, default=str),
            original_line=json.dumps(row, ensure_ascii=False, default=str),
        )
        return {"event_type": event_type, "event_data": event_data, "entry": entry}

    def _fingerprint(self, row: Dict[str, Any]) -> str:
        return str(row.get("id") or "")

    # ── 轮询主逻辑 ─────────────────────────────────────────────────────────────

    def _poll_once(self, cursor: Dict[str, Any]) -> Dict[str, Any]:
        last_finished_at = str(cursor.get("last_finished_at") or "")
        last_id = int(cursor.get("last_id") or 0)

        last_ts = _iso_to_timestamp(last_finished_at)
        lower_bound_ts = max(0.0, last_ts - SCHEDULER_LOOKBACK_SECONDS)
        rows = self._fetch_rows(lower_bound_ts)

        max_finished_at = last_finished_at
        max_id = last_id

        for row in rows:
            row_finished_at = str(row.get("finished_at") or "")
            row_id = int(row.get("id") or 0)
            row_ts = _iso_to_timestamp(row_finished_at)

            # 更新最大水位
            if (row_ts, row_id) > (_iso_to_timestamp(max_finished_at), max_id):
                max_finished_at = row_finished_at
                max_id = row_id

            # 只处理严格晚于已确认水位的记录，或同时间戳但 id 更大的
            if (row_ts, row_id) <= (last_ts, last_id):
                continue

            fp = self._fingerprint(row)
            if fp in self._dedup_seen:
                continue

            ev = self._to_event(row)
            if ev is None:
                continue

            et = ev["event_type"]
            if self.monitor_events and et not in self.monitor_events:
                continue

            handler = self.event_handlers.get(et)
            if not handler:
                continue

            try:
                handler(ev["event_data"], ev["entry"])
                self._dedup_seen[fp] = int(time.time())
            except Exception as e:
                self.logger.error("处理任务计划事件失败 %s: %s", et, e, exc_info=True)

        self._prune_dedup()
        self._save_dedup()
        self._write_cursor(max_finished_at, max_id)
        return {"last_finished_at": max_finished_at, "last_id": max_id}

    def _run_loop(self) -> None:
        self._load_dedup()
        cursor = self._read_cursor()
        # 首次启动：初始化水位，避免补发历史记录
        if not cursor.get("last_finished_at") and cursor.get("last_id") == 0:
            cursor = self._get_latest_watermark()
            self._write_cursor(cursor["last_finished_at"], cursor["last_id"])
            self.logger.info(
                "任务计划轮询启动，仅处理水位之后的新记录: finished_at=%s id=%s",
                cursor["last_finished_at"],
                cursor["last_id"],
            )
        while self.running:
            try:
                cursor = self._poll_once(cursor)
            except Exception as e:
                self.logger.error("任务计划轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def _align_cursor_to_latest(self) -> None:
        """每次启用时对齐到当前最新水位，避免补发停用期间的存量记录。"""
        latest = self._get_latest_watermark()
        self._write_cursor(latest["last_finished_at"], latest["last_id"])
        self.logger.info(
            "任务计划轮询启用时已对齐当前水位（不补历史）: finished_at=%s id=%s",
            latest["last_finished_at"],
            latest["last_id"],
        )

    def start(self) -> None:
        if self.running:
            return
        if not (SCHEDULER_POLL_EVENTS & set(self.monitor_events or [])):
            self.logger.info("monitor_events 未包含任务计划事件，跳过 SchedulerDBPoller")
            return
        self._align_cursor_to_latest()
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="SchedulerDBPoller", daemon=False)
        self._thread.start()
        self.logger.info("SchedulerDBPoller 已启动，db=%s", self.db_path)

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("SchedulerDBPoller 已停止")
