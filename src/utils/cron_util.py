"""
巡检 Cron 工具：标准 5 段表达式（分 时 日 月 周），与 Linux crontab 一致。
不支持 Quartz 的「?」占位符，请用「*」。

优先使用 croniter；若运行环境未安装，则用内置回退实现（覆盖常见 */N、定点等），
避免「手动巡检正常、定时永不触发」。
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

try:
    from croniter import croniter
except ImportError:  # pragma: no cover
    croniter = None  # type: ignore

DEFAULT_NAS_PATROL_CRON = "0 12 * * *"  # 每天中午 12:00


def croniter_available() -> bool:
    return croniter is not None


def normalize_cron_expr(expr: str) -> str:
    """去空白；将 Quartz 风格的 ? 替换为 *（仅作兼容提示，保存时仍建议用户写标准 Cron）。"""
    s = (expr or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    return s


def _parse_cron_field(field: str, min_v: int, max_v: int) -> List[int]:
    """解析单个 Cron 字段为允许取值列表。支持 *、N、A-B、*/N、A-B/N、逗号列表。"""
    field = (field or "").strip()
    if not field:
        raise ValueError("Cron 字段为空")
    if field == "?":
        raise ValueError("不支持 Quartz 的「?」，请改用「*」")
    out: List[int] = []

    def _add_range(start: int, end: int, step: int = 1) -> None:
        if step <= 0:
            raise ValueError("Cron 步长必须为正整数")
        if start < min_v or end > max_v or start > end:
            raise ValueError(f"Cron 范围无效: {start}-{end}（允许 {min_v}-{max_v}）")
        for v in range(start, end + 1, step):
            out.append(v)

    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            part = base
        if part == "*":
            _add_range(min_v, max_v, step)
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            _add_range(int(a_s), int(b_s), step)
        else:
            v = int(part)
            if v < min_v or v > max_v:
                raise ValueError(f"Cron 值 {v} 超出范围 {min_v}-{max_v}")
            if step == 1:
                out.append(v)
            else:
                # N/step：从 N 起按 step 到 max（少见，按常见实现处理）
                _add_range(v, max_v, step)
    if not out:
        raise ValueError(f"无法解析 Cron 字段: {field}")
    return sorted(set(out))


def _cron_matches(dt: datetime, parts: Sequence[str]) -> bool:
    minute, hour, dom, month, dow = parts
    minutes = _parse_cron_field(minute, 0, 59)
    hours = _parse_cron_field(hour, 0, 23)
    months = _parse_cron_field(month, 1, 12)
    # 日/周：与 Vixie cron 类似，二者都非 * 时为 OR；其一为 * 则只看另一边
    dom_any = dom in {"*", "?"}
    dow_any = dow in {"*", "?"}
    if not dom_any:
        doms = _parse_cron_field(dom, 1, 31)
    else:
        doms = list(range(1, 32))
    if not dow_any:
        # cron：0、7 都是周日；Python weekday(): Mon=0 ... Sun=6 → cron Sun=0
        dows_raw = _parse_cron_field(dow.replace("7", "0"), 0, 7)
        dows = {(d % 7) for d in dows_raw}
    else:
        dows = set(range(0, 7))

    if dt.minute not in minutes or dt.hour not in hours or dt.month not in months:
        return False
    cron_dow = (dt.weekday() + 1) % 7  # Mon=1 ... Sat=6, Sun=0
    day_ok = dt.day in doms
    dow_ok = cron_dow in dows
    if dom_any and dow_any:
        return True
    if dom_any:
        return dow_ok
    if dow_any:
        return day_ok
    return day_ok or dow_ok


def _next_cron_timestamp_fallback(expr: str, base_ts: float) -> float:
    """无分钟步进计算下次触发（不依赖 croniter）。"""
    s = normalize_cron_expr(expr)
    parts = s.split(" ")
    if len(parts) != 5:
        raise ValueError("请使用标准 5 段 Cron：分 时 日 月 周（例如 */10 * * * *）")
    # 预解析校验字段
    for i, (f, lo, hi) in enumerate(
        ((parts[0], 0, 59), (parts[1], 0, 23), (parts[2], 1, 31), (parts[3], 1, 12), (parts[4], 0, 7))
    ):
        if f in {"*", "?"} and i >= 2:
            continue
        if f == "?":
            raise ValueError("不支持 Quartz 的「?」，请改用「*」")
        if i == 2 and f == "*":
            continue
        if i == 4 and f == "*":
            continue
        try:
            _parse_cron_field(f.replace("7", "0") if i == 4 else f, lo, hi if i != 4 else 7)
        except ValueError as e:
            raise ValueError(f"Cron 表达式无效：{e}") from e

    base = datetime.fromtimestamp(float(base_ts))
    t = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # 最多向前搜约 2 年
    for _ in range(2 * 366 * 24 * 60):
        # 跳过非法日（如 2 月 30）
        try:
            _ = calendar.monthrange(t.year, t.month)
            if t.day > _:
                t += timedelta(minutes=1)
                continue
        except Exception:
            pass
        if _cron_matches(t, parts):
            return float(t.timestamp())
        t += timedelta(minutes=1)
    raise ValueError("无法计算下次 Cron 触发时间")


def validate_cron_expr(expr: str) -> str:
    """
    校验并返回规范化后的 5 段 Cron。
    非法时抛出 ValueError（文案可直接给前端）。
    """
    s = normalize_cron_expr(expr)
    if not s:
        raise ValueError("Cron 表达式不能为空")
    if "?" in s:
        raise ValueError("不支持 Quartz 的「?」，请改用「*」（标准 5 段：分 时 日 月 周）")
    parts = s.split(" ")
    if len(parts) != 5:
        raise ValueError("请使用标准 5 段 Cron：分 时 日 月 周（例如 0 12 * * * 或 */10 * * * *）")

    if croniter is not None:
        try:
            base = datetime.now()
            it = croniter(s, base)
            it.get_next(datetime)
            it.get_next(datetime)
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"Cron 表达式无效：{e}") from e
        return s

    # 无 croniter：用回退实现算两次，确认可解析
    try:
        t1 = _next_cron_timestamp_fallback(s, datetime.now().timestamp())
        _next_cron_timestamp_fallback(s, t1)
    except ValueError as e:
        raise ValueError(str(e)) from e
    return s


def minutes_to_cron(minutes: int) -> str:
    """将旧「间隔分钟」粗略转为 Cron（用于迁移）。"""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_NAS_PATROL_CRON
    m = max(5, min(10080, m))
    if m < 60:
        return f"*/{m} * * * *"
    if m % 60 == 0:
        hours = m // 60
        if hours < 24:
            return f"0 */{hours} * * *"
        days = hours // 24
        if days <= 1:
            return "0 0 * * *"
        if days < 28:
            return f"0 0 */{days} * *"
        return "0 0 1 * *"
    hours = max(1, m // 60)
    if hours < 24:
        return f"0 */{hours} * * *"
    return DEFAULT_NAS_PATROL_CRON


def resolve_nas_patrol_cron(
    cron_expr: Optional[str] = None,
    interval_minutes: Optional[int] = None,
) -> str:
    """优先使用 Cron；空则按旧分钟字段迁移；再不行用默认。

    若用户填写了表达式但校验失败，不再静默改成默认午间 Cron（避免 */10 被吃掉）。
    """
    raw = normalize_cron_expr(cron_expr or "")
    if raw:
        return validate_cron_expr(raw)
    if interval_minutes is not None:
        try:
            return validate_cron_expr(minutes_to_cron(int(interval_minutes)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_NAS_PATROL_CRON


def next_cron_timestamp(expr: str, base_ts: float) -> float:
    """从 base_ts（不含）起下一次触发的 Unix 时间戳。

    croniter 在非 UTC 容器时区下，即使传入 float，仍可能把 naive 本地时间当 UTC，
    导致下次触发偏约 +8 小时（如 */10 算到当晚 20:30）。以本地回退实现为准，
    仅在与 croniter 结果接近（<2h）时采用 croniter（复杂表达式兼容）。
    """
    s = validate_cron_expr(expr)
    base = float(base_ts)
    fb = _next_cron_timestamp_fallback(s, base)
    if croniter is not None:
        try:
            ci = float(croniter(s, base).get_next(float))
            if abs(ci - fb) <= 2 * 3600:
                return ci
        except Exception:
            pass
    return fb
