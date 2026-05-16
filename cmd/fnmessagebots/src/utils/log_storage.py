"""
日志存储模块
使用.log文件存储需要推送的原始系统日志，便于后期问题分析
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List
from dataclasses import dataclass


@dataclass
class StoredLogEntry:
    """存储的日志条目"""
    event_type: str    # 事件类型
    timestamp: str     # 事件时间戳
    raw_log: str       # 原始日志内容
    processed_data: Dict[str, Any]  # 处理后的事件数据
    notification_sent: bool  # 是否已发送通知
    stored_at: str     # 存储时间
    source: str        # 日志来源（db 等）


class LogStorage:
    """日志存储管理器 - 使用.log文件存储"""

    def __init__(self, storage_dir: str = "./logs", days_to_keep: int = 30, enable_auto_cleanup: bool = True):
        """
        初始化日志存储

        Args:
            storage_dir: 存储目录路径
            days_to_keep: 保留日志的天数，默认30天
            enable_auto_cleanup: 是否启用自动清理，默认True
        """
        # 统一使用项目根目录下的data/logs目录
        if storage_dir == "./logs":
            # 获取项目根目录 (__file__ 在 src/utils/log_storage.py 中)
            project_root = Path(__file__).parent.parent.parent
            self.storage_dir = project_root / "data" / "logs"
        else:
            self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # 设置日志
        self.logger = logging.getLogger(__name__)

        # 清理配置
        self.days_to_keep = days_to_keep
        self.cleanup_stop_flag = threading.Event()
        self.cleanup_thread = None

        # 启动自动清理线程
        if enable_auto_cleanup:
            self._start_cleanup_thread()

        self.logger.info(f"日志存储初始化完成，存储目录: {self.storage_dir}, 保留天数: {days_to_keep}")

    def _start_cleanup_thread(self):
        """启动日志清理线程"""
        def cleanup_loop():
            """清理循环"""
            # 启动后立即执行一次清理
            try:
                self.logger.info("执行初始日志清理...")
                deleted = self.cleanup_old_logs(self.days_to_keep)
                if deleted > 0:
                    self.logger.info(f"初始清理完成，删除了 {deleted} 个旧日志文件")
            except Exception as e:
                self.logger.error(f"初始日志清理失败: {e}")

            # 每24小时执行一次清理
            while not self.cleanup_stop_flag.is_set():
                try:
                    # 使用短间隔检查停止标志，避免关闭时等待太久
                    for _ in range(24 * 3600):  # 24小时 = 86400秒
                        if self.cleanup_stop_flag.wait(1):  # 每秒检查一次
                            self.logger.info("日志清理线程收到停止信号")
                            return

                    # 执行清理
                    self.logger.info("开始定期日志清理...")
                    deleted = self.cleanup_old_logs(self.days_to_keep)
                    if deleted > 0:
                        self.logger.info(f"定期清理完成，删除了 {deleted} 个旧日志文件")
                    else:
                        self.logger.debug("定期清理完成，没有需要删除的文件")

                except Exception as e:
                    self.logger.error(f"日志清理线程出错: {e}")
                    # 出错后等待1小时再重试
                    for _ in range(3600):
                        if self.cleanup_stop_flag.wait(1):
                            return

        # 启动后台清理线程
        self.cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True, name="LogStorageCleanup")
        self.cleanup_thread.start()
        self.logger.info(f"日志清理线程已启动，每24小时清理一次，保留 {self.days_to_keep} 天内的数据")

    def stop_cleanup_thread(self):
        """停止日志清理线程"""
        if self.cleanup_thread and self.cleanup_thread.is_alive():
            self.logger.info("正在停止日志清理线程...")
            self.cleanup_stop_flag.set()
            self.cleanup_thread.join(timeout=5)  # 最多等待5秒
            if self.cleanup_thread.is_alive():
                self.logger.warning("日志清理线程未能在5秒内停止")
            else:
                self.logger.info("日志清理线程已停止")

    def store_log(self, event_type: str, raw_log: str, processed_data: Dict[str, Any], 
                  source: str = "unknown") -> bool:
        """
        存储日志条目到.log文件
        
        Args:
            event_type: 事件类型
            raw_log: 原始日志内容
            processed_data: 处理后的事件数据
            source: 日志来源
            
        Returns:
            存储是否成功
        """
        try:
            # 当前时间
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 生成日志文件名（按事件类型和日期分类）
            date_str = datetime.now().strftime('%Y-%m-%d')
            log_filename = f"{event_type}_{date_str}.log"
            log_filepath = self.storage_dir / log_filename
            
            # 准备日志条目
            log_entry = {
                'event_type': event_type,
                'timestamp': processed_data.get('timestamp', current_time),
                'raw_log': raw_log,
                'processed_data': processed_data,
                'notification_sent': True,
                'stored_at': current_time,
                'source': source
            }
            
            # 追加写入文件
            with open(log_filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            
            self.logger.info(f"日志已存储 - 事件类型: {event_type}, 文件: {log_filename}")
            return True
            
        except Exception as e:
            self.logger.error(f"存储日志失败: {e}")
            return False
    
    def get_logs_by_event_type(self, event_type: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        根据事件类型获取日志
        
        Args:
            event_type: 事件类型
            limit: 返回记录数量限制
            
        Returns:
            日志条目列表
        """
        try:
            logs = []
            
            # 查找匹配的.log文件
            date_str = datetime.now().strftime('%Y-%m-%d')
            log_filename = f"{event_type}_{date_str}.log"
            log_filepath = self.storage_dir / log_filename
            
            if log_filepath.exists():
                with open(log_filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                log_entry = json.loads(line)
                                logs.append(log_entry)
                                if len(logs) >= limit:
                                    break
                            except json.JSONDecodeError:
                                continue
            
            # 按存储时间倒序排列
            logs.sort(key=lambda x: x.get('stored_at', ''), reverse=True)
            return logs[:limit]
            
        except Exception as e:
            self.logger.error(f"查询日志失败: {e}")
            return []
    
    def get_logs_by_date_range(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        根据日期范围获取日志
        
        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            日志条目列表
        """
        try:
            logs = []
            
            # 遍历指定日期范围内的所有.log文件
            current_date = datetime.strptime(start_date, '%Y-%m-%d')
            end_datetime = datetime.strptime(end_date, '%Y-%m-%d')
            
            while current_date <= end_datetime:
                date_str = current_date.strftime('%Y-%m-%d')
                
                # 查找该日期的所有日志文件
                for log_file in self.storage_dir.glob(f"*_{date_str}.log"):
                    if log_file.exists():
                        with open(log_file, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        log_entry = json.loads(line)
                                        logs.append(log_entry)
                                    except json.JSONDecodeError:
                                        continue
                
                current_date += timedelta(days=1)
            
            # 按存储时间倒序排列
            logs.sort(key=lambda x: x.get('stored_at', ''), reverse=True)
            return logs
            
        except Exception as e:
            self.logger.error(f"按日期查询日志失败: {e}")
            return []
    
    def get_recent_logs(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        获取最近几小时的日志
        
        Args:
            hours: 小时数
            
        Returns:
            日志条目列表
        """
        try:
            logs = []
            cutoff_time = datetime.now() - timedelta(hours=hours)
            
            # 遍历最近几天的日志文件
            for i in range((hours // 24) + 2):  # 多检查几天确保覆盖所有可能
                check_date = datetime.now() - timedelta(days=i)
                date_str = check_date.strftime('%Y-%m-%d')
                
                # 查找该日期的所有日志文件
                for log_file in self.storage_dir.glob(f"*_{date_str}.log"):
                    if log_file.exists():
                        with open(log_file, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        log_entry = json.loads(line)
                                        stored_time = datetime.fromisoformat(log_entry.get('stored_at', '').replace(' ', 'T'))
                                        if stored_time >= cutoff_time:
                                            logs.append(log_entry)
                                    except (json.JSONDecodeError, ValueError):
                                        continue
            
            # 按存储时间倒序排列
            logs.sort(key=lambda x: x.get('stored_at', ''), reverse=True)
            return logs
            
        except Exception as e:
            self.logger.error(f"获取近期日志失败: {e}")
            return []
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取存储统计信息
        
        Returns:
            统计信息字典
        """
        try:
            total_count = 0
            event_stats = {}
            recent_week_count = 0
            recent_day_count = 0
            
            week_ago = datetime.now() - timedelta(days=7)
            day_ago = datetime.now() - timedelta(days=1)
            
            # 遍历所有日志文件（文件名格式: EventType_YYYY-MM-DD.log，事件类型可含下划线如 APP_CRASH）
            for log_file in self.storage_dir.glob("*.log"):
                if log_file.exists():
                    stem = log_file.stem
                    event_type = "unknown"
                    if "_" in stem:
                        # 最后一段为日期 YYYY-MM-DD 则前面为事件类型
                        parts = stem.rsplit("_", 1)
                        if len(parts) == 2 and len(parts[1]) == 10 and parts[1][4] == "-" and parts[1][7] == "-":
                            event_type = parts[0]
                        else:
                            event_type = stem.split("_")[0]
                    elif stem:
                        event_type = stem
                    
                    with open(log_file, 'r', encoding='utf-8') as f:
                        file_count = 0
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    log_entry = json.loads(line)
                                    file_count += 1
                                    total_count += 1
                                    
                                    # 统计事件类型分布
                                    if event_type in event_stats:
                                        event_stats[event_type] += 1
                                    else:
                                        event_stats[event_type] = 1
                                    
                                    # 统计近期记录
                                    stored_time = datetime.fromisoformat(log_entry.get('stored_at', '').replace(' ', 'T'))
                                    if stored_time >= week_ago:
                                        recent_week_count += 1
                                    if stored_time >= day_ago:
                                        recent_day_count += 1
                                        
                                except (json.JSONDecodeError, ValueError):
                                    continue
            
            return {
                'total_records': total_count,
                'records_last_week': recent_week_count,
                'records_last_day': recent_day_count,
                'event_distribution': event_stats,
                'storage_path': str(self.storage_dir)
            }
            
        except Exception as e:
            self.logger.error(f"获取统计信息失败: {e}")
            return {}
    
    def export_logs(self, output_path: str, event_type: str = None, 
                   start_date: str = None, end_date: str = None) -> bool:
        """
        导出日志到文件
        
        Args:
            output_path: 输出文件路径
            event_type: 事件类型过滤（可选）
            start_date: 开始日期（可选）
            end_date: 结束日期（可选）
            
        Returns:
            导出是否成功
        """
        try:
            export_data = []
            
            # 收集符合条件的日志
            if event_type and start_date and end_date:
                # 特定事件类型和日期范围
                logs = self.get_logs_by_date_range(start_date, end_date)
                export_data = [log for log in logs if log.get('event_type') == event_type]
            elif event_type:
                # 特定事件类型
                export_data = self.get_logs_by_event_type(event_type)
            elif start_date and end_date:
                # 特定日期范围
                export_data = self.get_logs_by_date_range(start_date, end_date)
            else:
                # 所有日志
                for log_file in self.storage_dir.glob("*.log"):
                    if log_file.exists():
                        with open(log_file, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        log_entry = json.loads(line)
                                        export_data.append(log_entry)
                                    except json.JSONDecodeError:
                                        continue
            
            # 写入文件
            output_file = Path(output_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
            
            self.logger.info(f"日志导出完成: {output_path}, 共 {len(export_data)} 条记录")
            return True
            
        except Exception as e:
            self.logger.error(f"导出日志失败: {e}")
            return False
    
    def cleanup_old_logs(self, days_to_keep: int = 30) -> int:
        """
        清理旧日志文件
        
        Args:
            days_to_keep: 保留天数
            
        Returns:
            删除的文件数
        """
        try:
            deleted_count = 0
            cutoff_date = datetime.now() - timedelta(days=days_to_keep)
            
            # 遍历所有日志文件
            for log_file in self.storage_dir.glob("*.log"):
                if log_file.exists():
                    # 从文件名提取日期
                    try:
                        date_part = log_file.stem.split('_')[-1]  # 获取日期部分
                        file_date = datetime.strptime(date_part, '%Y-%m-%d')
                        
                        if file_date < cutoff_date:
                            log_file.unlink()  # 删除文件
                            deleted_count += 1
                            self.logger.info(f"删除旧日志文件: {log_file.name}")
                    except (ValueError, IndexError):
                        # 无法解析日期的文件跳过
                        continue
            
            self.logger.info(f"清理旧日志完成，删除 {deleted_count} 个文件，保留 {days_to_keep} 天内的数据")
            return deleted_count
            
        except Exception as e:
            self.logger.error(f"清理旧日志失败: {e}")
            return 0
