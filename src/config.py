"""
配置管理模块
"""

import copy
import os
import json
from typing import List, Dict, Any
from dataclasses import dataclass, field, fields
from pathlib import Path
from utils.value_parser import as_bool
from valid_event_ids import filter_monitor_events

# 推送标题前缀默认值；配置文件无 title_prefix 项时使用。仅当配置里显式存空字符串时才关闭前缀。
TITLE_PREFIX_DEFAULT = "飞牛NAS"


@dataclass
class Config:
    """应用配置"""
    
    # Webhook配置
    wechat_webhook_url: str = ""  # 企业微信Webhook URL
    dingtalk_webhook_url: str = ""  # 钉钉Webhook URL
    feishu_webhook_url: str = ""   # 飞书Webhook URL
    bark_url: str = ""  # Bark推送URL
    bark_icon: str = ""  # Bark 自定义图标 URL（留空使用默认图标）
    pushplus_params: str = ""  # PushPlus 推送参数（JSON 字符串，多个用 | 分隔）
    magic_push_params: str = ""  # 魔法推送（JSON：base_url、token、可选 title，多个用 | 分隔）
    smtp_params: str = ""  # SMTP 邮件参数（JSON：server、port、username、password、from、to；多个用 | 分隔）
    
    # 通知标题配置；默认「飞牛NAS」。仅在配置中显式写入空字符串时，推送标题不包含此前缀。
    title_prefix: str = field(default=TITLE_PREFIX_DEFAULT)
    # 极简推送：开启后所有渠道仅推送一行关键信息（默认关闭）
    minimal_push_enabled: bool = False

    # 监控配置
    monitor_events: List[str] = field(default_factory=lambda: [
        "LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout", "FoundDisk", "InsertDisk", "EjectDisk", "StorageBroken", "STORAGE_DEGRADED", "APP_CRASH",
        "APP_UPDATE_FAILED", "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
        "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",         "CPU_USAGE_ALARM",
        "CPU_USAGE_RESTORED",
        "MEMORY_USAGE_ALARM",
        "MEMORY_USAGE_RESTORED",
        "CPU_TEMPERATURE_ALARM", "UPS_ONBATT", "UPS_ONBATT_LOWBATT", "UPS_ONLINE",
        "UPS_ENABLE", "UPS_DISABLE",
        "DiskWakeup", "DiskSpindown",
        "SSH_INVALID_USER", "SSH_AUTH_FAILED",
        "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
        "DISK_IO_ERR",
        "BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS",
    ])
    
    # 日志配置
    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30  # 原始推送日志保留天数
    
    # 连接池配置
    http_pool_size: int = 10
    http_retry_count: int = 3
    http_timeout: int = 10
    dedup_window: int = 300
    
    # 系统路径配置（默认空：请在 config.json 或环境变量 LOGGER_DB_PATH / BACKUP_DB_PATH 等中配置）
    cursor_dir: str = "./data/cursor"  # 数据库轮询游标等
    logger_db_path: str = ""
    backup_db_path: str = ""
    # 影视库：logger 中按 serviceId/parameter 匹配；trimmedia / trimactivity 路径为空则不启用对应轮询
    media_lib_logger_enabled: bool = False
    media_lib_service_patterns: List[str] = field(default_factory=lambda: ["mediadb", "trimmedia"])
    media_lib_app_name_patterns: List[str] = field(default_factory=lambda: ["影视"])
    trim_media_db_path: str = ""
    trim_activity_db_path: str = ""
    photo_db_path: str = ""  # 相册 photo.db，空则不轮询相册事件
    scheduler_db_path: str = ""  # fn-scheduler scheduler.db，空则不轮询任务计划事件
    docker_socket_path: str = "/var/run/docker.sock"  # Docker Engine Unix socket；勾选 Docker 容器事件且文件可访问时监听
    logger_poll_interval: int = 5  # 秒，轮询间隔

    # 轮询汇总模式：开启后同一轮查询到的多条事件合并为一条通知；关闭则逐条推送（易触发渠道限流）
    poll_batch_summary_enabled: bool = False
    
    # 勿扰模式（开启后在该时段内不推送，结束后汇总为一条消息）
    dnd_enabled: bool = False
    dnd_start_time: str = "22:00"  # HH:MM，如 22:00
    dnd_end_time: str = "07:00"   # HH:MM，如 07:00（跨日则到次日该时间结束）

    # 高级配置
    max_log_age: int = 7  # 应用运行日志 monitor_*.log 保留天数
    notification_restart_enabled: bool = True
    notification_restart_consecutive_failures: int = 10
    notification_restart_window: int = 1800  # 30分钟
    notification_restart_cooldown: int = 3600  # 1小时
    

    
    def __post_init__(self):
        """初始化后处理"""
        # 记录哪些配置项是从环境变量设置的
        self._env_set_keys = set()
        # 首先从环境变量加载配置
        self._load_from_env()
        # 然后从配置文件加载，但仅当配置项未从环境变量设置时才覆盖
        self._load_from_file_skip_if_set()
        self.title_prefix = (self.title_prefix or "").strip()
        self._validate()
        self._ensure_directories()
    
    def _get_config_file_path(self) -> Path:
        """获取配置文件路径：Docker 下用 /app/config，本地用项目根目录 config。"""
        app_home = os.getenv("APP_HOME")
        if app_home:
            return Path(app_home) / "config" / "config.json"
        if Path("/app/config/config.json").exists():
            return Path("/app/config/config.json")
        # 从 src/config.py 向上到项目根
        candidate = Path(__file__).resolve().parent.parent / "config" / "config.json"
        if candidate.exists():
            return candidate
        return Path("/app/config/config.json")

    def _load_from_file_skip_if_set(self):
        """从配置文件加载（可选），但跳过已从环境变量设置的配置项"""
        config_file = self._get_config_file_path()
        if config_file.exists():
            try:
                with open(config_file, 'r') as f:
                    data = json.load(f)

                # 覆盖配置 - 仅当配置项未从环境变量设置时才使用配置文件的值
                for key, value in data.items():
                    if hasattr(self, key):
                        # 如果这个配置项已经从环境变量设置过，跳过
                        if key in self._env_set_keys:
                            continue

                        # 如果值是字符串且包含环境变量占位符，则进行替换
                        if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                            env_var_name = value[2:-1]  # 提取变量名
                            env_value = os.getenv(env_var_name, '')  # 获取环境变量值，不存在则为空字符串
                            setattr(self, key, env_value)
                        else:
                            setattr(self, key, value)
            except Exception as e:
                print(f"警告: 配置文件读取失败 - {e}")

    def reload_from_file(self, config_path: Path) -> bool:
        """从配置文件重新加载 Web UI 可修改的项（保存时热加载用）。返回是否成功。"""
        if not config_path.exists():
            return False
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False

        # 先备份全部 dataclass 字段，校验失败时回滚，避免热加载后处于未调用 _validate 的不一致状态
        _backup = {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}

        def env_skip(field: str) -> bool:
            return field in self._env_set_keys

        if not env_skip("monitor_events") and "monitor_events" in data and isinstance(data["monitor_events"], list):
            self.monitor_events = data["monitor_events"]
        if not env_skip("wechat_webhook_url") and "wechat_webhook_url" in data and isinstance(data["wechat_webhook_url"], str):
            self.wechat_webhook_url = data["wechat_webhook_url"]
        if not env_skip("dingtalk_webhook_url") and "dingtalk_webhook_url" in data and isinstance(data["dingtalk_webhook_url"], str):
            self.dingtalk_webhook_url = data["dingtalk_webhook_url"]
        if not env_skip("feishu_webhook_url") and "feishu_webhook_url" in data and isinstance(data["feishu_webhook_url"], str):
            self.feishu_webhook_url = data["feishu_webhook_url"]
        if not env_skip("bark_url") and "bark_url" in data and isinstance(data["bark_url"], str):
            self.bark_url = data["bark_url"]
        if not env_skip("bark_icon") and "bark_icon" in data and isinstance(data["bark_icon"], str):
            self.bark_icon = data["bark_icon"]
        if not env_skip("minimal_push_enabled") and "minimal_push_enabled" in data:
            self.minimal_push_enabled = as_bool(data["minimal_push_enabled"], False)
        if not env_skip("smtp_params") and "smtp_params" in data and isinstance(data["smtp_params"], str):
            self.smtp_params = data["smtp_params"]
        if "pushplus_params" in data and isinstance(data["pushplus_params"], str):
            self.pushplus_params = data["pushplus_params"]
        if "magic_push_params" in data and isinstance(data["magic_push_params"], str):
            self.magic_push_params = data["magic_push_params"]
        if "title_prefix" in data and isinstance(data["title_prefix"], str):
            self.title_prefix = (data["title_prefix"] or "").strip()
        if not env_skip("log_retention_days") and "log_retention_days" in data and data["log_retention_days"] is not None:
            try:
                self.log_retention_days = int(data["log_retention_days"])
            except (TypeError, ValueError):
                pass
        if not env_skip("logger_poll_interval") and "logger_poll_interval" in data and data["logger_poll_interval"] is not None:
            try:
                self.logger_poll_interval = int(data["logger_poll_interval"])
            except (TypeError, ValueError):
                pass
        if not env_skip("logger_db_path") and "logger_db_path" in data and isinstance(data["logger_db_path"], str):
            self.logger_db_path = data["logger_db_path"].strip()
        if not env_skip("backup_db_path") and "backup_db_path" in data and isinstance(data["backup_db_path"], str):
            self.backup_db_path = data["backup_db_path"].strip()
        if "dnd_enabled" in data:
            self.dnd_enabled = as_bool(data["dnd_enabled"], False)
        if "dnd_start_time" in data and isinstance(data["dnd_start_time"], str):
            self.dnd_start_time = data["dnd_start_time"].strip()
        if "dnd_end_time" in data and isinstance(data["dnd_end_time"], str):
            self.dnd_end_time = data["dnd_end_time"].strip()
        if "poll_batch_summary_enabled" in data:
            self.poll_batch_summary_enabled = as_bool(data["poll_batch_summary_enabled"], False)
        if not env_skip("media_lib_logger_enabled") and "media_lib_logger_enabled" in data:
            self.media_lib_logger_enabled = as_bool(data["media_lib_logger_enabled"], False)
        if not env_skip("media_lib_service_patterns") and "media_lib_service_patterns" in data and isinstance(data["media_lib_service_patterns"], list):
            self.media_lib_service_patterns = [str(x).strip() for x in data["media_lib_service_patterns"] if str(x).strip()]
        if not env_skip("media_lib_app_name_patterns") and "media_lib_app_name_patterns" in data and isinstance(data["media_lib_app_name_patterns"], list):
            self.media_lib_app_name_patterns = [str(x).strip() for x in data["media_lib_app_name_patterns"] if str(x).strip()]
        if not env_skip("trim_media_db_path") and "trim_media_db_path" in data and isinstance(data["trim_media_db_path"], str):
            self.trim_media_db_path = data["trim_media_db_path"].strip()
        if not env_skip("trim_activity_db_path") and "trim_activity_db_path" in data and isinstance(data["trim_activity_db_path"], str):
            self.trim_activity_db_path = data["trim_activity_db_path"].strip()
        if not env_skip("photo_db_path") and "photo_db_path" in data and isinstance(data["photo_db_path"], str):
            self.photo_db_path = data["photo_db_path"].strip()
        if not env_skip("scheduler_db_path") and "scheduler_db_path" in data and isinstance(data["scheduler_db_path"], str):
            self.scheduler_db_path = data["scheduler_db_path"].strip()
        if not env_skip("docker_socket_path") and "docker_socket_path" in data and isinstance(data["docker_socket_path"], str):
            self.docker_socket_path = (data["docker_socket_path"] or "").strip() or "/var/run/docker.sock"
        self.title_prefix = (self.title_prefix or "").strip()
        try:
            self._validate()
        except ValueError as e:
            for f in fields(self):
                setattr(self, f.name, _backup[f.name])
            print(f"警告: 热加载配置未通过校验，已保持热加载前内存中的配置: {e}")
            return False
        return True

    def _load_from_env(self):
        """从环境变量加载配置"""
        # 端口配置（保留此行以兼容旧版本，但不实际使用）
        # port = os.getenv('PORT')  # 未使用，保留会造成误导

        # Webhook URLs
        if wechat_webhook := os.getenv('WECHAT_WEBHOOK_URL'):
            self.wechat_webhook_url = wechat_webhook
            self._env_set_keys.add('wechat_webhook_url')
        elif webhook := os.getenv('WEBHOOK_URL'):  # 兼容旧的环境变量名
            self.wechat_webhook_url = webhook
            self._env_set_keys.add('wechat_webhook_url')

        if dingtalk_webhook := os.getenv('DINGTALK_WEBHOOK_URL'):
            self.dingtalk_webhook_url = dingtalk_webhook
            self._env_set_keys.add('dingtalk_webhook_url')

        if feishu_webhook := os.getenv('FEISHU_WEBHOOK_URL'):
            self.feishu_webhook_url = feishu_webhook
            self._env_set_keys.add('feishu_webhook_url')

        if bark_url := os.getenv('BARK_URL'):
            self.bark_url = bark_url
            self._env_set_keys.add('bark_url')
        if bark_icon := os.getenv('BARK_ICON'):
            self.bark_icon = bark_icon
            self._env_set_keys.add('bark_icon')
        if minimal_push_enabled := os.getenv('MINIMAL_PUSH_ENABLED'):
            self.minimal_push_enabled = minimal_push_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('minimal_push_enabled')
        if smtp_params := os.getenv('SMTP_PARAMS'):
            self.smtp_params = smtp_params
            self._env_set_keys.add('smtp_params')

        # 监控事件
        if events := os.getenv('MONITOR_EVENTS'):
            self.monitor_events = [e.strip() for e in events.split(',')]
            self._env_set_keys.add('monitor_events')

        # 日志级别
        if log_level := os.getenv('LOG_LEVEL'):
            self.log_level = log_level.upper()
            self._env_set_keys.add('log_level')


        # HTTP配置
        if pool_size := os.getenv('HTTP_POOL_SIZE'):
            try:
                self.http_pool_size = int(pool_size)
                self._env_set_keys.add('http_pool_size')
            except (TypeError, ValueError):
                print(f"警告: HTTP_POOL_SIZE 不是整数，已忽略: {pool_size}")

        if retry_count := os.getenv('HTTP_RETRY_COUNT'):
            try:
                self.http_retry_count = int(retry_count)
                self._env_set_keys.add('http_retry_count')
            except (TypeError, ValueError):
                print(f"警告: HTTP_RETRY_COUNT 不是整数，已忽略: {retry_count}")

        if timeout := os.getenv('HTTP_TIMEOUT'):
            try:
                self.http_timeout = int(timeout)
                self._env_set_keys.add('http_timeout')
            except (TypeError, ValueError):
                print(f"警告: HTTP_TIMEOUT 不是整数，已忽略: {timeout}")

        if dedup_window := os.getenv('DEDUP_WINDOW'):
            try:
                self.dedup_window = int(dedup_window)
                self._env_set_keys.add('dedup_window')
            except (TypeError, ValueError):
                print(f"警告: DEDUP_WINDOW 不是整数，已忽略: {dedup_window}")

        # 数据库日志监控
        if db_path := os.getenv('LOGGER_DB_PATH'):
            self.logger_db_path = db_path
            self._env_set_keys.add('logger_db_path')
        if backup_db_path := os.getenv('BACKUP_DB_PATH'):
            self.backup_db_path = backup_db_path
            self._env_set_keys.add('backup_db_path')
        if poll_interval := os.getenv('LOGGER_POLL_INTERVAL'):
            try:
                self.logger_poll_interval = int(poll_interval)
                self._env_set_keys.add('logger_poll_interval')
            except (TypeError, ValueError):
                print(f"警告: LOGGER_POLL_INTERVAL 不是整数，已忽略: {poll_interval}")

        if os.getenv('MEDIA_LIB_LOGGER_ENABLED', '').strip().lower() in ('1', 'true', 'yes', 'on'):
            self.media_lib_logger_enabled = True
            self._env_set_keys.add('media_lib_logger_enabled')
        if trim_m := os.getenv('TRIM_MEDIA_DB_PATH'):
            self.trim_media_db_path = trim_m.strip()
            self._env_set_keys.add('trim_media_db_path')
        if trim_a := os.getenv('TRIM_ACTIVITY_DB_PATH'):
            self.trim_activity_db_path = trim_a.strip()
            self._env_set_keys.add('trim_activity_db_path')
        if photo_p := os.getenv('PHOTO_DB_PATH'):
            self.photo_db_path = photo_p.strip()
            self._env_set_keys.add('photo_db_path')
        if scheduler_p := os.getenv('SCHEDULER_DB_PATH'):
            self.scheduler_db_path = scheduler_p.strip()
            self._env_set_keys.add('scheduler_db_path')

        if docker_sock := os.getenv('DOCKER_SOCKET_PATH'):
            self.docker_socket_path = docker_sock.strip() or '/var/run/docker.sock'
            self._env_set_keys.add('docker_socket_path')

        # 高级配置
        if max_age := os.getenv('MAX_LOG_AGE'):
            try:
                self.max_log_age = int(max_age)
                self._env_set_keys.add('max_log_age')
            except (TypeError, ValueError):
                print(f"警告: MAX_LOG_AGE 不是整数，已忽略: {max_age}")

        if log_retention := os.getenv('LOG_RETENTION_DAYS'):
            try:
                self.log_retention_days = int(log_retention)
                self._env_set_keys.add('log_retention_days')
            except (TypeError, ValueError):
                print(f"警告: LOG_RETENTION_DAYS 不是整数，已忽略: {log_retention}")

        if notify_restart_enabled := os.getenv('NOTIFY_RESTART_ENABLED'):
            self.notification_restart_enabled = notify_restart_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('notification_restart_enabled')

        if notify_restart_failures := os.getenv('NOTIFY_RESTART_CONSECUTIVE'):
            try:
                self.notification_restart_consecutive_failures = int(notify_restart_failures)
                self._env_set_keys.add('notification_restart_consecutive_failures')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_CONSECUTIVE 不是整数，已忽略: {notify_restart_failures}")

        if notify_restart_window := os.getenv('NOTIFY_RESTART_WINDOW'):
            try:
                self.notification_restart_window = int(notify_restart_window)
                self._env_set_keys.add('notification_restart_window')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_WINDOW 不是整数，已忽略: {notify_restart_window}")

        if notify_restart_cooldown := os.getenv('NOTIFY_RESTART_COOLDOWN'):
            try:
                self.notification_restart_cooldown = int(notify_restart_cooldown)
                self._env_set_keys.add('notification_restart_cooldown')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_COOLDOWN 不是整数，已忽略: {notify_restart_cooldown}")

    
    def _validate(self):
        """验证配置（部署时不强制 Webhook，可在 UI 中配置）"""
        if self.wechat_webhook_url and not self.wechat_webhook_url.startswith('http'):
            raise ValueError("WECHAT_WEBHOOK_URL 必须是有效的URL")
        
        if self.dingtalk_webhook_url and not self.dingtalk_webhook_url.startswith('http'):
            raise ValueError("DINGTALK_WEBHOOK_URL 必须是有效的URL")
        
        if self.feishu_webhook_url and not self.feishu_webhook_url.startswith('http'):
            raise ValueError("FEISHU_WEBHOOK_URL 必须是有效的URL")
        
        if self.bark_url and not self.bark_url.startswith('http'):
            raise ValueError("BARK_URL 必须是有效的URL")

        # pushplus_params 为 JSON 或 JSON|JSON...，发送时再校验
        if self.pushplus_params:
            for part in (p.strip() for p in self.pushplus_params.split('|') if p.strip()):
                try:
                    obj = json.loads(part)
                    if not isinstance(obj, dict) or 'token' not in obj:
                        raise ValueError("PushPlus 参数必须为包含 token 的 JSON 对象")
                except json.JSONDecodeError as e:
                    raise ValueError(f"PushPlus 参数不是合法 JSON: {e}")

        if self.magic_push_params:
            for part in (p.strip() for p in self.magic_push_params.split("|") if p.strip()):
                try:
                    obj = json.loads(part)
                    if not isinstance(obj, dict):
                        raise ValueError("魔法推送参数必须为 JSON 对象")
                    base = (obj.get("base_url") or "").strip()
                    token = (obj.get("token") or "").strip()
                    if not base or not token:
                        raise ValueError("魔法推送须包含 base_url 与 token")
                    if not base.startswith("http"):
                        raise ValueError("魔法推送 base_url 须为有效 http(s) 地址")
                except json.JSONDecodeError as e:
                    raise ValueError(f"魔法推送参数不是合法 JSON: {e}")

        if self.smtp_params:
            for part in (p.strip() for p in self.smtp_params.split("|") if p.strip()):
                try:
                    obj = json.loads(part)
                    if not isinstance(obj, dict):
                        raise ValueError("SMTP 参数必须为 JSON 对象")
                    server = (obj.get("server") or "").strip()
                    username = (obj.get("username") or "").strip()
                    password = obj.get("password") or ""
                    to_raw = (obj.get("to") or "").strip()
                    if not server or not username or not password or not to_raw:
                        raise ValueError("SMTP 参数须包含 server、username、password、to")
                    try:
                        int(obj.get("port", 465))
                    except (TypeError, ValueError):
                        raise ValueError("SMTP port 必须为整数")
                except json.JSONDecodeError as e:
                    raise ValueError(f"SMTP 参数不是合法 JSON: {e}")

        if not self.monitor_events:
            raise ValueError("必须配置至少一个监控事件")
        
        # 过滤未知事件类型（白名单见 valid_event_ids.MONITOR_EVENT_IDS；含 VM 的动态 eventId）
        self.monitor_events = filter_monitor_events(self.monitor_events)
        if not self.monitor_events:
            raise ValueError("必须配置至少一个监控事件")
        

    
    def _ensure_directories(self):
        """确保目录存在"""
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        Path(self.cursor_dir).mkdir(parents=True, exist_ok=True)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'monitor_events': self.monitor_events,
            'log_level': self.log_level,
            'http_pool_size': self.http_pool_size,
            'dedup_window': self.dedup_window,
            'wechat_webhook_url': self.wechat_webhook_url[:50] + '...' 
                if len(self.wechat_webhook_url) > 50 else self.wechat_webhook_url
        }
