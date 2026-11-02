"""
CPU 占用 Top-N 采样。

用于 CPU 使用率告警正文补充「当前占用最高的应用」。

优先尝试飞牛开放 API（需 api-scope 授权且网关可用）；当前 FPK 仅声明
``trim.system.getPlatformConfig``，故多数环境会落到 psutil。
FPK 与宿主机同 PID 命名空间时 psutil 结果可信；普通 Docker 未 ``pid: host``
时只能看到容器内进程，采样失败或结果为空时由调用方静默省略。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

DEFAULT_LIMIT = 3
DEFAULT_SAMPLE_INTERVAL = 0.45
DEFAULT_TIMEOUT = 1.5

def sample_top_cpu_processes(
    limit: int = DEFAULT_LIMIT,
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """
    返回最多 ``limit`` 条占用最高的应用：
    ``[{"name": str, "cpu_percent": float, "pid": int}, ...]``。
    失败或无数据时返回空列表，不抛异常。
    """
    limit = max(1, int(limit or DEFAULT_LIMIT))
    timeout = max(0.2, float(timeout or DEFAULT_TIMEOUT))
    sample_interval = max(0.05, float(sample_interval or DEFAULT_SAMPLE_INTERVAL))
    deadline = time.monotonic() + timeout

    try:
        items = _sample_via_open_api(limit=limit, deadline=deadline)
        if items:
            return items
    except Exception as exc:
        logger.debug("cpu top via open API failed: %s", exc)

    if time.monotonic() >= deadline:
        return []

    try:
        remain = max(0.05, deadline - time.monotonic())
        interval = min(sample_interval, max(0.05, remain - 0.05))
        return _sample_via_psutil(limit=limit, sample_interval=interval, deadline=deadline)
    except Exception as exc:
        logger.debug("cpu top via psutil failed: %s", exc)
        return []


def format_top_cpu_lines(items: List[Dict[str, Any]], limit: int = DEFAULT_LIMIT) -> str:
    """渲染告警正文用的 Top-N 多行文本（不含标题）；无数据返回空串。"""
    rows = _normalize_items(items, limit=limit)
    if not rows:
        return ""
    lines = ["当前占用 Top3:" if limit == 3 else f"当前占用 Top{limit}:"]
    for idx, row in enumerate(rows, 1):
        name = str(row.get("name") or "?").strip() or "?"
        try:
            pct = float(row.get("cpu_percent") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        lines.append(f"{idx}. {name}  {pct:.1f}%")
    return "\n".join(lines)


def format_top_cpu_short(items: List[Dict[str, Any]], limit: int = DEFAULT_LIMIT) -> str:
    """批量摘要用短文案，如 ``a 12%/b 8%/c 3%``。"""
    rows = _normalize_items(items, limit=limit)
    if not rows:
        return ""
    parts: List[str] = []
    for row in rows:
        name = str(row.get("name") or "?").strip() or "?"
        try:
            pct = float(row.get("cpu_percent") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        parts.append(f"{name} {pct:.0f}%")
    return "/".join(parts)


def _normalize_items(items: Optional[List[Dict[str, Any]]], limit: int) -> List[Dict[str, Any]]:
    if not items:
        return []
    out: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        try:
            pct = float(raw.get("cpu_percent") or 0.0)
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            continue
        pid = raw.get("pid")
        try:
            pid_i = int(pid) if pid is not None else 0
        except (TypeError, ValueError):
            pid_i = 0
        out.append({"name": name[:80], "cpu_percent": pct, "pid": pid_i})
    out.sort(key=lambda x: x["cpu_percent"], reverse=True)
    return out[: max(1, int(limit))]


def _sample_via_open_api(limit: int, deadline: float) -> List[Dict[str, Any]]:
    """
    通过开放 API 网关拉进程列表。

    仅当显式设置 ``TRIM_PROC_LIST_REQ``（且 token/socket/权限可用）时才会调用；
    当前 FPK api-scope 未包含进程列表接口，默认跳过以免每次告警空耗约 1s。
    """
    req = (os.getenv("TRIM_PROC_LIST_REQ") or "").strip()
    if not req:
        return []

    remain = deadline - time.monotonic()
    if remain <= 0.05:
        return []
    try:
        from utils.fnos_platform import call_trimapp
    except ImportError:
        try:
            from src.utils.fnos_platform import call_trimapp  # type: ignore
        except ImportError:
            return []

    data = call_trimapp(req, data={}, timeout=min(1.0, remain))
    if data is None:
        return []
    rows = _extract_proc_rows(data)
    return _aggregate_by_name(rows, limit=limit)


def _extract_proc_rows(data: Any) -> List[Dict[str, Any]]:
    """从开放 API / CGI 响应中抽出进程行。"""
    candidates: List[Any]
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("list", "procs", "processes", "items", "data", "result"):
            val = data.get(key)
            if isinstance(val, list):
                candidates = val
                break
            if isinstance(val, dict):
                # 再包一层 data.list
                nested = _extract_proc_rows(val)
                if nested:
                    return nested
        else:
            candidates = []
    else:
        candidates = []

    rows: List[Dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = (
            item.get("name")
            or item.get("Name")
            or item.get("comm")
            or item.get("cmd")
            or item.get("cmdline")
            or item.get("exe")
            or ""
        )
        if isinstance(name, list):
            name = name[0] if name else ""
        name = _short_app_name(str(name))
        pct = _first_number(
            item,
            ("cpu_percent", "cpuPercent", "cpu", "CPU", "busy", "cpuUsage", "usage"),
        )
        pid = item.get("pid") or item.get("PID") or item.get("processId") or 0
        if name and pct is not None and pct > 0:
            rows.append({"name": name, "cpu_percent": float(pct), "pid": pid})
    return rows


def _sample_via_psutil(
    limit: int,
    sample_interval: float,
    deadline: float,
) -> List[Dict[str, Any]]:
    if psutil is None:
        return []

    primed: List[Any] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if time.monotonic() >= deadline:
            break
        try:
            proc.cpu_percent(None)
            primed.append(proc)
        except (psutil.Error, ProcessLookupError, PermissionError):
            continue

    sleep_for = min(sample_interval, max(0.0, deadline - time.monotonic() - 0.05))
    if sleep_for > 0:
        time.sleep(sleep_for)

    rows: List[Dict[str, Any]] = []
    for proc in primed:
        if time.monotonic() >= deadline:
            break
        try:
            pct = float(proc.cpu_percent(None) or 0.0)
            if pct <= 0:
                continue
            info = proc.as_dict(attrs=["pid", "name", "cmdline"])
            name = _short_app_name_from_proc(info.get("name"), info.get("cmdline"))
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "cpu_percent": pct,
                    "pid": int(info.get("pid") or 0),
                }
            )
        except (psutil.Error, ProcessLookupError, PermissionError, TypeError, ValueError):
            continue

    return _aggregate_by_name(rows, limit=limit)


def _aggregate_by_name(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """同名应用累加 CPU%，保留占用最高的那条 PID。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        try:
            pct = float(row.get("cpu_percent") or 0.0)
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            continue
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        cur = buckets.get(name)
        if cur is None:
            buckets[name] = {"name": name, "cpu_percent": pct, "pid": pid, "_peak": pct}
            continue
        cur["cpu_percent"] = float(cur["cpu_percent"]) + pct
        # 记录该聚合下瞬时占用更高的 PID，便于排查
        if pct >= float(cur.get("_peak") or 0):
            cur["pid"] = pid
            cur["_peak"] = pct
        else:
            cur.setdefault("_peak", 0.0)

    merged = []
    for item in buckets.values():
        item.pop("_peak", None)
        merged.append(item)
    return _normalize_items(merged, limit=limit)


def _short_app_name_from_proc(name: Any, cmdline: Any) -> str:
    base = _short_app_name(str(name or ""))
    # 解释器类进程尽量用脚本/模块名，便于认出「应用」
    if base.lower() in {"python", "python3", "python2", "java", "node", "ruby", "perl"}:
        cmd_name = _cmdline_app_name(cmdline)
        if cmd_name:
            return cmd_name
    return base


def _cmdline_app_name(cmdline: Any) -> str:
    if not cmdline:
        return ""
    if isinstance(cmdline, str):
        parts = cmdline.split()
    elif isinstance(cmdline, (list, tuple)):
        parts = [str(x) for x in cmdline if x is not None]
    else:
        return ""
    if not parts:
        return ""
    # 跳过解释器本身，找第一个不像 flag 的参数
    for part in parts[1:]:
        p = part.strip()
        if not p or p.startswith("-"):
            continue
        return _short_app_name(p)
    return _short_app_name(parts[0])


def _short_app_name(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    # cmdline 可能是绝对路径
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    if "\\" in text:
        text = text.rstrip("\\").split("\\")[-1]
    return text[:80]


def _first_number(item: Dict[str, Any], keys: tuple) -> Optional[float]:
    for key in keys:
        if key not in item:
            continue
        val = item.get(key)
        if val is None or val == "":
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None
