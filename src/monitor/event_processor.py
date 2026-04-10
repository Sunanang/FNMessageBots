"""
事件处理器模块
"""

import logging
import json
from typing import Dict, Any, Callable, Optional, List
from datetime import datetime
from threading import Timer

from .models import JournalEntry
from src.notifier.unified_notifier import UnifiedNotifier
from src.utils.log_storage import LogStorage

class EventProcessor:
    """事件处理器"""
    
    def __init__(self, notifier: UnifiedNotifier, config):
        """
        初始化事件处理器
        
        Args:
            notifier: 通知器实例
            config: 配置对象
        """
        self.notifier = notifier
        self.config = config
        self.logger = logging.getLogger(__name__)

        # 初始化日志存储器
        log_retention_days = getattr(config, 'log_retention_days', 30)
        self.log_storage = LogStorage(
            storage_dir=getattr(config, 'log_dir', './data/logs'),
            days_to_keep=log_retention_days,
            enable_auto_cleanup=True
        )
        
        # 事件处理器映射
        self.handlers = {
            'LoginSucc': self._handle_login_success,
            'LoginSucc2FA1': self._handle_login_2fa,
            'LoginFail': self._handle_login_fail,
            'Logout': self._handle_logout,
            'FoundDisk': self._handle_found_disk,
            'SSH_INVALID_USER': self._handle_ssh_invalid_user,
            'SSH_AUTH_FAILED': self._handle_ssh_auth_failed,
            'SSH_LOGIN_SUCCESS': self._handle_ssh_login_success,
            'SSH_DISCONNECTED': self._handle_ssh_disconnected,
            'APP_CRASH': self._handle_app_crash,
            'APP_UPDATE_FAILED': self._handle_app_update_failed,
            'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION': self._handle_app_start_failed_local,
            'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE': self._handle_app_auto_start_failed_docker,
            'CPU_USAGE_ALARM': self._handle_cpu_usage_alarm,
            'CPU_USAGE_RESTORED': self._handle_cpu_usage_restored,
            'MEMORY_USAGE_ALARM': self._handle_memory_usage_alarm,
            'MEMORY_USAGE_RESTORED': self._handle_memory_usage_restored,
            'CPU_TEMPERATURE_ALARM': self._handle_cpu_temperature_alarm,
            'UPS_ONBATT': self._handle_ups_onbatt,
            'UPS_ONBATT_LOWBATT': self._handle_ups_onbatt_lowbatt,
            'UPS_ONLINE': self._handle_ups_online,
            'UPS_ENABLE': self._handle_ups_enable,
            'UPS_DISABLE': self._handle_ups_disable,
            'DiskWakeup': self._handle_disk_wakeup,
            'DiskSpindown': self._handle_disk_spindown,
            # 数据库 log 表应用生命周期事件
            'APP_STARTED': lambda ed, e: self._handle_app_lifecycle('APP_STARTED', ed, e),
            'APP_STOPPED': lambda ed, e: self._handle_app_lifecycle('APP_STOPPED', ed, e),
            'APP_UPDATED': lambda ed, e: self._handle_app_lifecycle('APP_UPDATED', ed, e),
            'APP_INSTALLED': lambda ed, e: self._handle_app_lifecycle('APP_INSTALLED', ed, e),
            'APP_AUTO_STARTED': lambda ed, e: self._handle_app_lifecycle('APP_AUTO_STARTED', ed, e),
            'APP_UNINSTALLED': lambda ed, e: self._handle_app_lifecycle('APP_UNINSTALLED', ed, e),
            'DISK_IO_ERR': self._handle_disk_io_err,
            # 可选事件（默认不推送，需用户勾选）
            'ARCHIVING_SUCCESS': lambda ed, e: self._handle_simple_notification('ARCHIVING_SUCCESS', ed, e),
            'DeleteFile': lambda ed, e: self._handle_simple_notification('DeleteFile', ed, e),
            'MovetoTrashbin': lambda ed, e: self._handle_simple_notification('MovetoTrashbin', ed, e),
            'SHARE_EVENTID_DEL': lambda ed, e: self._handle_simple_notification('SHARE_EVENTID_DEL', ed, e),
            'SHARE_EVENTID_PUT': lambda ed, e: self._handle_simple_notification('SHARE_EVENTID_PUT', ed, e),
            'WEBDAV_ENABLED': lambda ed, e: self._handle_simple_notification('WEBDAV_ENABLED', ed, e),
            'WEBDAV_DISABLED': lambda ed, e: self._handle_simple_notification('WEBDAV_DISABLED', ed, e),
            'SAMBA_ENABLED': lambda ed, e: self._handle_simple_notification('SAMBA_ENABLED', ed, e),
            'SAMBA_DISABLED': lambda ed, e: self._handle_simple_notification('SAMBA_DISABLED', ed, e),
            'DLNA_ENABLED': lambda ed, e: self._handle_simple_notification('DLNA_ENABLED', ed, e),
            'DLNA_DISABLED': lambda ed, e: self._handle_simple_notification('DLNA_DISABLED', ed, e),
            'FTP_ENABLED': lambda ed, e: self._handle_simple_notification('FTP_ENABLED', ed, e),
            'FTP_DISABLED': lambda ed, e: self._handle_simple_notification('FTP_DISABLED', ed, e),
            'NFS_ENABLED': lambda ed, e: self._handle_simple_notification('NFS_ENABLED', ed, e),
            'NFS_DISABLED': lambda ed, e: self._handle_simple_notification('NFS_DISABLED', ed, e),
            'FW_ENABLE': lambda ed, e: self._handle_simple_notification('FW_ENABLE', ed, e),
            'FW_DISABLE': lambda ed, e: self._handle_simple_notification('FW_DISABLE', ed, e),
            'SECURITY_PORTCHANGED': lambda ed, e: self._handle_simple_notification('SECURITY_PORTCHANGED', ed, e),
            # 虚拟机事件（默认不勾选；勾选后直接推送）
            'SHUTDOWN_VM': lambda ed, e: self._handle_simple_notification('SHUTDOWN_VM', ed, e),
            'STATUS_RUNNING_VM': lambda ed, e: self._handle_simple_notification('STATUS_RUNNING_VM', ed, e),
            'DESTROY_VM': lambda ed, e: self._handle_simple_notification('DESTROY_VM', ed, e),
        }
        
        # 磁盘事件合并缓存
        self.disk_wakeup_cache = []
        self.disk_spindown_cache = []
        self.merge_window = 30  # 30秒合并窗口
        self.wakeup_timer = None
        self.spindown_timer = None

        # SSH认证失败去重（避免pam_unix与Failed password重复推送）
        self.ssh_auth_fail_cache = {}
        self.ssh_auth_fail_window = 5  # 秒
        self.ssh_auth_fail_cache_max = 10000
        
        # SSH事件合并（短窗口内合并相近事件）
        self.ssh_merge_window = 5  # 秒
        self.ssh_pending = {}
        
        self.logger.info("事件处理器初始化完成")

    def _build_batch_event_brief(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """为批量汇总生成单条简述。"""
        data = event_data.get('data') if isinstance(event_data.get('data'), dict) else {}
        parts = []
        if event_data.get('user') or event_data.get('IP'):
            parts.append(f"{event_data.get('user', '')}@{event_data.get('IP', '')}".strip("@"))
        if data.get('DISPLAY_NAME') or data.get('APP_NAME'):
            parts.append(data.get('DISPLAY_NAME') or data.get('APP_NAME'))
        if event_data.get('name'):
            parts.append(str(event_data.get('name')))
        if event_data.get('message'):
            parts.append(str(event_data.get('message'))[:40])
        if not parts:
            parts.append(event_type)
        return " | ".join([str(p).strip() for p in parts if p])[:120]

    def process_batch_events(self, batch_events: List[Dict[str, Any]]) -> bool:
        """同一轮轮询的事件合并为一条消息推送，避免高频触发渠道限流。"""
        if not batch_events:
            return False

        latest_entry = batch_events[-1].get("entry")
        timestamp = getattr(latest_entry, "timestamp", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        by_type: Dict[str, int] = {}
        preview_items: List[Dict[str, str]] = []
        grouped_events: Dict[str, List[Dict[str, Any]]] = {}
        raw_for_storage: List[Dict[str, Any]] = []

        for item in batch_events:
            event_type = str(item.get("event_type") or "unknown")
            event_data = item.get("event_data") or {}
            entry = item.get("entry")
            by_type[event_type] = by_type.get(event_type, 0) + 1
            grouped_events.setdefault(event_type, []).append({
                "timestamp": getattr(entry, "timestamp", timestamp),
                "event_data": event_data,
                "raw_log": getattr(entry, "raw_data", "{}"),
            })

            if len(preview_items) < 10:
                preview_items.append({
                    "event_type": event_type,
                    "timestamp": getattr(entry, "timestamp", timestamp),
                    "brief": self._build_batch_event_brief(event_type, event_data),
                })

            raw_for_storage.append({
                "event_type": event_type,
                "event_data": event_data,
                "timestamp": getattr(entry, "timestamp", timestamp),
                "raw_log": getattr(entry, "raw_data", "{}"),
            })

            # 原始日志仍逐条落库，便于后续排查
            self._store_notification_log(
                event_type=event_type,
                event_data=event_data,
                raw_log=getattr(entry, "raw_data", "{}"),
                entry=entry,
                source='db'
            )

        summary_event_data = {
            "count": len(batch_events),
            "by_type": by_type,
            "items": preview_items,
            "grouped_events": grouped_events,
        }
        raw_log = json.dumps(raw_for_storage, ensure_ascii=False)[:6000]
        self.logger.info("轮询批量汇总推送：count=%s, types=%s", len(batch_events), len(by_type))
        self.notifier.send_notification(
            event_type='POLL_BATCH_SUMMARY',
            event_data=summary_event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        return True

    def _send_ssh_notification(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        """发送SSH相关通知并存储日志"""
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log(event_type, event_data, raw_log, entry)

    def _schedule_ssh_event(self, key: str, event_type: str, event_data: Dict[str, Any],
                            entry: JournalEntry, log_message: str):
        """短窗口延迟发送SSH事件，等待合并"""
        existing = self.ssh_pending.pop(key, None)
        if existing:
            existing.get('timer').cancel()

        def _flush():
            pending = self.ssh_pending.pop(key, None)
            if not pending:
                return
            self.logger.info(pending['log_message'])
            self._send_ssh_notification(pending['event_type'], pending['event_data'], pending['entry'])

        timer = Timer(self.ssh_merge_window, _flush)
        timer.daemon = True
        self.ssh_pending[key] = {
            'event_type': event_type,
            'event_data': event_data,
            'entry': entry,
            'log_message': log_message,
            'timer': timer
        }
        timer.start()
    
    def _store_notification_log(self, event_type: str, event_data: Dict[str, Any], 
                               raw_log: str, entry: JournalEntry, source: str = "db"):
        """
        存储通知日志
        
        Args:
            event_type: 事件类型
            event_data: 事件数据
            raw_log: 原始日志
            entry: 日志条目对象
            source: 日志来源
        """
        try:
            # 优先使用真正的原始日志行，如果没有则使用传入的raw_log
            actual_raw_log = entry.original_line if entry.original_line else raw_log
            
            # 存储日志到数据库
            success = self.log_storage.store_log(
                event_type=event_type,
                raw_log=actual_raw_log,  # 使用真正的原始日志
                processed_data=event_data,
                source=source
            )
            
            if success:
                self.logger.debug(f"日志存储成功: {event_type}")
            else:
                self.logger.warning(f"日志存储失败: {event_type}")
                
        except Exception as e:
            self.logger.error(f"存储日志时发生错误: {e}")
    
    def get_handler(self, event_type: str) -> Optional[Callable]:
        """
        获取事件处理器
        
        Args:
            event_type: 事件类型
            
        Returns:
            处理函数或None
        """
        handler = self.handlers.get(event_type)
        if handler:
            return handler
        # 数据库中若出现新的 VM 事件（如 CREATE_VM / START_VM 等），
        # 且用户已在 monitor_events 中勾选该 eventId，则走通用推送。
        if "VM" in str(event_type).upper():
            return lambda ed, e, _et=event_type: self._handle_simple_notification(_et, ed, e)
        return None
    
    def _handle_login_success(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理登录成功事件"""
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        
        self.logger.info(f"登录成功: {user}@{ip}")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        self.notifier.send_notification(
            event_type='LoginSucc',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        
        # 存储原始日志
        self._store_notification_log(
            event_type='LoginSucc',
            event_data=event_data,
            raw_log=raw_log,
            entry=entry,
            source='db'
        )
    
    def _handle_login_2fa(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理二次验证登录事件"""
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        
        self.logger.info(f"二次验证登录: {user}@{ip}")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        self.notifier.send_notification(
            event_type='LoginSucc2FA1',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_login_fail(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理登录失败事件"""
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        
        self.logger.warning(f"登录失败: {user}@{ip}")
        
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        self.notifier.send_notification(
            event_type='LoginFail',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _handle_logout(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理退出登录事件"""
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        
        self.logger.info(f"退出登录: {user}@{ip}")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        self.notifier.send_notification(
            event_type='Logout',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _handle_found_disk(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理发现硬盘事件"""
        name = event_data.get('name', '')
        model = event_data.get('model', '')
        serial = event_data.get('serial', '')
        
        self.logger.info(f"发现硬盘: {name} ({model})")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        self.notifier.send_notification(
            event_type='FoundDisk',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_ssh_invalid_user(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理SSH无效用户尝试事件"""
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        self.logger.warning(f"SSH无效用户尝试: {user}@{ip}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type='SSH_INVALID_USER',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('SSH_INVALID_USER', event_data, raw_log, entry)

    def _handle_ssh_auth_failed(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理SSH认证失败事件"""
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        # 基于IP+user做短窗口去重，避免同一次尝试多条日志重复推送
        key = f"{ip or 'unknown'}|{user or 'unknown'}"
        now = datetime.now().timestamp()
        last_ts = self.ssh_auth_fail_cache.get(key)
        if last_ts and (now - last_ts) < self.ssh_auth_fail_window:
            self.logger.debug(f"SSH认证失败去重: {user}@{ip}")
            return
        self.ssh_auth_fail_cache[key] = now
        # 清理过期缓存，控制规模
        if len(self.ssh_auth_fail_cache) > self.ssh_auth_fail_cache_max:
            cutoff = now - (self.ssh_auth_fail_window * 2)
            self.ssh_auth_fail_cache = {
                k: ts for k, ts in self.ssh_auth_fail_cache.items()
                if ts >= cutoff
            }

        self.logger.warning(f"SSH认证失败: {user}@{ip}")
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.notifier.send_notification(
            event_type='SSH_AUTH_FAILED',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('SSH_AUTH_FAILED', event_data, raw_log, entry)

    def _handle_ssh_login_success(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理SSH登录成功事件"""
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        key_suffix = user
        self._schedule_ssh_event(
            key=f"ssh_login_success:{key_suffix}",
            event_type='SSH_LOGIN_SUCCESS',
            event_data=event_data,
            entry=entry,
            log_message=f"SSH登录成功: {user}@{ip}"
        )

    def _handle_ssh_disconnected(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理SSH断开连接事件"""
        user = event_data.get('user', 'unknown')
        ip = event_data.get('IP', 'unknown')
        key_suffix = user
        self._schedule_ssh_event(
            key=f"ssh_disconnected:{key_suffix}",
            event_type='SSH_DISCONNECTED',
            event_data=event_data,
            entry=entry,
            log_message=f"SSH断开连接: {user}@{ip}"
        )

    def _handle_app_crash(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理应用崩溃事件"""
        data = event_data.get('data', {})
        display_name = data.get('DISPLAY_NAME', data.get('APP_NAME', '未知应用'))
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 输出标准格式日志
        print(f"[错误] 应用: {display_name}, 崩溃异常退出, 时间: {timestamp}")
        
        self.logger.warning(f"应用崩溃: {display_name}")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        
        self.notifier.send_notification(
            event_type='APP_CRASH',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _handle_app_update_failed(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理应用更新失败事件"""
        data = event_data.get('data', {})
        display_name = data.get('DISPLAY_NAME', data.get('APP_NAME', '未知应用'))
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 输出标准格式日志
        print(f"[错误] 应用: {display_name}, 更新失败, 时间: {timestamp}")
        
        self.logger.warning(f"应用更新失败: {display_name}")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        
        self.notifier.send_notification(
            event_type='APP_UPDATE_FAILED',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_app_start_failed_local(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理应用启动失败事件（本地运行异常）"""
        data = event_data.get('data', {})
        display_name = data.get('DISPLAY_NAME', data.get('APP_NAME', '未知应用'))
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"应用启动失败: {display_name}")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_app_auto_start_failed_docker(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理应用自启动失败事件（Docker 不可用）"""
        data = event_data.get('data', {})
        display_name = data.get('DISPLAY_NAME', data.get('APP_NAME', '未知应用'))
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"应用自启动失败(Docker不可用): {display_name}")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_app_lifecycle(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        """处理应用生命周期事件（APP_STARTED/STOPPED/UPDATED/INSTALLED/AUTO_STARTED/UNINSTALLED，来自数据库）"""
        data = event_data.get('data', {})
        display_name = data.get('DISPLAY_NAME', data.get('APP_NAME', '未知应用'))
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info(f"应用生命周期: {event_type} - {display_name}")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log(event_type, event_data, raw_log, entry, source='db')

    def _handle_disk_io_err(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理磁盘IO错误事件（data: DEV, SN, MODEL, ERR_CNT）"""
        data = event_data.get('data', {})
        dev = data.get('DEV', data.get('dev', ''))
        err_cnt = data.get('ERR_CNT', data.get('err_cnt', 0))
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"磁盘IO错误: {dev}, 错误次数 {err_cnt}")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='DISK_IO_ERR',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('DISK_IO_ERR', event_data, raw_log, entry, source='db')

    def _handle_simple_notification(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        """可选事件的通用处理：解析 parameter 后直接推送，无额外逻辑。"""
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type=event_type,
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log(event_type, event_data, raw_log, entry, source='db')

    def _handle_cpu_usage_alarm(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理 CPU 使用率告警（parameter 含 data.THRESHOLD，如 {"from":"trim.resource-manager","eventId":"CPU_USAGE_ALARM","data":{"THRESHOLD":90},"datetime":...}）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"CPU使用率告警: 超过 {threshold}%")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='CPU_USAGE_ALARM',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('CPU_USAGE_ALARM', event_data, raw_log, entry, source='db')

    def _handle_cpu_usage_restored(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理 CPU 使用率恢复（parameter 含 data.THRESHOLD，如 {"from":"trim.resource-manager","eventId":"CPU_USAGE_RESTORED","data":{"THRESHOLD":90},"datetime":...}）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info(f"CPU使用率恢复: 已低于阈值 {threshold}%")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='CPU_USAGE_RESTORED',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('CPU_USAGE_RESTORED', event_data, raw_log, entry, source='db')

    def _handle_memory_usage_alarm(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理内存使用率告警（trim.resource-manager，parameter 含 data.THRESHOLD）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"内存使用率告警: 超过 {threshold}%")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='MEMORY_USAGE_ALARM',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('MEMORY_USAGE_ALARM', event_data, raw_log, entry, source='db')

    def _handle_memory_usage_restored(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理内存使用率恢复（parameter 含 data.THRESHOLD）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info(f"内存使用率恢复: 已低于阈值 {threshold}%")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='MEMORY_USAGE_RESTORED',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
        self._store_notification_log('MEMORY_USAGE_RESTORED', event_data, raw_log, entry, source='db')

    def _handle_cpu_temperature_alarm(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理 CPU 温度告警"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.warning(f"CPU温度告警: 超过 {threshold}°C")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='CPU_TEMPERATURE_ALARM',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _handle_ups_onbatt(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理UPS切换到电池供电事件"""
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 输出标准格式日志
        print(f"[警告] UPS启动，切换到电池供电, 时间: {timestamp}")
        
        self.logger.warning("UPS启动，切换到电池供电")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        
        self.notifier.send_notification(
            event_type='UPS_ONBATT',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _handle_ups_onbatt_lowbatt(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理UPS切换到电池供电且电池电量低事件"""
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 输出标准格式日志
        print(f"[严重警告] UPS启动，切换到电池供电，电池电量低警告, 时间: {timestamp}")
        
        self.logger.warning("UPS启动，切换到电池供电，电池电量低警告")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        
        self.notifier.send_notification(
            event_type='UPS_ONBATT_LOWBATT',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _handle_ups_online(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理UPS切换到市电供电事件"""
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 输出标准格式日志
        print(f"[通知] UPS启动，切换到市电供电模式, 时间: {timestamp}")
        
        self.logger.info("UPS启动，切换到市电供电模式")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        
        self.notifier.send_notification(
            event_type='UPS_ONLINE',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_ups_enable(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理开启 UPS 支持事件"""
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info("UPS 支持已开启")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='UPS_ENABLE',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )

    def _handle_ups_disable(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理关闭 UPS 支持事件"""
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info("UPS 支持已关闭")
        raw_log = getattr(entry, 'raw_data', '{}')
        self.notifier.send_notification(
            event_type='UPS_DISABLE',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _add_to_cache_and_schedule_send(self, cache_list, timer_attr, event_data, event_type, entry):
        """将事件添加到缓存并安排发送"""
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        disk_details = self._extract_disk_details(event_data)
        if not disk_details:
            disk_details = [{
                'disk': '',
                'model': '',
                'serial': ''
            }]

        for detail in disk_details:
            event_entry = {
                'disk': detail.get('disk', ''),
                'model': detail.get('model', ''),
                'serial': detail.get('serial', ''),
                'timestamp': timestamp,
                'raw_log': getattr(entry, 'raw_data', '{}'),
                'full_event_data': event_data
            }
            cache_list.append(event_entry)

            event_desc = "磁盘唤醒" if event_type == 'DiskWakeup' else "磁盘休眠"
            disk_label = event_entry['disk'] or event_entry['serial'] or event_entry['model'] or "(未提供磁盘信息)"
            print(f"[{event_desc}] 磁盘: {disk_label}, 型号: {event_entry['model']}, 序列号: {event_entry['serial']}, 时间: {timestamp}")
        
        # 取消之前的定时器
        old_timer = getattr(self, timer_attr, None)
        if old_timer and old_timer.is_alive():
            old_timer.cancel()
        
        # 设置新的定时器，延迟发送合并事件
        new_timer = Timer(self.merge_window, lambda: self._send_merged_events(cache_list, event_type))
        new_timer.start()
        setattr(self, timer_attr, new_timer)
    
    def _handle_disk_wakeup(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理磁盘唤醒事件"""
        self._add_to_cache_and_schedule_send(
            self.disk_wakeup_cache, 
            'wakeup_timer', 
            event_data, 
            'DiskWakeup', 
            entry
        )
        
        # 存储原始日志用于后续分析
        raw_log = getattr(entry, 'raw_data', '{}')
        self._store_notification_log('DiskWakeup', event_data, raw_log, entry)

    def _handle_disk_spindown(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理磁盘休眠事件"""
        self._add_to_cache_and_schedule_send(
            self.disk_spindown_cache, 
            'spindown_timer', 
            event_data, 
            'DiskSpindown', 
            entry
        )
        
        # 存储原始日志用于后续分析
        raw_log = getattr(entry, 'raw_data', '{}')
        self._store_notification_log('DiskSpindown', event_data, raw_log, entry)

    def _send_merged_events(self, cache_list, event_type):
        """发送合并的磁盘事件"""
        if not cache_list:
            return
        
        # 创建合并的事件数据
        merged_data = {
            'merged_disks': cache_list,
            'count': len(cache_list),
            'event_type': event_type
        }
        
        # 获取最新的时间戳作为事件时间
        latest_timestamp = cache_list[-1]['timestamp']
        
        # 输出合并的日志
        event_desc = "磁盘唤醒" if event_type == 'DiskWakeup' else "磁盘休眠"
        print(f"[{event_desc}合并] 共 {len(cache_list)} 个磁盘事件:")
        for event in cache_list:
            print(f"  - 磁盘: {event['disk']}, 型号: {event['model']}, 序列号: {event['serial']}")
        
        # 发送通知
        self.notifier.send_notification(
            event_type=event_type,
            event_data=merged_data,
            raw_log=str(cache_list),
            timestamp=latest_timestamp
        )
        
        # 清空缓存
        cache_list.clear()

    def _extract_disk_details(self, event_data: Dict[str, Any]) -> List[Dict[str, str]]:
        """从事件数据中提取磁盘信息。支持飞牛 NAS 的顶层格式：{"template":"DiskSpindown","user":"Lando","model":"...","serial":"..."} 或 {"disk":"sdb","model":"...","serial":"..."}"""
        details = []

        def add_candidate(source):
            if isinstance(source, dict):
                details.append(source)

        # 飞牛 NAS：parameter 顶层即为 disk/model/serial（或 template/user/model/serial），无 data 嵌套
        disk = self._pick_disk_field(event_data)
        model = self._pick_field(event_data, [
            'model', 'MODEL', 'Model', 'disk_model', 'diskModel', 'modelName', 'model_name'
        ])
        serial = self._pick_field(event_data, [
            'serial', 'SERIAL', 'Serial', 'sn', 'SN', 'serial_number', 'serialNumber', 'SerialNumber'
        ])
        if disk or model or serial:
            return [{'disk': disk, 'model': model, 'serial': serial}]

        data_section = event_data.get('data')
        add_candidate(event_data)
        if isinstance(data_section, dict):
            add_candidate(data_section)

        list_keys = [
            'disks', 'disk_list', 'diskList', 'devices', 'device_list',
            'DISKS', 'DISK_LIST', 'DEVICES'
        ]
        for container in filter(lambda x: isinstance(x, dict), [event_data, data_section]):
            for key in list_keys:
                items = container.get(key)
                if isinstance(items, list):
                    for item in items:
                        add_candidate(item)

        # 有些结构会把实际磁盘对象放在 'disk' 键下
        for container in list(details):
            disk_field = container.get('disk')
            if isinstance(disk_field, dict):
                add_candidate(disk_field)

        normalized = []
        for candidate in details:
            normalized_entry = {
                'disk': self._pick_disk_field(candidate),
                'model': self._pick_field(candidate, [
                    'model', 'MODEL', 'disk_model', 'diskModel', 'modelName', 'model_name', 'Model'
                ]),
                'serial': self._pick_field(candidate, [
                    'serial', 'SERIAL', 'sn', 'SN', 'serial_number', 'serialNumber', 'SerialNumber'
                ])
            }
            if any(normalized_entry.values()):
                normalized.append(normalized_entry)

        # 若未从列表/嵌套结构解析出任何磁盘，尝试把 event_data 或 data 当作单条磁盘信息再解析一次
        if not normalized:
            for single in [event_data, event_data.get('data') or {}]:
                if not isinstance(single, dict) or single in details:
                    continue
                disk = self._pick_disk_field(single)
                model = self._pick_field(single, [
                    'model', 'MODEL', 'disk_model', 'diskModel', 'modelName', 'model_name', 'Model'
                ])
                serial = self._pick_field(single, [
                    'serial', 'SERIAL', 'sn', 'SN', 'serial_number', 'serialNumber', 'SerialNumber'
                ])
                if disk or model or serial:
                    normalized.append({'disk': disk, 'model': model, 'serial': serial})
                    break

        return normalized

    def _pick_disk_field(self, candidate: Dict[str, Any]) -> str:
        disk = self._pick_field(candidate, [
            'disk', 'device', 'path', 'name', 'disk_name', 'diskName', 'DEVICE', 'DISK',
            'deviceName', 'device_name', 'devicePath', 'device_path', 'dev', 'DEV'
        ])
        if disk:
            return disk

        # 尝试槽位/序号
        slot = self._pick_field(candidate, ['slot', 'slot_id', 'bay', 'index', 'slotId'])
        if slot:
            return f"槽位 {slot}"

        # 某些日志只提供路径列表
        paths = candidate.get('paths') or candidate.get('PATHS')
        if isinstance(paths, list) and paths:
            return str(paths[0])

        return ''

    def _pick_field(self, candidate: Dict[str, Any], keys: List[str]) -> str:
        for key in keys:
            if key in candidate and candidate[key]:
                return self._coerce_str(candidate[key])
            upper = key.upper()
            if upper in candidate and candidate[upper]:
                return self._coerce_str(candidate[upper])
            camel = key[:1].lower() + key[1:]
            if camel in candidate and candidate[camel]:
                return self._coerce_str(candidate[camel])
        return ''

    def _coerce_str(self, value: Any) -> str:
        if isinstance(value, dict):
            # 常见结构: {'path': '/dev/sda', 'sn': '123'}
            for nested_key in ['path', 'device', 'name', 'disk', 'value']:
                if nested_key in value and value[nested_key]:
                    return str(value[nested_key])
            return ''
        if isinstance(value, list):
            return str(value[0]) if value else ''
        if value is None:
            return ''
        return str(value)
    
    def _handle_generic_login(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理通用登录事件"""
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        
        self.logger.info(f"通用登录成功: {user}@{ip}")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        self.notifier.send_notification(
            event_type='LoginSucc',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def _handle_generic_logout(self, event_data: Dict[str, Any], entry: JournalEntry):
        """处理通用登出事件"""
        user = event_data.get('user', '')
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        
        self.logger.info(f"通用登出: {user}@{ip}")
        
        # 如果entry为None，使用默认值
        raw_log = getattr(entry, 'raw_data', '{}')
        timestamp = getattr(entry, 'timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        self.notifier.send_notification(
            event_type='Logout',
            event_data=event_data,
            raw_log=raw_log,
            timestamp=timestamp
        )
    
    def process_event(self, event_type: str, event_data: Dict[str, Any], entry: JournalEntry):
        """
        处理事件（通用接口）
        
        Args:
            event_type: 事件类型
            event_data: 事件数据
            entry: 日志条目
        """
        handler = self.get_handler(event_type)
        if handler:
            try:
                handler(event_data, entry)
                return True
            except Exception as e:
                self.logger.error(f"处理事件失败: {e}")
                return False
        else:
            self.logger.warning(f"未知事件类型: {event_type}")
            return False
