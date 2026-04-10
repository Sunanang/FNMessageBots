"""
统一通知器
根据配置决定使用企业微信webhook、钉钉或飞书进行消息推送
支持勿扰模式：时段内缓冲事件，结束后汇总为一条推送
"""

import json
import logging
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config import TITLE_PREFIX_DEFAULT
from .multi_platform_notifier import MultiPlatformNotifier
from monitor.db_log_poller import DB_EVENT_ID_TO_PROJECT


def _truncate_channel_results_for_storage(channel_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """截断每条渠道的 response 便于入库，避免 detail 超长。"""
    out = []
    for cr in channel_results:
        c = dict(cr)
        r = c.get("response")
        if r is not None and isinstance(r, dict):
            s = json.dumps(r, ensure_ascii=False)
            if len(s) > 500:
                c["response"] = {"_preview": s[:500] + "…"}
        out.append(c)
    return out


def _event_summary(event_type: str, event_data: Dict[str, Any]) -> str:
    """从 event_data 生成一条简短摘要，用于推送记录列表展示。"""
    data = event_data.get("data") if isinstance(event_data.get("data"), dict) else {}
    parts = []
    if event_data.get("user") or event_data.get("IP"):
        parts.append(f"{event_data.get('user', '')}@{event_data.get('IP', '')}".strip("@"))
    if data.get("DISPLAY_NAME") or data.get("APP_NAME"):
        parts.append(data.get("DISPLAY_NAME") or data.get("APP_NAME", ""))
    if event_data.get("name"):
        parts.append(event_data.get("name", ""))
    if event_data.get("message"):
        msg = (event_data.get("message") or "")[:80]
        if msg:
            parts.append(msg)
    if not parts:
        parts.append(event_type)
    return " | ".join(str(p).strip() for p in parts if p)[:200]


@dataclass
class NotificationResult:
    """通知发送结果"""
    success: bool
    method: str  # 'wechat', 'dingtalk', 'feishu', 'multiple', 'none'
    details: Dict[str, Any] = None


class UnifiedNotifier:
    """统一通知器，支持多平台推送"""
    
    def __init__(self, config):
        """
        初始化统一通知器
        
        Args:
            config: 配置对象
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        # 记录最近一次已发送的勿扰汇总时段结束时间（ISO 字符串），避免重复发送
        self._last_dnd_summary_end: str = ""
        self._load_last_dnd_summary_end()

        # 根据配置初始化多平台通知器
        self.multi_platform_notifier = MultiPlatformNotifier(
            wechat_webhook_url=config.wechat_webhook_url,
            dingtalk_webhook_url=config.dingtalk_webhook_url,
            feishu_webhook_url=config.feishu_webhook_url,
            bark_url=config.bark_url,
            pushplus_params=config.pushplus_params,
            magic_push_params=getattr(config, "magic_push_params", "") or "",
            title_prefix=getattr(config, "title_prefix", TITLE_PREFIX_DEFAULT),
            dedup_window=config.dedup_window,
            pool_size=config.http_pool_size,
            retries=config.http_retry_count,
            timeout=config.http_timeout
        )
        self.logger.info("多平台通知器已初始化")

    def reload_config(self):
        """按当前 self.config 重新创建多平台通知器（保存配置后热加载用）。"""
        old = self.multi_platform_notifier
        self.multi_platform_notifier = MultiPlatformNotifier(
            wechat_webhook_url=self.config.wechat_webhook_url,
            dingtalk_webhook_url=self.config.dingtalk_webhook_url,
            feishu_webhook_url=self.config.feishu_webhook_url,
            bark_url=self.config.bark_url,
            pushplus_params=self.config.pushplus_params,
            magic_push_params=getattr(self.config, "magic_push_params", "") or "",
            title_prefix=getattr(self.config, "title_prefix", TITLE_PREFIX_DEFAULT),
            dedup_window=self.config.dedup_window,
            pool_size=self.config.http_pool_size,
            retries=self.config.http_retry_count,
            timeout=self.config.http_timeout,
        )
        if old:
            try:
                old.close()
            except Exception as e:
                self.logger.warning("关闭旧通知器失败: %s", e)
        self.logger.info("多平台通知器已热加载配置")
        self._load_last_dnd_summary_end()

    def _dnd_summary_cursor_file(self) -> Optional[Path]:
        """勿扰汇总游标文件路径（持久化最近汇总结束点，避免重启后重复推送）。"""
        cursor_dir = (getattr(self.config, "cursor_dir", "") or "").strip()
        if not cursor_dir:
            return None
        p = Path(cursor_dir)
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.debug("创建 cursor_dir 失败，跳过勿扰游标持久化: %s", e)
            return None
        return p / "dnd_summary_cursor.txt"

    def _load_last_dnd_summary_end(self) -> None:
        fp = self._dnd_summary_cursor_file()
        if not fp or not fp.exists():
            return
        try:
            raw = fp.read_text(encoding="utf-8").strip()
            if raw:
                self._last_dnd_summary_end = raw
        except Exception as e:
            self.logger.debug("读取勿扰游标失败: %s", e)

    def _save_last_dnd_summary_end(self, end_key: str) -> None:
        self._last_dnd_summary_end = end_key
        fp = self._dnd_summary_cursor_file()
        if not fp:
            return
        try:
            fp.write_text(end_key, encoding="utf-8")
        except Exception as e:
            self.logger.debug("写入勿扰游标失败: %s", e)

    def _dnd_minutes_since_midnight(self, time_str: str) -> int:
        """将 HH:MM 转为当日 0 点起的分钟数。"""
        try:
            parts = time_str.strip().split(":")
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            return max(0, min(24 * 60 - 1, h * 60 + m))
        except (ValueError, IndexError):
            return 0

    def _in_dnd_window(self) -> bool:
        """当前时间是否在勿扰时段内（使用 Asia/Shanghai）。"""
        enabled = getattr(self.config, "dnd_enabled", False)
        if not enabled:
            return False
        start_s = getattr(self.config, "dnd_start_time", "22:00") or "22:00"
        end_s = getattr(self.config, "dnd_end_time", "07:00") or "07:00"
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        current = now.hour * 60 + now.minute
        start_m = self._dnd_minutes_since_midnight(start_s)
        end_m = self._dnd_minutes_since_midnight(end_s)
        if end_m <= start_m:
            return current >= start_m or current < end_m
        return start_m <= current < end_m

    def _normalize_db_event_type(self, db_event_id: str, uname: str) -> str:
        """将数据库 eventId 归一化为项目内 event_type（与轮询器一致）。"""
        et = DB_EVENT_ID_TO_PROJECT.get(db_event_id, db_event_id)
        if db_event_id == "SshdLoginAuthFail" and (uname or "").strip().lower() == "invalid":
            return "SSH_INVALID_USER"
        return et

    def _calc_latest_dnd_period(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        计算“最近一个已结束的勿扰时段”时间窗（Asia/Shanghai）。
        返回 (start_dt, end_dt)。若当前在勿扰时段内或无法确定，返回 (None, None)。
        """
        if not getattr(self.config, "dnd_enabled", False):
            return None, None
        start_s = getattr(self.config, "dnd_start_time", "22:00") or "22:00"
        end_s = getattr(self.config, "dnd_end_time", "07:00") or "07:00"
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        start_m = self._dnd_minutes_since_midnight(start_s)
        end_m = self._dnd_minutes_since_midnight(end_s)
        current_m = now.hour * 60 + now.minute
        cross_day = end_m <= start_m

        # 当前仍在勿扰时段，不生成汇总
        if self._in_dnd_window():
            return None, None

        if cross_day:
            # 跨天：仅在 end~start 这个“白天窗口”允许汇总（典型 07:00~22:00）
            if not (end_m <= current_m < start_m):
                return None, None
            end_dt = now.replace(hour=end_m // 60, minute=end_m % 60, second=0, microsecond=0)
            start_dt = (end_dt - timedelta(days=1)).replace(hour=start_m // 60, minute=start_m % 60)
            return start_dt, end_dt

        # 非跨天：选择最近一个已结束的时段
        if current_m < start_m:
            end_dt = (now - timedelta(days=1)).replace(hour=end_m // 60, minute=end_m % 60, second=0, microsecond=0)
            start_dt = end_dt.replace(hour=start_m // 60, minute=start_m % 60)
        else:
            end_dt = now.replace(hour=end_m // 60, minute=end_m % 60, second=0, microsecond=0)
            start_dt = now.replace(hour=start_m // 60, minute=start_m % 60, second=0, microsecond=0)
            if current_m < end_m:
                return None, None
        return start_dt, end_dt

    def _query_dnd_events_summary(self, start_dt: datetime, end_dt: datetime) -> Dict[str, int]:
        """按勿扰时段从 logger_data.db3 查询并统计事件数（仅统计 monitor_events 内事件）。"""
        db_path = getattr(self.config, "logger_db_path", "") or ""
        if not db_path or not os.path.exists(db_path):
            return {}
        offset = int(os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS", "28800"))
        start_ts = int(start_dt.timestamp()) - offset
        end_ts = int(end_dt.timestamp()) - offset
        monitor_set = set(getattr(self.config, "monitor_events", []) or [])
        if not monitor_set:
            return {}

        by_type: Dict[str, int] = defaultdict(int)
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT eventId, uname FROM log WHERE logtime >= ? AND logtime < ? ORDER BY id ASC",
                (start_ts, end_ts),
            )
            for row in cur.fetchall():
                et = self._normalize_db_event_type(str(row["eventId"] or ""), str(row["uname"] or ""))
                if et in monitor_set:
                    by_type[et] += 1
        finally:
            conn.close()
        return dict(by_type)

    def _build_dnd_summary_from_db(self, start_dt: datetime, end_dt: datetime, by_type: Dict[str, int]) -> str:
        """基于数据库统计结果生成勿扰汇总文案。"""
        titles = MultiPlatformNotifier.EVENT_TITLES
        lines = [f"【勿扰时段汇总】{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"]
        for event_type in sorted(by_type.keys()):
            count = by_type[event_type]
            label = titles.get(event_type, event_type)
            if "飞牛NAS-" in label:
                label = label.split("飞牛NAS-", 1)[-1].strip()
            label = self.multi_platform_notifier._strip_body_emojis(str(label)).strip()
            lines.append(f"· {label} {count} 次")
        self.logger.info("勿扰结束，发送汇总消息（共 %s 条事件）", sum(by_type.values()))
        return "\n".join(lines)

    def flush_dnd_buffer_if_needed(self) -> None:
        """勿扰结束后按数据库时间窗汇总（不依赖内存缓存），同一时段仅发送一次。"""
        start_dt, end_dt = self._calc_latest_dnd_period()
        if start_dt is None or end_dt is None:
            return
        end_key = end_dt.isoformat()
        if end_key == self._last_dnd_summary_end:
            return
        by_type = self._query_dnd_events_summary(start_dt, end_dt)
        if not by_type:
            self._save_last_dnd_summary_end(end_key)
            self.logger.info("勿扰结束：数据库查询到 0 条监控事件，跳过汇总推送")
            return
        summary = self._build_dnd_summary_from_db(start_dt, end_dt, by_type)
        try:
            out = self.multi_platform_notifier.send_system_notification(
                "DND_SUMMARY",
                summary,
                {"hostname": "", "version": ""},
            )
            success = out.get("success", False) if isinstance(out, dict) else bool(out)
            sc = out.get("success_count", 0) if isinstance(out, dict) else 0
            fc = out.get("fail_count", 0) if isinstance(out, dict) else 0
            # 勿扰汇总不参与推送汇总统计，仅事件推送（send_notification）统计
            # 再发一条短消息：勿扰汇总推送结果（成功/失败渠道数）
            result_msg = f"本次汇总推送：成功 {sc} 个渠道，失败 {fc} 个渠道"
            self.multi_platform_notifier.send_system_notification(
                "DND_SUMMARY",
                result_msg,
                {"hostname": "", "version": ""},
            )
            self._save_last_dnd_summary_end(end_key)
        except Exception as e:
            self.logger.warning("勿扰汇总推送失败: %s", e)

    def send_notification(self, 
                         event_type: str,
                         event_data: Dict[str, Any],
                         raw_log: str,
                         timestamp: str) -> NotificationResult:
        """
        发送通知
        
        Args:
            event_type: 事件类型
            event_data: 事件数据
            raw_log: 原始日志
            timestamp: 时间戳
            
        Returns:
            通知发送结果
        """
        if self._in_dnd_window():
            # 勿扰时段内不再缓存事件；结束后按数据库时间窗汇总。
            return NotificationResult(
                success=True,
                method="dnd_skipped",
                details={"event_type": event_type, "dnd_skipped": True},
            )
        # 通过多平台通知器发送
        success, channel_results = self.multi_platform_notifier.send_notification(
            event_type, event_data, raw_log, timestamp
        )
        try:
            from utils.push_stats import record as record_push
            record_push(success)
        except Exception:
            pass
        try:
            from utils.push_history import add_record as add_push_history
            # 摘要改为“实际推送文案预览”（去掉表情/emoji）
            summary = self.multi_platform_notifier.build_history_summary(
                event_type=event_type,
                event_data=event_data,
                raw_log=raw_log,
                timestamp=timestamp,
            ) or _event_summary(event_type, event_data)
            detail = {
                "event_type": event_type,
                "timestamp": timestamp,
                "event_data": event_data,
                "channel_results": _truncate_channel_results_for_storage(channel_results),
            }
            if event_type == "POLL_BATCH_SUMMARY":
                by_type = event_data.get("by_type") if isinstance(event_data.get("by_type"), dict) else {}
                render_meta = event_data.get("batch_render_meta") if isinstance(event_data.get("batch_render_meta"), dict) else {}
                grouped = event_data.get("grouped_events") if isinstance(event_data.get("grouped_events"), dict) else {}
                grouped_preview = {}
                for et, rows in grouped.items():
                    if not isinstance(rows, list):
                        continue
                    preview_rows = []
                    for r in rows[:3]:
                        if not isinstance(r, dict):
                            continue
                        preview_rows.append({
                            "timestamp": r.get("timestamp"),
                            "event_data": r.get("event_data") if isinstance(r.get("event_data"), dict) else {},
                        })
                    grouped_preview[str(et)] = preview_rows
                detail.update({
                    "batch_total": int(event_data.get("count") or 0),
                    "batch_type_count": len(by_type),
                    "batch_render_meta": render_meta,
                    "grouped_events_preview": grouped_preview,
                })
                # 汇总事件不在 push_history 中保存全量 grouped_events，避免 detail 过大
                detail["event_data"] = {
                    "count": int(event_data.get("count") or 0),
                    "by_type": by_type,
                    "items": event_data.get("items") if isinstance(event_data.get("items"), list) else [],
                }
            add_push_history(success=success, event_type=event_type, summary=summary, detail=detail)
        except Exception:
            pass
        # 确定实际使用的方法（检查哪些平台真正发送了）
        active_platforms = []
        if self.config.wechat_webhook_url:
            active_platforms.append('wechat')
        if self.config.dingtalk_webhook_url:
            active_platforms.append('dingtalk')
        if self.config.feishu_webhook_url:
            active_platforms.append('feishu')
        if self.config.bark_url:
            active_platforms.append('bark')
        if self.config.pushplus_params:
            active_platforms.append('pushplus')
        if getattr(self.config, "magic_push_params", ""):
            active_platforms.append('magic_push')
        
        if len(active_platforms) == 0:
            method = 'none'
        elif len(active_platforms) == 1:
            method = active_platforms[0]
        else:
            method = 'multiple'
        
        return NotificationResult(
            success=success,
            method=method,
            details={
                'platforms': active_platforms,
                'event_type': event_type
            }
        )
    
    def send_system_notification(self, 
                                event_type: str, 
                                message: str, 
                                additional_info: Dict[str, Any] = None) -> NotificationResult:
        """
        发送系统事件通知
        
        Args:
            event_type: 事件类型
            message: 消息内容
            additional_info: 额外信息
            
        Returns:
            通知发送结果
        """
        if self._in_dnd_window():
            return NotificationResult(
                success=True,
                method="dnd_skipped",
                details={"event_type": event_type, "message": message[:50]},
            )
        # 通过多平台通知器发送系统通知（返回 dict: success, success_count, fail_count）
        out = self.multi_platform_notifier.send_system_notification(
            event_type, message, additional_info
        )
        success = out.get("success", False) if isinstance(out, dict) else bool(out)
        # 系统通知（APP_START/APP_STOP/勿扰汇总等）不参与推送汇总统计，仅事件推送（send_notification）统计

        # 确定实际使用的方法（检查哪些平台真正发送了）
        active_platforms = []
        if self.config.wechat_webhook_url:
            active_platforms.append('wechat')
        if self.config.dingtalk_webhook_url:
            active_platforms.append('dingtalk')
        if self.config.feishu_webhook_url:
            active_platforms.append('feishu')
        if self.config.bark_url:
            active_platforms.append('bark')
        if self.config.pushplus_params:
            active_platforms.append('pushplus')
        if getattr(self.config, "magic_push_params", ""):
            active_platforms.append('magic_push')
        
        if len(active_platforms) == 0:
            method = 'none'
        elif len(active_platforms) == 1:
            method = active_platforms[0]
        else:
            method = 'multiple'
        
        return NotificationResult(
            success=success,
            method=method,
            details={
                'platforms': active_platforms,
                'event_type': event_type,
                'message': message
            }
        )
    
    def cleanup_cache(self):
        """清理缓存"""
        if self.multi_platform_notifier:
            self.multi_platform_notifier.cleanup_cache()
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            'has_multi_platform_notifier': self.multi_platform_notifier is not None
        }
        
        if self.multi_platform_notifier:
            stats['multi_platform_notifier'] = self.multi_platform_notifier.get_stats()
        
        return stats

    def get_delivery_health(self) -> Dict[str, Any]:
        """获取通知发送健康状态"""
        if self.multi_platform_notifier:
            return self.multi_platform_notifier.get_delivery_health()
        return {
            'last_attempt_time': None,
            'last_success_time': None,
            'consecutive_failures': 0,
            'first_failure_time': None,
            'total_failures_since_success': 0,
            'active_platforms': {
                'wechat': False,
                'dingtalk': False,
                'feishu': False,
                'bark': False,
                'pushplus': False,
                'magic_push': False,
            }
        }
    
    def close(self):
        """关闭通知器"""
        if self.multi_platform_notifier:
            self.multi_platform_notifier.close()
        
        self.logger.info("统一通知器已关闭")
