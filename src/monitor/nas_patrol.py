"""
NAS 定时巡检：按配置间隔（分钟）采集本机 CPU/内存/磁盘状态，经已配置的推送渠道发送。
首次启用仅锚定周期起点不立即推送；推送失败时按指数退避重试。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

_STATE_FILENAME = "nas_patrol_state.json"

RETRY_BACKOFF_BASE_SEC = 30
RETRY_BACKOFF_MAX_SEC = 3600


def _state_path(cursor_dir: str) -> Path:
    p = Path(cursor_dir or "./data/cursor")
    p.mkdir(parents=True, exist_ok=True)
    return p / _STATE_FILENAME


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _save_state(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.getLogger(__name__).warning("写入巡检状态失败: %s", e)


def _normalize_patrol_state(state: Dict[str, Any]) -> None:
    if "last_success_ts" not in state and "last_report_ts" in state:
        try:
            state["last_success_ts"] = float(state["last_report_ts"])
        except (TypeError, ValueError):
            state["last_success_ts"] = 0.0
    if "last_success_ts" not in state:
        state["last_success_ts"] = 0.0
    state.pop("last_report_ts", None)


def _cpu_mem_disk() -> Tuple[str, str, str]:
    """返回 (cpu%, mem%, 根分区剩余 GB) 的字符串，不可用时为 —"""
    if not psutil:
        return "—", "—", "—"
    try:
        cpu = f"{psutil.cpu_percent(interval=1.0):.1f}"
    except Exception:
        cpu = "—"
    try:
        mem = f"{psutil.virtual_memory().percent:.1f}"
    except Exception:
        mem = "—"
    try:
        free_gb = psutil.disk_usage("/").free / (1024**3)
        disk = f"{free_gb:.1f}"
    except Exception:
        disk = "—"
    return cpu, mem, disk


def _cpu_disk_temp_c() -> Tuple[str, str]:
    """返回 CPU 温度、磁盘/存储相关温度（摄氏度数值字符串，无时为 —）。"""
    cpu_t, disk_t = "—", "—"
    if not psutil:
        return cpu_t, disk_t
    try:
        t = psutil.sensors_temperatures()
        if isinstance(t, dict):
            if "coretemp" in t and t["coretemp"]:
                cpu_t = f"{t['coretemp'][0].current:.1f}"
            elif "cpu_thermal" in t and t["cpu_thermal"]:
                cpu_t = f"{t['cpu_thermal'][0].current:.1f}"
            elif "cpu-thermal" in t and t["cpu-thermal"]:
                cpu_t = f"{t['cpu-thermal'][0].current:.1f}"
            if "nvme" in t and t["nvme"]:
                disk_t = f"{t['nvme'][0].current:.1f}"
            elif "sata" in t and t["sata"]:
                disk_t = f"{t['sata'][0].current:.1f}"
    except Exception:
        pass
    if disk_t == "—":
        for hw in ("/sys/class/hwmon/hwmon1/temp1_input", "/sys/class/hwmon/hwmon0/temp1_input"):
            try:
                v = int(open(hw, "r", encoding="utf-8").read().strip())
                disk_t = f"{v / 1000.0:.1f}"
                break
            except Exception:
                continue
    return cpu_t, disk_t


def _collect_patrol_payload(_cfg: Any, _state: Dict[str, Any]) -> Dict[str, Any]:
    cpu_pct, mem_pct, disk_free_gb = _cpu_mem_disk()
    cpu_temp, disk_temp = _cpu_disk_temp_c()
    return {
        "cpu_percent": cpu_pct,
        "cpu_temp_c": cpu_temp,
        "mem_percent": mem_pct,
        "disk_free_gb": disk_free_gb,
        "disk_temp_c": disk_temp,
    }


def _send_patrol_notification(app: Any, logger: Optional[logging.Logger]) -> bool:
    cfg = app.config
    if not cfg or not getattr(cfg, "nas_patrol_enabled", False) or not app.notifier:
        return False
    payload = _collect_patrol_payload(cfg, {})
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw_log = json.dumps(payload, ensure_ascii=False)
    try:
        result = app.notifier.send_notification(
            event_type="NAS_PATROL_REPORT",
            event_data=payload,
            raw_log=raw_log,
            timestamp=ts,
        )
    except Exception as e:
        if logger:
            logger.error("NAS 巡检推送异常: %s", e, exc_info=True)
        return False

    if not getattr(result, "success", False):
        if logger:
            logger.warning("NAS 巡检推送未成功（渠道失败或未配置等）")
        return False
    return True


def nas_patrol_worker_loop(app: Any) -> None:
    log = logging.getLogger(__name__)
    log.info("NAS 定时巡检线程已启动")
    while app.running:
        try:
            cfg = app.config
            if not cfg or not getattr(cfg, "nas_patrol_enabled", False) or not app.notifier:
                time.sleep(30)
                continue
            try:
                minutes = int(getattr(cfg, "nas_patrol_interval_minutes", 720) or 720)
            except (TypeError, ValueError):
                minutes = 720
            minutes = max(5, min(10080, minutes))
            interval = minutes * 60
            sp = _state_path(getattr(cfg, "cursor_dir", "./data/cursor") or "./data/cursor")
            state = _load_state(sp)
            _normalize_patrol_state(state)
            if bool(state.get("patrol_anchor_done")) and float(state.get("last_success_ts") or 0) <= 0:
                state["patrol_anchor_done"] = False
                _save_state(sp, state)
            now = time.time()

            if not state.get("patrol_anchor_done"):
                state["patrol_anchor_done"] = True
                last_s = float(state.get("last_success_ts") or 0)
                if last_s <= 0:
                    state["last_success_ts"] = now
                    state["retry_until_ts"] = 0.0
                    state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                    _save_state(sp, state)
                    log.info(
                        "NAS 巡检已锚定首次周期，第一次采集将在约 %s 分钟后执行（不立即推送）",
                        minutes,
                    )
                    time.sleep(5)
                    continue
                _save_state(sp, state)

            retry_until = float(state.get("retry_until_ts") or 0)
            if now < retry_until:
                time.sleep(min(60.0, max(5.0, retry_until - now)))
                continue

            last_success = float(state.get("last_success_ts") or 0)
            if last_success <= 0 or (now - last_success) < interval:
                sleep_s = min(60.0, max(5.0, last_success + interval - now))
                time.sleep(sleep_s)
                continue

            ok = _send_patrol_notification(app, log)
            now_after = time.time()
            if ok:
                state["last_success_ts"] = now_after
                state["retry_until_ts"] = 0.0
                state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                log.info("NAS 巡检推送成功，下次约在 %s 分钟后", minutes)
            else:
                cur = int(state.get("retry_backoff_sec") or RETRY_BACKOFF_BASE_SEC)
                cur = max(RETRY_BACKOFF_BASE_SEC, min(cur, RETRY_BACKOFF_MAX_SEC))
                wait_sec = cur
                next_bo = min(cur * 2, RETRY_BACKOFF_MAX_SEC)
                state["retry_until_ts"] = now_after + float(wait_sec)
                state["retry_backoff_sec"] = next_bo
                log.warning(
                    "NAS 巡检将在 %s 秒后重试（指数退避，下次失败等待上限 %s 秒）",
                    wait_sec,
                    next_bo,
                )
            _save_state(sp, state)

        except Exception as e:
            log.error("NAS 巡检线程异常: %s", e, exc_info=True)
        time.sleep(30)


def start_nas_patrol_thread(app: Any) -> Optional[threading.Thread]:
    t = threading.Thread(target=nas_patrol_worker_loop, args=(app,), name="NasPatrol", daemon=True)
    t.start()
    return t
