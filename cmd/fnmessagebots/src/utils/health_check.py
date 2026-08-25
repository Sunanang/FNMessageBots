"""
健康检查模块
"""

import sys
import time
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any

try:
    import psutil
except ImportError:
    psutil = None

from src.config import Config

class HealthChecker:
    """健康检查器"""
    
    def __init__(self, config):
        """
        初始化健康检查器
        
        Args:
            config: 配置对象
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # 检查间隔
        self.check_interval = 60
        self.last_check = 0
        
    def check_all(self) -> Dict[str, bool]:
        """
        执行所有健康检查
        
        Returns:
            检查结果字典
        """
        current_time = time.time()
        if current_time - self.last_check < self.check_interval:
            return {'skipped': True}
        
        self.last_check = current_time
        
        results = {
            'cursor_dir': self.check_cursor_dir(),
            'log_directory': self.check_log_directory(),
            'webhook_url': self.check_webhook_url(),
            'python_process': self.check_python_process()
        }
        
        # 记录检查结果
        all_healthy = all(results.values())
        status = "健康" if all_healthy else "异常"
        
        self.logger.info(f"健康检查: {status}")
        for check_name, result in results.items():
            self.logger.debug(f"  {check_name}: {'通过' if result else '失败'}")
        
        return results
    
    def check_cursor_dir(self) -> bool:
        """检查游标目录（数据库轮询游标写入）是否可写"""
        cursor_dir = Path(self.config.cursor_dir)
        try:
            cursor_dir.mkdir(parents=True, exist_ok=True)
            test_file = cursor_dir / ".healthcheck_cursor"
            test_file.write_text("ok")
            test_file.unlink(missing_ok=True)
            return True
        except Exception as e:
            self.logger.error(f"游标目录检查失败: {e}")
            return False
    
    def check_log_directory(self) -> bool:
        """检查日志目录"""
        # 统一使用项目根目录下的data/logs目录
        # __file__ 在 src/utils/health_check.py 中，所以需要向上三级到达项目根目录
        project_root = Path(__file__).parent.parent.parent
        log_dir = project_root / "data" / "logs"
        
        if not log_dir.exists():
            try:
                log_dir.mkdir(parents=True)
                return True
            except Exception:
                return False
        
        # 检查是否可写
        test_file = log_dir / ".healthcheck"
        try:
            test_file.write_text("test")
            test_file.unlink()
            return True
        except Exception:
            return False
    
    def check_webhook_url(self) -> bool:
        """检查Webhook URL配置"""
        return bool(self.config.wechat_webhook_url) and \
               self.config.wechat_webhook_url.startswith('http')
    
    def check_python_process(self) -> bool:
        """检查Python进程"""
        try:
            if psutil is None:
                return True
            current_pid = psutil.Process().pid
            
            # 检查是否有其他监控进程在运行
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info['cmdline']
                    if cmdline and 'main.py' in ' '.join(cmdline):
                        if proc.info['pid'] != current_pid:
                            self.logger.warning("发现其他监控进程在运行")
                            return False
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            return True
        except ImportError:
            # 如果没有psutil，跳过这个检查
            return True
    
    def get_status_report(self) -> Dict[str, Any]:
        """获取状态报告"""
        check_results = self.check_all()
        
        return {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'checks': check_results,
            'all_healthy': all(check_results.get(k, False) for k in [
                'cursor_dir', 'log_directory', 'webhook_url', 'python_process'
            ]),
            'config': {
                'monitor_events': len(self.config.monitor_events),
                'log_level': self.config.log_level,
                'dedup_window': self.config.dedup_window
            }
        }

def perform_health_check(config_path: str = None) -> int:
    """
    执行健康检查（命令行入口）
    
    Args:
        config_path: 配置文件路径
        
    Returns:
        退出码（0=健康，1=异常）
    """
    try:
        config = Config()
        checker = HealthChecker(config)
        report = checker.get_status_report()
        
        print("飞牛NAS日志监控系统 - 健康检查报告")
        print("=" * 50)
        print(f"检查时间: {report['timestamp']}")
        print(f"整体状态: {'✅ 健康' if report['all_healthy'] else '❌ 异常'}")
        print()
        
        print("详细检查结果:")
        for check_name, result in report['checks'].items():
            status = "✅ 通过" if result else "❌ 失败"
            print(f"  {check_name}: {status}")
        
        print()
        print("配置信息:")
        print(f"  监控事件数: {report['config']['monitor_events']}")
        print(f"  日志级别: {report['config']['log_level']}")
        print(f"  去重窗口: {report['config']['dedup_window']}秒")
        
        return 0 if report['all_healthy'] else 1
        
    except Exception as e:
        print(f"健康检查失败: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(perform_health_check())
