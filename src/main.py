
import sys
import signal
import socket
import os
import traceback
from datetime import datetime
import time
from pathlib import Path
import threading

# 添加src目录到Python路径，解决模块导入问题
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from utils.logger import setup_logging
from utils.push_stats import init as init_push_stats
from monitor.db_log_poller import DBLogPoller
from monitor.event_processor import EventProcessor
from notifier.unified_notifier import UnifiedNotifier
from web.ui_app import start_ui_server_in_background

class Application:
    """主应用程序"""
    
    def __init__(self):
        """初始化应用"""
        self.config = None
        self.notifier = None
        self.event_processor = None
        self.log_poller = None
        self.logger = None
        self.running = False
        self.notification_health_thread = None

        
    def _print_banner(self):
        """打印启动横幅"""
        banner = f"""
        启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        监控模式: 数据库轮询 (logger_data.db3 log 表)
        通知方式: 企业微信/钉钉/飞书机器人/Bark/PushPlus/魔法推送

        """
        print(banner)
    
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
            )
            if getattr(self.config, "poll_batch_summary_enabled", False):
                self.log_poller.set_batch_handler(self.event_processor.process_batch_events)
                print("轮询汇总模式：已开启（同一轮查询内多事件合并为一条推送）")
            else:
                self.log_poller.set_batch_handler(None)
                print("轮询汇总模式：已关闭（逐条推送；事件密集时可能触发渠道限流）")
            print(f"数据库轮询器初始化完成（间隔: {self.config.logger_poll_interval}秒，数据库: {self.config.logger_db_path}）")
            
            print("开始注册事件处理器...")
            for event_type in self.config.monitor_events:
                handler = self.event_processor.get_handler(event_type)
                if handler:
                    self.log_poller.add_handler(event_type, handler)
                    print(f"✓ 注册事件处理器: {event_type}")
                else:
                    print(f"✗ 未知事件类型: {event_type}")
            
            print(f"\n初始化完成，开始监控...")
            return True
        except Exception as e:
            print(f"初始化失败: {e}")
            traceback.print_exc()
            return False

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
            )
            if getattr(self.config, "poll_batch_summary_enabled", False):
                self.log_poller.set_batch_handler(self.event_processor.process_batch_events)
            else:
                self.log_poller.set_batch_handler(None)
            for event_type in self.config.monitor_events:
                handler = self.event_processor.get_handler(event_type)
                if handler:
                    self.log_poller.add_handler(event_type, handler)
            if self.log_poller:
                self.log_poller.start()
            if self.logger:
                self.logger.info("热加载完成：监控已启动")
        elif self.notifier is not None:
            self.notifier.reload_config()
            if self.log_poller is not None:
                self.log_poller.update_config(
                    monitor_events=self.config.monitor_events,
                    poll_interval=self.config.logger_poll_interval,
                    db_path=self.config.logger_db_path,
                )
                if getattr(self.config, "poll_batch_summary_enabled", False):
                    self.log_poller.set_batch_handler(self.event_processor.process_batch_events)
                else:
                    self.log_poller.set_batch_handler(None)
                self.log_poller.clear_handlers()
                for event_type in self.config.monitor_events:
                    handler = self.event_processor.get_handler(event_type)
                    if handler:
                        self.log_poller.add_handler(event_type, handler)
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
                        {'hostname': socket.gethostname(), 'version': '2.1.0'}
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
                    {'hostname': socket.gethostname(), 'version': '2.1.0'}
                )
                if self.log_poller:
                    print("启动数据库日志轮询器...")
                    self.log_poller.start()
                else:
                    print("无法启动数据库轮询器")
            
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
                    {'hostname': socket.gethostname(), 'version': '2.1.0'}
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
                {'hostname': socket.gethostname(), 'version': '2.1.0'}
            )

        # 停止数据库轮询器
        if self.log_poller:
            self.log_poller.stop()

        # 停止运行日志清理线程
        if self.logger and hasattr(self.logger, 'cleanup_stop_flag'):
            print("正在停止运行日志清理线程...")
            self.logger.cleanup_stop_flag.set()

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
