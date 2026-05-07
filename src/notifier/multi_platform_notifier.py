"""
多平台通知器
支持企业微信、钉钉和飞书的WebHook通知
"""

import json
import time
import logging
import hashlib
import urllib.parse
import threading
import re
import smtplib
import ssl
import sqlite3
import os
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.header import Header
from email.utils import formataddr

from .connection_pool import ConnectionPool

# PushPlus 固定接口地址
PUSHPLUS_URL = "http://www.pushplus.plus/send"


@dataclass
class MultiPlatformMessage:
    """多平台消息"""
    
    title: str = ""
    content: str = ""

    def merged_plain_text(self, *, blank_line_between: bool = False) -> str:
        """标题与正文合并为一段纯文本。正文为空时不追加分隔符，避免极简模式末尾多余空行。"""
        title = self.title or ""
        content_raw = self.content or ""
        if not content_raw.strip():
            return title
        sep = "\n\n" if blank_line_between else "\n"
        return f"{title}{sep}{content_raw}"
    
    def to_wechat_format(self) -> Dict[str, Any]:
        """转换为企业微信格式"""
        return {
            "msgtype": "text",
            "text": {
                "content": self.merged_plain_text()
            }
        }
    
    def to_dingtalk_format(self) -> Dict[str, Any]:
        """转换为钉钉格式"""
        return {
            "msgtype": "text",
            "text": {
                "content": self.merged_plain_text()
            }
        }
    
    def to_feishu_format(self) -> Dict[str, Any]:
        """转换为飞书格式"""
        return {
            "msg_type": "text",
            "content": {
                "text": self.merged_plain_text()
            }
        }


class MultiPlatformNotifier:
    """多平台通知器"""
    BATCH_PER_TYPE_LIMIT = 3
    BATCH_MAX_TYPES = 6
    BATCH_MAX_CHARS = 3200
    _EMOJI_RE = re.compile(
        "["  # 常见 emoji / pictographs 范围（覆盖面足够用于“去表情展示”）
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F680-\U0001F6FF"  # transport & map
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # geometric extended
        "\U0001F800-\U0001F8FF"  # arrows-c
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA00-\U0001FAFF"  # symbols & pictographs extended
        "\u2600-\u27BF"          # misc symbols + dingbats
        "\u200d"                 # zero width joiner
        "\ufe0f"                 # variation selector
        "]+",
        flags=re.UNICODE,
    )
    
    # 事件标题映射
    EVENT_TITLES = {
        'LoginSucc': '🔐 飞牛NAS-登录成功通知',
        'LoginSucc2FA1': '🔐 飞牛NAS-二次验证登录',
        'LoginFail': '❌ 飞牛NAS-登录失败告警',
        'Logout': '👋 飞牛NAS-退出登录通知',
        'FoundDisk': '💾 飞牛NAS-发现新硬盘',
        'InsertDisk': '💾 飞牛NAS-插入硬盘',
        'EjectDisk': '🧷 飞牛NAS-弹出硬盘',
        'StorageBroken': '⚠️ 飞牛NAS-存储空间损坏',
        'SSH_INVALID_USER': '⚠️ 飞牛NAS-SSH无效用户尝试',
        'SSH_AUTH_FAILED': '❌ 飞牛NAS-SSH认证失败',
        'SSH_LOGIN_SUCCESS': '🔐 飞牛NAS-SSH登录成功',
        'SSH_DISCONNECTED': '👋 飞牛NAS-SSH断开连接',
        'APP_CRASH': '💥 飞牛NAS-应用崩溃告警',
        'APP_UPDATE_FAILED': '💥 飞牛NAS-应用更新失败告警',
        'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION': '💥 飞牛NAS-应用启动失败告警',
        'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE': '💥 飞牛NAS-应用自启动失败告警',
        'CPU_USAGE_ALARM': '📊 飞牛NAS-CPU使用率告警',
        'CPU_USAGE_RESTORED': '✅ 飞牛NAS-CPU使用率恢复',
        'MEMORY_USAGE_ALARM': '📊 飞牛NAS-内存使用率告警',
        'MEMORY_USAGE_RESTORED': '✅ 飞牛NAS-内存使用率恢复',
        'CPU_TEMPERATURE_ALARM': '🌡️ 飞牛NAS-CPU温度告警',
        'UPS_ONBATT': '⚠️ 飞牛NAS-UPS切换到电池供电模式',
        'UPS_ONBATT_LOWBATT': '🚨 飞牛NAS-UPS切换到电池供电模式',
        'UPS_ONLINE': '✅ 飞牛NAS-UPS切换到市电供电模式',
        'UPS_ENABLE': '🔌 飞牛NAS-开启UPS支持',
        'UPS_DISABLE': '🔌 飞牛NAS-关闭UPS支持',
        'DiskWakeup': '☀️ 飞牛NAS-磁盘唤醒通知',
        'DiskSpindown': '🌙 飞牛NAS-磁盘休眠通知',
        'APP_START': '🔔 飞牛NAS-监控启动通知',
        'APP_STOP': '🔕 飞牛NAS-监控关闭通知',
        # 数据库 log 表 eventId 直接对应（应用生命周期）
        'APP_STARTED': '✅ 飞牛NAS-应用已启动',
        'APP_STOPPED': '🛑 飞牛NAS-应用已停止',
        'APP_UPDATED': '🔄 飞牛NAS-应用已更新',
        'APP_INSTALLED': '📦 飞牛NAS-应用已安装',
        'APP_AUTO_STARTED': '▶️ 飞牛NAS-应用已自启动',
        'APP_UNINSTALLED': '🗑️ 飞牛NAS-应用已卸载',
        'DISK_IO_ERR': '⚠️ 飞牛NAS-磁盘IO错误告警',
        'TEST_PUSH': '🧪 飞牛NAS-测试推送',
        'DND_SUMMARY': '📋 飞牛NAS-勿扰时段汇总',
        'POLL_BATCH_SUMMARY': '飞牛NAS-多事件合并通知',
        'BACKUP_TASK_SUCCESS': '✅ 飞牛NAS-备份任务完成',
        'BACKUP_TASK_FAILED': '❌ 飞牛NAS-备份任务失败',
        'BACKUP_TASK_PARTIAL_SUCCESS': '⚠️ 飞牛NAS-备份任务部分成功',
        'SCHEDULER_TASK_SUCCESS': '✅ 飞牛NAS-任务计划执行成功',
        'SCHEDULER_TASK_FAILED': '❌ 飞牛NAS-任务计划执行失败',
        'SCHEDULER_TASK_CONDITION_FAILED': '⚠️ 飞牛NAS-任务计划条件不满足',
        # 可选事件（默认不推送）
        'ARCHIVING_SUCCESS': '📦 飞牛NAS-归档成功',
        'DeleteFile': '🗑️ 飞牛NAS-文件删除',
        'MovetoTrashbin': '🗑️ 飞牛NAS-移到回收站',
        'SHARE_EVENTID_DEL': '📤 飞牛NAS-共享删除',
        'SHARE_EVENTID_PUT': '📤 飞牛NAS-共享添加/更新',
        'WEBDAV_ENABLED': '🌐 飞牛NAS-WebDAV已启用',
        'WEBDAV_DISABLED': '🛑 飞牛NAS-WebDAV已关闭',
        'SAMBA_ENABLED': '📂 飞牛NAS-Samba已启用',
        'SAMBA_DISABLED': '🛑 飞牛NAS-Samba已关闭',
        'DLNA_ENABLED': '📺 飞牛NAS-DLNA已启用',
        'DLNA_DISABLED': '🛑 飞牛NAS-DLNA已关闭',
        'FTP_ENABLED': '📁 飞牛NAS-FTP已启用',
        'FTP_DISABLED': '🛑 飞牛NAS-FTP已关闭',
        'NFS_ENABLED': '📂 飞牛NAS-NFS已启用',
        'NFS_DISABLED': '🛑 飞牛NAS-NFS已关闭',
        'FW_ENABLE': '🔥 飞牛NAS-防火墙已开启',
        'FW_DISABLE': '🔥 飞牛NAS-防火墙已关闭',
        'SECURITY_PORTCHANGED': '🔒 飞牛NAS-安全/端口变更',
        'CREATE_VM': '🖥️ 飞牛NAS-创建虚拟机',
        'START_VM': '🖥️ 飞牛NAS-操作虚拟机开机',
        'STATUS_SHUTOFF_VM': '🖥️ 飞牛NAS-虚拟机已关机',
        'STATUS_PAUSED_VM': '🖥️ 飞牛NAS-虚拟机已暂停',
        'PAUSE_VM': '🖥️ 飞牛NAS-操作虚拟机暂停',
        'RESUME_VM': '🖥️ 飞牛NAS-操作虚拟机恢复',
        'STATUS_RESUMED_VM': '🖥️ 飞牛NAS-虚拟机已恢复',
        'REBOOT_VM': '🖥️ 飞牛NAS-操作虚拟机重启',
        'STATUS_REBOOTED_VM': '🖥️ 飞牛NAS-虚拟机已重启',
        'EDIT_VM': '🖥️ 飞牛NAS-修改虚拟机配置',
        'OVA_EXPORT_VM': '🖥️ 飞牛NAS-导出 OVA 成功',
        'DELETE_VM': '🗑️ 飞牛NAS-删除虚拟机',
        'SHUTDOWN_VM': '🖥️ 飞牛NAS-操作虚拟机关机',
        'STATUS_RUNNING_VM': '🖥️ 飞牛NAS-虚拟机已开机',
        'DESTROY_VM': '🗑️ 飞牛NAS-虚拟机已销毁',
        # 影视库
        'MEDIA_LOGIN_SUCC': '🎬 飞牛NAS-影视库登录成功',
        'MEDIA_LOGOUT': '🎬 飞牛NAS-影视库退出登录',
        'MEDIA_USER_CREATED': '👤 飞牛NAS-影视库创建用户',
        'TRIM_RESOURCE_ADDED': '📥 飞牛NAS-影视资源入库',
        'TRIM_SCRAPE_SUCCESS': '✅ 飞牛NAS-影视刮削完成',
        'PHOTO_SHARE_CREATED': '📥 飞牛NAS-相册分享创建',
        'PHOTO_SHARE_EXPIRED': '⏱️ 飞牛NAS-相册分享过期',
        'PHOTO_DEVICE_REGISTERED': '📱 飞牛NAS-相册同步设备',
        'FACE_RECOGNITION_UPDATED': '✅ 飞牛NAS-相册人脸识别',
    }
    
    # Bark事件标题映射 - 用于Bark推送，标题统一为"飞牛NAS通知"
    BARK_EVENT_CONTENTS = {
        'LoginSucc': '用户{user}登录成功',
        'LoginSucc2FA1': '用户{user}登录触发二次校验',
        'LoginFail': '用户{user}登录失败，请检查是否有异常尝试。',
        'Logout': '用户{user}退出登录',
        'FoundDisk': '发现新硬盘{disk_info}',
        'InsertDisk': '插入硬盘{name}',
        'EjectDisk': '弹出硬盘{name}',
        'StorageBroken': '存储空间损坏 volume={volume}',
        'SSH_INVALID_USER': '无效用户{user}尝试登录',
        'SSH_AUTH_FAILED': 'SSH认证失败{user_info}',
        'SSH_LOGIN_SUCCESS': 'SSH用户{user}登录成功',
        'SSH_DISCONNECTED': 'SSH连接已断开',
        'APP_CRASH': '应用{name}崩溃',
        'APP_UPDATE_FAILED': '应用{name}更新失败',
        'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION': '应用{name}启动失败',
        'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE': '应用{name}自启动失败(Docker不可用)',
        'CPU_USAGE_ALARM': 'CPU使用率超过{threshold}%',
        'CPU_USAGE_RESTORED': 'CPU使用率已恢复至阈值{threshold}%以下',
        'MEMORY_USAGE_ALARM': '内存使用率超过{threshold}%',
        'MEMORY_USAGE_RESTORED': '内存使用率已恢复至阈值{threshold}%以下',
        'CPU_TEMPERATURE_ALARM': 'CPU温度超过{threshold}°C',
        'UPS_ONBATT': 'UPS提示：UPS切换到电池供电',
        'UPS_ONBATT_LOWBATT': 'UPS提示：UPS低电量自动关机',
        'UPS_ONLINE': 'UPS提示：UPS切换到市电供电',
        'UPS_ENABLE': '已开启UPS支持',
        'UPS_DISABLE': '已关闭UPS支持',
        'DiskWakeup': '磁盘被唤醒',
        'DiskSpindown': '磁盘进入休眠状态',
        'APP_START': '飞牛NAS通知启动',
        'APP_STOP': '飞牛NAS通知已停止',
        'APP_STARTED': '应用{name}已启动',
        'APP_STOPPED': '应用{name}已停止',
        'APP_UPDATED': '应用{name}已更新',
        'APP_INSTALLED': '应用{name}已安装',
        'APP_AUTO_STARTED': '应用{name}已自启动',
        'APP_UNINSTALLED': '应用{name}已卸载',
        'DISK_IO_ERR': '磁盘{dev}发生IO错误，错误次数{err_cnt}',
        'TEST_PUSH': '测试推送',
        'DND_SUMMARY': '勿扰时段事件汇总',
        'POLL_BATCH_SUMMARY': '多事件合并通知',
        'BACKUP_TASK_SUCCESS': '备份任务完成',
        'BACKUP_TASK_FAILED': '备份任务失败',
        'BACKUP_TASK_PARTIAL_SUCCESS': '备份任务部分成功',
        'SCHEDULER_TASK_SUCCESS': '任务计划执行成功',
        'SCHEDULER_TASK_FAILED': '任务计划执行失败',
        'SCHEDULER_TASK_CONDITION_FAILED': '任务计划条件不满足',
        'ARCHIVING_SUCCESS': '归档成功',
        'DeleteFile': '文件删除',
        'MovetoTrashbin': '移到回收站',
        'SHARE_EVENTID_DEL': '共享删除',
        'SHARE_EVENTID_PUT': '共享添加/更新',
        'WEBDAV_ENABLED': 'WebDAV已启用',
        'WEBDAV_DISABLED': 'WebDAV已关闭',
        'SAMBA_ENABLED': 'Samba已启用',
        'SAMBA_DISABLED': 'Samba已关闭',
        'DLNA_ENABLED': 'DLNA已启用',
        'DLNA_DISABLED': 'DLNA已关闭',
        'FTP_ENABLED': 'FTP已启用',
        'FTP_DISABLED': 'FTP已关闭',
        'NFS_ENABLED': 'NFS已启用',
        'NFS_DISABLED': 'NFS已关闭',
        'FW_ENABLE': '防火墙已开启',
        'FW_DISABLE': '防火墙已关闭',
        'SECURITY_PORTCHANGED': '安全/端口变更',
        'CREATE_VM': '创建虚拟机{vm_title}',
        'START_VM': '用户{user}操作虚拟机{vm_title}开机',
        'STATUS_SHUTOFF_VM': '虚拟机{vm_title}切换到关机状态',
        'STATUS_PAUSED_VM': '虚拟机{vm_title}切换到暂停状态',
        'PAUSE_VM': '用户{user}操作虚拟机{vm_title}暂停',
        'RESUME_VM': '用户{user}操作虚拟机{vm_title}恢复',
        'STATUS_RESUMED_VM': '虚拟机{vm_title}切换到恢复状态',
        'REBOOT_VM': '用户{user}操作虚拟机{vm_title}重启',
        'STATUS_REBOOTED_VM': '虚拟机{vm_title}已重启',
        'EDIT_VM': '修改了虚拟机{vm_title}的配置',
        'OVA_EXPORT_VM': '导出虚拟机{vm_title}的 OVA 文件成功',
        'DELETE_VM': '删除了虚拟机{vm_title}',
        'SHUTDOWN_VM': '用户{user}关闭虚拟机{vm_title}',
        'STATUS_RUNNING_VM': '用户{user}开启虚拟机{vm_title}',
        'DESTROY_VM': '用户{user}销毁虚拟机{vm_title}',
        'MEDIA_LOGIN_SUCC': '影视库登录 {user}',
        'MEDIA_LOGOUT': '影视库退出 {user}',
        'MEDIA_USER_CREATED': '影视库创建用户',
        'TRIM_RESOURCE_ADDED': '影视资源入库',
        'TRIM_SCRAPE_SUCCESS': '影视刮削完成',
        'PHOTO_SHARE_CREATED': '相册分享创建',
        'PHOTO_SHARE_EXPIRED': '相册分享过期',
        'PHOTO_DEVICE_REGISTERED': '相册同步设备',
        'FACE_RECOGNITION_UPDATED': '相册人脸识别',
    }
    
    # 事件备注
    EVENT_NOTES = {
        'LoginSucc': '系统检测到用户登录成功，请确认是否为本人操作。',
        'LoginSucc2FA1': '用户已完成两步验证的第一步，等待二次验证。',
        'LoginFail': '系统检测到登录失败，请检查是否有异常尝试。',
        'Logout': '用户已安全退出系统。',
        'FoundDisk': '检测到新存储设备接入系统。',
        'InsertDisk': '检测到存储设备插入。',
        'EjectDisk': '检测到存储设备弹出。',
        'StorageBroken': '检测到存储空间损坏，请尽快检查。',
        'SSH_INVALID_USER': '检测到无效用户登录尝试，请注意安全。',
        'SSH_AUTH_FAILED': 'SSH认证失败，请确认是否为合法用户。',
        'SSH_LOGIN_SUCCESS': 'SSH登录成功，请确认是否为本人操作。',
        'SSH_DISCONNECTED': 'SSH连接已断开。',
        'APP_CRASH': '应用程序异常退出，建议检查应用状态和日志。',
        'APP_UPDATE_FAILED': '应用程序更新失败，建议检查应用状态和日志。',
        'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION': '应用程序启动失败（本地运行异常），建议检查应用状态和日志。',
        'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE': '应用程序自启动失败（Docker 不可用），请检查 Docker 服务。',
        'CPU_USAGE_ALARM': 'CPU 使用率超过阈值，建议检查系统负载或关闭占用高的进程。',
        'CPU_USAGE_RESTORED': 'CPU 使用率已恢复至阈值以下，负载正常。',
        'MEMORY_USAGE_ALARM': '内存使用率超过阈值，建议检查占用内存的进程或服务。',
        'MEMORY_USAGE_RESTORED': '内存使用率已恢复至阈值以下。',
        'CPU_TEMPERATURE_ALARM': 'CPU 温度超过阈值，请检查散热与机箱通风。',
        'UPS_ONBATT': 'UPS切换到电池供电模式，请注意电池电量。',
        'UPS_ONBATT_LOWBATT': 'UPS切换到电池供电模式，低电量自动关机，请尽快恢复市电供应。',
        'UPS_ONLINE': 'UPS切换到市电供电模式，电力供应恢复正常。',
        'UPS_ENABLE': '系统已开启 UPS 支持。',
        'UPS_DISABLE': '系统已关闭 UPS 支持。',
        'DiskWakeup': '磁盘已被唤醒。',
        'DiskSpindown': '磁盘已进入休眠状态。',
        'APP_START': '飞牛NAS日志监控服务已启动，开始监控系统事件。',
        'APP_STOP': '飞牛NAS日志监控服务已停止，暂停监控系统事件。',
        'APP_STARTED': '应用已成功启动。',
        'APP_STOPPED': '应用已停止运行。',
        'APP_UPDATED': '应用已更新到新版本。',
        'APP_INSTALLED': '新应用已安装。',
        'APP_AUTO_STARTED': '▶应用已随系统自启动。',
        'APP_UNINSTALLED': '应用已卸载。',
        'DISK_IO_ERR': '磁盘发生IO错误，请检查硬盘健康与连接。',
        'TEST_PUSH': 'Web 配置页发送的测试消息。',
        'POLL_BATCH_SUMMARY': '',
        'BACKUP_TASK_SUCCESS': '备份任务执行完成。',
        'BACKUP_TASK_FAILED': '备份任务执行失败，请检查任务配置与网络状态。',
        'BACKUP_TASK_PARTIAL_SUCCESS': '备份任务部分完成，部分文件未能成功传输。',
        'SCHEDULER_TASK_SUCCESS': '任务计划执行完成。',
        'SCHEDULER_TASK_FAILED': '任务计划执行失败，请检查脚本与执行日志。',
        'SCHEDULER_TASK_CONDITION_FAILED': '任务计划触发条件不满足，任务未执行。',
        'ARCHIVING_SUCCESS': '系统完成归档任务。',
        'DeleteFile': '文件已被删除。',
        'MovetoTrashbin': '文件已移至回收站。',
        'SHARE_EVENTID_DEL': '共享已删除。',
        'SHARE_EVENTID_PUT': '共享已添加或更新。',
        'WEBDAV_ENABLED': 'WebDAV 服务已启用。',
        'WEBDAV_DISABLED': 'WebDAV 服务已关闭。',
        'SAMBA_ENABLED': 'Samba 服务已启用。',
        'SAMBA_DISABLED': 'Samba 服务已关闭。',
        'DLNA_ENABLED': 'DLNA 服务已启用。',
        'DLNA_DISABLED': 'DLNA 服务已关闭。',
        'FTP_ENABLED': 'FTP 服务已启用。',
        'FTP_DISABLED': 'FTP 服务已关闭。',
        'NFS_ENABLED': 'NFS 服务已启用。',
        'NFS_DISABLED': 'NFS 服务已关闭。',
        'FW_ENABLE': '防火墙已开启。',
        'FW_DISABLE': '防火墙已关闭。',
        'SECURITY_PORTCHANGED': '安全或端口设置已变更。',
        'CREATE_VM': '用户已创建虚拟机。',
        'START_VM': '用户已操作虚拟机开机。',
        'STATUS_SHUTOFF_VM': '虚拟机当前为关机状态。',
        'STATUS_PAUSED_VM': '虚拟机当前为暂停状态。',
        'PAUSE_VM': '用户已操作虚拟机暂停。',
        'RESUME_VM': '用户已操作虚拟机恢复。',
        'STATUS_RESUMED_VM': '虚拟机当前为恢复状态。',
        'REBOOT_VM': '用户已操作虚拟机重启。',
        'STATUS_REBOOTED_VM': '虚拟机已完成重启。',
        'EDIT_VM': '用户已修改虚拟机配置。',
        'OVA_EXPORT_VM': '虚拟机 OVA 导出成功。',
        'DELETE_VM': '用户已删除虚拟机。',
        'SHUTDOWN_VM': '用户已执行虚拟机关机操作。',
        'STATUS_RUNNING_VM': '用户已执行虚拟机开机操作。',
        'DESTROY_VM': '用户已销毁虚拟机，请确认是否为预期操作。',
        'MEDIA_LOGIN_SUCC': '影视库用户登录成功。',
        'MEDIA_LOGOUT': '影视库用户退出登录。',
        'MEDIA_USER_CREATED': '影视库管理员创建用户成功。',
        'TRIM_RESOURCE_ADDED': '影视库资源入库成功。',
        'TRIM_SCRAPE_SUCCESS': '影视资源刮削完成。',
        'PHOTO_SHARE_CREATED': '相册库新增了对外分享链接。',
        'PHOTO_SHARE_EXPIRED': '该相册分享已超过有效期。',
        'PHOTO_DEVICE_REGISTERED': '相册库已登记新的同步设备。',
        'FACE_RECOGNITION_UPDATED': '相册人脸识别有新的任务记录。',
    }
    
    def __init__(self, 
                 wechat_webhook_url: str = "",
                 dingtalk_webhook_url: str = "",
                 feishu_webhook_url: str = "",
                 bark_url: str = "",
                 bark_icon: str = "",
                 pushplus_params: str = "",
                 magic_push_params: str = "",
                 smtp_params: str = "",
                 title_prefix: str = "",
                 minimal_push_enabled: bool = False,
                 user_lookup_db_path: str = "",
                 activity_user_lookup_db_path: str = "",
                 logger_user_lookup_db_path: str = "",
                 dedup_window: int = 300,
                 pool_size: int = 10,
                 retries: int = 3,
                 timeout: int = 10):
        """
        初始化通知器
        
        Args:
            wechat_webhook_url: 企业微信Webhook URL
            dingtalk_webhook_url: 钉钉Webhook URL
            feishu_webhook_url: 飞书Webhook URL
            bark_url: Bark推送URL
            pushplus_params: PushPlus 参数（JSON 字符串，多个用 | 分隔）
            magic_push_params: 魔法推送（JSON 含 base_url、token、可选 title，多个用 | 分隔）
            smtp_params: SMTP 邮件配置（JSON：server、port、username、password、to，可选 from）
            dedup_window: 去重时间窗口（秒）
            pool_size: 连接池大小
            retries: 重试次数
            timeout: 超时时间
        """
        self.wechat_webhook_url = wechat_webhook_url
        self.dingtalk_webhook_url = dingtalk_webhook_url
        self.feishu_webhook_url = feishu_webhook_url
        self.bark_url = bark_url
        self.bark_icon = (bark_icon or "").strip()
        self.pushplus_params = pushplus_params or ""
        self.magic_push_params = magic_push_params or ""
        self.smtp_params = smtp_params or ""
        if not isinstance(title_prefix, str):
            self.title_prefix = ""
        else:
            self.title_prefix = (title_prefix or "").strip()
        self.minimal_push_enabled = bool(minimal_push_enabled)
        self.user_lookup_db_path = (user_lookup_db_path or "").strip()
        self.activity_user_lookup_db_path = (activity_user_lookup_db_path or "").strip()
        self.logger_user_lookup_db_path = (logger_user_lookup_db_path or "").strip()
        self._user_db_cache = {}
        self._user_db_cache_loaded_at = 0.0
        self._nas_uid_name_cache = {}
        self._nas_uid_name_cache_loaded_at = 0.0
        self.dedup_window = dedup_window
        
        # 连接池
        self.connection_pool = ConnectionPool(
            pool_size=pool_size,
            max_retries=retries,
            timeout=timeout
        )
        
        # 事件去重缓存
        self.sent_events = {}
        
        # 磁盘事件合并缓存 - 使用时间窗口缓存多个磁盘事件
        self.disk_wakeup_cache = {}  # {time_window: [event_data_list]}
        self.disk_spindown_cache = {}  # {time_window: [event_data_list]}
        self.merge_window = 5  # 5秒合并窗口

        # 线程控制
        self._stop_flag = False
        self._cache_lock = threading.Lock()  # 保护磁盘事件缓存的线程锁

        # 发送健康状态
        self._health_lock = threading.Lock()
        self.last_attempt_time = None
        self.last_success_time = None
        self.consecutive_failures = 0
        self.first_failure_time = None
        self.total_failures_since_success = 0

        # 合并事件定时发送线程
        self._start_merge_timer()

        # 日志
        self.logger = logging.getLogger(__name__)

        platforms = []
        if self.wechat_webhook_url:
            platforms.append('企业微信')
        if self.dingtalk_webhook_url:
            platforms.append('钉钉')
        if self.feishu_webhook_url:
            platforms.append('飞书')
        if self.bark_url:
            platforms.append('Bark')
        if self.pushplus_params:
            platforms.append('PushPlus')
        if self.magic_push_params:
            platforms.append('魔法推送')
        if self.smtp_params:
            platforms.append('SMTP邮件')

        self.logger.info(f"多平台通知器初始化完成，支持平台: {', '.join(platforms) if platforms else '无'}, 去重窗口: {dedup_window}秒")

    def _fallback_event_title(self, event_type: str) -> str:
        """未知事件类型的标题模板（与 EVENT_TITLES 中占位格式一致）。"""
        if self.title_prefix:
            return f"📋 {self.title_prefix}-系统事件: {event_type}"
        return f"📋 系统事件: {event_type}"

    def _with_title_prefix(self, title: str) -> str:
        """把标题中的默认「飞牛NAS」替换为配置前缀；前缀留空时去掉「飞牛NAS-」仅保留事件类型部分。"""
        if not isinstance(title, str):
            return str(title)
        if not self.title_prefix:
            t = title.replace("飞牛NAS-", "", 1)
            if "飞牛NAS" in t:
                t = t.replace("飞牛NAS", "")
            return t
        return title.replace("飞牛NAS", self.title_prefix)

    def _record_send_result(self, success: bool):
        """记录发送结果用于健康监控"""
        now = time.time()
        with self._health_lock:
            self.last_attempt_time = now
            if success:
                self.last_success_time = now
                self.consecutive_failures = 0
                self.first_failure_time = None
                self.total_failures_since_success = 0
            else:
                if self.first_failure_time is None:
                    self.first_failure_time = now
                self.consecutive_failures += 1
                self.total_failures_since_success += 1

    def get_delivery_health(self) -> Dict[str, Any]:
        """获取通知发送健康状态"""
        with self._health_lock:
            return {
                'last_attempt_time': self.last_attempt_time,
                'last_success_time': self.last_success_time,
                'consecutive_failures': self.consecutive_failures,
                'first_failure_time': self.first_failure_time,
                'total_failures_since_success': self.total_failures_since_success,
                'active_platforms': {
                    'wechat': bool(self.wechat_webhook_url),
                    'dingtalk': bool(self.dingtalk_webhook_url),
                    'feishu': bool(self.feishu_webhook_url),
                    'bark': bool(self.bark_url),
                    'pushplus': bool(self.pushplus_params),
                    'magic_push': bool(self.magic_push_params),
                    'smtp': bool(self.smtp_params),
                }
            }
    
    def _start_merge_timer(self):
        """启动合并事件定时处理线程"""
        self.timer_thread = threading.Thread(target=self._merge_timer_worker, daemon=True)
        self.timer_thread.start()
    
    def _merge_timer_worker(self):
        """合并事件定时处理工作线程"""
        while not self._stop_flag:
            try:
                # 检查并处理过期的合并事件
                current_time = time.time()
                current_window = int(current_time / self.merge_window)

                # 检查前一个窗口是否有待合并的事件
                prev_window = current_window - 1

                # 使用锁保护缓存访问
                with self._cache_lock:
                    # 处理待合并的磁盘唤醒事件
                    if prev_window in self.disk_wakeup_cache and self.disk_wakeup_cache[prev_window]:
                        self._send_merged_disk_event('DiskWakeup', self.disk_wakeup_cache[prev_window], prev_window)
                        del self.disk_wakeup_cache[prev_window]

                    # 处理待合并的磁盘休眠事件
                    if prev_window in self.disk_spindown_cache and self.disk_spindown_cache[prev_window]:
                        self._send_merged_disk_event('DiskSpindown', self.disk_spindown_cache[prev_window], prev_window)
                        del self.disk_spindown_cache[prev_window]

                    # 清理太久之前的缓存（超过2个窗口的）
                    too_old_window = current_window - 3
                    self.disk_wakeup_cache = {k: v for k, v in self.disk_wakeup_cache.items() if k > too_old_window}
                    self.disk_spindown_cache = {k: v for k, v in self.disk_spindown_cache.items() if k > too_old_window}

                # 使用短间隔睡眠以便快速响应停止信号
                for _ in range(10):
                    if self._stop_flag:
                        break
                    time.sleep(0.5)
            except Exception as e:
                self.logger.error(f"合并定时器工作线程出错: {e}", exc_info=True)
                if self._stop_flag:
                    break
    
    def _send_merged_disk_event(
        self, event_type: str, event_list: List[Dict], time_window: int = 0
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """发送合并的磁盘事件。event_list 为磁盘信息列表，每项含 disk/model/serial 等字段。
        返回 (是否至少一渠道成功, 各渠道结果列表)，与 send_notification 一致供推送记录展示。"""
        if not event_list:
            return False, []

        # 创建合并事件数据
        merged_data = {
            'merged_disks': event_list,
            'count': len(event_list),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        # 与普通通知一致走 _build_message，才能应用极简推送等统一逻辑
        ts = merged_data.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = self._build_message(event_type, merged_data, ts, "")

        results: List[bool] = []
        channel_results: List[Dict[str, Any]] = []
        if self.wechat_webhook_url:
            ok, cr = self._send_to_wechat(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("合并事件-企业微信: %s", cr)
        if self.dingtalk_webhook_url:
            ok, cr = self._send_to_dingtalk(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("合并事件-钉钉: %s", cr)
        if self.feishu_webhook_url:
            ok, cr = self._send_to_feishu(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("合并事件-飞书: %s", cr)
        if self.bark_url:
            ok, cr = self._send_to_bark(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("合并事件-Bark: %s", cr)
        if self.pushplus_params:
            ok, cr = self._send_to_pushplus(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("合并事件-PushPlus: %s", cr)
        if self.magic_push_params:
            ok, cr = self._send_to_magic_push(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("合并事件-魔法推送: %s", cr)
        if self.smtp_params:
            ok, cr = self._send_to_smtp(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("合并事件-SMTP邮件: %s", cr)
        if results and any(results):
            self._record_send_result(True)
            self.logger.info(f"合并事件发送成功: {event_type}, 数量: {len(event_list)}")
            return True, channel_results
        self._record_send_result(False)
        self.logger.warning(f"合并事件发送失败: {event_type}, 数量: {len(event_list)}")
        return False, channel_results
    
    def send_notification(self, 
                         event_type: str,
                         event_data: Dict[str, Any],
                         raw_log: str,
                         timestamp: str):
        """
        发送通知
        
        Args:
            event_type: 事件类型
            event_data: 事件数据
            raw_log: 原始日志
            timestamp: 时间戳
            
        Returns:
            (success, channel_results): 是否发送成功；channel_results 为 [{"channel": "企业微信", "success": True}, ...]
        """
        # 特殊处理磁盘事件的合并
        if event_type in ['DiskWakeup', 'DiskSpindown']:
            merged_disks = event_data.get('merged_disks') if isinstance(event_data.get('merged_disks'), list) else None
            if merged_disks:
                success, crs = self._send_merged_disk_event(event_type, merged_disks, 0)
                return success, crs
            ok = self._handle_disk_event(event_type, event_data, raw_log, timestamp)
            return ok, []
        
        # 生成事件指纹（用于去重）
        event_fingerprint = self._generate_fingerprint(event_type, event_data)
        
        # 检查去重
        if self._is_duplicate(event_fingerprint):
            self.logger.debug(f"跳过重复事件: {event_type}")
            return False, []
        
        # 构建消息
        message = self._build_message(event_type, event_data, timestamp, raw_log)
        
        results: List[bool] = []
        channel_results: List[Dict[str, Any]] = []
        
        if self.wechat_webhook_url:
            ok, cr = self._send_to_wechat(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("企业微信通知发送结果: %s", cr)
        if self.dingtalk_webhook_url:
            ok, cr = self._send_to_dingtalk(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("钉钉通知发送结果: %s", cr)
        if self.feishu_webhook_url:
            ok, cr = self._send_to_feishu(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("飞书通知发送结果: %s", cr)
        if self.bark_url:
            bark_message = self._build_bark_message(event_type, event_data, timestamp, raw_log)
            ok, cr = self._send_to_bark(bark_message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("Bark通知发送结果: %s", cr)
        if self.pushplus_params:
            pushplus_message = self._build_bark_message(event_type, event_data, timestamp, raw_log)
            ok, cr = self._send_to_pushplus(pushplus_message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("PushPlus通知发送结果: %s", cr)
        if self.magic_push_params:
            magic_message = self._build_bark_message(event_type, event_data, timestamp, raw_log)
            ok, cr = self._send_to_magic_push(magic_message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("魔法推送通知发送结果: %s", cr)
        if self.smtp_params:
            ok, cr = self._send_to_smtp(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("SMTP邮件通知发送结果: %s", cr)
        
        if results and any(results):  # 至少一个平台发送成功
            self.sent_events[event_fingerprint] = time.time()
            self._record_send_result(True)
            self.logger.info(f"通知发送成功: {event_type}")
            return True, channel_results
        else:
            self._record_send_result(False)
            self.logger.warning(f"所有通知发送失败: {event_type}")
            return False, channel_results

    def build_history_summary(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        raw_log: str,
        timestamp: str,
    ) -> str:
        """
        用于「推送记录」列表的摘要：尽量还原实际推送文案，但去掉表情/emoji，并压成单行。
        """
        try:
            msg = self._build_message(event_type, event_data, timestamp, raw_log)
            title = self._strip_emoji(msg.title or "")
            content = self._strip_emoji(msg.content or "")
            merged = (title + "\n" + content).strip()
            merged = re.sub(r"[ \t]+", " ", merged)
            merged = re.sub(r"\n{3,}", "\n\n", merged)
            # 表格里更易读：压成单行
            merged = merged.replace("\r\n", "\n").replace("\r", "\n")
            merged = " / ".join([p.strip() for p in merged.split("\n") if p.strip()])
            return merged[:500]
        except Exception:
            # 回退：至少保证有事件类型
            return str(event_type)[:200]

    def _strip_emoji(self, text: str) -> str:
        if not text:
            return ""
        return self._EMOJI_RE.sub("", text).strip()

    def _strip_body_emojis(self, text: str) -> str:
        """正文去掉符号/表情类字符（标题单独构建，不在此处理）。"""
        if not text:
            return ""
        out_lines = []
        for line in text.split("\n"):
            cleaned = self._EMOJI_RE.sub("", line)
            if cleaned.startswith("\t"):
                out_lines.append("\t" + cleaned[1:].lstrip())
            else:
                out_lines.append(cleaned.lstrip())
        return "\n".join(out_lines)
    
    def _handle_disk_event(self, event_type: str, event_data: Dict[str, Any], raw_log: str, timestamp: str) -> bool:
        """处理磁盘事件，将其添加到合并缓存中"""
        # 获取当前时间窗口
        current_time = time.time()
        current_window = int(current_time / self.merge_window)

        # 使用锁保护缓存访问，防止竞态条件
        with self._cache_lock:
            # 将事件数据添加到对应类型的缓存中
            if event_type == 'DiskWakeup':
                if current_window not in self.disk_wakeup_cache:
                    self.disk_wakeup_cache[current_window] = []
                self.disk_wakeup_cache[current_window].append(event_data.copy())  # 复制数据以避免后续修改影响
            elif event_type == 'DiskSpindown':
                if current_window not in self.disk_spindown_cache:
                    self.disk_spindown_cache[current_window] = []
                self.disk_spindown_cache[current_window].append(event_data.copy())

        # 返回True表示事件已加入合并队列
        self.logger.debug(f"磁盘事件已加入合并队列: {event_type} -> 窗口 {current_window}")
        return True
    
    def _iter_urls(self, raw: str):
        """将配置的URL拆分为列表，支持使用 '|' 分隔配置多个地址。"""
        if not raw:
            return []
        return [u.strip() for u in str(raw).split('|') if u.strip()]

    def _channel_result(self, channel_name: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """从多次请求结果聚合成一条渠道结果（多 URL 取首个成功或最后一条）。"""
        if not results:
            return {"channel": channel_name, "success": False, "response": None, "error": "未请求"}
        ok = next((r for r in results if r.get("success")), None)
        last = results[-1]
        if ok:
            return {"channel": channel_name, "success": True, "response": ok.get("response"), "error": None}
        return {
            "channel": channel_name,
            "success": False,
            "response": last.get("response"),
            "error": last.get("error") or "请求失败",
        }

    def _send_to_wechat(self, message: MultiPlatformMessage) -> tuple:
        """发送到企业微信。返回 (是否有任一成功, 渠道结果 dict)。"""
        payload = message.to_wechat_format()
        urls = self._iter_urls(self.wechat_webhook_url)
        results = [self.connection_pool.post(url, payload) for url in urls]
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("企业微信", results)

    def _send_to_dingtalk(self, message: MultiPlatformMessage) -> tuple:
        """发送到钉钉。返回 (是否有任一成功, 渠道结果 dict)。"""
        payload = message.to_dingtalk_format()
        urls = self._iter_urls(self.dingtalk_webhook_url)
        results = [self.connection_pool.post(url, payload) for url in urls]
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("钉钉", results)

    def _send_to_feishu(self, message: MultiPlatformMessage) -> tuple:
        """发送到飞书。返回 (是否有任一成功, 渠道结果 dict)。"""
        payload = message.to_feishu_format()
        urls = self._iter_urls(self.feishu_webhook_url)
        results = [self.connection_pool.post(url, payload) for url in urls]
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("飞书", results)

    def _bark_key_from_url(self, raw_url: str) -> str:
        """从 Bark URL 中提取 key（路径最后一段）。"""
        return raw_url.rstrip('/').rsplit('/', 1)[-1] if '/' in raw_url else raw_url

    def _bark_base_url(self, raw_url: str) -> str:
        """从 Bark URL 中提取 base URL（scheme + host）。"""
        parts = urllib.parse.urlsplit(raw_url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else "https://api.day.app"

    def _send_to_bark(self, message: MultiPlatformMessage) -> tuple:
        """发送到Bark（POST JSON，避免 URL 编码问题）。"""
        urls = self._iter_urls(self.bark_url)
        if not urls:
            return False, {"channel": "Bark", "success": False, "response": None, "error": "未配置"}
        results = []
        for raw_url in urls:
            push_url = f"{self._bark_base_url(raw_url)}/push"
            payload = {
                "device_key": self._bark_key_from_url(raw_url),
                "title": message.title,
                "body": message.content,
            }
            if self.bark_icon:
                payload["icon"] = self.bark_icon
            results.append(self.connection_pool.post(push_url, payload))
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("Bark", results)

    def _send_to_pushplus(self, message: MultiPlatformMessage) -> tuple:
        """发送到 PushPlus。返回 (是否有任一成功, 渠道结果 dict)。"""
        param_list = self._iter_urls(self.pushplus_params)
        if not param_list:
            return False, {"channel": "PushPlus", "success": False, "response": None, "error": "未配置"}
        results = []
        for param_str in param_list:
            try:
                payload = json.loads(param_str)
                if not isinstance(payload, dict) or 'token' not in payload:
                    continue
                user_title = (payload.get('title') or '').strip()
                if user_title == '{title}':
                    final_title = message.title
                    final_content = message.content
                else:
                    final_title = user_title or message.title
                    final_content = message.merged_plain_text(blank_line_between=True)
                payload['title'] = final_title
                payload['content'] = final_content
                results.append(self.connection_pool.post(PUSHPLUS_URL, payload))
            except json.JSONDecodeError:
                results.append({"success": False, "response": None, "error": "参数 JSON 解析失败"})
            except Exception as e:
                results.append({"success": False, "response": None, "error": str(e)[:80]})
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("PushPlus", results)

    def _send_to_magic_push(self, message: MultiPlatformMessage) -> tuple:
        """魔法推送：POST {base_url}/api/push，Bearer token，JSON body title/content。"""
        param_list = self._iter_urls(self.magic_push_params)
        if not param_list:
            return False, {"channel": "魔法推送", "success": False, "response": None, "error": "未配置"}
        results = []
        for param_str in param_list:
            try:
                cfg = json.loads(param_str)
                if not isinstance(cfg, dict):
                    results.append({"success": False, "response": None, "error": "参数须为 JSON 对象"})
                    continue
                base = (cfg.get("base_url") or "").strip().rstrip("/")
                token = (cfg.get("token") or "").strip()
                if not base or not token:
                    results.append({"success": False, "response": None, "error": "缺少 base_url 或 token"})
                    continue
                if not base.startswith("http"):
                    results.append({"success": False, "response": None, "error": "base_url 须为 http(s) 地址"})
                    continue
                url = f"{base}/api/push"
                custom_title = (cfg.get("title") or "").strip()
                if custom_title:
                    # 用户显式填写标题时：同样应用事件标题前缀（与事件标题一致的体验）
                    # 若用户已包含前缀则不重复追加。
                    api_title = custom_title.replace("飞牛NAS", self.title_prefix or "飞牛NAS")
                    if self.title_prefix and self.title_prefix not in api_title:
                        api_title = f"{self.title_prefix}-{api_title}"
                else:
                    api_title = message.title or ""
                api_content = (message.content or "").strip()
                if not api_content and message.title:
                    api_content = (message.title or "").strip()
                payload = {"title": api_title, "content": api_content}
                hdrs = {"Authorization": f"Bearer {token}"}
                results.append(self.connection_pool.post(url, payload, headers=hdrs))
            except json.JSONDecodeError:
                results.append({"success": False, "response": None, "error": "参数 JSON 解析失败"})
            except Exception as e:
                results.append({"success": False, "response": None, "error": str(e)[:80]})
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("魔法推送", results)

    def _send_to_smtp(self, message: MultiPlatformMessage) -> tuple:
        """发送到 SMTP 邮件。返回 (是否有任一成功, 渠道结果 dict)。"""
        param_list = self._iter_urls(self.smtp_params)
        if not param_list:
            return False, {"channel": "SMTP邮件", "success": False, "response": None, "error": "未配置"}
        results = []
        for param_str in param_list:
            try:
                cfg = json.loads(param_str)
                if not isinstance(cfg, dict):
                    results.append({"success": False, "response": None, "error": "参数须为 JSON 对象"})
                    continue
                server = (cfg.get("server") or "").strip()
                username = (cfg.get("username") or "").strip()
                password = cfg.get("password") or ""
                to_raw = (cfg.get("to") or "").strip()
                from_addr = (cfg.get("from") or username).strip()
                if not server or not username or not password or not to_raw:
                    results.append({"success": False, "response": None, "error": "缺少 server/username/password/to"})
                    continue

                recipients = [x.strip() for x in to_raw.split(",") if x.strip()]
                if not recipients:
                    results.append({"success": False, "response": None, "error": "收件人不能为空"})
                    continue

                try:
                    port = int(cfg.get("port", 465))
                except (TypeError, ValueError):
                    results.append({"success": False, "response": None, "error": "端口必须是整数"})
                    continue
                em = EmailMessage()
                em["Subject"] = (message.title or "").strip() or "系统通知"
                if from_addr and "@" not in from_addr:
                    # 用户填写昵称时，按 RFC 组装为“昵称 <登录邮箱>”。
                    em["From"] = formataddr((str(Header(from_addr, "utf-8")), username))
                else:
                    em["From"] = from_addr or username
                em["To"] = ", ".join(recipients)
                body = (message.content or "").strip()
                if not body and message.title:
                    body = (message.title or "").strip()
                em.set_content(body or "系统通知")

                # 按端口自动判定加密方式：465 用 SMTP_SSL，其它端口走 STARTTLS。
                if port == 465:
                    with smtplib.SMTP_SSL(server, port, timeout=15, context=ssl.create_default_context()) as client:
                        client.login(username, password)
                        # 兼容多数服务商限制：信封发件人必须是登录账号。
                        client.send_message(em, from_addr=username, to_addrs=recipients)
                else:
                    with smtplib.SMTP(server, port, timeout=15) as client:
                        client.ehlo()
                        client.starttls(context=ssl.create_default_context())
                        client.ehlo()
                        client.login(username, password)
                        # 兼容多数服务商限制：信封发件人必须是登录账号。
                        client.send_message(em, from_addr=username, to_addrs=recipients)

                results.append({
                    "success": True,
                    "response": {"to": recipients, "port": port, "secure_mode": ("ssl" if port == 465 else "starttls")},
                    "error": None
                })
            except json.JSONDecodeError:
                results.append({"success": False, "response": None, "error": "参数 JSON 解析失败"})
            except Exception as e:
                results.append({"success": False, "response": None, "error": str(e)[:120]})
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("SMTP邮件", results)
    
    def _generate_fingerprint(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """生成事件指纹（用于去重）"""
        # 根据不同事件类型生成不同的指纹
        
        if event_type == 'FoundDisk':
            # 硬盘发现：按设备名和时间（小时）去重
            name = event_data.get('name', 'unknown')
            hour_window = int(time.time() / 3600)
            key = f"disk_{name}_{hour_window}"
        
        elif event_type == 'APP_CRASH':
            # 应用崩溃：按应用名和时间（5分钟）去重
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"crash_{app_name}_{minute_window}"
        
        elif event_type == 'APP_UPDATE_FAILED':
            # 应用更新失败：按应用名和时间（5分钟）去重
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"update_failed_{app_name}_{minute_window}"
        elif event_type in ['APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION', 'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE']:
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{app_name}_{minute_window}"
        elif event_type in ['APP_STARTED', 'APP_STOPPED', 'APP_UPDATED', 'APP_INSTALLED', 'APP_AUTO_STARTED', 'APP_UNINSTALLED']:
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{app_name}_{minute_window}"
        elif event_type in [
            'CPU_USAGE_ALARM', 'CPU_USAGE_RESTORED',
            'MEMORY_USAGE_ALARM', 'MEMORY_USAGE_RESTORED',
            'CPU_TEMPERATURE_ALARM',
        ]:
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{minute_window}"
        
        elif event_type == 'UPS_ONBATT_LOWBATT':
            # UPS切换到电池供电：按时间（5分钟）去重
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"ups_battery_{minute_window}"
        
        elif event_type == 'UPS_ONLINE':
            # UPS切换到市电供电：按时间（5分钟）去重
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"ups_online_{minute_window}"
        elif event_type in ['UPS_ENABLE', 'UPS_DISABLE']:
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{minute_window}"
        elif event_type in ['SSH_INVALID_USER', 'SSH_AUTH_FAILED', 'SSH_LOGIN_SUCCESS', 'SSH_DISCONNECTED']:
            # SSH事件：按用户/IP和时间（分钟）去重
            user = event_data.get('user', 'unknown')
            ip = event_data.get('IP', 'unknown')
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{user}_{ip}_{minute_window}"
        elif event_type == 'DISK_IO_ERR':
            data = event_data.get('data', {})
            dev = data.get('DEV', data.get('dev', 'unknown'))
            minute_window = int(time.time() / 60)
            key = f"disk_io_err_{dev}_{minute_window}"
        elif event_type in {
            'ARCHIVING_SUCCESS', 'DeleteFile', 'MovetoTrashbin', 'SHARE_EVENTID_DEL', 'SHARE_EVENTID_PUT',
            'WEBDAV_ENABLED', 'WEBDAV_DISABLED', 'SAMBA_ENABLED', 'SAMBA_DISABLED',
            'DLNA_ENABLED', 'DLNA_DISABLED', 'FTP_ENABLED', 'FTP_DISABLED', 'NFS_ENABLED', 'NFS_DISABLED',
            'FW_ENABLE', 'FW_DISABLE', 'SECURITY_PORTCHANGED',
        }:
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{minute_window}"
        elif event_type == 'POLL_BATCH_SUMMARY':
            minute_window = int(time.time() / 60)
            count = int(event_data.get('count') or 0)
            by_type_raw = event_data.get('by_type')
            by_type: Dict[str, Any] = by_type_raw if isinstance(by_type_raw, dict) else {}
            type_count = len(by_type)
            # 避免同一分钟内“总数和类型数相同但内容不同”的批次被误去重
            by_type_sig = "|".join(f"{k}:{by_type[k]}" for k in sorted(by_type.keys()))
            items_raw = event_data.get('items')
            items: List[Dict[str, Any]] = items_raw if isinstance(items_raw, list) else []
            preview_sig_parts = []
            for it in items[:5]:
                if not isinstance(it, dict):
                    continue
                et = str(it.get('event_type') or '')
                ts = str(it.get('timestamp') or '')
                brief = str(it.get('brief') or '')[:30]
                preview_sig_parts.append(f"{et}@{ts}#{brief}")
            preview_sig = "|".join(preview_sig_parts)
            key = f"{event_type}_{minute_window}_{count}_{type_count}_{by_type_sig}_{preview_sig}"
        else:
            # 登录/退出：按用户、IP和时间（分钟）去重
            user = event_data.get('user', 'unknown')
            ip = event_data.get('IP', 'unknown')
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{user}_{ip}_{minute_window}"
        
        # 使用MD5生成固定长度的指纹
        return hashlib.md5(key.encode()).hexdigest()
    
    def _is_duplicate(self, fingerprint: str) -> bool:
        """检查是否为重复事件"""
        if fingerprint in self.sent_events:
            last_time = self.sent_events[fingerprint]
            if time.time() - last_time < self.dedup_window:
                return True
            else:
                # 超过去重窗口，删除旧记录
                del self.sent_events[fingerprint]
        
        return False
    
    def _build_message(self, event_type: str, event_data: Dict[str, Any], 
                      timestamp: str, raw_log: str) -> MultiPlatformMessage:
        """构建多平台消息"""
        # 轮询汇总类消息优先级高于极简：始终按完整文案推送
        if self.minimal_push_enabled and event_type != "POLL_BATCH_SUMMARY":
            one_line = self._build_minimal_one_line(event_type, event_data)
            return MultiPlatformMessage(title=one_line, content="")

        title_event_type = event_type
        # 批量汇总若仅包含单一事件类型，则标题回退为该事件原始标题
        if event_type == 'POLL_BATCH_SUMMARY':
            try:
                total = int(event_data.get('count') or 0)
            except Exception:
                total = 0
            by_type_raw = event_data.get('by_type')
            by_type: Dict[str, Any] = by_type_raw if isinstance(by_type_raw, dict) else {}
            if total >= 1 and len(by_type) == 1:
                title_event_type = str(next(iter(by_type.keys())))

        title = self._with_title_prefix(
            self.EVENT_TITLES.get(title_event_type, self._fallback_event_title(title_event_type))
        )
        content = self._build_content(event_type, event_data, timestamp, raw_log)
        
        return MultiPlatformMessage(title=title, content=content)

    def _build_minimal_one_line(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """极简推送：仅一行核心信息。"""
        if event_type in ("DiskWakeup", "DiskSpindown"):
            action = "磁盘唤醒" if event_type == "DiskWakeup" else "磁盘休眠"
            merged = event_data.get("merged_disks")
            if isinstance(merged, list) and merged:
                names: List[str] = []
                for item in merged:
                    if not isinstance(item, dict):
                        continue
                    d = (item.get("disk") or "").strip()
                    if d:
                        names.append(d)
                n = len(merged)
                if names:
                    head = "、".join(names[:4])
                    if n > len(names) or n > 4:
                        body = f"{head}{action}等共{n}块"
                    elif n > 1:
                        body = f"{head}{action}（{n}块）"
                    else:
                        body = f"{head}{action}"
                else:
                    body = f"{action}（{n}块）"
                prefix = (self.title_prefix or "").strip()
                if prefix:
                    return f"{prefix}-{body}"
                return body
            disk = (event_data.get("disk") or "").strip()
            body = f"{disk}{action}" if disk else action
            prefix = (self.title_prefix or "").strip()
            if prefix:
                return f"{prefix}-{body}"
            return body

        user = self._display_user(
            event_data.get("user")
            or event_data.get("user_guid")
            or event_data.get("uname")
            or ""
        )
        app_name = (event_data.get("app_name") or "").strip()
        task_name = (event_data.get("task_name") or "").strip()
        title = (event_data.get("title") or event_data.get("name") or "").strip()

        action_map = {
            "LoginSucc": "登录成功",
            "LoginSucc2FA1": "二次验证登录",
            "LoginFail": "登录失败",
            "Logout": "退出登录",
            "MEDIA_LOGIN_SUCC": "影视库登录成功",
            "MEDIA_LOGOUT": "影视库退出登录",
            "MEDIA_USER_CREATED": "影视库创建用户",
            "TRIM_RESOURCE_ADDED": "影视资源入库",
            "TRIM_SCRAPE_SUCCESS": "影视刮削完成",
            "APP_CRASH": "应用崩溃",
            "APP_UPDATE_FAILED": "应用更新失败",
            "BACKUP_TASK_SUCCESS": "备份任务完成",
            "BACKUP_TASK_FAILED": "备份任务失败",
            "BACKUP_TASK_PARTIAL_SUCCESS": "备份任务部分成功",
            "SCHEDULER_TASK_SUCCESS": "任务计划完成",
            "SCHEDULER_TASK_FAILED": "任务计划失败",
            "SCHEDULER_TASK_CONDITION_FAILED": "任务计划条件不满足",
        }
        action = action_map.get(event_type)
        if not action:
            fallback_title = self.EVENT_TITLES.get(event_type, event_type)
            action = self._strip_body_emojis(fallback_title)
            action = action.replace("飞牛NAS-", "").replace("飞牛NAS", "").strip(" -")

        subject = user or app_name or task_name or title
        prefix = (self.title_prefix or "").strip()
        body = f"{subject}{action}" if subject else action
        if prefix:
            return f"{prefix}-{body}"
        return body
    
    def _build_content(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        timestamp: str,
        raw_log: str,
        *,
        for_batch_inner: bool = False,
    ) -> str:
        """构建消息内容。for_batch_inner=True 时用于多事件汇总内的子块：无首行时间、无 EVENT_NOTES。"""
        # 批量汇总：多条时整段在入口处理（仅一条时间行 + 子块无时间/无备注）
        if event_type == 'POLL_BATCH_SUMMARY':
            single_event = self._extract_single_event_from_batch(event_data)
            if single_event:
                original_type, original_data, _original_ts, original_raw = single_event
                return self._build_content(
                    original_type,
                    original_data,
                    timestamp,
                    original_raw,
                    for_batch_inner=False,
                )
            body = self._build_poll_batch_summary_content(event_data)
            content = f"{timestamp}\n{body}" if body else timestamp
            return self._strip_body_emojis(content)

        fragments: List[str] = []
        if not for_batch_inner:
            fragments.append(str(timestamp))

        detail = ""
        if event_type in ['LoginSucc', 'LoginSucc2FA1', 'LoginFail', 'Logout']:
            detail = self._build_login_content(event_data)
        elif event_type in ['SSH_INVALID_USER', 'SSH_AUTH_FAILED', 'SSH_LOGIN_SUCCESS', 'SSH_DISCONNECTED']:
            detail = self._build_ssh_content(event_type, event_data)
        elif event_type in ('FoundDisk', 'InsertDisk', 'EjectDisk'):
            detail = self._build_disk_content(event_data)
        elif event_type == 'StorageBroken':
            detail = self._build_storage_broken_content(event_data)
        elif event_type == 'APP_CRASH':
            detail = self._build_app_crash_content(event_data)
        elif event_type in ('APP_STARTED', 'APP_STOPPED', 'APP_UPDATED', 'APP_INSTALLED', 'APP_AUTO_STARTED', 'APP_UNINSTALLED'):
            detail = self._build_app_lifecycle_content(event_data)
        elif event_type == 'APP_UPDATE_FAILED':
            detail = self._build_app_update_failed_content(event_data)
        elif event_type == 'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION':
            detail = self._build_app_start_failed_content(event_data)
        elif event_type == 'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE':
            detail = self._build_app_auto_start_failed_content(event_data)
        elif event_type == 'CPU_USAGE_ALARM':
            detail = self._build_cpu_usage_alarm_content(event_data)
        elif event_type == 'CPU_USAGE_RESTORED':
            detail = self._build_cpu_usage_restored_content(event_data)
        elif event_type == 'MEMORY_USAGE_ALARM':
            detail = self._build_memory_usage_alarm_content(event_data)
        elif event_type == 'MEMORY_USAGE_RESTORED':
            detail = self._build_memory_usage_restored_content(event_data)
        elif event_type == 'CPU_TEMPERATURE_ALARM':
            detail = self._build_cpu_temperature_alarm_content(event_data)
        elif event_type == 'UPS_ONBATT':
            detail = self._build_ups_onbatt_content(event_data)
        elif event_type == 'UPS_ONBATT_LOWBATT':
            detail = self._build_ups_onbatt_lowbatt_content(event_data)
        elif event_type == 'UPS_ONLINE':
            detail = self._build_ups_online_content(event_data)
        elif event_type == 'UPS_ENABLE':
            detail = self._build_ups_enable_disable_content('UPS_ENABLE', event_data)
        elif event_type == 'UPS_DISABLE':
            detail = self._build_ups_enable_disable_content('UPS_DISABLE', event_data)
        elif event_type == 'DiskWakeup':
            if 'merged_disks' in event_data:
                detail = self._build_merged_disk_wakeup_content(event_data)
            else:
                detail = self._build_disk_wakeup_content(event_data)
        elif event_type == 'DiskSpindown':
            if 'merged_disks' in event_data:
                detail = self._build_merged_disk_spindown_content(event_data)
            else:
                detail = self._build_disk_spindown_content(event_data)
        elif event_type == 'DISK_IO_ERR':
            detail = self._build_disk_io_err_content(event_data)
        elif event_type in {'BACKUP_TASK_SUCCESS', 'BACKUP_TASK_FAILED', 'BACKUP_TASK_PARTIAL_SUCCESS'}:
            detail = self._build_backup_task_content(event_data)
        elif event_type in {'SCHEDULER_TASK_SUCCESS', 'SCHEDULER_TASK_FAILED', 'SCHEDULER_TASK_CONDITION_FAILED'}:
            detail = self._build_scheduler_task_content(event_data)
        elif event_type in {'SHUTDOWN_VM', 'STATUS_RUNNING_VM', 'DESTROY_VM'} or ("VM" in str(event_type).upper()):
            detail = self._build_vm_content(event_data)
        elif event_type in {
            'ARCHIVING_SUCCESS', 'DeleteFile', 'MovetoTrashbin', 'SHARE_EVENTID_DEL', 'SHARE_EVENTID_PUT',
            'WEBDAV_ENABLED', 'WEBDAV_DISABLED', 'SAMBA_ENABLED', 'SAMBA_DISABLED',
            'DLNA_ENABLED', 'DLNA_DISABLED', 'FTP_ENABLED', 'FTP_DISABLED', 'NFS_ENABLED', 'NFS_DISABLED',
            'FW_ENABLE', 'FW_DISABLE', 'SECURITY_PORTCHANGED',
        }:
            detail = self._build_simple_content(event_data)
        elif event_type in ('MEDIA_LOGIN_SUCC', 'MEDIA_LOGOUT'):
            detail = self._build_media_login_content(event_data)
        elif event_type in ('MEDIA_USER_CREATED', 'TRIM_RESOURCE_ADDED', 'TRIM_SCRAPE_SUCCESS'):
            detail = self._build_trim_library_content(event_type, event_data)
        elif event_type in (
            'PHOTO_SHARE_CREATED',
            'PHOTO_SHARE_EXPIRED',
            'PHOTO_DEVICE_REGISTERED',
            'FACE_RECOGNITION_UPDATED',
        ):
            detail = self._build_photo_album_content(event_type, event_data)

        if detail:
            fragments.append(detail.rstrip('\n'))

        content = "\n".join(fragments)
        content = self._strip_body_emojis(content)

        # 备注行保留 EVENT_NOTES 中的图标（仅单事件正文；批量子块 for_batch_inner 不追加备注）
        note = self.EVENT_NOTES.get(event_type, '')
        if note and not for_batch_inner:
            content = content.rstrip('\n') + f"\n{note}"
        return content

    def _extract_single_event_from_batch(
        self, event_data: Dict[str, Any]
    ) -> Optional[Tuple[str, Dict[str, Any], str, str]]:
        """从批量事件中提取唯一的一条原始事件。"""
        try:
            total = int(event_data.get('count') or 0)
        except Exception:
            total = 0
        if total != 1:
            return None

        grouped_events_raw = event_data.get('grouped_events')
        grouped_events: Dict[str, Any] = grouped_events_raw if isinstance(grouped_events_raw, dict) else {}
        if len(grouped_events) != 1:
            return None

        original_type = str(next(iter(grouped_events.keys())))
        events_raw = grouped_events.get(original_type)
        events: List[Dict[str, Any]] = events_raw if isinstance(events_raw, list) else []
        if not events:
            return None

        first = events[0] if isinstance(events[0], dict) else {}
        ed_raw = first.get('event_data')
        original_data: Dict[str, Any] = ed_raw if isinstance(ed_raw, dict) else {}
        original_ts = str(first.get('timestamp') or '')
        original_raw = str(first.get('raw_log') or '')
        return original_type, original_data, original_ts, original_raw

    def _strip_batch_inner_title_prefix(self, title: str) -> str:
        """批量汇总内部类型标题去前缀（如“飞牛NAS-”）。"""
        result = title
        result = result.replace("飞牛NAS-", "", 1)
        custom_prefix = (self.title_prefix or "").strip()
        if custom_prefix:
            result = result.replace(f"{custom_prefix}-", "", 1)
        return result
    
    def _build_login_content(self, event_data: Dict[str, Any]) -> str:
        """构建登录相关事件内容"""
        content = ""

        user = self._display_user(event_data.get('user', ''))
        if user:
            content += f"👤 用户名: {user}\n"
        else:
            content += "👤 用户名: \n"
        
        ip = event_data.get('IP', '')
        if ip:
            content += f"📍 IP地址: {ip}\n"
        else:
            content += "📍 IP地址: \n"
        
        via = event_data.get('via', '')
        if via:
            content += f"🔑 认证方式: {via}\n"
        
        return content

    def _build_media_login_content(self, event_data: Dict[str, Any]) -> str:
        """影视库登录/退出内容（简洁版，减少图标）。"""
        lines: List[str] = []
        user = self._display_user(event_data.get('user', ''))
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        lines.append(f"用户名: {user}")
        lines.append(f"IP地址: {ip}")
        if via:
            lines.append(f"认证方式: {via}")
        return "\n".join(lines)

    def _display_user(self, user_value: Any) -> str:
        """将 user_guid 等标识转换为可读用户名（命中映射时）。"""
        raw = str(user_value or "").strip()
        if not raw:
            return ""
        # 从 trimmedia.db 的 user(guid, username) 自动映射
        db_name = self._lookup_username_from_db(raw)
        if db_name:
            return db_name
        return raw

    def _lookup_username_from_db(self, guid: str) -> str:
        """从媒体相关数据库自动读取 guid 对应可读用户名（带短时缓存）。"""
        if not guid:
            return ""
        db_paths = []
        if self.user_lookup_db_path and os.path.exists(self.user_lookup_db_path):
            db_paths.append(self.user_lookup_db_path)
        if self.activity_user_lookup_db_path and os.path.exists(self.activity_user_lookup_db_path):
            db_paths.append(self.activity_user_lookup_db_path)
        if not db_paths:
            return ""
        now = time.time()
        # 60 秒缓存，避免每条推送都查库
        if now - self._user_db_cache_loaded_at > 60:
            try:
                mapping = {}
                guid_cols = ["guid", "user_guid"]
                name_cols = ["username", "nickname", "name", "account", "user_name"]

                for db_path in db_paths:
                    conn = sqlite3.connect(
                        f"file:{db_path}?mode=ro&immutable=1",
                        uri=True,
                        timeout=3.0,
                    )
                    try:
                        tables = []
                        for (tname,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                            tn = str(tname or "").strip()
                            if tn and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tn):
                                tables.append(tn)

                        for table in tables:
                            cols = [
                                str(r[1] or "").strip()
                                for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()
                                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(r[1] or "").strip())
                            ]
                            guid_col = next((c for c in guid_cols if c in cols), "")
                            name_col = next((c for c in name_cols if c in cols), "")
                            if not guid_col or not name_col:
                                continue
                            cur = conn.execute(
                                f'SELECT "{guid_col}", "{name_col}" FROM "{table}" '
                                f'WHERE "{guid_col}" IS NOT NULL AND "{name_col}" IS NOT NULL'
                            )
                            for g, u in cur.fetchall():
                                sg = str(g or "").strip()
                                su = str(u or "").strip()
                                if sg and su and sg not in mapping:
                                    mapping[sg] = su
                    finally:
                        conn.close()

                self._user_db_cache = mapping
                self._user_db_cache_loaded_at = now
            except Exception:
                return ""
        return self._user_db_cache.get(guid, "")

    def _lookup_nas_uid_name_from_logger_db(self, nas_uid: Any) -> str:
        """从 logger_data.db3 的 log(uid, uname) 自动映射 NAS UID -> 昵称。"""
        try:
            uid = int(nas_uid) if nas_uid is not None and str(nas_uid).strip() != "" else None
        except (TypeError, ValueError):
            uid = None
        if uid is None or not self.logger_user_lookup_db_path:
            return ""
        if not os.path.exists(self.logger_user_lookup_db_path):
            return ""
        now = time.time()
        if now - self._nas_uid_name_cache_loaded_at > 60:
            try:
                conn = sqlite3.connect(
                    f"file:{self.logger_user_lookup_db_path}?mode=ro&immutable=1",
                    uri=True,
                    timeout=3.0,
                )
                cur = conn.execute(
                    "SELECT uid, uname FROM log WHERE uid IS NOT NULL AND uname IS NOT NULL "
                    "AND TRIM(uname) != '' ORDER BY id DESC LIMIT 20000"
                )
                mapping = {}
                for u, n in cur.fetchall():
                    su = str(u or "").strip()
                    sn = str(n or "").strip()
                    if su and sn and su not in mapping:
                        mapping[su] = sn
                conn.close()
                self._nas_uid_name_cache = mapping
                self._nas_uid_name_cache_loaded_at = now
            except Exception:
                return ""
        return self._nas_uid_name_cache.get(str(uid), "")

    def _album_nickname_from_map(self, nas_uid: Any, _photo_user_id: Any) -> str:
        """相册所有者/设备所属用户：自动从 logger_data.db3 反查用户名称。"""
        try:
            nuid = int(nas_uid) if nas_uid is not None and str(nas_uid).strip() != "" else None
        except (TypeError, ValueError):
            nuid = None
        # 自动从 logger_data.db3 反查 UID 的最近用户名
        auto_name = self._lookup_nas_uid_name_from_logger_db(nuid)
        if auto_name:
            return auto_name
        return ""

    def _append_album_owner_display_line(self, lines: List[str], event_data: Dict[str, Any]) -> None:
        """优先输出自动匹配到的用户名称；否则输出 UID/id 兜底信息。"""
        nick = self._album_nickname_from_map(
            event_data.get("owner_nas_uid"), event_data.get("owner_photo_user_id")
        )
        if nick:
            lines.append(f"用户: {nick}")
            return
        fb = (event_data.get("owner_label") or "").strip()
        if fb:
            lines.append(f"用户: {fb}")
    
    def _build_disk_content(self, event_data: Dict[str, Any]) -> str:
        """构建硬盘发现事件内容"""
        content = ""
        
        if name := event_data.get('name', ''):
            content += f"📛 设备名称: {name}\n"
        
        if model := event_data.get('model', ''):
            content += f"🔧 硬盘型号: {model}\n"
        
        if serial := event_data.get('serial', ''):
            content += f"🔢 序列号: {serial}\n"
        
        return content

    def _build_storage_broken_content(self, event_data: Dict[str, Any]) -> str:
        """存储空间损坏事件内容。"""
        content = ""
        vol = event_data.get('volume') or event_data.get('VOL') or event_data.get('vol')
        if vol not in (None, ""):
            content += f"📦 存储卷: {vol}\n"
        name = event_data.get('name', '')
        if name:
            content += f"📛 设备名称: {name}\n"
        model = event_data.get('model', '')
        if model:
            content += f"🔧 硬盘型号: {model}\n"
        serial = event_data.get('serial', '')
        if serial:
            content += f"🔢 序列号: {serial}\n"
        return content or "（无额外详情）"

    def _build_vm_content(self, event_data: Dict[str, Any]) -> str:
        """虚拟机事件内容：展示 VM_TITLE、USER_NAME 等（parameter 中 data）。"""
        content = ""
        data = event_data.get('data', {}) or {}
        vm_title = data.get('VM_TITLE', data.get('vm_title', ''))
        if vm_title:
            content += f"🖥️ 虚拟机: {vm_title}\n"
        user = event_data.get('user') or data.get('USER_NAME', data.get('user', ''))
        if user:
            content += f"👤 操作用户: {user}\n"
        from_src = event_data.get('from', '')
        if from_src:
            content += f"📦 来源: {from_src}\n"
        return content or "（无额外详情）"

    def _build_simple_content(self, event_data: Dict[str, Any]) -> str:
        """可选事件通用内容：展示操作用户、来源等（parameter 中 data.USER_NAME、from 等）。"""
        content = ""
        data = event_data.get('data', {}) or {}
        user = self._display_user(event_data.get('user') or data.get('USER_NAME', data.get('user', '')))
        if user:
            content += f"👤 操作用户: {user}\n"
        from_src = event_data.get('from', '')
        if from_src:
            content += f"📦 来源: {from_src}\n"
        for key in ('path', 'PATH', 'name', 'share_name', 'SHARE_NAME'):
            val = event_data.get(key) or data.get(key)
            if val:
                content += f"📁 {key}: {val}\n"
                break
        return content or "（无额外详情）"

    def _build_trim_library_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """影视库事件正文：仅保留用户可读信息。"""
        lines: List[str] = []
        data_raw = event_data.get("data")
        data: Dict[str, Any] = data_raw if isinstance(data_raw, dict) else {}
        title = (event_data.get("title") or data.get("title") or "").strip()
        item_type = (event_data.get("item_type") or data.get("item_type") or "").strip()
        season_number = event_data.get("season_number") if event_data.get("season_number") is not None else data.get("season_number")
        episode_number = event_data.get("episode_number") if event_data.get("episode_number") is not None else data.get("episode_number")
        path = (event_data.get("path") or data.get("path") or "").strip()
        app_name = (event_data.get("app_name") or data.get("app_name") or "").strip()
        user = self._display_user((event_data.get("user") or data.get("USER_NAME") or data.get("user") or "").strip())
        ip = (event_data.get("IP") or data.get("ip") or "").strip()
        runtime = event_data.get("runtime")
        if runtime in (None, ""):
            runtime = data.get("runtime")
        release_date = (event_data.get("release_date") or data.get("release_date") or "").strip()
        overview = (event_data.get("overview") or data.get("overview") or "").strip()

        # # 标题行：若上游 message 含技术内容则忽略，改为更友好的动作描述
        # if event_type == "TRIM_RESOURCE_ADDED":
        #     lines.append("影视库资源入库")
        # elif event_type == "TRIM_SCRAPE_SUCCESS":
        #     lines.append("影视库刮削成功")
        # elif event_type == "MEDIA_USER_CREATED":
        #     lines.append("影视库创建用户")

        if title:
            lines.append(f"资源名称: {title}")
        if item_type:
            type_map = {
                "Episode": "单集",
                "Season": "季",
                "TV": "剧集",
                "Movie": "电影",
            }
            lines.append(f"资源类型: {type_map.get(item_type, item_type)}")
        if event_type in ("TRIM_SCRAPE_SUCCESS", "TRIM_RESOURCE_ADDED"):
            if season_number not in (None, "", 0) and episode_number not in (None, "", 0):
                lines.append(f"集: 第{season_number}季{episode_number}集")
            elif episode_number not in (None, "", 0):
                lines.append(f"集: 第{episode_number}集")
            elif season_number not in (None, "", 0):
                lines.append(f"季: 第{season_number}季")
        else:
            if season_number not in (None, "", 0):
                lines.append(f"季: {season_number}")
            if episode_number not in (None, "", 0):
                lines.append(f"集: {episode_number}")
        if event_type == "TRIM_SCRAPE_SUCCESS":
            try:
                rt = int(runtime or 0)
            except (TypeError, ValueError):
                rt = 0
            if rt > 0:
                lines.append(f"影片时长: {rt}分钟")
            if release_date:
                lines.append(f"上映时间: {release_date}")
            if overview:
                lines.append(f"影片介绍: {overview[:120]}{'...' if len(overview) > 120 else ''}")
        if path:
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            lines.append(f"文件: {filename}")
        if app_name and event_type in ("MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT", "MEDIA_USER_CREATED"):
            lines.append(f"应用: {app_name}")
        if user:
            if event_type == "MEDIA_USER_CREATED":
                lines.append(f"操作人: {user}")
            else:
                lines.append(f"用户: {user}")
        if ip:
            lines.append(f"IP: {ip}")

        if event_type == "MEDIA_USER_CREATED":
            # 创建用户仅展示常见可读字段，不暴露原始技术字段
            new_user = (
                data.get("NEW_USER")
                or data.get("new_user")
                or data.get("username")
                or data.get("USER_NAME")
                or ""
            )
            role = data.get("ROLE") or data.get("role") or ""
            email = data.get("EMAIL") or data.get("email") or ""
            if str(new_user).strip():
                lines.append(f"新建用户: {self._display_user(new_user)}")
            if str(role).strip():
                lines.append(f"角色: {role}")
            if str(email).strip():
                lines.append(f"邮箱: {email}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_photo_album_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """相册 photo.db 事件正文：与影视库一致，仅字段行（总述与收尾见标题 / EVENT_NOTES）。"""
        lines: List[str] = []
        if event_type == "PHOTO_SHARE_CREATED":
            if event_data.get("share_name"):
                lines.append(f"分享名: {event_data['share_name']}")
            if event_data.get("share_id"):
                lines.append(f"分享ID: {event_data['share_id']}")
            self._append_album_owner_display_line(lines, event_data)
            if event_data.get("valid_to_str"):
                lines.append(f"过期时间: {event_data['valid_to_str']}")
        elif event_type == "PHOTO_SHARE_EXPIRED":
            if event_data.get("share_name"):
                lines.append(f"分享名: {event_data['share_name']}")
            if event_data.get("share_id"):
                lines.append(f"分享ID: {event_data['share_id']}")
            if event_data.get("expired_at_str"):
                lines.append(f"过期时间: {event_data['expired_at_str']}")
            self._append_album_owner_display_line(lines, event_data)
        elif event_type == "PHOTO_DEVICE_REGISTERED":
            if event_data.get("device_display"):
                lines.append(f"设备名称: {event_data['device_display']}")
            if event_data.get("device_id"):
                lines.append(f"设备ID: {event_data['device_id']}")
            self._append_album_owner_display_line(lines, event_data)
        elif event_type == "FACE_RECOGNITION_UPDATED":
            if event_data.get("record_count") not in (None, "", 0):
                lines.append(f"本轮识别记录: {event_data['record_count']}")
            if event_data.get("photo_id") is not None:
                lines.append(f"照片: {event_data['photo_id']}")
            if event_data.get("user_photo_id") is not None:
                lines.append(f"条目: {event_data['user_photo_id']}")
            if not lines and event_data.get("task_log_id") is not None:
                lines.append(f"识别记录: {event_data['task_log_id']}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_backup_task_content(self, event_data: Dict[str, Any]) -> str:
        """构建备份任务事件内容。"""
        def _fmt_ts(v: Any) -> str:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return ""
            if iv > 10_000_000_000:
                iv = int(iv / 1000)
            try:
                return datetime.fromtimestamp(iv).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, ValueError):
                return ""

        def _fmt_size(v: Any) -> str:
            try:
                n = float(v)
            except (TypeError, ValueError):
                return str(v) if v not in (None, "") else ""
            units = ["B", "KB", "MB", "GB", "TB"]
            i = 0
            while n >= 1024 and i < len(units) - 1:
                n /= 1024
                i += 1
            return f"{n:.2f} {units[i]}" if i > 0 else f"{int(n)} {units[i]}"

        task_name = str(event_data.get("task_name") or "")
        operation_id = event_data.get("operation_id")
        storage_name = str(event_data.get("storage_name") or "")
        storage_addr = str(event_data.get("storage_address") or "")
        target_path = str(event_data.get("target_path") or "")
        source_paths = str(event_data.get("source_paths") or "")
        start_time = _fmt_ts(event_data.get("start_time"))
        end_time = _fmt_ts(event_data.get("finished_time"))
        files_count = event_data.get("files_count")
        completed_count = event_data.get("completed_count")
        actual_count = event_data.get("actual_count")
        actual_size = _fmt_size(event_data.get("actual_size"))
        ignoring_files = event_data.get("ignoring_files")
        status = event_data.get("status")
        error_code = event_data.get("error_code")
        error_message = str(event_data.get("error_message") or "")

        # 无变更跳过 = 已处理文件数 - 实际传输文件数
        skipped: Optional[int] = None
        try:
            if completed_count is not None and actual_count is not None:
                v = int(completed_count) - int(actual_count)
                skipped = max(0, v)
        except (TypeError, ValueError):
            pass

        # 耗时 = finished_time - start_time（秒）
        elapsed_str = ""
        try:
            s = int(event_data.get("start_time") or 0)
            f = int(event_data.get("finished_time") or 0)
            if s > 10_000_000_000:
                s = int(s / 1000)
            if f > 10_000_000_000:
                f = int(f / 1000)
            if s > 0 and f > 0 and f >= s:
                sec = f - s
                if sec < 60:
                    elapsed_str = f"{sec} 秒"
                elif sec < 3600:
                    elapsed_str = f"{sec // 60} 分 {sec % 60} 秒"
                else:
                    elapsed_str = f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"
        except (TypeError, ValueError):
            pass

        lines: List[str] = []
        if task_name:
            lines.append(f"任务名称: {task_name}")
        if operation_id not in (None, ""):
            lines.append(f"执行ID: {operation_id}")
        if storage_name or storage_addr:
            where = storage_name
            if storage_addr:
                where = f"{where} ({storage_addr})" if where else storage_addr
            lines.append(f"存储位置: {where}")
        if target_path:
            lines.append(f"备份路径: {target_path}")
        if source_paths:
            lines.append(f"源路径: {source_paths}")
        if start_time:
            lines.append(f"开始时间: {start_time}")
        if end_time:
            lines.append(f"结束时间: {end_time}")
        if actual_count not in (None, ""):
            lines.append(f"传输文件数: {actual_count}")
        if actual_size:
            lines.append(f"传输文件大小: {actual_size}")
        if ignoring_files not in (None, ""):
            lines.append(f"排除文件数: {ignoring_files}")
        if skipped is not None:
            lines.append(f"无变更跳过: {skipped}")
        if files_count not in (None, ""):
            lines.append(f"总文件数: {files_count}")
        if elapsed_str:
            lines.append(f"耗时: {elapsed_str}")
        if status not in (None, ""):
            lines.append(f"状态码: {status}")
        if error_code not in (None, "") and int(error_code or 0) != 0:
            lines.append(f"错误码: {error_code}")
        if error_message:
            lines.append(f"错误信息: {error_message[:300]}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_scheduler_task_content(self, event_data: Dict[str, Any]) -> str:
        """构建 fn-scheduler 任务执行结果内容。"""
        task_name = str(event_data.get("task_name") or "")
        result_id = event_data.get("result_id")
        account = str(event_data.get("account") or "")
        trigger_label = str(event_data.get("trigger_reason_label") or event_data.get("trigger_reason") or "")
        schedule_expr = str(event_data.get("schedule_expression") or "")
        started_at = str(event_data.get("started_at") or "")
        finished_at = str(event_data.get("finished_at") or "")
        duration = str(event_data.get("duration") or "")
        log_preview = str(event_data.get("log_preview") or "")
        status = str(event_data.get("status") or "")

        lines: List[str] = []
        if task_name:
            lines.append(f"任务名称: {task_name}")
        if result_id not in (None, ""):
            lines.append(f"执行ID: {result_id}")
        if account:
            lines.append(f"执行账户: {account}")
        if trigger_label:
            lines.append(f"触发方式: {trigger_label}")
        if schedule_expr:
            lines.append(f"计划表达式: {schedule_expr}")
        if started_at:
            lines.append(f"开始时间: {started_at}")
        if finished_at:
            lines.append(f"结束时间: {finished_at}")
        if duration:
            lines.append(f"耗时: {duration}")
        if log_preview:
            log_title = "执行结果" if status == "success" else "执行日志"
            lines.append(f"{log_title}:\n{log_preview}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _batch_summary_item_lines(self, event_type: str, ed: Dict[str, Any]) -> List[str]:
        """多事件合并时，单条事件只展示关键字段（无图标，多行时每条一行）。"""
        data_raw = ed.get("data")
        data: Dict[str, Any] = data_raw if isinstance(data_raw, dict) else {}
        et = str(event_type)

        if et in ("LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout"):
            login_bits: List[str] = []
            if ed.get("user") is not None:
                login_bits.append(f"用户名: {self._display_user(ed.get('user', ''))}")
            if ed.get("IP"):
                login_bits.append(f"IP地址: {ed.get('IP')}")
            if ed.get("via"):
                login_bits.append(f"认证: {ed.get('via')}")
            return [" · ".join(login_bits)] if login_bits else [et]

        if et in ("SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED"):
            parts = []
            if ed.get("user") is not None:
                parts.append(f"用户名: {self._display_user(ed.get('user', ''))}")
            if ed.get("IP"):
                parts.append(f"IP地址: {ed.get('IP')}")
            if ed.get("port"):
                parts.append(f"端口: {ed.get('port')}")
            if ed.get("reason"):
                parts.append(f"原因: {ed.get('reason')}")
            return [" · ".join(parts)] if parts else [et]

        if et == "APP_CRASH":
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else ["应用: （未知）"]

        if et == "APP_UPDATE_FAILED":
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else ["应用更新失败"]

        if et in (
            "APP_STARTED",
            "APP_STOPPED",
            "APP_UPDATED",
            "APP_INSTALLED",
            "APP_AUTO_STARTED",
            "APP_UNINSTALLED",
        ):
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else [et]

        if et in (
            "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
            "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
        ):
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else [et]

        if et == "CPU_USAGE_ALARM":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("cpu_usage", data.get("USAGE", data.get("usage", "")))
            cpu_bits: List[str] = []
            if usage != "" and usage is not None:
                cpu_bits.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                cpu_bits.append(f"使用率阈值: {th}%")
            return [" · ".join(cpu_bits)] if cpu_bits else ["CPU使用率告警"]

        if et == "CPU_USAGE_RESTORED":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("cpu_usage", "")
            cpu_bits2: List[str] = []
            if usage != "" and usage is not None:
                cpu_bits2.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                cpu_bits2.append(f"阈值: {th}%")
            return [" · ".join(cpu_bits2)] if cpu_bits2 else ["CPU使用率已恢复"]

        if et == "MEMORY_USAGE_ALARM":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("memory_usage", data.get("USAGE", data.get("usage", "")))
            mem_bits: List[str] = []
            if usage != "" and usage is not None:
                mem_bits.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                mem_bits.append(f"使用率阈值: {th}%")
            return [" · ".join(mem_bits)] if mem_bits else ["内存使用率告警"]

        if et == "MEMORY_USAGE_RESTORED":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("memory_usage", "")
            mem_bits2: List[str] = []
            if usage != "" and usage is not None:
                mem_bits2.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                mem_bits2.append(f"阈值: {th}%")
            return [" · ".join(mem_bits2)] if mem_bits2 else ["内存使用率已恢复"]

        if et == "CPU_TEMPERATURE_ALARM":
            th = data.get("THRESHOLD", ed.get("threshold", 0))
            return [f"温度阈值: {th}°C"]

        if et in ("DiskWakeup", "DiskSpindown"):
            d = ed.get("disk", "")
            if not d:
                return ["磁盘事件"]
            bits = [d]
            if ed.get("model"):
                bits.append(str(ed.get("model")))
            if ed.get("serial"):
                bits.append(str(ed.get("serial")))
            return [f"磁盘: {' / '.join(bits)}"]

        if et == "FoundDisk":
            out: List[str] = []
            if ed.get("name"):
                out.append(f"设备名称: {ed.get('name')}")
            if ed.get("model"):
                out.append(f"型号: {ed.get('model')}")
            return out or ["发现新硬盘"]

        if et == "DISK_IO_ERR":
            dev = data.get("DEV", data.get("dev", ""))
            cnt = data.get("ERR_CNT", data.get("err_cnt", ""))
            lines = []
            if dev:
                lines.append(f"设备: {dev}")
            if cnt != "" and cnt is not None:
                lines.append(f"错误次数: {cnt}")
            return lines or ["磁盘IO错误"]

        if et in ("BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS"):
            backup_lines: List[str] = []
            if ed.get("task_name"):
                backup_lines.append(f"任务: {ed.get('task_name')}")
            if ed.get("operation_id") is not None:
                backup_lines.append(f"执行ID: {ed.get('operation_id')}")
            if ed.get("actual_count") is not None or ed.get("files_count") is not None:
                backup_lines.append(f"传输: {ed.get('actual_count', 0)}/{ed.get('files_count', 0)}")
            if int(ed.get("error_code") or 0) != 0:
                backup_lines.append(f"错误码: {ed.get('error_code')}")
            return [" · ".join(backup_lines)] if backup_lines else [et]

        if et in ("SCHEDULER_TASK_SUCCESS", "SCHEDULER_TASK_FAILED", "SCHEDULER_TASK_CONDITION_FAILED"):
            sched_lines: List[str] = []
            if ed.get("task_name"):
                sched_lines.append(f"任务: {ed.get('task_name')}")
            if ed.get("trigger_reason_label"):
                sched_lines.append(str(ed.get("trigger_reason_label")))
            if ed.get("duration"):
                sched_lines.append(f"耗时: {ed.get('duration')}")
            return [" · ".join(sched_lines)] if sched_lines else [et]

        if et in ("UPS_ONBATT", "UPS_ONBATT_LOWBATT"):
            ups_bits: List[str] = []
            if ed.get("battery") is not None:
                ups_bits.append(f"电量: {ed.get('battery')}%")
            if ed.get("load") is not None:
                ups_bits.append(f"负载: {ed.get('load')}%")
            return [" · ".join(ups_bits)] if ups_bits else [et]

        if et == "UPS_ONLINE":
            if ed.get("battery") is not None:
                return [f"电量: {ed.get('battery')}%"]
            return [et]

        if et in ("UPS_ENABLE", "UPS_DISABLE"):
            un = data.get("USERNAME", "")
            return [f"用户: {un}"] if un else [et]

        if et in {"SHUTDOWN_VM", "STATUS_RUNNING_VM", "DESTROY_VM"} or ("VM" in et.upper()):
            vm = data.get("VM_TITLE", data.get("vm_title", ""))
            user = ed.get("user") or data.get("USER_NAME", "")
            lines = []
            if vm:
                lines.append(f"虚拟机: {vm}")
            if user:
                lines.append(f"用户: {user}")
            return lines or [et]

        if et in {
            "ARCHIVING_SUCCESS",
            "DeleteFile",
            "MovetoTrashbin",
            "SHARE_EVENTID_DEL",
            "SHARE_EVENTID_PUT",
            "WEBDAV_ENABLED",
            "WEBDAV_DISABLED",
            "SAMBA_ENABLED",
            "SAMBA_DISABLED",
            "DLNA_ENABLED",
            "DLNA_DISABLED",
            "FTP_ENABLED",
            "FTP_DISABLED",
            "NFS_ENABLED",
            "NFS_DISABLED",
            "FW_ENABLE",
            "FW_DISABLE",
            "SECURITY_PORTCHANGED",
        }:
            user = ed.get("user") or data.get("USER_NAME", "")
            if user:
                return [f"用户: {user}"]
            if ed.get("from"):
                return [f"来源: {ed.get('from')}"]
            for key in ("path", "PATH", "share_name", "SHARE_NAME", "name"):
                v = ed.get(key) or data.get(key)
                if v:
                    return [f"{key}: {v}"]
            return [et]

        # 通用回退
        bits: List[str] = []
        if ed.get("user") or ed.get("IP"):
            bits.append(f"{ed.get('user', '')}@{ed.get('IP', '')}".strip("@"))
        dn = data.get("DISPLAY_NAME") or data.get("APP_NAME")
        if dn:
            bits.append(str(dn))
        if ed.get("name"):
            bits.append(str(ed.get("name")))
        if bits:
            return [" · ".join(b for b in bits if b)[:200]]
        return [et]

    def _build_poll_batch_summary_content(self, event_data: Dict[str, Any]) -> str:
        """构建轮询批量汇总内容（按类型分组并展示每条差异数据）。"""
        total = int(event_data.get('count') or 0)
        by_type_raw = event_data.get('by_type')
        by_type: Dict[str, Any] = by_type_raw if isinstance(by_type_raw, dict) else {}
        items_raw = event_data.get('items')
        items: List[Dict[str, Any]] = items_raw if isinstance(items_raw, list) else []
        grouped_events_raw_in = event_data.get('grouped_events')
        grouped_events_raw: Dict[str, Any] = grouped_events_raw_in if isinstance(grouped_events_raw_in, dict) else {}
        grouped_events: Dict[str, List[Dict[str, Any]]] = {
            str(k): v for k, v in grouped_events_raw.items() if isinstance(v, list)
        }

        lines: List[str] = []
        current_len = 0
        truncated = False
        omitted_types = 0
        omitted_items = 0

        def _append(line: str) -> bool:
            nonlocal current_len, truncated
            addition = ("" if not lines else "\n") + line
            if current_len + len(addition) > self.BATCH_MAX_CHARS:
                truncated = True
                return False
            lines.append(line)
            current_len += len(addition)
            return True

        # 优先展示按类型分组后的完整明细；无分组数据时回退到简版预览
        if grouped_events:
            sorted_types = sorted(
                grouped_events.keys(),
                key=lambda et: (-int(by_type.get(et, len(grouped_events.get(et, [])))), str(et)),
            )
            selected_types = sorted_types[: self.BATCH_MAX_TYPES]
            omitted_types = max(0, len(sorted_types) - len(selected_types))
            for ti, event_type in enumerate(selected_types):
                evs = grouped_events.get(event_type) or []
                title = self._with_title_prefix(self.EVENT_TITLES.get(str(event_type), self._fallback_event_title(str(event_type))))
                title = self._strip_batch_inner_title_prefix(title)
                title = self._strip_body_emojis(title).strip()
                if not _append(f"【{title}】"):
                    break
                shown = evs[: self.BATCH_PER_TYPE_LIMIT]
                for item in shown:
                    ed_raw = item.get('event_data')
                    ed = ed_raw if isinstance(ed_raw, dict) else {}
                    try:
                        key_lines = self._batch_summary_item_lines(str(event_type), ed)
                    except Exception:
                        brief = str(item.get('brief') or str(event_type))
                        key_lines = [self._strip_body_emojis(f"解析失败: {brief}").strip() or str(event_type)]
                    for kl in key_lines:
                        if not _append("\t" + kl):
                            break
                if len(evs) > self.BATCH_PER_TYPE_LIMIT:
                    left = len(evs) - self.BATCH_PER_TYPE_LIMIT
                    omitted_items += left
                    if not _append(f"… 该类型其余 {left} 条（可能包含异常数据）请去日志查看"):
                        break
                if ti < len(selected_types) - 1:
                    if not _append(""):
                        break
            if omitted_types > 0:
                _append(f"… 其余 {omitted_types} 类事件已省略（可能包含异常数据）请去日志查看")
        elif items:
            _append("事件预览:")
            for idx, it in enumerate(items[:8], 1):
                brief = str(it.get('brief') or it.get('event_type') or '（无摘要）')
                ts = str(it.get('timestamp') or '')
                line = f"\t{idx}. [{ts}] {brief}" if ts else f"\t{idx}. {brief}"
                if not _append(line):
                    break
            if total > len(items):
                omitted_items += total - len(items)
                _append(f"  … 其余 {total - len(items)} 条请查看日志")
        if truncated:
            _append("… 内容已截断（可能包含异常数据）请去日志查看")

        # 暴露渲染元数据，便于推送记录详情展示
        event_data["batch_render_meta"] = {
            "truncated": truncated,
            "max_chars": self.BATCH_MAX_CHARS,
            "max_types": self.BATCH_MAX_TYPES,
            "per_type_limit": self.BATCH_PER_TYPE_LIMIT,
            "omitted_types": omitted_types,
            "omitted_items": omitted_items,
        }
        return "\n".join(lines)

    def _build_disk_io_err_content(self, event_data: Dict[str, Any]) -> str:
        """构建磁盘IO错误事件内容（data: DEV, SN, MODEL, ERR_CNT）"""
        content = ""
        data = event_data.get('data', {})
        dev = data.get('DEV', data.get('dev', ''))
        sn = data.get('SN', data.get('sn', ''))
        model = data.get('MODEL', data.get('model', ''))
        err_cnt = data.get('ERR_CNT', data.get('err_cnt', 0))
        if dev:
            content += f"📛 设备: {dev}\n"
        if model:
            content += f"🔧 型号: {model}\n"
        if sn:
            content += f"🔢 序列号: {sn}\n"
        content += f"⚠️ 错误次数: {err_cnt}\n"
        return content

    def _build_ssh_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """构建SSH相关事件内容"""
        content = ""
        if event_type == 'SSH_INVALID_USER':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            content += f"👤 用户名: {user}\n"
            content += f"📍 IP地址: {ip}\n"
            if port:
                content += f"🔌 端口: {port}\n"
        elif event_type == 'SSH_AUTH_FAILED':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            reason = event_data.get('reason', '')
            if user:
                content += f"👤 用户名: {user}\n"
            if ip:
                content += f"📍 IP地址: {ip}\n"
            if port:
                content += f"🔌 端口: {port}\n"
            if reason:
                content += f"⚠️ 失败原因: {reason}\n"
        elif event_type == 'SSH_LOGIN_SUCCESS':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            content += f"👤 用户名: {user}\n"
            content += f"📍 IP地址: {ip}\n"
            if port:
                content += f"🔌 端口: {port}\n"
        elif event_type == 'SSH_DISCONNECTED':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            content += f"👤 用户名: {user}\n"
            content += f"📍 IP地址: {ip}\n"
            if port:
                content += f"🔌 端口: {port}\n"
        return content
    
    def _build_disk_wakeup_content(self, event_data: Dict[str, Any]) -> str:
        """构建单个磁盘唤醒事件内容"""
        content = ""
        
        if disk := event_data.get('disk', ''):
            content += f"📛 磁盘设备: {disk}\n"
        
        if model := event_data.get('model', ''):
            content += f"🔧 硬盘型号: {model}\n"
        
        if serial := event_data.get('serial', ''):
            content += f"🔢 序列号: {serial}\n"
        
        return content
    
    def _build_disk_spindown_content(self, event_data: Dict[str, Any]) -> str:
        """构建单个磁盘休眠事件内容"""
        content = ""
        
        if disk := event_data.get('disk', ''):
            content += f"📛 磁盘设备: {disk}\n"
        
        if model := event_data.get('model', ''):
            content += f"🔧 硬盘型号: {model}\n"
        
        if serial := event_data.get('serial', ''):
            content += f"🔢 序列号: {serial}\n"
        
        return content
    
    def _format_disk_fallback(self, disk_info: Dict[str, Any]) -> str:
        """当 disk/model/serial 都为空时，从 full_event_data 或 data 中拼一条可读摘要（支持飞牛顶层 template/user/model/serial）"""
        raw = disk_info.get('full_event_data') or disk_info.get('data')
        if not isinstance(raw, dict):
            return ""
        # 优先从 data 子段取，否则用顶层（飞牛格式：template, user, model, serial 在顶层）
        data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
        if not isinstance(data, dict):
            return ""
        parts = []
        for key in ('disk', 'device', 'path', 'name', 'DEVICE', 'DISK', 'deviceName', 'dev'):
            if key in data and data[key]:
                v = data[key]
                if isinstance(v, dict):
                    v = v.get('path') or v.get('device') or v.get('name') or str(v)[:80]
                parts.append(f"设备: {v}")
                break
        for key in ('model', 'MODEL', 'Model', 'modelName'):
            if key in data and data[key]:
                parts.append(f"型号: {data[key]}")
                break
        for key in ('serial', 'SERIAL', 'Serial', 'sn', 'SN', 'serialNumber'):
            if key in data and data[key]:
                parts.append(f"序列号: {data[key]}")
                break
        if parts:
            return " ".join(parts)
        # 无标准字段时，列出 data 中部分键值便于排查
        skip = {'raw', 'datetime', 'eventId', 'level', 'from', 'template', 'cat'}
        extra = [f"{k}: {v}" for k, v in list(data.items())[:5] if k not in skip and v]
        if extra:
            return "原始: " + ", ".join(extra)[:120]
        return ""

    def _build_merged_disk_wakeup_content(self, event_data: Dict[str, Any]) -> str:
        """构建合并磁盘唤醒事件内容"""
        content = ""
        
        merged_disks = event_data.get('merged_disks', [])
        for i, disk_info in enumerate(merged_disks, 1):
            content += f"磁盘 #{i}:\n"
            disk = disk_info.get('disk', '') or ''
            model = disk_info.get('model', '') or ''
            serial = disk_info.get('serial', '') or ''
            if not (disk or model or serial) and isinstance(disk_info.get('full_event_data'), dict):
                raw = disk_info['full_event_data']
                model = model or raw.get('model') or raw.get('MODEL') or raw.get('Model') or ''
                serial = serial or raw.get('serial') or raw.get('SERIAL') or raw.get('Serial') or raw.get('sn') or raw.get('SN') or ''
                disk = disk or raw.get('disk') or raw.get('device') or raw.get('dev') or ''
            if disk:
                content += f"  📛 磁盘设备: {disk}\n"
            if model:
                content += f"  🔧 硬盘型号: {model}\n"
            if serial:
                content += f"  🔢 序列号: {serial}\n"
            if not (disk or model or serial):
                fallback = self._format_disk_fallback(disk_info)
                if fallback:
                    content += f"  {fallback}\n"
                else:
                    content += "  （未解析到磁盘详情，请查看系统日志）\n"
            if i < len(merged_disks):
                content += "\n"
        
        return content
    
    def _build_merged_disk_spindown_content(self, event_data: Dict[str, Any]) -> str:
        """构建合并磁盘休眠事件内容"""
        content = ""
        
        merged_disks = event_data.get('merged_disks', [])
        for i, disk_info in enumerate(merged_disks, 1):
            content += f"磁盘 #{i}:\n"
            disk = disk_info.get('disk', '') or ''
            model = disk_info.get('model', '') or ''
            serial = disk_info.get('serial', '') or ''
            if not (disk or model or serial) and isinstance(disk_info.get('full_event_data'), dict):
                raw = disk_info['full_event_data']
                model = model or raw.get('model') or raw.get('MODEL') or raw.get('Model') or ''
                serial = serial or raw.get('serial') or raw.get('SERIAL') or raw.get('Serial') or raw.get('sn') or raw.get('SN') or ''
                disk = disk or raw.get('disk') or raw.get('device') or raw.get('dev') or ''
            if disk:
                content += f"  📛 磁盘设备: {disk}\n"
            if model:
                content += f"  🔧 硬盘型号: {model}\n"
            if serial:
                content += f"  🔢 序列号: {serial}\n"
            if not (disk or model or serial):
                fallback = self._format_disk_fallback(disk_info)
                if fallback:
                    content += f"  {fallback}\n"
                else:
                    content += "  （未解析到磁盘详情，请查看系统日志）\n"
            if i < len(merged_disks):
                content += "\n"
        
        return content
    
    def _build_app_lifecycle_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用生命周期事件内容（APP_STARTED/STOPPED/UPDATED 等，与 APP_CRASH 同结构）"""
        return self._build_app_crash_content(event_data)

    def _build_app_crash_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用崩溃事件内容"""
        content = ""
        data = event_data.get('data', {})
        
        if app_name := data.get('DISPLAY_NAME', data.get('APP_NAME', '')):
            content += f"📱 应用名称: {app_name}\n"
        
        if app_id := data.get('APP_ID', ''):
            content += f"🆔 应用ID: {app_id}\n"
        
        if from_src := event_data.get('from', ''):
            content += f"📦 来源模块: {from_src}\n"
        
        return content
    
    def _build_app_update_failed_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用更新失败事件内容"""
        content = ""
        data = event_data.get('data', {})
        
        if app_name := data.get('DISPLAY_NAME', data.get('APP_NAME', '')):
            content += f"📱 应用名称: {app_name}\n"
        
        if app_id := data.get('APP_ID', ''):
            content += f"🆔 应用ID: {app_id}\n"
        
        if from_src := event_data.get('from', ''):
            content += f"📦 来源模块: {from_src}\n"
        
        return content

    def _build_app_start_failed_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用启动失败（本地运行异常）事件内容"""
        return self._build_app_crash_content(event_data)

    def _build_app_auto_start_failed_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用自启动失败（Docker 不可用）事件内容"""
        content = self._build_app_crash_content(event_data)
        content += "⚠️ 原因: Docker 服务不可用\n"
        return content

    def _build_cpu_usage_alarm_content(self, event_data: Dict[str, Any]) -> str:
        """构建 CPU 使用率告警内容（parameter 格式: data.THRESHOLD）"""
        content = ""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        content += f"📊 使用率阈值: {threshold}%\n"
        return content

    def _build_cpu_usage_restored_content(self, event_data: Dict[str, Any]) -> str:
        """构建 CPU 使用率恢复内容（parameter 格式: data.THRESHOLD）"""
        content = ""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        content += f"✅ 使用率已恢复至阈值 {threshold}% 以下\n"
        return content

    def _build_memory_usage_alarm_content(self, event_data: Dict[str, Any]) -> str:
        """构建内存使用率告警内容（parameter: data.THRESHOLD）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        return f"📊 内存使用率阈值: {threshold}%\n"

    def _build_memory_usage_restored_content(self, event_data: Dict[str, Any]) -> str:
        """构建内存使用率恢复内容（parameter: data.THRESHOLD）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        return f"✅ 内存使用率已恢复至阈值 {threshold}% 以下\n"

    def _build_cpu_temperature_alarm_content(self, event_data: Dict[str, Any]) -> str:
        """构建 CPU 温度告警内容"""
        content = ""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        content += f"🌡️ 温度阈值: {threshold}°C\n"
        return content
    
    def _build_ups_onbatt_content(self, event_data: Dict[str, Any]) -> str:
        """构建UPS切换到电池供电事件内容"""
        content = ""
        
        content += f"🔋 UPS状态: 切换到电池供电\n"
        content += f"⚠️ 请注意电池电量\n"
        
        return content
    
    def _build_ups_onbatt_lowbatt_content(self, event_data: Dict[str, Any]) -> str:
        """构建UPS电池电量低警告事件内容"""
        content = ""
        
        content += f"🔋 UPS状态: 电池供电模式，电量低警告\n"
        content += f"🚨 电池电量不足，请尽快恢复市电供应\n"
        
        return content
    
    def _build_ups_online_content(self, event_data: Dict[str, Any]) -> str:
        """构建UPS切换到市电供电事件内容"""
        content = ""

        content += f"🔌 UPS状态: 切换到市电供电模式\n"
        content += f"✅ 电力供应恢复正常\n"

        return content

    def _build_ups_enable_disable_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """构建开启/关闭 UPS 支持事件内容（parameter 含 data.USERNAME, from）"""
        content = ""
        data = event_data.get('data', {})
        if username := data.get('USERNAME', ''):
            content += f"👤 操作用户: {username}\n"
        if from_src := event_data.get('from', ''):
            content += f"📦 来源: {from_src}\n"
        return content
    
    def _build_system_content(self, event_type: str, event_data: Dict[str, Any], message: str) -> str:
        """构建系统事件消息内容"""
        content = f"{message}\n"
        
        # 添加简化的时间信息（正文不保留时钟类符号）
        content += f"\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self._strip_body_emojis(content)

    def _build_minimal_system_one_line(self, event_type: str, message: str) -> str:
        """极简模式下系统通知一行文案。"""
        prefix = (self.title_prefix or "").strip()
        action_map = {
            "APP_START": "监控启动",
            "APP_STOP": "监控停止",
            "APP_ERROR": "监控异常",
            "DND_SUMMARY": "勿扰汇总",
            "TEST_PUSH": "测试推送",
        }
        action = action_map.get(event_type, event_type)
        msg = (message or "").strip().replace("\n", " ")
        body = f"{action} {msg}".strip()
        if prefix:
            return f"{prefix}-{body}"
        return body
    
    def _build_bark_message(self, event_type: str, event_data: Dict[str, Any], 
                           timestamp: str, raw_log: str) -> MultiPlatformMessage:
        """构建Bark消息。标题通过 URL 单独传，正文不再重复包含标题，避免推送时标题显示两次。"""
        message = self._build_message(event_type, event_data, timestamp, raw_log)
        return MultiPlatformMessage(title=message.title, content=message.content)
    
    def send_system_notification(self, event_type: str, message: str, additional_info: Optional[Dict[str, Any]] = None):
        """
        发送系统事件通知
        
        Args:
            event_type: 事件类型 ('APP_START', 'APP_STOP', 'APP_ERROR', 'DND_SUMMARY')
            message: 详细消息
            additional_info: 额外信息字典
            
        Returns:
            dict: {"success": bool, "success_count": int, "fail_count": int}
        """
        self.logger.info(f"准备发送系统事件通知: {event_type}")
        
        # 构建事件数据
        event_data = {
            'message': message,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'hostname': additional_info.get('hostname', '') if additional_info else '',
            'version': additional_info.get('version', '1.0') if additional_info else '1.0',
        }
        
        # 生成事件指纹
        event_fingerprint = self._generate_system_fingerprint(event_type, event_data)
        
        # 检查去重
        if self._is_duplicate(event_fingerprint):
            self.logger.debug(f"跳过重复系统事件: {event_type}")
            return {"success": False, "success_count": 0, "fail_count": 0}
        
        # 构建消息
        # 勿扰汇总始终走完整模式，不受极简开关影响
        if self.minimal_push_enabled and event_type != "DND_SUMMARY":
            one_line = self._build_minimal_system_one_line(event_type, message)
            multi_msg = MultiPlatformMessage(title=one_line, content="")
        else:
            title = self._with_title_prefix(
                self.EVENT_TITLES.get(event_type, self._fallback_event_title(event_type))
            )
            content = self._build_system_content(event_type, event_data, message)
            multi_msg = MultiPlatformMessage(title=title, content=content)
        
        results = []
        if self.wechat_webhook_url:
            ok, cr = self._send_to_wechat(multi_msg)
            results.append(ok)
            self.logger.debug("企业微信系统通知: %s", cr)
        if self.dingtalk_webhook_url:
            ok, cr = self._send_to_dingtalk(multi_msg)
            results.append(ok)
            self.logger.debug("钉钉系统通知: %s", cr)
        if self.feishu_webhook_url:
            ok, cr = self._send_to_feishu(multi_msg)
            results.append(ok)
            self.logger.debug("飞书系统通知: %s", cr)
        if self.bark_url:
            ok, cr = self._send_to_bark(multi_msg)
            results.append(ok)
            self.logger.debug("Bark系统通知: %s", cr)
        if self.pushplus_params:
            ok, cr = self._send_to_pushplus(multi_msg)
            results.append(ok)
            self.logger.debug("PushPlus系统通知: %s", cr)
        if self.magic_push_params:
            ok, cr = self._send_to_magic_push(multi_msg)
            results.append(ok)
            self.logger.debug("魔法推送系统通知: %s", cr)
        if self.smtp_params:
            ok, cr = self._send_to_smtp(multi_msg)
            results.append(ok)
            self.logger.debug("SMTP邮件系统通知: %s", cr)
        success_count = sum(1 for r in results if r)
        fail_count = len(results) - success_count
        any_ok = bool(results and success_count > 0)
        if any_ok:
            self.sent_events[event_fingerprint] = time.time()
            self._record_send_result(True)
            self.logger.info(f"系统事件通知发送成功: {event_type}")
        else:
            self._record_send_result(False)
            self.logger.warning(f"系统事件通知发送失败: {event_type}")
        return {"success": any_ok, "success_count": success_count, "fail_count": fail_count}
    
    def _generate_system_fingerprint(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """生成系统事件指纹（用于去重）"""
        # 根据事件类型生成不同的指纹
        if event_type == 'APP_START':
            # 启动事件：按小时去重
            hour_window = int(time.time() / 3600)
            key = f"{event_type}_{hour_window}"
        elif event_type == 'APP_STOP':
            # 停止事件：按分钟去重
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{minute_window}"
        elif event_type == 'APP_ERROR':
            # 错误事件：按5分钟去重
            window = int(time.time() / 300)  # 5分钟窗口
            key = f"{event_type}_{window}"
        elif event_type == 'TEST_PUSH':
            # 测试推送：每次发送独立，不去重
            key = f"TEST_PUSH_{time.time()}"
        else:
            # 其他事件：按分钟去重
            minute_window = int(time.time() / 60)
            key = f"sys_{event_type}_{minute_window}"
        
        # 使用MD5生成固定长度的指纹
        return hashlib.md5(key.encode()).hexdigest()
    
    def cleanup_cache(self):
        """清理过期的缓存"""
        current_time = time.time()
        expired_keys = [
            key for key, ts in self.sent_events.items()
            if current_time - ts > self.dedup_window * 2
        ]
        
        for key in expired_keys:
            del self.sent_events[key]
        
        if expired_keys:
            self.logger.debug(f"清理了 {len(expired_keys)} 个过期缓存")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        pool_stats = self.connection_pool.get_stats()
        
        return {
            **pool_stats,
            'cache_size': len(self.sent_events),
            'dedup_window': self.dedup_window,
            'has_wechat_webhook': bool(self.wechat_webhook_url),
            'has_dingtalk_webhook': bool(self.dingtalk_webhook_url),
            'has_feishu_webhook': bool(self.feishu_webhook_url),
            'disk_wakeup_cache_size': len(self.disk_wakeup_cache),
            'disk_spindown_cache_size': len(self.disk_spindown_cache),
            'merge_window': self.merge_window,
            'wechat_webhook_url': self.wechat_webhook_url[:50] + '...' 
                if len(self.wechat_webhook_url) > 50 else self.wechat_webhook_url,
            'dingtalk_webhook_url': self.dingtalk_webhook_url[:50] + '...' 
                if len(self.dingtalk_webhook_url) > 50 else self.dingtalk_webhook_url,
            'feishu_webhook_url': self.feishu_webhook_url[:50] + '...' 
                if len(self.feishu_webhook_url) > 50 else self.feishu_webhook_url
        }
    
    def close(self):
        """关闭通知器"""
        self.logger.info("正在关闭多平台通知器...")

        # 设置停止标志
        self._stop_flag = True

        # 刷新所有待发送的磁盘事件
        self._flush_pending_disk_events()

        # 等待定时器线程结束
        if hasattr(self, 'timer_thread') and self.timer_thread.is_alive():
            self.timer_thread.join(timeout=10)

        # 关闭连接池
        self.connection_pool.close()

        # 清理缓存
        self.cleanup_cache()

        self.logger.info("多平台通知器已关闭")

    def _flush_pending_disk_events(self):
        """刷新所有待发送的磁盘事件"""
        try:
            with self._cache_lock:
                # 发送所有待发送的磁盘唤醒事件
                for time_window, event_list in self.disk_wakeup_cache.items():
                    if event_list:
                        self.logger.info(f"刷新待发送的磁盘唤醒事件: {len(event_list)} 个")
                        self._send_merged_disk_event('DiskWakeup', event_list, time_window)

                # 发送所有待发送的磁盘休眠事件
                for time_window, event_list in self.disk_spindown_cache.items():
                    if event_list:
                        self.logger.info(f"刷新待发送的磁盘休眠事件: {len(event_list)} 个")
                        self._send_merged_disk_event('DiskSpindown', event_list, time_window)

                # 清空缓存
                self.disk_wakeup_cache.clear()
                self.disk_spindown_cache.clear()
        except Exception as e:
            self.logger.error(f"刷新待发送事件时出错: {e}", exc_info=True)
