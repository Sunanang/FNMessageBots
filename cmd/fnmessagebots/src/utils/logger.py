"""
日志配置模块
"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
import threading
import time

def setup_logging(config) -> logging.Logger:
    """
    设置日志配置
    
    Args:
        config: 配置对象
        
    Returns:
        根日志记录器
    """
    # 创建日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 获取根日志记录器
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, config.log_level))
    
    # 清除现有处理器
    logger.handlers.clear()
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(getattr(logging, config.log_level))
    logger.addHandler(console_handler)
    
    # 文件处理器
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"monitor_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(getattr(logging, config.log_level))
    logger.addHandler(file_handler)
    
    # 设置第三方库日志级别
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    # 启动日志清理线程（使用实际日志目录）
    cleanup_stop_flag = start_log_cleanup_thread(config, log_dir)

    # 将停止标志附加到 logger 对象，以便外部可以停止线程
    logger.cleanup_stop_flag = cleanup_stop_flag

    return logger

def cleanup_old_logs(log_dir: str, max_age_days: int = 7):
    """
    清理旧的日志文件
    
    Args:
        log_dir: 日志目录
        max_age_days: 保留日志的最大天数
    """
    log_path = Path(log_dir)
    if not log_path.exists():
        return
    
    cutoff_date = datetime.now() - timedelta(days=max_age_days)
    
    # 查找所有匹配的日志文件
    log_files = list(log_path.glob("monitor_*.log"))
    
    for log_file in log_files:
        try:
            # 从文件名提取日期 (monitor_YYYYMMDD.log)
            filename = log_file.name
            if filename.startswith("monitor_") and filename.endswith(".log"):
                date_str = filename.replace("monitor_", "").replace(".log", "")
                if len(date_str) == 8:  # YYYYMMDD
                    file_date = datetime.strptime(date_str, "%Y%m%d")
                    if file_date < cutoff_date:
                        log_file.unlink()  # 删除文件
                        print(f"已删除旧日志文件: {log_file}")
        except (ValueError, OSError) as e:
            print(f"处理日志文件时出错 {log_file}: {e}")

def start_log_cleanup_thread(config, log_dir: Path):
    """
    启动日志清理线程

    Args:
        config: 配置对象
        log_dir: 实际日志目录
    """
    stop_flag = threading.Event()

    def cleanup_loop():
        while not stop_flag.is_set():
            try:
                cleanup_old_logs(str(log_dir), config.max_log_age)
                # 每24小时运行一次清理，使用短间隔检查停止标志
                for _ in range(24 * 3600):
                    if stop_flag.wait(1):  # 每秒检查一次停止标志
                        return
            except Exception as e:
                print(f"日志清理线程出错: {e}")
                # 即使出错也继续运行
                for _ in range(3600):
                    if stop_flag.wait(1):  # 每秒检查一次停止标志
                        return

    # 启动后台清理线程
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    # 返回停止标志，以便外部可以停止线程
    return stop_flag

def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志记录器
    
    Args:
        name: 记录器名称
        
    Returns:
        日志记录器实例
    """
    return logging.getLogger(name)
