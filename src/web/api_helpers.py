"""
Web API 辅助函数：请求参数解析、测试通知器构建。
"""

from __future__ import annotations

from notifier.multi_platform_notifier import MultiPlatformNotifier

from web.config_store import title_prefix_from_dict


def build_notifier_from_raw(raw: dict) -> MultiPlatformNotifier:
    """按当前原始配置构建通知器（用于测试推送）。"""
    return MultiPlatformNotifier(
        wechat_webhook_url=raw.get("wechat_webhook_url", ""),
        dingtalk_webhook_url=raw.get("dingtalk_webhook_url", ""),
        feishu_webhook_url=raw.get("feishu_webhook_url", ""),
        bark_url=raw.get("bark_url", ""),
        pushplus_params=raw.get("pushplus_params", ""),
        magic_push_params=raw.get("magic_push_params", "") or "",
        title_prefix=title_prefix_from_dict(raw),
        dedup_window=int(raw.get("dedup_window", 300)),
        pool_size=int(raw.get("http_pool_size", 10)),
        retries=int(raw.get("http_retry_count", 3)),
        timeout=int(raw.get("http_timeout", 10)),
    )


def parse_success_filter(success_param: str):
    """解析查询参数 success：true/1, false/0, 其他为 None。"""
    if success_param in ("1", "true"):
        return True
    if success_param in ("0", "false"):
        return False
    return None

