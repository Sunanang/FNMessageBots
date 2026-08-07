"""
外网 IP 变化监控：
- 用户勾选 WAN_IP_CHANGED 后记录当前外网 IP（基线，不推送）
- 每 10 分钟检测一次；NAS 巡检触发时也会检测
- 与基线不一致时推送 WAN_IP_CHANGED，并更新基线
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

WAN_IP_CHANGED = "WAN_IP_CHANGED"
WAN_IP_CHECK_INTERVAL_SEC = 600  # 10 分钟
_STATE_FILENAME = "wan_ip_monitor_state.json"

_lock = threading.Lock()


def _state_path(cursor_dir: str) -> Path:
    return Path(cursor_dir or "./data/cursor") / _STATE_FILENAME


def _load_state(path: Path) -> dict:
    default = {
        "version": 1,
        "last_wan_ip": "",
        "last_check_ts": 0.0,
        "last_change_ts": 0.0,
        "baseline_done": False,
    }
    try:
        if path.exists():
            raw = path.read_text().strip()
            if raw:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    default.update(obj)
    except Exception as e:
        logging.getLogger(__name__).warning("读取外网 IP 状态失败: %s", e)
    return default


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False))
    except Exception as e:
        logging.getLogger(__name__).warning("写入外网 IP 状态失败: %s", e)


def _event_enabled(app: Any) -> bool:
    cfg = getattr(app, "config", None)
    if not cfg:
        return False
    me = set(getattr(cfg, "monitor_events", []) or [])
    return WAN_IP_CHANGED in me


def _fetch_wan_ip(known_ip: Optional[str] = None) -> str:
    text = (known_ip or "").strip()
    if text and text != "--":
        return text
    from monitor.nas_patrol import _pick_wan_ip

    return (_pick_wan_ip() or "").strip() or "--"


def check_and_notify_wan_ip_change(
    app: Any,
    *,
    source: str = "timer",
    known_ip: Optional[str] = None,
) -> Optional[str]:
    """
    检测外网 IP；首次仅记录基线，变化则推送。

    Returns:
        当前测得的 IP（失败为 None / 未启用时返回 None）
    """
    log = logging.getLogger(__name__)
    if not app or not _event_enabled(app):
        return None
    if not getattr(app, "notifier", None):
        return None

    cfg = app.config
    cursor_dir = getattr(cfg, "cursor_dir", "./data/cursor") or "./data/cursor"
    sp = _state_path(cursor_dir)

    with _lock:
        state = _load_state(sp)
        current = _fetch_wan_ip(known_ip)
        now = time.time()
        state["last_check_ts"] = now

        if not current or current == "--":
            _save_state(sp, state)
            log.info("外网 IP 检测失败（source=%s），保留原基线", source)
            return None

        last = str(state.get("last_wan_ip") or "").strip()
        baseline_done = bool(state.get("baseline_done")) and bool(last)

        if not baseline_done:
            state["last_wan_ip"] = current
            state["baseline_done"] = True
            _save_state(sp, state)
            msg = f"外网 IP 已记录基线: {current}（source={source}，不推送）"
            log.info(msg)
            print(msg, flush=True)
            return current

        if current == last:
            _save_state(sp, state)
            return current

        # IP 已变化：先更新基线，再推送，避免并发重复推送
        state["last_wan_ip"] = current
        state["last_change_ts"] = now
        _save_state(sp, state)

    event_data = {
        "old_wan_ip": last,
        "new_wan_ip": current,
        "wan_ip": current,
        "previous_wan_ip": last,
        "check_source": source,
        "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    raw_log = json.dumps(event_data, ensure_ascii=False)
    ts = event_data["changed_at"]
    msg = f"外网 IP 变化: {last} → {current}（source={source}）"
    log.warning(msg)
    print(msg, flush=True)
    try:
        app.notifier.send_notification(
            event_type=WAN_IP_CHANGED,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=ts,
        )
    except Exception as e:
        log.error("外网 IP 变化推送失败: %s", e, exc_info=True)
    return current


def wan_ip_monitor_worker_loop(app: Any) -> None:
    log = logging.getLogger(__name__)
    log.info("外网 IP 监控线程已启动（间隔 %ss）", WAN_IP_CHECK_INTERVAL_SEC)
    print(f"外网 IP 监控线程已启动（间隔 {WAN_IP_CHECK_INTERVAL_SEC}s）", flush=True)
    # 启动后稍等再检，避免与 APP_START 抢推送
    for _ in range(15):
        if not getattr(app, "running", True):
            return
        time.sleep(1)
    while getattr(app, "running", False):
        try:
            if _event_enabled(app) and getattr(app, "notifier", None):
                check_and_notify_wan_ip_change(app, source="timer")
            else:
                time.sleep(30)
                continue
        except Exception as e:
            log.error("外网 IP 监控异常: %s", e, exc_info=True)
        for _ in range(WAN_IP_CHECK_INTERVAL_SEC):
            if not getattr(app, "running", True):
                return
            time.sleep(1)


def start_wan_ip_monitor_thread(app: Any) -> Optional[threading.Thread]:
    t = threading.Thread(
        target=wan_ip_monitor_worker_loop,
        args=(app,),
        name="WanIpMonitor",
        daemon=True,
    )
    t.start()
    return t
