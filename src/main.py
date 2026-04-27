
import sys
import signal
import socket
import os
import traceback
from datetime import datetime
import time
from pathlib import Path
import threading
import sqlite3
import stat

# 添加src目录到Python路径，解决模块导入问题
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from utils.logger import setup_logging
from utils.push_stats import init as init_push_stats
from monitor.db_log_poller import DBLogPoller
from monitor.backup_db_poller import (
    BackupDBPoller,
    BACKUP_FAILED_EVENT,
    BACKUP_POLL_EVENTS,
    BACKUP_SUCCESS_EVENT,
)
from monitor.media_db_poller import MediaDBPoller
from monitor.trim_activity_poller import TrimActivityPoller
from monitor.photo_db_poller import PHOTO_POLL_EVENTS, PhotoDBPoller
from monitor.scheduler_db_poller import (
    SchedulerDBPoller,
    SCHEDULER_POLL_EVENTS,
    SCHEDULER_TASK_SUCCESS_EVENT,
    SCHEDULER_TASK_FAILED_EVENT,
    SCHEDULER_TASK_CONDITION_FAILED_EVENT,
)
from monitor.event_processor import EventProcessor
from notifier.unified_notifier import UnifiedNotifier
from web.ui_app import start_ui_server_in_background

# 仅当 monitor_events 包含对应事件时才轮询各库（避免空跑）
TRIMMEDIA_POLL_EVENTS = frozenset({"TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS"})
TRIMACTIVITY_POLL_EVENTS = frozenset({"MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT"})


class Application:
    """主应用程序"""
    
    def __init__(self):
        """初始化应用"""
        self.config = None
        self.notifier = None
        self.event_processor = None
        self.log_poller = None
        self.backup_poller = None
        self.media_db_poller = None
        self.trim_activity_poller = None
        self.photo_db_poller = None
        self.scheduler_db_poller = None
        self.logger = None
        self.running = False
        self.notification_health_thread = None

        
    def _print_banner(self):
        """打印启动横幅"""
        banner = f"""
        启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        监控模式: 数据库轮询 (logger_data.db3 log 表)
        通知方式: 企业微信/钉钉/飞书机器人/Bark/PushPlus/魔法推送/SMTP邮件

        """
        print(banner)

    def _dispatch_batch_events(self, batch_events):
        """适配 DBLogPoller 批处理回调签名（忽略返回值）。"""
        if self.event_processor:
            self.event_processor.process_batch_events(batch_events)


    def _probe_db_readable(self, label: str, path: str) -> bool:
        """检查 SQLite 路径是否可读。返回是否可访问。"""
        db_path = (path or "").strip()
        if not db_path:
            return False
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            print(f"✓ {label} 可访问: {db_path}")
            return True
        except Exception as e:
            print(f"⚠ {label} 不可访问: {db_path}（{e}）")
            if self.logger:
                self.logger.warning("%s 不可访问: %s (%s)", label, db_path, e)
            self._print_db_permission_hint(label, db_path)
            return False

    def _unix_mode_str(self, mode: int) -> str:
        """将 mode 转成人类可读的 rwx 形式。"""
        return stat.filemode(mode)

    def _print_db_permission_hint(self, label: str, db_path: str) -> None:
        """输出路径权限诊断与修复建议（不自动提权/修复）。"""
        p = Path(db_path)
        parts = [Path("/")]
        for item in p.parts[1:-1]:
            parts.append(parts[-1] / item)

        blocked_dir = None
        blocked_reason = ""
        for d in parts:
            try:
                st = d.stat()
                can_enter = os.access(d, os.X_OK)
                can_read_dir = os.access(d, os.R_OK)
                if not can_enter or not can_read_dir:
                    blocked_dir = d
                    blocked_reason = (
                        f"目录权限不足（mode={self._unix_mode_str(st.st_mode)}，"
                        f"need: 读取+进入目录）"
                    )
                    break
            except FileNotFoundError:
                blocked_dir = d
                blocked_reason = "目录不存在"
                break
            except PermissionError:
                blocked_dir = d
                blocked_reason = "当前进程无权限访问该目录"
                break
            except Exception as ex:
                blocked_dir = d
                blocked_reason = f"目录检查失败: {ex}"
                break

        if blocked_dir is not None:
            print(f"  → {label} 诊断：{blocked_dir} {blocked_reason}")
            print("  → 建议修复（任选其一）：")
            print(f"     1) chmod 755 '{blocked_dir}'")
            print(f"     2) chown -R <服务用户>:<服务组> '{blocked_dir}' && chmod 750 '{blocked_dir}'")
            return

        try:
            st = p.stat()
            if not os.access(p, os.R_OK):
                print(
                    f"  → {label} 诊断：文件不可读（mode={self._unix_mode_str(st.st_mode)}）：{p}"
                )
                print("  → 建议修复（任选其一）：")
                print(f"     1) chmod 644 '{p}'")
                print(f"     2) chown <服务用户>:<服务组> '{p}' && chmod 640 '{p}'")
                return
        except FileNotFoundError:
            print(f"  → {label} 诊断：数据库文件不存在：{p}")
            return
        except PermissionError:
            print(f"  → {label} 诊断：当前进程无权限访问数据库文件：{p}")
            return
        except Exception as ex:
            print(f"  → {label} 诊断：文件检查失败：{p} ({ex})")
            return

        # 到这里说明路径权限看起来正常，但 sqlite 仍打不开（例如文件损坏/被锁）
        print("  → 路径权限检查通过，但数据库仍不可读；请检查文件是否损坏或被占用。")

    def _report_external_db_access(self) -> None:
        """对当前启用的外部数据库做读权限探测，便于快速发现权限问题。"""
        if not self.config:
            return
        me = set(self.config.monitor_events or [])
        if me & BACKUP_POLL_EVENTS:
            self._probe_db_readable("备份库 basic_backup.db3", getattr(self.config, "backup_db_path", "") or "")
        if me & TRIMMEDIA_POLL_EVENTS:
            self._probe_db_readable("影视库 trimmedia.db", getattr(self.config, "trim_media_db_path", "") or "")
        if me & TRIMACTIVITY_POLL_EVENTS:
            self._probe_db_readable("影视库 trimactivity.db", getattr(self.config, "trim_activity_db_path", "") or "")
        if me & PHOTO_POLL_EVENTS:
            self._probe_db_readable("相册库 photo.db", getattr(self.config, "photo_db_path", "") or "")
        if me & SCHEDULER_POLL_EVENTS:
            self._probe_db_readable("任务计划库 scheduler.db", getattr(self.config, "scheduler_db_path", "") or "")
    
    def initialize(self) -> bool:
        """初始化应用组件"""
        try:
            print("开始初始化应用组件...")
            
            # 加载配置（可不配置 Webhook，部署后通过 UI 配置）
            self.config = Config()
            init_push_stats(self.config.cursor_dir)
            has_webhook = any([
                self.config.wechat_webhook_url,
                self.config.dingtalk_webhook_url,
                self.config.feishu_webhook_url,
                self.config.bark_url,
                self.config.pushplus_params,
                getattr(self.config, "magic_push_params", "") or "",
                getattr(self.config, "smtp_params", "") or "",
            ])
            if has_webhook:
                print("配置加载完成（已配置推送渠道）")
            else:
                print("配置加载完成（未配置推送渠道，可在 Web 配置页面添加）")
            
            # 设置日志
            self.logger = setup_logging(self.config)
            print("日志设置完成")
            
            # 打印横幅
            self._print_banner()
            
            # 显示配置信息
            print(f"监控事件: {', '.join(self.config.monitor_events)}")
            print(f"日志级别: {self.config.log_level}")
            print(f"去重窗口: {self.config.dedup_window}秒")
            print(f"连接池大小: {self.config.http_pool_size}")
            
            # 检查推送渠道配置
            if self.config.wechat_webhook_url:
                print(f"企业微信Webhook: 已配置")
            if self.config.dingtalk_webhook_url:
                print(f"钉钉Webhook: 已配置")
            if self.config.feishu_webhook_url:
                print(f"飞书Webhook: 已配置")
            if self.config.bark_url:
                print(f"Bark: 已配置")
            if self.config.pushplus_params:
                print(f"PushPlus: 已配置")
            if getattr(self.config, "magic_push_params", ""):
                print(f"魔法推送: 已配置")
            if getattr(self.config, "smtp_params", ""):
                print("SMTP邮件: 已配置")
            if not has_webhook:
                print("未配置推送渠道：不轮询数据库、不推送消息，仅提供 Web 配置页面。")
                print("初始化完成（待配置）。")
                return True
            
            # 已配置推送渠道：初始化通知器、事件处理器、数据库轮询器
            print("初始化多平台通知器...")
            self.notifier = UnifiedNotifier(self.config)
            print("多平台通知器初始化完成")
            
            print("正在初始化事件处理器...")
            self.event_processor = EventProcessor(self.notifier, self.config)
            print("事件处理器初始化完成")
            
            print("正在初始化数据库日志轮询器...")
            self.log_poller = DBLogPoller(
                db_path=self.config.logger_db_path,
                cursor_dir=self.config.cursor_dir,
                poll_interval=self.config.logger_poll_interval,
                monitor_events=self.config.monitor_events,
                media_lib_logger_enabled=getattr(self.config, "media_lib_logger_enabled", False),
                media_lib_service_patterns=getattr(self.config, "media_lib_service_patterns", []),
            )
            if getattr(self.config, "poll_batch_summary_enabled", False):
                self.log_poller.set_batch_handler(self._dispatch_batch_events)
                print("轮询汇总模式：已开启（同一轮查询内多事件合并为一条推送）")
            else:
                self.log_poller.set_batch_handler(None)
                print("轮询汇总模式：已关闭（逐条推送；事件密集时可能触发渠道限流）")
            print(f"数据库轮询器初始化完成（间隔: {self.config.logger_poll_interval}秒，数据库: {self.config.logger_db_path}）")

            print("正在按监控事件初始化备份库/影视库/相册轮询器...")
            self._sync_optional_pollers()
            if self.backup_poller:
                print(
                    f"备份库轮询已启用（间隔 {self.config.logger_poll_interval} 秒，"
                    f"{getattr(self.config, 'backup_db_path', '')}）"
                )
            else:
                print("未勾选备份任务事件，跳过备份库轮询")
            if self.media_db_poller:
                print(
                    f"trimmedia 轮询已启用（间隔 {self.config.logger_poll_interval} 秒，"
                    f"{getattr(self.config, 'trim_media_db_path', '') or '(路径未配置)'}）"
                )
            else:
                print("未勾选影视库入库/刮削事件，跳过 trimmedia.db 轮询")
            if self.trim_activity_poller:
                print(
                    f"trimactivity 轮询已启用（间隔 {self.config.logger_poll_interval} 秒，"
                    f"{getattr(self.config, 'trim_activity_db_path', '') or '(路径未配置)'}）"
                )
            else:
                print("未勾选影视库登录/登出事件，跳过 trimactivity.db 轮询")
            if self.photo_db_poller:
                print(
                    f"相册 photo.db 轮询已启用（间隔 {self.config.logger_poll_interval} 秒，"
                    f"{getattr(self.config, 'photo_db_path', '') or '(路径未配置)'}）"
                )
            else:
                print("未勾选相册相关事件，跳过 photo.db 轮询")
            if self.scheduler_db_poller:
                print(
                    f"任务计划 scheduler.db 轮询已启用（间隔 {self.config.logger_poll_interval} 秒，"
                    f"{getattr(self.config, 'scheduler_db_path', '') or '(路径未配置)'}）"
                )
            else:
                print("未勾选任务计划事件，跳过 scheduler.db 轮询")


            print("开始注册事件处理器...")
            self._register_db_event_handlers()
            
            print(f"\n初始化完成，开始监控...")
            return True
        except Exception as e:
            print(f"初始化失败: {e}")
            traceback.print_exc()
            return False

    def _sync_optional_pollers(self) -> None:
        """按 monitor_events 创建或停止备份库 / trimmedia / trimactivity / photo.db 轮询器。"""
        if not self.config:
            return
        me = set(self.config.monitor_events or [])
        interval = self.config.logger_poll_interval
        cdir = self.config.cursor_dir

        backup_path = getattr(self.config, "backup_db_path", "/usr/trim/var/backup_service/basic_backup.db3")
        backup_ok = self._probe_db_readable("备份库 basic_backup.db3", backup_path) if (me & BACKUP_POLL_EVENTS) else False
        if (me & BACKUP_POLL_EVENTS) and backup_ok:
            if self.backup_poller is None:
                self.backup_poller = BackupDBPoller(
                    db_path=backup_path,
                    cursor_dir=cdir,
                    poll_interval=interval,
                    monitor_events=self.config.monitor_events,
                )
            else:
                self.backup_poller.update_config(
                    monitor_events=self.config.monitor_events,
                    poll_interval=interval,
                    db_path=backup_path,
                )
        else:
            if self.backup_poller is not None:
                self.backup_poller.stop()
                self.backup_poller = None

        trim_media_path = getattr(self.config, "trim_media_db_path", "") or ""
        trim_media_ok = self._probe_db_readable("影视库 trimmedia.db", trim_media_path) if (me & TRIMMEDIA_POLL_EVENTS) else False
        if (me & TRIMMEDIA_POLL_EVENTS) and trim_media_ok:
            if self.media_db_poller is None:
                self.media_db_poller = MediaDBPoller(
                    db_path=trim_media_path,
                    cursor_dir=cdir,
                    poll_interval=interval,
                    monitor_events=self.config.monitor_events,
                )
            else:
                self.media_db_poller.update_config(
                    monitor_events=self.config.monitor_events,
                    poll_interval=interval,
                    db_path=trim_media_path,
                )
        else:
            if self.media_db_poller is not None:
                self.media_db_poller.stop()
                self.media_db_poller = None

        trim_activity_path = getattr(self.config, "trim_activity_db_path", "") or ""
        trim_activity_ok = self._probe_db_readable("影视库 trimactivity.db", trim_activity_path) if (me & TRIMACTIVITY_POLL_EVENTS) else False
        if (me & TRIMACTIVITY_POLL_EVENTS) and trim_activity_ok:
            if self.trim_activity_poller is None:
                self.trim_activity_poller = TrimActivityPoller(
                    db_path=trim_activity_path,
                    cursor_dir=cdir,
                    app_name_patterns=getattr(self.config, "media_lib_app_name_patterns", []),
                    poll_interval=interval,
                    monitor_events=self.config.monitor_events,
                )
            else:
                self.trim_activity_poller.update_config(
                    monitor_events=self.config.monitor_events,
                    poll_interval=interval,
                    db_path=trim_activity_path,
                    app_name_patterns=getattr(self.config, "media_lib_app_name_patterns", []),
                )
        else:
            if self.trim_activity_poller is not None:
                self.trim_activity_poller.stop()
                self.trim_activity_poller = None

        photo_path = getattr(self.config, "photo_db_path", "") or ""
        photo_ok = self._probe_db_readable("相册库 photo.db", photo_path) if (me & PHOTO_POLL_EVENTS) else False
        if (me & PHOTO_POLL_EVENTS) and photo_ok:
            if self.photo_db_poller is None:
                self.photo_db_poller = PhotoDBPoller(
                    db_path=photo_path,
                    cursor_dir=cdir,
                    poll_interval=interval,
                    monitor_events=self.config.monitor_events,
                )
            else:
                self.photo_db_poller.update_config(
                    monitor_events=self.config.monitor_events,
                    poll_interval=interval,
                    db_path=photo_path,
                )
        else:
            if self.photo_db_poller is not None:
                self.photo_db_poller.stop()
                self.photo_db_poller = None

        scheduler_path = getattr(self.config, "scheduler_db_path", "") or ""
        scheduler_ok = self._probe_db_readable("任务计划库 scheduler.db", scheduler_path) if (me & SCHEDULER_POLL_EVENTS) else False
        if (me & SCHEDULER_POLL_EVENTS) and scheduler_ok:
            if self.scheduler_db_poller is None:
                self.scheduler_db_poller = SchedulerDBPoller(
                    db_path=scheduler_path,
                    cursor_dir=cdir,
                    poll_interval=interval,
                    monitor_events=self.config.monitor_events,
                )
            else:
                self.scheduler_db_poller.update_config(
                    monitor_events=self.config.monitor_events,
                    poll_interval=interval,
                    db_path=scheduler_path,
                )
        else:
            if self.scheduler_db_poller is not None:
                self.scheduler_db_poller.stop()
                self.scheduler_db_poller = None

    def _register_db_event_handlers(self) -> None:
        """为 logger / 备份 / 影视库 / 相册 SQLite 轮询器注册 monitor_events 中的处理器。"""
        if not self.log_poller or not self.event_processor or not self.config:
            return
        self.log_poller.clear_handlers()
        if self.backup_poller:
            self.backup_poller.clear_handlers()
        if self.media_db_poller:
            self.media_db_poller.clear_handlers()
        if self.trim_activity_poller:
            self.trim_activity_poller.clear_handlers()
        if self.photo_db_poller:
            self.photo_db_poller.clear_handlers()
        if self.scheduler_db_poller:
            self.scheduler_db_poller.clear_handlers()
        trim_ev = {"TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS"}
        act_ev = {"MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT"}
        photo_ev = set(PHOTO_POLL_EVENTS)
        scheduler_ev = set(SCHEDULER_POLL_EVENTS)
        for event_type in self.config.monitor_events:
            handler = self.event_processor.get_handler(event_type)
            if not handler:
                print(f"✗ 未知事件类型: {event_type}")
                continue
            # 登录/退出保留 logger 兜底，避免 trimactivity 的 app_name 过滤导致漏报。
            self.log_poller.add_handler(event_type, handler)
            if self.backup_poller and event_type in (BACKUP_SUCCESS_EVENT, BACKUP_FAILED_EVENT):
                self.backup_poller.add_handler(event_type, handler)
            if self.media_db_poller and event_type in trim_ev:
                self.media_db_poller.add_handler(event_type, handler)
            if self.trim_activity_poller and event_type in act_ev:
                self.trim_activity_poller.add_handler(event_type, handler)
            if self.photo_db_poller and event_type in photo_ev:
                self.photo_db_poller.add_handler(event_type, handler)
            if self.scheduler_db_poller and event_type in scheduler_ev:
                self.scheduler_db_poller.add_handler(event_type, handler)
            print(f"✓ 注册事件处理器: {event_type}")

    def reload_config(self) -> None:
        """保存配置后热加载：从配置文件重新加载并更新通知器与轮询器，无需重启容器。"""
        from web.ui_app import CONFIG_FILE
        if not self.config:
            return
        ok = self.config.reload_from_file(CONFIG_FILE)
        if not ok:
            return
        has_webhook = any([
            self.config.wechat_webhook_url,
            self.config.dingtalk_webhook_url,
            self.config.feishu_webhook_url,
            self.config.bark_url,
            self.config.pushplus_params,
            getattr(self.config, "magic_push_params", "") or "",
            getattr(self.config, "smtp_params", "") or "",
        ])
        if self.notifier is None and has_webhook:
            print("配置已保存并热加载：检测到新配置的推送渠道，正在启动监控...")
            self.notifier = UnifiedNotifier(self.config)
            self.event_processor = EventProcessor(self.notifier, self.config)
            self.log_poller = DBLogPoller(
                db_path=self.config.logger_db_path,
                cursor_dir=self.config.cursor_dir,
                poll_interval=self.config.logger_poll_interval,
                monitor_events=self.config.monitor_events,
                media_lib_logger_enabled=getattr(self.config, "media_lib_logger_enabled", False),
                media_lib_service_patterns=getattr(self.config, "media_lib_service_patterns", []),
            )
            if getattr(self.config, "poll_batch_summary_enabled", False):
                self.log_poller.set_batch_handler(self._dispatch_batch_events)
            else:
                self.log_poller.set_batch_handler(None)
            self._sync_optional_pollers()
            self._register_db_event_handlers()
            if self.log_poller:
                self.log_poller.start()
            if self.backup_poller:
                self.backup_poller.start()
            if self.media_db_poller:
                self.media_db_poller.start()
            if self.trim_activity_poller:
                self.trim_activity_poller.start()
            if self.photo_db_poller:
                self.photo_db_poller.start()
            if self.scheduler_db_poller:
                self.scheduler_db_poller.start()
            if self.logger:
                self.logger.info("热加载完成：监控已启动")
        elif self.notifier is not None:
            self.notifier.reload_config()
            if self.log_poller is not None:
                self.log_poller.update_config(
                    monitor_events=self.config.monitor_events,
                    poll_interval=self.config.logger_poll_interval,
                    db_path=self.config.logger_db_path,
                    media_lib_logger_enabled=getattr(self.config, "media_lib_logger_enabled", False),
                    media_lib_service_patterns=getattr(self.config, "media_lib_service_patterns", []),
                )
                if getattr(self.config, "poll_batch_summary_enabled", False):
                    self.log_poller.set_batch_handler(self._dispatch_batch_events)
                else:
                    self.log_poller.set_batch_handler(None)
            self._sync_optional_pollers()
            if self.log_poller is not None:
                self._register_db_event_handlers()
                if self.backup_poller:
                    self.backup_poller.start()
                if self.media_db_poller:
                    self.media_db_poller.start()
                if self.trim_activity_poller:
                    self.trim_activity_poller.start()
                if self.photo_db_poller:
                    self.photo_db_poller.start()
                if self.scheduler_db_poller:
                    self.scheduler_db_poller.start()
            if self.logger:
                self.logger.info("热加载完成：监控配置已更新")
    
    def _signal_handler(self, signum, frame):
        """信号处理器"""
        print(f"\n接收到信号 {signum}，准备关闭应用...")
        self.running = False
        # 不立即关闭，让应用正常关闭流程
    
    def run(self):
        """运行应用"""
        try:
            if not self.initialize():
                # 发送启动失败通知
                if self.notifier:
                    self.notifier.send_system_notification(
                        'APP_ERROR',
                        '应用初始化失败: 未知错误',
                        {'hostname': socket.gethostname(), 'version': '2.2.0'}
                    )
                sys.exit(1)
            
            self.running = True

            try:
                ui_thread = start_ui_server_in_background(on_config_saved=self.reload_config)
                print(f"配置 UI 已启动，线程: {ui_thread.name}")
            except Exception as e:
                print(f"配置 UI 启动失败: {e}")
            if not self.notifier:
                # 未配置推送渠道：不轮询数据库、不推送消息，仅提示用户去 Web 配置
                print("")
                print("  >>> 请访问 Web 配置页面完成推送渠道配置 （保存后自动生效，无需重启）  <<<")
                print("")
                pass
            else:
                # 已配置推送渠道：正常启动监控与推送
                self._start_notification_health_monitor()
                self.notifier.send_system_notification(
                    'APP_START',
                    '飞牛NAS日志监控系统已启动，开始监控系统事件',
                    {'hostname': socket.gethostname(), 'version': '2.2.0'}
                )
                if self.log_poller:
                    print("启动数据库日志轮询器...")
                    self.log_poller.start()
                else:
                    print("无法启动数据库日志轮询器")
                if self.backup_poller:
                    print("启动备份数据库轮询器...")
                    self.backup_poller.start()
                else:
                    print("未勾选备份任务事件，跳过备份库轮询")
                if self.media_db_poller:
                    print("启动影视库 trimmedia 轮询器...")
                    self.media_db_poller.start()
                else:
                    print("未勾选影视库入库/刮削事件，跳过 trimmedia.db 轮询")
                if self.trim_activity_poller:
                    print("启动影视库 trimactivity 轮询器...")
                    self.trim_activity_poller.start()
                else:
                    print("未勾选影视库登录/登出事件，跳过 trimactivity.db 轮询")
                if self.photo_db_poller:
                    print("启动相册 photo.db 轮询器...")
                    self.photo_db_poller.start()
                else:
                    print("未勾选相册相关事件，跳过 photo.db 轮询")
                if self.scheduler_db_poller:
                    print("启动任务计划 scheduler.db 轮询器...")
                    self.scheduler_db_poller.start()
                else:
                    print("未勾选任务计划事件，跳过 scheduler.db 轮询")
            
            # 设置信号处理
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

            # 保持主线程运行，直到收到 SIGINT/SIGTERM 将 self.running 置为 False
            loop_count = 0
            while self.running:
                loop_count += 1
                if loop_count % 60 == 0 and self.notifier:
                    try:
                        self.notifier.flush_dnd_buffer_if_needed()
                    except Exception as e:
                        if self.logger:
                            self.logger.warning("勿扰汇总检查异常: %s", e)
                time.sleep(1)

        except KeyboardInterrupt:
            print("\n接收到中断信号...")
        except Exception as e:
            print(f"运行时错误: {e}")
            traceback.print_exc()
        finally:
            self.shutdown()

    def _start_notification_health_monitor(self):
        """启动通知发送健康监控"""
        if not self.notifier or not self.config:
            return
        if not self.config.notification_restart_enabled:
            return

        self.notification_health_thread = threading.Thread(
            target=self._notification_health_loop,
            name="NotificationHealthMonitor",
            daemon=True
        )
        self.notification_health_thread.start()

    def _notification_health_loop(self):
        """定期检查通知发送健康状态"""
        check_interval = 60
        while self.running:
            try:
                if not self.notifier or not self.config:
                    time.sleep(check_interval)
                    continue
                health = self.notifier.get_delivery_health()
                active_platforms = health.get('active_platforms', {})
                if not any(active_platforms.values()):
                    time.sleep(check_interval)
                    continue

                last_attempt = health.get('last_attempt_time')
                if last_attempt is None:
                    time.sleep(check_interval)
                    continue

                consecutive_failures = health.get('consecutive_failures', 0)
                first_failure_time = health.get('first_failure_time')

                if consecutive_failures >= self.config.notification_restart_consecutive_failures and first_failure_time:
                    failure_duration = time.time() - first_failure_time
                    if failure_duration >= self.config.notification_restart_window:
                        if self._should_throttle_notification_restart():
                            time.sleep(check_interval)
                            continue

                        reason = (
                            f"通知连续失败 {consecutive_failures} 次，持续 {failure_duration:.0f} 秒"
                        )
                        self._trigger_app_restart(reason)
                        return
            except Exception as e:
                if self.logger:
                    self.logger.error(f"通知健康监控出错: {e}", exc_info=True)
            time.sleep(check_interval)

    def _should_throttle_notification_restart(self) -> bool:
        """防止通知故障导致频繁重启"""
        if not self.config:
            return False
        cooldown = self.config.notification_restart_cooldown
        if cooldown <= 0:
            return False

        marker = Path("/tmp/notification_restart.lock")
        now = time.time()
        try:
            if marker.exists():
                last_ts = float(marker.read_text().strip() or "0")
                if now - last_ts < cooldown:
                    if self.logger:
                        self.logger.warning(
                            f"通知重启冷却中，距离上次 {now - last_ts:.0f} 秒"
                        )
                    return True
            marker.write_text(str(now))
        except Exception as e:
            if self.logger:
                self.logger.error(f"写入通知重启标记失败: {e}")
        return False

    def _trigger_app_restart(self, reason: str):
        """触发应用重启（依赖容器/守护进程策略）"""
        if self.logger:
            self.logger.critical(f"触发应用重启，原因: {reason}")
        else:
            print(f"触发应用重启，原因: {reason}")

        try:
            restart_log = Path("/tmp/restart_reason.log")
            with open(restart_log, "a") as f:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"{timestamp} - {reason}\n")
        except Exception:
            pass

        try:
            if self.notifier:
                self.notifier.send_system_notification(
                    'APP_ERROR',
                    f'触发自动重启: {reason}',
                    {'hostname': socket.gethostname(), 'version': '2.2.0'}
                )
        except Exception:
            pass

        time.sleep(2)
        os._exit(1)
    
    def shutdown(self):
        """关闭应用"""
        print("\n正在关闭应用...")

        # 发送停止通知
        if self.notifier:
            self.notifier.send_system_notification(
                'APP_STOP',
                '飞牛NAS日志监控系统已停止，监控服务暂停',
                {'hostname': socket.gethostname(), 'version': '2.2.0'}
            )

        # 停止数据库轮询器
        if self.log_poller:
            self.log_poller.stop()
        if self.backup_poller:
            self.backup_poller.stop()
        if self.media_db_poller:
            self.media_db_poller.stop()
        if self.trim_activity_poller:
            self.trim_activity_poller.stop()
        if self.photo_db_poller:
            self.photo_db_poller.stop()
        if self.scheduler_db_poller:
            self.scheduler_db_poller.stop()

        # 停止运行日志清理线程
        cleanup_flag = getattr(self.logger, 'cleanup_stop_flag', None) if self.logger else None
        if cleanup_flag is not None:
            print("正在停止运行日志清理线程...")
            cleanup_flag.set()

        # 停止原始推送日志清理线程
        if self.event_processor and hasattr(self.event_processor, 'log_storage'):
            print("正在停止原始推送日志清理线程...")
            self.event_processor.log_storage.stop_cleanup_thread()

        # 关闭通知器
        if self.notifier:
            stats = self.notifier.get_stats()
            print("\n运行统计:")
            print(f"  发送请求: {stats.get('request_count', 0)}")
            print(f"  成功通知: {stats.get('success_count', 0)}")
            print(f"  失败通知: {stats.get('error_count', 0)}")

            # success_rate 在连接池中已经是格式化的字符串（例如 "0.0%"）
            success_rate = stats.get('success_rate', '0.0%')
            print(f"  成功率: {success_rate}")

            self.notifier.close()

        print(f"应用已关闭 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    """主函数"""
    app = Application()
    app.run()

if __name__ == "__main__":
    main()
