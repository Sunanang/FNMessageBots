"""启动静默期：应用启动（或开机）后的一小段时间内只记录、不推送。

NAS 重启后本应用先起来，其它应用与容器陆续启动会把通知刷屏。这里提供可开关的窗口：
窗口内的事件只计数、不发送；窗口结束时补一条汇总。登录、SSH、UPS、磁盘错误、应用崩溃
这类安全事件按白名单照常实时推送。

状态落盘到 cursor_dir/startup_quiet.json。窗口用「配置指纹 + 开机秒数/时刻」判定是否延续，
所以重启进程不会把窗口往后推；容器内无开机信息时沿用已有窗口，避免 window_end 被反复顺延。
汇总走 peek + confirm，推送成功后才清空计数，失败可重试。

线程安全：通知路径会被多个轮询线程并发调用，内部状态统一在 self._lock 下读写。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

DEFAULT_SECONDS = 180
MIN_SECONDS = 30
MAX_SECONDS = 1800

# 判断「是不是同一次开机」时允许的开机时刻漂移（秒）；仅用于没有 uptime 字段的老状态文件
BOOT_DRIFT_SECONDS = 300

# /proc/uptime 读数抖动容忍（秒）。倒退超过此值视为真正重启；
# 不能用 BOOT_DRIFT_SECONDS：短 uptime 时 ±300 会把「早重启」误判成同一次开机。
UPTIME_JITTER_SECONDS = 2.0

# 静默期内仍然实时推送的安全类事件（白名单）
URGENT_EVENT_TYPES = frozenset({
    "LoginSucc",
    "LoginSucc2FA1",
    "LoginFail",
    "Logout",
    "SSH_LOGIN_SUCCESS",
    "SSH_AUTH_FAILED",
    "SSH_INVALID_USER",
    "SSH_ENABLED",
    "SSH_DISABLED",
    "UPS_ONBATT",
    "UPS_ONBATT_LOWBATT",
    "UPS_ONLINE",
    "DISK_IO_ERR",
    "StorageBroken",
    "STORAGE_DEGRADED",
    "APP_CRASH",
})

# 汇总类事件：静默时按事件自带的条数折叠，而不是算一条
SUMMARY_EVENT_TYPES = frozenset({
    "POLL_BATCH_SUMMARY",
    "MEDIA_LIBRARY_SUMMARY",
    "MUSIC_LIBRARY_SUMMARY",
    "NAS_PATROL_REPORT",
})

_APP_START_TYPES = frozenset({
    "APP_STARTED", "APP_AUTO_STARTED", "APP_INSTALLED", "APP_UPDATED", "APP_UNINSTALLED", "APP_STOPPED",
})
_DISK_TYPES = frozenset({
    "FoundDisk", "InsertDisk", "EjectDisk", "DiskWakeup", "DiskSpindown",
})


def _category(event_type: str) -> str:
    """把一个事件类型归到汇总文案里的类目。"""
    name = str(event_type or "")
    if name in _APP_START_TYPES:
        return "应用启动"
    if name.startswith("DOCKER_"):
        return "容器"
    if name in _DISK_TYPES or name.startswith("DISK_") or name.startswith("STORAGE_"):
        return "磁盘存储"
    if name in SUMMARY_EVENT_TYPES:
        return "合并通知"
    return "其他"


def clamp_seconds(value: Any) -> int:
    """把配置里的秒数收敛到 [MIN_SECONDS, MAX_SECONDS]，非法值回退默认。"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    return max(MIN_SECONDS, min(seconds, MAX_SECONDS))


# 容器里 /proc/uptime 是宿主机的开机秒数，拿它判断「是不是开机首启」会永远判成不是，
# 于是 Docker 部署打开开关也永远不生效 —— 容器内一律按「应用启动」计。
_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")


def in_container() -> bool:
    """是否跑在容器里（Docker / Podman）。"""
    return any(os.path.exists(marker) for marker in _CONTAINER_MARKERS)


def read_system_uptime(path: str = "/proc/uptime") -> Optional[float]:
    """读系统已开机秒数；容器内或读不到时返回 None（调用方按应用启动时间计窗口）。"""
    if in_container():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float(str(f.read()).split()[0])
    except (OSError, ValueError, IndexError):
        return None


def event_amount(event_type: str, event_data: Optional[Dict[str, Any]]) -> int:
    """汇总类事件折叠时用「实际条数」，取不到再退回 1 条。

    各汇总事件的字段不统一：轮询批次用 count，影视/音乐汇总用 total 或 items/by_type，
    这里按顺序兜底，避免把「一轮 20 条」压成 1 条。
    """
    if str(event_type) not in SUMMARY_EVENT_TYPES or not isinstance(event_data, dict):
        return 1
    for key in ("count", "total"):
        try:
            value = int(event_data.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    items = event_data.get("items")
    if isinstance(items, list) and items:
        return len(items)
    by_type = event_data.get("by_type")
    if isinstance(by_type, dict):
        total = 0
        for value in by_type.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
        if total > 0:
            return total
    return 1


class StartupQuietWindow:
    """启动静默期的判定与计数；内部加锁，可在多线程通知路径上共用。"""

    def __init__(
        self,
        state_dir: Any,
        *,
        clock: Callable[[], float] = time.time,
        uptime_reader: Optional[Callable[[], Optional[float]]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._clock = clock
        # 不写死默认值，方便测试替换 read_system_uptime
        self._uptime_reader = uptime_reader if uptime_reader is not None else read_system_uptime
        self.logger = logger or logging.getLogger(__name__)
        self._state_dir = Path(state_dir) if str(state_dir or "").strip() else None
        self._state_file = (self._state_dir / "startup_quiet.json") if self._state_dir else None
        self._state: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        self.enabled = False
        self.seconds = DEFAULT_SECONDS
        self.keep_urgent = True
        self.only_after_boot = True

    # ---------- 配置 ----------
    def _fingerprint(self) -> str:
        """配置指纹：只有这些值真变了才重开窗口，进程重启不算变化。"""
        return "|".join((
            "1" if self.enabled else "0",
            str(int(self.seconds)),
            "1" if self.keep_urgent else "0",
            "1" if self.only_after_boot else "0",
        ))

    def configure(self, *, enabled: bool, seconds: Any, keep_urgent: bool, only_after_boot: bool) -> None:
        """只记录配置，不动状态。

        窗口是否延续由 _ensure_window 按「配置指纹 + 开机时刻」判断，所以重启进程不会重开窗口，
        换设置则会在下次判定时按新配置重算（未发送的折叠计数会被带到新一轮一起汇总）。
        """
        with self._lock:
            self.enabled = bool(enabled)
            self.seconds = clamp_seconds(seconds)
            self.keep_urgent = bool(keep_urgent)
            self.only_after_boot = bool(only_after_boot)

    # ---------- 状态读写 ----------
    def _load(self) -> Dict[str, Any]:
        if self._state is not None:
            return self._state
        state: Dict[str, Any] = {}
        if self._state_file and self._state_file.exists():
            try:
                raw = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state = raw
            except Exception as e:  # 坏文件不该影响推送主流程
                self.logger.debug("读取启动静默期状态失败，按空状态处理: %s", e)
        for key in ("folded", "folded_categories", "carry", "carry_categories"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        self._state = state
        return state

    def _save(self, state: Dict[str, Any], *, force: bool = False) -> None:
        """落盘。

        计数器必须每条都落盘：进程重启后要靠它把窗口内已折叠的条数接着算下去
        （一次开机最多也就几十到几百条事件，单条 JSON 很小，写盘代价可以忽略）。
        force 保留给调用点标明「关键节点」，行为上同样立刻写。
        """
        self._state = state
        if not self._state_file:
            return
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            tmp = self._state_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._state_file)
        except Exception as e:
            self.logger.debug("写入启动静默期状态失败: %s", e)

    def _uptime(self) -> Optional[float]:
        try:
            value = self._uptime_reader() if self._uptime_reader else None
        except Exception:
            return None
        try:
            return None if value is None else max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    def _same_boot(self, state: Dict[str, Any], boot_at: Optional[float],
                   uptime: Optional[float] = None) -> bool:
        """判断是否还是同一次开机。

        首选「开机秒数没有倒退」（时钟被校正、应用重启都不会误判）；
        容器 / 无 /proc/uptime 时双方都没有开机信息，无法识别新开机，沿用已有窗口
        （否则每次 _ensure_window 都会把 window_end 往后推，静默期永远结束不了）；
        老状态文件没有 uptime 字段时退回比较开机时刻（±BOOT_DRIFT_SECONDS）。
        """
        stored_boot = state.get("boot_at")
        stored_uptime = state.get("uptime")
        if (uptime is None and boot_at is None
                and stored_uptime is None and stored_boot is None):
            return True
        if uptime is not None and isinstance(stored_uptime, (int, float)):
            return float(uptime) + UPTIME_JITTER_SECONDS >= float(stored_uptime)
        if stored_boot is None or boot_at is None:
            return False
        return abs(float(boot_at) - float(stored_boot)) <= BOOT_DRIFT_SECONDS

    def _carry_unsent(self, state: Dict[str, Any]) -> None:
        """把上一次没来得及汇总的计数带到 carry，等本轮窗口结束时一起发。"""
        if state.get("summary_sent"):
            return
        for counts_key, carry_key in (("folded", "carry"), ("folded_categories", "carry_categories")):
            carry = dict(state.get(carry_key) or {})
            for key, value in (state.get(counts_key) or {}).items():
                try:
                    carry[str(key)] = int(carry.get(key, 0)) + int(value)
                except (TypeError, ValueError):
                    continue
            state[carry_key] = carry
        state["folded"] = {}
        state["folded_categories"] = {}

    # ---------- 窗口 ----------
    def _ensure_window(self) -> Dict[str, Any]:
        with self._lock:
            state = self._load()
            if not self.enabled:
                if state.get("window_end") is not None or state.get("inactive_reason") != "disabled":
                    state["window_end"] = None
                    state["inactive_reason"] = "disabled"
                    self._save(state, force=True)
                return state
            now = self._clock()
            uptime = self._uptime()
            boot_at = (now - uptime) if uptime is not None else None
            fingerprint = self._fingerprint()
            same_boot = self._same_boot(state, boot_at, uptime)
            same_config = state.get("config_fingerprint") == fingerprint
            if same_config and "window_end" in state and same_boot:
                # 同一次开机 + 同一套配置（含进程重启）：沿用原窗口，不重复计时、不重复汇总
                return state
            if same_boot:
                # 仅配置变更时把未汇总计数带到新窗口；真开机不掺上一段
                self._carry_unsent(state)
            else:
                state["folded"] = {}
                state["folded_categories"] = {}
                state["carry"] = {}
                state["carry_categories"] = {}
            state["config_fingerprint"] = fingerprint
            state["boot_at"] = boot_at
            state["uptime"] = uptime
            state["summary_sent"] = False
            state.pop("inactive_reason", None)
            if self.only_after_boot and uptime is not None and uptime >= self.seconds:
                # 不是开机首启（例如手动重启应用），本次不静默
                state["window_end"] = None
                state["inactive_reason"] = "not_boot_launch"
                self._save(state, force=True)
                return state
            if self.only_after_boot and boot_at is not None:
                # 窗口锚定开机时刻：重启应用不会把窗口往后推
                state["window_end"] = float(boot_at) + self.seconds
            else:
                state["window_end"] = now + self.seconds
            self._save(state, force=True)
            self.logger.info("启动静默期开始：%s 秒内只记录不推送", self.seconds)
            return state

    def in_window(self) -> bool:
        """当前是否处在静默窗口内。"""
        with self._lock:
            state = self._ensure_window()
            window_end = state.get("window_end")
            return window_end is not None and self._clock() < float(window_end)

    @staticmethod
    def _add(state: Dict[str, Any], counts_key: str, categories_key: str,
             event_type: str, category: str, amount: int) -> None:
        counts = state.setdefault(counts_key, {})
        counts[event_type] = int(counts.get(event_type, 0)) + amount
        categories = state.setdefault(categories_key, {})
        categories[category] = int(categories.get(category, 0)) + amount

    def should_suppress(self, event_type: str, count: int = 1) -> bool:
        """是否吞掉这条通知（吞掉时已记入折叠计数）。"""
        with self._lock:
            if not self.enabled:
                return False
            state = self._ensure_window()
            window_end = state.get("window_end")
            if window_end is None or self._clock() >= float(window_end):
                return False
            name = str(event_type or "")
            try:
                amount = max(1, int(count or 1))
            except (TypeError, ValueError):
                amount = 1
            if name in SUMMARY_EVENT_TYPES:
                self._add(state, "folded", "folded_categories", "合并通知", "合并通知", amount)
                self._save(state)
                return True
            if self.keep_urgent and name in URGENT_EVENT_TYPES:
                return False
            self._add(state, "folded", "folded_categories", name or "unknown", _category(name), amount)
            self._save(state)
            return True

    def peek_summary(self, *, force: bool = False) -> Optional[str]:
        """窗口结束后返回汇总文案，不清空计数。

        force=True：关闭静默期等场景，忽略 enabled / 窗口是否结束，只要有未发送折叠就返回。
        """
        with self._lock:
            if not force and not self.enabled:
                return None
            state = self._load() if force else self._ensure_window()
            if not force:
                window_end = state.get("window_end")
                if window_end is not None and self._clock() < float(window_end):
                    return None
            folded = dict(state.get("folded") or {})
            carry = dict(state.get("carry") or {})
            total = self._total(folded) + self._total(carry)
            if total <= 0 or state.get("summary_sent"):
                return None
            categories: Dict[str, int] = {}
            for key in ("folded_categories", "carry_categories"):
                for name, count in (state.get(key) or {}).items():
                    try:
                        categories[str(name)] = categories.get(str(name), 0) + int(count)
                    except (TypeError, ValueError):
                        continue
            return self._format_summary(total, categories, aborted=force and not self.enabled)

    def confirm_summary(self) -> None:
        """推送成功后再清空折叠计数并标记已发送；失败时不要调用，以便重试。"""
        with self._lock:
            state = self._load()
            state["folded"] = {}
            state["folded_categories"] = {}
            state["carry"] = {}
            state["carry_categories"] = {}
            state["summary_sent"] = True
            self._save(state, force=True)

    def take_summary(self, *, force: bool = False) -> Optional[str]:
        """窗口结束后取出汇总并立即确认（单测兼容）；投递路径请用 peek + confirm。"""
        with self._lock:
            summary = self.peek_summary(force=force)
            if summary:
                self.confirm_summary()
            return summary

    @staticmethod
    def _total(bucket: Dict[str, Any]) -> int:
        total = 0
        for value in bucket.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
        return total

    def _format_summary(self, total: int, categories: Dict[str, int], aborted: bool = False) -> str:
        seconds = self.seconds
        window_text = f"{seconds // 60} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} 秒"
        parts = [f"{name} {count}" for name, count in
                 sorted((kv for kv in categories.items() if kv[1] > 0), key=lambda kv: -kv[1])[:4]]
        detail = f"（{' / '.join(parts)}）" if parts else ""
        tail = "；静默期已关闭，补发此前折叠的事件" if aborted else ""
        return f"开机静默期 {window_text}：折叠 {total} 条事件{detail}{tail}"

    def status(self) -> Dict[str, Any]:
        """只读快照，供自检与测试使用。"""
        with self._lock:
            state = self._ensure_window()
            return {
                "enabled": self.enabled,
                "seconds": self.seconds,
                "in_window": self.in_window(),
                "window_end": state.get("window_end"),
                "folded": dict(state.get("folded") or {}),
                "folded_categories": dict(state.get("folded_categories") or {}),
                "carry": dict(state.get("carry") or {}),
                "summary_sent": bool(state.get("summary_sent")),
                "inactive_reason": state.get("inactive_reason"),
            }
