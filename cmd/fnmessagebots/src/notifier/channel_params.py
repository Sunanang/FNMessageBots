"""
企业微信应用、通用 Webhook、MeoW 渠道的参数解析与校验（配置加载、Web 保存、发送共用）。
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, Optional, Tuple

WECOM_API_BASE_DEFAULT = "https://qyapi.weixin.qq.com"
MEOW_API_BASE_DEFAULT = "https://api.chuckfang.com"

WEBHOOK_METHODS = ("POST", "GET", "PUT")
WEBHOOK_CONTENT_TYPES = ("json", "form", "text")
WEBHOOK_DEFAULT_BODY = '{"title": "{title}", "content": "{content}"}'
WEBHOOK_PLACEHOLDERS = ("title", "content", "text", "time")

_CONTENT_TYPE_HEADERS = {
    "json": "application/json; charset=utf-8",
    "form": "application/x-www-form-urlencoded; charset=utf-8",
    "text": "text/plain; charset=utf-8",
}


def _is_http_url(v: str) -> bool:
    return v.startswith("http://") or v.startswith("https://")


def parse_json_object(raw: str, label: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"{label}配置不是合法 JSON：{e}"
    if not isinstance(obj, dict):
        return None, f"{label}配置须为 JSON 对象。"
    return obj, ""


# ---------- 企业微信应用 ----------

def wecom_app_fields(obj: Dict[str, Any]) -> Dict[str, Any]:
    base = str(obj.get("api_base") or "").strip().rstrip("/") or WECOM_API_BASE_DEFAULT
    to_user = str(obj.get("to_user") or "").strip()
    to_party = str(obj.get("to_party") or "").strip()
    to_tag = str(obj.get("to_tag") or "").strip()
    if not (to_user or to_party or to_tag):
        to_user = "@all"
    return {
        "corp_id": str(obj.get("corp_id") or "").strip(),
        "corp_secret": str(obj.get("corp_secret") or "").strip(),
        "agent_id": str(obj.get("agent_id") or "").strip(),
        "to_user": to_user,
        "to_party": to_party,
        "to_tag": to_tag,
        "api_base": base,
    }


def validate_wecom_app(obj: Dict[str, Any]) -> str:
    f = wecom_app_fields(obj)
    if not f["corp_id"] or not f["corp_secret"] or not f["agent_id"]:
        return "企业微信应用须填写企业ID、应用Secret 和 AgentId。"
    if not f["agent_id"].isdigit():
        return "企业微信应用 AgentId 须为数字。"
    if not _is_http_url(f["api_base"]):
        return "企业微信应用 API 地址须为 http(s) 地址。"
    return ""


# ---------- MeoW ----------

def meow_fields(obj: Dict[str, Any]) -> Dict[str, str]:
    return {
        "nickname": str(obj.get("nickname") or "").strip(),
        "url": str(obj.get("url") or "").strip(),
        "api_base": str(obj.get("api_base") or "").strip().rstrip("/") or MEOW_API_BASE_DEFAULT,
    }


def validate_meow(obj: Dict[str, Any]) -> str:
    f = meow_fields(obj)
    if not f["nickname"]:
        return "MeoW 须填写昵称。"
    if "/" in f["nickname"]:
        return "MeoW 昵称不能包含斜杠。"
    if f["url"] and not _is_http_url(f["url"]):
        return "MeoW 跳转链接须为 http(s) 地址。"
    if not _is_http_url(f["api_base"]):
        return "MeoW API 地址须为 http(s) 地址。"
    return ""


# ---------- 通用 Webhook ----------

def parse_headers(raw: Any) -> Tuple[Dict[str, str], str]:
    """支持 {"K": "V"} 或每行一个「K: V」的文本。"""
    if not raw:
        return {}, ""
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}, ""
    out: Dict[str, str] = {}
    for line in str(raw).splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            return {}, f"请求头格式错误（应为「名称: 值」）：{line[:40]}"
        k, v = line.split(":", 1)
        if not k.strip():
            return {}, f"请求头名称为空：{line[:40]}"
        out[k.strip()] = v.strip()
    return out, ""


def webhook_fields(obj: Dict[str, Any]) -> Dict[str, Any]:
    method = str(obj.get("method") or "POST").strip().upper()
    content_type = str(obj.get("content_type") or "json").strip().lower()
    body = obj.get("body")
    body = str(body) if body is not None else ""
    if not body.strip() and method != "GET" and content_type == "json":
        body = WEBHOOK_DEFAULT_BODY
    return {
        "url": str(obj.get("url") or "").strip(),
        "method": method,
        "content_type": content_type,
        "headers": obj.get("headers") or "",
        "body": body,
    }


def _fill(template: str, values: Dict[str, str], encode) -> str:
    out = template
    for k in WEBHOOK_PLACEHOLDERS:
        out = out.replace("{" + k + "}", encode(values.get(k, "")))
    return out


def render_webhook_request(
    obj: Dict[str, Any], values: Dict[str, str]
) -> Tuple[Optional[Dict[str, Any]], str]:
    """按占位符生成 {method, url, headers, body}；失败返回 (None, 错误说明)。"""
    f = webhook_fields(obj)
    url = _fill(f["url"], values, lambda v: urllib.parse.quote(v, safe=""))
    headers, err = parse_headers(f["headers"])
    if err:
        return None, err
    body: Optional[str] = None
    if f["method"] != "GET" and f["body"].strip():
        ct = f["content_type"]
        if ct == "json":
            body = _fill(f["body"], values, lambda v: json.dumps(v, ensure_ascii=False)[1:-1])
            try:
                json.loads(body)
            except json.JSONDecodeError as e:
                return None, f"请求体替换占位符后不是合法 JSON：{e}"
        elif ct == "form":
            body = _fill(f["body"], values, lambda v: urllib.parse.quote_plus(v))
        else:
            body = _fill(f["body"], values, lambda v: v)
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = _CONTENT_TYPE_HEADERS[ct]
    return {"method": f["method"], "url": url, "headers": headers, "body": body}, ""


def validate_webhook(obj: Dict[str, Any]) -> str:
    f = webhook_fields(obj)
    if not _is_http_url(f["url"]):
        return "通用 Webhook 地址须为 http(s) 地址。"
    if f["method"] not in WEBHOOK_METHODS:
        return f"通用 Webhook 请求方式仅支持 {'/'.join(WEBHOOK_METHODS)}。"
    if f["content_type"] not in WEBHOOK_CONTENT_TYPES:
        return "通用 Webhook 内容类型仅支持 json / form / text。"
    sample = {k: f"示例\"{k}\"\n" for k in WEBHOOK_PLACEHOLDERS}
    _, err = render_webhook_request(obj, sample)
    return f"通用 Webhook {err}" if err else ""


def validate_channel_json(ch_type: str, raw: str) -> str:
    """校验 wecom_app / webhook / meow 的单条 JSON 配置，返回错误说明（空串表示通过）。"""
    label = {"wecom_app": "企业微信应用", "webhook": "通用 Webhook", "meow": "MeoW"}[ch_type]
    obj, err = parse_json_object(raw, label)
    if err:
        return err
    assert obj is not None
    validator = {"wecom_app": validate_wecom_app, "webhook": validate_webhook, "meow": validate_meow}[ch_type]
    return validator(obj)
