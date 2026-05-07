"""
Docker Engine 容器事件监听（docker.sock）

通过官方 docker SDK 订阅 container 类型事件，按 Action 映射为多个 monitor_events ID
（如 DOCKER_CONTAINER_START / DOCKER_CONTAINER_DIE）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import JournalEntry

try:
    import docker  # type: ignore
except ImportError:
    docker = None  # noqa: N816

# Docker API 原始 Action -> 项目内事件 ID（用户可在配置中分别勾选）
DOCKER_ACTION_TO_EVENT: Dict[str, str] = {
    "start": "DOCKER_CONTAINER_START",
    "die": "DOCKER_CONTAINER_DIE",
    "stop": "DOCKER_CONTAINER_STOP",
    "oom": "DOCKER_CONTAINER_OOM",
    "kill": "DOCKER_CONTAINER_KILL",
    "pause": "DOCKER_CONTAINER_PAUSE",
    "unpause": "DOCKER_CONTAINER_UNPAUSE",
    "restart": "DOCKER_CONTAINER_RESTART",
    "destroy": "DOCKER_CONTAINER_DESTROY",
    "create": "DOCKER_CONTAINER_CREATE",
}

DOCKER_POLL_EVENTS = frozenset(DOCKER_ACTION_TO_EVENT.values())


def _docker_engine_event_filters(monitor_events: Optional[List[str]]) -> Dict[str, List[str]]:
    """构建 Docker Engine /events 的 filters：仅订阅用户勾选的容器 Action（减少无用事件）。"""
    me = set(monitor_events or [])
    actions = [
        a for a, et in DOCKER_ACTION_TO_EVENT.items()
        if et in me
    ]
    f: Dict[str, List[str]] = {"type": ["container"]}
    if actions:
        f["event"] = actions
    return f


_DEDUP_TTL_SECONDS = 24 * 3600

# 重连时 since 与磁盘游标差过大则限制回溯窗口，避免一次补拉过多（引擎仍可能保留上限）
_SINCE_MAX_LOOKBACK_SECONDS = 86400


def _event_unix_seconds(ev: Dict[str, Any]) -> int:
    """从 Docker decode 事件取 Unix 秒时间戳（用于 since 游标）。"""
    t = ev.get("time") or ev.get("Time")
    if isinstance(t, int) and t > 0:
        return t
    if isinstance(t, float) and t > 0:
        return int(t)
    tn = ev.get("timeNano") or ev.get("TimeNano")
    if tn:
        try:
            return int(tn) // 1_000_000_000
        except (TypeError, ValueError):
            pass
    return int(time.time())


def _short_container_id(cid: str) -> str:
    s = (cid or "").strip()
    return s[:12] if len(s) >= 12 else s


def _parse_event_timestamp(ev: Dict[str, Any]) -> str:
    ts = ev.get("time") or ev.get("Time")
    if isinstance(ts, int) and ts > 0:
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DockerEventsPoller:
    """后台线程阻塞读取 Docker events，分发到 EventProcessor handler。"""

    def __init__(
        self,
        socket_path: str,
        cursor_dir: str,
        monitor_events: Optional[List[str]] = None,
    ):
        self.socket_path = (socket_path or "").strip() or "/var/run/docker.sock"
        self.cursor_dir = Path(cursor_dir)
        self.monitor_events = set(monitor_events or [])
        self.event_handlers: Dict[str, Callable] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._dedup_file = self.cursor_dir / "docker_events_poller_dedup.json"
        self._since_file = self.cursor_dir / "docker_events_poller_since.json"
        self._dedup_seen: Dict[str, int] = {}
        self._events_since_dedup_flush: int = 0
        self._since_cursor_ts: int = 0
        self._since_dirty_count: int = 0
        self.logger = logging.getLogger(__name__)

    def add_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        socket_path: Optional[str] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if socket_path is not None:
            self.socket_path = (socket_path or "").strip() or "/var/run/docker.sock"
        # 勾选变化后断开当前流，下一轮循环用新的 event 过滤器重连
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _load_dedup(self) -> None:
        try:
            if self._dedup_file.exists():
                obj = json.loads(self._dedup_file.read_text() or "{}")
                if isinstance(obj, dict):
                    now = int(time.time())
                    self._dedup_seen = {
                        str(k): int(v)
                        for k, v in obj.items()
                        if isinstance(v, (int, float)) and int(v) >= now - _DEDUP_TTL_SECONDS
                    }
                    return
        except Exception as e:
            self.logger.warning("读取 Docker 事件去重缓存失败: %s", e)
        self._dedup_seen = {}

    def _save_dedup(self) -> None:
        try:
            self._dedup_file.write_text(json.dumps(self._dedup_seen, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入 Docker 事件去重缓存失败: %s", e)

    def _prune_dedup(self) -> None:
        now = int(time.time())
        cutoff = now - _DEDUP_TTL_SECONDS
        self._dedup_seen = {k: v for k, v in self._dedup_seen.items() if v >= cutoff}

    def _load_since_cursor(self) -> Optional[int]:
        """读取上次提交的事件时间（秒）。不存在或损坏则返回 None（首次启动）。"""
        try:
            if not self._since_file.exists():
                return None
            obj = json.loads(self._since_file.read_text() or "{}")
            if not isinstance(obj, dict):
                return None
            v = obj.get("last_event_time")
            if v is None:
                return None
            ts = int(v)
            return ts if ts > 0 else None
        except Exception as e:
            self.logger.warning("读取 Docker events since 游标失败: %s", e)
            return None

    def _save_since_cursor(self, ts: int) -> None:
        """持久化 since 游标（供断线重连与进程重启后 client.events(since=...)）。"""
        try:
            self.cursor_dir.mkdir(parents=True, exist_ok=True)
            payload = {"last_event_time": int(ts), "updated_at": int(time.time())}
            self._since_file.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self.logger.warning("写入 Docker events since 游标失败: %s", e)

    def _effective_since_for_connect(self) -> int:
        """本次连接使用的 since：磁盘值与「当前时刻减回溯上限」取较大者；无磁盘则用当前时刻（不补历史）。"""
        now = int(time.time())
        stored = self._load_since_cursor()
        if stored is None:
            self.logger.info(
                "Docker events since 游标不存在，从当前时刻起订阅（不补发历史）",
            )
            self._since_cursor_ts = now
            self._save_since_cursor(now)
            return now
        floor = now - _SINCE_MAX_LOOKBACK_SECONDS
        since_ts = max(stored, floor)
        self._since_cursor_ts = since_ts
        if since_ts > stored:
            self.logger.info(
                "Docker events since 游标过旧，限制回溯至 %s 秒内",
                _SINCE_MAX_LOOKBACK_SECONDS,
            )
        return since_ts

    def _advance_since_from_stream_event(self, ev: Dict[str, Any]) -> None:
        """根据流里每条事件推进内存游标并节流写盘。"""
        if not isinstance(ev, dict):
            return
        if (ev.get("Type") or "").lower() != "container":
            return
        ts = _event_unix_seconds(ev)
        if ts > self._since_cursor_ts:
            self._since_cursor_ts = ts
            self._since_dirty_count += 1
            if self._since_dirty_count >= 15:
                self._save_since_cursor(self._since_cursor_ts)
                self._since_dirty_count = 0

    def _flush_since_cursor(self) -> None:
        if self._since_cursor_ts > 0:
            self._save_since_cursor(self._since_cursor_ts)
        self._since_dirty_count = 0

    def _event_to_internal(
        self, ev: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """解析单条 Docker decode 事件 -> event_type + event_data + entry。"""
        if (ev.get("Type") or "").lower() != "container":
            return None
        action_raw = (ev.get("Action") or ev.get("status") or "").lower()
        if not action_raw:
            return None
        event_type = DOCKER_ACTION_TO_EVENT.get(action_raw)
        if not event_type:
            return None

        actor = ev.get("Actor") or {}
        attrs = actor.get("Attributes") or {}
        if isinstance(attrs, dict) is False:
            attrs = {}
        cid = (actor.get("ID") or ev.get("id") or "").strip()
        name = (attrs.get("name") or "").lstrip("/")
        image = (attrs.get("image") or ev.get("from") or "").strip()
        exit_code = attrs.get("exitCode") or attrs.get("exit_code")

        event_data: Dict[str, Any] = {
            "container_id": _short_container_id(cid),
            "container_id_full": cid,
            "container_name": name,
            "image": image,
            "docker_action": action_raw,
        }
        if exit_code is not None and str(exit_code).strip() != "":
            event_data["exit_code"] = exit_code

        time_nano = ev.get("timeNano") or ev.get("TimeNano") or 0
        try:
            time_nano = int(time_nano)
        except (TypeError, ValueError):
            time_nano = 0

        ts_str = _parse_event_timestamp(ev)
        cursor_key = f"{cid}:{action_raw}:{time_nano}"
        entry = JournalEntry(
            cursor=cursor_key,
            timestamp=ts_str,
            hostname="docker",
            syslog_identifier=event_type,
            message=json.dumps(event_data, ensure_ascii=False),
            priority=0,
            pid=0,
            raw_data=json.dumps(ev, ensure_ascii=False, default=str),
            original_line=json.dumps(ev, ensure_ascii=False, default=str),
        )
        return {
            "event_type": event_type,
            "event_data": event_data,
            "entry": entry,
            "dedup_key": cursor_key,
        }

    def _handle_one(self, ev: Dict[str, Any]) -> None:
        parsed = self._event_to_internal(ev)
        if not parsed:
            return
        et = parsed["event_type"]
        if self.monitor_events and et not in self.monitor_events:
            return
        dk = parsed["dedup_key"]
        if dk in self._dedup_seen:
            return
        handler = self.event_handlers.get(et)
        if not handler:
            return
        try:
            handler(parsed["event_data"], parsed["entry"])
            self._dedup_seen[dk] = int(time.time())
        except Exception as e:
            self.logger.error("处理 Docker 事件失败 %s: %s", et, e, exc_info=True)

    def _run_loop(self) -> None:
        if docker is None:
            self.logger.error("未安装 docker SDK，请安装依赖: pip install docker")
            return
        self._load_dedup()
        base_url = f"unix://{self.socket_path}"
        while self.running:
            try:
                if not DOCKER_POLL_EVENTS & set(self.monitor_events or []):
                    time.sleep(3)
                    continue
                since_ts = self._effective_since_for_connect()
                ev_filters = _docker_engine_event_filters(list(self.monitor_events))
                self.logger.debug(
                    "Docker events 连接 since=%s filters=%s",
                    since_ts,
                    ev_filters,
                )
                self._client = docker.DockerClient(base_url=base_url, timeout=3600)
                try:
                    stream = self._client.events(
                        decode=True,
                        filters=ev_filters,
                        since=since_ts,
                    )
                except Exception as ex:
                    # 旧版 Engine 对 event 组合过滤支持不一，回退为全量 container 事件（仍由 _handle_one 按勾选过滤）
                    self.logger.warning(
                        "Docker events 使用 event 过滤器失败，回退为仅 type=container: %s",
                        ex,
                    )
                    stream = self._client.events(
                        decode=True,
                        filters={"type": ["container"]},
                        since=since_ts,
                    )
                for ev in stream:
                    if not self.running:
                        break
                    if not isinstance(ev, dict):
                        continue
                    self._advance_since_from_stream_event(ev)
                    self._handle_one(ev)
                    self._prune_dedup()
                    self._events_since_dedup_flush += 1
                    if self._events_since_dedup_flush >= 50:
                        self._save_dedup()
                        self._events_since_dedup_flush = 0
            except Exception as e:
                if self.running:
                    self.logger.warning("Docker events 流异常，将重试: %s", e, exc_info=True)
            finally:
                self._flush_since_cursor()
                if self._client:
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    self._client = None
                if self.running:
                    self._save_dedup()
                    self._events_since_dedup_flush = 0
                    time.sleep(3)

    def start(self) -> None:
        if self.running:
            return
        if docker is None:
            self.logger.info("docker SDK 不可用，跳过 DockerEventsPoller")
            return
        if not DOCKER_POLL_EVENTS & set(self.monitor_events or []):
            self.logger.info(
                "monitor_events 未勾选任何 Docker 容器事件，不连接 Docker Engine、不订阅 events",
            )
            return
        if not Path(self.socket_path).exists():
            self.logger.warning("Docker socket 不存在: %s，跳过 DockerEventsPoller", self.socket_path)
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="DockerEventsPoller", daemon=True)
        self._thread.start()
        self.logger.info("DockerEventsPoller 已启动，socket=%s", self.socket_path)

    def stop(self) -> None:
        self.running = False
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._flush_since_cursor()
        self._save_dedup()
        self._events_since_dedup_flush = 0
        self.logger.info("DockerEventsPoller 已停止")
