"""日志库时间显示：优先 parameter.datetime；否则用 logtime，并用库内两字段差值校准。"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

_logger = logging.getLogger(__name__)

# 由「同一行 parameter.datetime - logtime」学到的存库差值（秒）。
# 这是数据库两列的差，不是写死的时区偏移；换 TZ 不会改这个差值。
_learned_logtime_skew_seconds: Optional[int] = None
_skew_state_file: Optional[Path] = None
_tz_applied = False
_env_skew_conflict_warned = False

# 兜底：库里从未出现过 parameter.datetime 时，用「本轮新写入的行」的 now - logtime 估计差值。
_WALL_CLOCK_MIN_SKEW_SECONDS = 3600  # 不超过 1 小时的差视为正常（轮询延迟 / 小误差），不学习
_WALL_CLOCK_MAX_SKEW_SECONDS = 172800
_WALL_CLOCK_QUANTUM_SECONDS = 900  # 15 分钟取整，覆盖常见时区偏移，并吸收秒级轮询延迟


def apply_process_timezone() -> str:
    """按环境变量 TZ 刷新进程本地时区（Docker 依赖此步骤，否则 naive fromtimestamp 仍是 UTC）。"""
    global _tz_applied
    name = (os.environ.get("TZ") or "").strip()
    if name:
        try:
            time.tzset()
        except Exception as e:
            _logger.warning("TZ=%s 调用 tzset 失败: %s", name, e)
    _tz_applied = True
    try:
        now = datetime.now(get_display_tz())
        label = now.strftime("%Z (%z)")
    except Exception:
        label = name or "未知"
    return label


def get_display_tz() -> tzinfo:
    """展示时区：优先环境变量 TZ，否则用系统当前本地时区。"""
    name = (os.environ.get("TZ") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            _logger.warning("无效 TZ=%s，回退系统本地时区", name)
    local = datetime.now().astimezone().tzinfo
    return local or ZoneInfo("UTC")


def set_logtime_skew_state_path(path: Any) -> None:
    """设置差值落盘路径（通常 cursor_dir/logtime_skew.json），并尝试加载已有校准。"""
    global _skew_state_file, _learned_logtime_skew_seconds
    text = str(path or "").strip()
    _skew_state_file = Path(text) if text else None
    if _skew_state_file is None or not _skew_state_file.exists():
        return
    try:
        raw = json.loads(_skew_state_file.read_text(encoding="utf-8"))
        skew = int(raw.get("skew_seconds"))
    except Exception as e:
        _logger.debug("读取 logtime 校准缓存失败: %s", e)
        return
    if abs(skew) > 172800:
        return
    if _learned_logtime_skew_seconds != skew:
        _learned_logtime_skew_seconds = skew
        _logger.info(
            "已加载 logtime 校准缓存：parameter.datetime - logtime = %+d 秒",
            skew,
        )


def _persist_learned_skew(skew: int) -> None:
    if _skew_state_file is None:
        return
    try:
        _skew_state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = _skew_state_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"skew_seconds": int(skew)}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(_skew_state_file)
    except Exception as e:
        _logger.debug("写入 logtime 校准缓存失败: %s", e)


def get_logtime_display_offset_seconds() -> int:
    """显式 LOGTIME_DISPLAY_OFFSET_SECONDS 优先；否则用库内学到的 logtime 相对 datetime 差值。"""
    raw = os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS")
    if raw is not None and str(raw).strip() != "":
        try:
            env_offset = int(raw)
        except (TypeError, ValueError):
            env_offset = 0
        global _env_skew_conflict_warned
        if (
            not _env_skew_conflict_warned
            and _learned_logtime_skew_seconds is not None
            and abs(int(_learned_logtime_skew_seconds) - env_offset) > 3600
        ):
            _env_skew_conflict_warned = True
            _logger.warning(
                "LOGTIME_DISPLAY_OFFSET_SECONDS=%s 与库内学到的 %+d 秒不一致；"
                "将以环境变量为准。若推送时间多偏 8 小时，请删掉该环境变量后重建容器",
                raw,
                int(_learned_logtime_skew_seconds),
            )
        return env_offset
    if _learned_logtime_skew_seconds is not None:
        return _learned_logtime_skew_seconds
    return 0


def is_logtime_skew_known() -> bool:
    """是否已有可用差值：显式环境变量，或已从库 / 缓存学到。"""
    raw = os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS")
    if raw is not None and str(raw).strip() != "":
        return True
    return _learned_logtime_skew_seconds is not None


def describe_logtime_skew() -> str:
    """启动横幅用：给出当前差值及其来源；尚未学到时明确说明待校准，而不是显示误导性的 +0。"""
    raw = os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS")
    if raw is not None and str(raw).strip() != "":
        return f"{get_logtime_display_offset_seconds():+d} 秒（来自环境变量 LOGTIME_DISPLAY_OFFSET_SECONDS）"
    if _learned_logtime_skew_seconds is not None:
        return f"{_learned_logtime_skew_seconds:+d} 秒（来自库内校准缓存）"
    return "待校准（轮询对齐后按库内 datetime 或新写入的日志行自动学习）"


def format_unix_seconds_for_display(ts: int) -> str:
    """把真实 Unix 秒格式化为展示用本地时间（跟随 TZ；不加 logtime 列差值）。"""
    return datetime.fromtimestamp(int(ts), tz=get_display_tz()).strftime("%Y-%m-%d %H:%M:%S")


def _format_unix_seconds(ts: int) -> str:
    return format_unix_seconds_for_display(ts)


def extract_parameter_datetime_seconds(parameter: Any) -> Optional[int]:
    """从 parameter JSON 取飞牛写入的 datetime 字段（Unix 秒）。"""
    if isinstance(parameter, str) and parameter.strip():
        try:
            parameter = json.loads(parameter)
        except Exception:
            return None
    if not isinstance(parameter, dict):
        return None
    raw = parameter.get("datetime")
    if raw is None and isinstance(parameter.get("data"), dict):
        raw = parameter["data"].get("datetime")
    if raw is None:
        return None
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    if ts <= 0:
        return None
    return ts


def note_logtime_datetime_pair(logtime: Any, parameter_datetime: Any) -> Optional[int]:
    """用同一行的 logtime 与 parameter.datetime 学习差值，供仅有 logtime 的事件使用。"""
    global _learned_logtime_skew_seconds
    try:
        lt = int(logtime)
        dt = int(parameter_datetime)
    except (TypeError, ValueError):
        return _learned_logtime_skew_seconds
    if lt <= 0 or dt <= 0:
        return _learned_logtime_skew_seconds
    if dt > 10_000_000_000:
        dt //= 1000
    skew = dt - lt
    if abs(skew) > 172800:
        return _learned_logtime_skew_seconds
    if _learned_logtime_skew_seconds != skew:
        _learned_logtime_skew_seconds = skew
        _persist_learned_skew(skew)
        _logger.info(
            "已按数据库字段校准展示：parameter.datetime - logtime = %+d 秒"
            "（与 TZ 无关；可用 LOGTIME_DISPLAY_OFFSET_SECONDS 覆盖）",
            skew,
        )
    return _learned_logtime_skew_seconds


def observe_live_row_wall_clock(logtime: Any, parameter: Any = None, *, now: Optional[int] = None) -> Optional[int]:
    """库内从无 datetime 字段时的兜底：用本轮新写入的行估计 logtime 相对本机时钟的差值。

    调用方须保证该行是「轮询连续期间」新写入的（写入到被读取的延迟很小），否则 now - logtime
    会混入任意的历史滞后。单条即采用（与 datetime 单对学习一致），之后 datetime 行仍可覆盖。
    """
    global _learned_logtime_skew_seconds
    if _learned_logtime_skew_seconds is not None:
        return _learned_logtime_skew_seconds
    if extract_parameter_datetime_seconds(parameter) is not None:
        return _learned_logtime_skew_seconds
    try:
        lt = int(logtime or 0)
    except (TypeError, ValueError):
        return _learned_logtime_skew_seconds
    if lt <= 0:
        return _learned_logtime_skew_seconds
    current = int(time.time()) if now is None else int(now)
    estimate = current - lt
    if abs(estimate) <= _WALL_CLOCK_MIN_SKEW_SECONDS or abs(estimate) > _WALL_CLOCK_MAX_SKEW_SECONDS:
        return _learned_logtime_skew_seconds
    # 15 分钟取整：吸收秒级轮询延迟，同时保留真实时区偏移（均为 15 分钟的整数倍）
    value = int(round(estimate / _WALL_CLOCK_QUANTUM_SECONDS)) * _WALL_CLOCK_QUANTUM_SECONDS
    _learned_logtime_skew_seconds = value
    _persist_learned_skew(value)
    _logger.info(
        "库内无 parameter.datetime，已按新写入日志行与本机时钟校准展示：logtime 相对本机时间 %+d 秒"
        "（可用 LOGTIME_DISPLAY_OFFSET_SECONDS 覆盖）",
        value,
    )
    return _learned_logtime_skew_seconds


def observe_logger_row(logtime: Any, parameter: Any = None) -> Optional[int]:
    """任意日志行都可调用：有 datetime 就学习差值，不要求该事件在监控列表里。"""
    param_dt = extract_parameter_datetime_seconds(parameter)
    if param_dt is None:
        return _learned_logtime_skew_seconds
    try:
        lt = int(logtime or 0)
    except (TypeError, ValueError):
        return _learned_logtime_skew_seconds
    if lt <= 0:
        return _learned_logtime_skew_seconds
    return note_logtime_datetime_pair(lt, param_dt)


def format_logger_row_time(logtime: Any, parameter: Any = None) -> str:
    """格式化主日志库一行的展示时间。

    优先级：
    1) parameter.datetime（库内事件时间，Unix 秒 → 按 TZ/系统时区格式化）
    2) logtime + 显式 env / 从同库 datetime 学到的列差值
    """
    param_dt = extract_parameter_datetime_seconds(parameter)
    try:
        lt = int(logtime or 0)
    except (TypeError, ValueError):
        lt = 0
    if param_dt is not None:
        if lt > 0:
            note_logtime_datetime_pair(lt, param_dt)
        try:
            return _format_unix_seconds(param_dt)
        except (ValueError, OSError):
            pass
    if lt > 0:
        try:
            return _format_unix_seconds(lt + get_logtime_display_offset_seconds())
        except (ValueError, OSError):
            pass
    return display_now_str()


def display_now_str() -> str:
    """合并推送 / 系统通知等“发送时刻”：跟随 TZ / 系统本地时区。"""
    if not _tz_applied and (os.environ.get("TZ") or "").strip():
        apply_process_timezone()
    return datetime.now(get_display_tz()).strftime("%Y-%m-%d %H:%M:%S")
