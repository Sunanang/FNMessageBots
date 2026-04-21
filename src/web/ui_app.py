import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import sys
import threading
from pathlib import Path

# 直接运行本文件时（python src/web/ui_app.py），把 src 加入 path 以便导入 notifier 等
if __name__ == "__main__":
    _repo = Path(__file__).resolve().parent.parent.parent
    _src = _repo / "src"
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from flask import Flask, jsonify, request, render_template_string, send_from_directory, abort, redirect

from notifier.multi_platform_notifier import MultiPlatformNotifier
from web.auth_service import get_password_config as _get_password_config
from web.auth_service import hash_password as _hash_password
from web.auth_service import has_password_set as _has_password_set_fn
from web.auth_service import is_password_verification_enabled as _is_password_verification_enabled_fn
from web.auth_service import verify_password as _verify_password
from web.api_helpers import build_notifier_from_raw as _build_notifier_from_raw
from web.api_helpers import parse_success_filter as _parse_success_filter
from web.app_paths import BASE_DIR
from web.app_paths import CONFIG_FILE
from web.app_paths import GITHUB_ICON_FILE
from web.app_paths import ICON_FILE
from web.app_paths import SUPPORT_QR_DIR
from web.app_paths import SUPPORT_QR_FILENAMES
from web.config_store import join_urls as _join_urls
from web.config_store import load_raw_config as _load_raw_config_from_file
from web.config_store import save_raw_config as _save_raw_config_to_file
from web.config_store import split_urls as _split_urls
from web.config_store import title_prefix_from_dict as _title_prefix_from_dict
from web.event_catalog import APP_LIFECYCLE_EVENTS
from web.event_catalog import DEFAULT_SELECTED_EVENTS
from web.event_catalog import EVENT_IDS_HIDDEN_IN_UI
from web.event_catalog import OLD_DEFAULT_SELECTED_EVENTS_WITH_EXTRA
from web.event_catalog import build_events_for_ui
from web.push_history_service import get_record as get_push_history_record
from web.push_history_service import get_stats as get_push_history_stats
from web.push_history_service import list_records as list_push_history_records
from web.session_service import create_session as _create_session
from web.session_service import touch_session as _touch_session
from web.ui_templates import FAQ_PAGE_TEMPLATE, HISTORY_PAGE_TEMPLATE, SUPPORT_PAGE_TEMPLATE

# 配置页密码：会话空闲超时（秒），超时后需重新输入密码
SESSION_IDLE_SECONDS = 300
AUTH_COOKIE_NAME = "fnmb_session"


def _as_bool(value, default: bool = False) -> bool:
    """稳健布尔解析：兼容 bool/数字/字符串（如 'true'/'false'）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)

def _has_password_set() -> bool:
    return _has_password_set_fn(_load_raw_config)


def _get_session_id_from_cookie() -> str:
    return (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()


def _is_authenticated() -> bool:
    return _touch_session(_get_session_id_from_cookie(), SESSION_IDLE_SECONDS)


def _is_password_verification_enabled() -> bool:
    """是否开启密码验证（默认 True）。关闭后不删密码，但访问配置页无需验证。"""
    return _is_password_verification_enabled_fn(_load_raw_config)


def _load_raw_config() -> dict:
    return _load_raw_config_from_file(CONFIG_FILE)


def _save_raw_config(data: dict) -> None:
    _save_raw_config_to_file(CONFIG_FILE, data)

def _mode_str(mode: int) -> str:
    return stat.filemode(mode)


def _check_db_access_issue(db_path: str, probe_sql: str = "SELECT 1") -> str:
    """检查数据库可读性并返回可读提示；无问题时返回空字符串。"""
    path = (db_path or "").strip()
    if not path:
        return ""

    p = Path(path)
    chain = [Path("/")]
    for item in p.parts[1:-1]:
        chain.append(chain[-1] / item)

    for d in chain:
        try:
            st = d.stat()
            can_enter = os.access(d, os.X_OK)
            can_read = os.access(d, os.R_OK)
            if not can_enter or not can_read:
                return (
                    f"{path}: 目录 `{d}` 权限不足（{_mode_str(st.st_mode)}）。"
                    f" 建议 `chmod 755 '{d}'`，或 `chown/chmod 750` 给服务用户。"
                )
        except FileNotFoundError:
            return f"{path}: 目录不存在 `{d}`。"
        except PermissionError:
            return f"{path}: 当前进程无权限访问目录 `{d}`。"
        except Exception as e:
            return f"{path}: 目录检查失败 `{d}`（{e}）。"

    try:
        st = p.stat()
        if not os.access(p, os.R_OK):
            return (
                f"{path}: 文件不可读（{_mode_str(st.st_mode)}）。"
                f" 建议 `chmod 644 '{p}'`，或 `chown/chmod 640` 给服务用户。"
            )
    except FileNotFoundError:
        return f"{path}: 数据库文件不存在 `{p}`。"
    except PermissionError:
        return f"{path}: 当前进程无权限访问数据库文件 `{p}`。"
    except Exception as e:
        return f"{path}: 文件检查失败 `{p}`（{e}）。"

    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1",
            uri=True,
            timeout=2.0,
        )
        conn.execute(probe_sql).fetchone()
        conn.close()
    except Exception as e:
        return f"{path}: 路径权限看起来正常，但数据库探测失败（{e}）。"
    return ""


def _collect_external_db_access_warnings(raw_cfg: dict, events: list[str]) -> list[str]:
    """按已选择事件收集外部数据库权限/可读性告警。"""
    monitor_events = set(events or [])
    warnings: list[str] = []

    backup_events = {"BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED"}
    trimmedia_events = {"TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS"}
    trimactivity_events = {"MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT"}
    photo_events = {"PHOTO_SHARE_CREATED", "PHOTO_SHARE_EXPIRED", "PHOTO_DEVICE_REGISTERED", "FACE_RECOGNITION_UPDATED"}

    if monitor_events & backup_events:
        issue = _check_db_access_issue(
            (raw_cfg.get("backup_db_path") or "").strip(),
            "SELECT id FROM operations ORDER BY id DESC LIMIT 1",
        )
        if issue:
            warnings.append(f"备份库: {issue}")
    if monitor_events & trimmedia_events:
        issue = _check_db_access_issue(
            (raw_cfg.get("trim_media_db_path") or "").strip(),
            "SELECT guid FROM item LIMIT 1",
        )
        if issue:
            warnings.append(f"trimmedia 库: {issue}")
    if monitor_events & trimactivity_events:
        issue = _check_db_access_issue(
            (raw_cfg.get("trim_activity_db_path") or "").strip(),
            "SELECT token FROM user_token LIMIT 1",
        )
        if issue:
            warnings.append(f"trimactivity 库: {issue}")
    if monitor_events & photo_events:
        issue = _check_db_access_issue(
            (raw_cfg.get("photo_db_path") or "").strip(),
            "SELECT id FROM share_link LIMIT 1",
        )
        if issue:
            warnings.append(f"相册库: {issue}")

    return warnings


def create_app(on_config_saved=None) -> Flask:
    """创建 Flask 应用。on_config_saved: 保存配置成功后的回调（用于热加载，无需重启）。"""
    app = Flask(__name__)
    icon_ver = str(int(ICON_FILE.stat().st_mtime)) if ICON_FILE.exists() else ""
    icon_url = f"/assets/icons/app-icon.png?v={icon_ver}" if icon_ver else ""
    favicon_url = f"/favicon.ico?v={icon_ver}" if icon_ver else ""
    gh_ver = str(int(GITHUB_ICON_FILE.stat().st_mtime)) if GITHUB_ICON_FILE.exists() else ""
    github_icon_url = f"/assets/icons/github.svg?v={gh_ver}" if gh_ver else "/assets/icons/github.svg"
    assets_dir = BASE_DIR / "assets"

    @app.get("/assets/<path:filename>")
    def serve_assets(filename: str):
        """提供项目 assets 目录下的静态文件。"""
        if not assets_dir.exists():
            abort(404)
        return send_from_directory(str(assets_dir), filename)

    @app.get("/favicon.ico")
    def favicon():
        """浏览器 favicon；复用 app-icon.png，避免 404。"""
        if ICON_FILE.exists():
            return send_from_directory(str(ICON_FILE.parent), ICON_FILE.name)
        abort(404)

    from notifier.multi_platform_notifier import MultiPlatformNotifier

    titles = MultiPlatformNotifier.EVENT_TITLES
    notes = MultiPlatformNotifier.EVENT_NOTES
    raw_cfg = _load_raw_config()
    logger_db_path = (raw_cfg.get("logger_db_path") or "/usr/trim/var/eventlogger_service/logger_data.db3").strip()
    backup_db_path = (raw_cfg.get("backup_db_path") or "/usr/trim/var/backup_service/basic_backup.db3").strip()
    trim_media_db_path = (raw_cfg.get("trim_media_db_path") or "").strip()
    trim_activity_db_path = (raw_cfg.get("trim_activity_db_path") or "").strip()
    photo_db_path = (raw_cfg.get("photo_db_path") or "").strip()
    events_by_category, valid_event_ids, discovered_vm_event_ids = build_events_for_ui(
        logger_db_path=logger_db_path,
        backup_db_path=backup_db_path,
        trim_media_db_path=trim_media_db_path,
        trim_activity_db_path=trim_activity_db_path,
        photo_db_path=photo_db_path,
        titles=titles,
        notes=notes,
    )

    CHANNEL_OPTIONS = [
        {"id": "wechat", "name": "企业微信"},
        {"id": "dingtalk", "name": "钉钉"},
        {"id": "feishu", "name": "飞书"},
        {"id": "bark", "name": "Bark"},
        {"id": "pushplus", "name": "PushPlus"},
        {"id": "magic_push", "name": "魔法推送"},
        {"id": "smtp", "name": "SMTP邮件"},
    ]

    PROTECTED_PATHS = {"/", "/history", "/api/config", "/api/save-config", "/api/test", "/api/push-stats"}
    PROTECTED_PREFIXES = ("/api/push-history",)

    @app.before_request
    def _require_auth():
        # 首页与 /history 的 GET 始终返回 HTML，由前端根据接口 401 跳转登录
        if request.path == "/":
            return None
        if request.path == "/history" and request.method == "GET":
            return None
        # 捐赠页与收款码：与配置页同一套密码与空闲超时（SESSION_IDLE_SECONDS，默认 300s）
        if request.path == "/support" and request.method == "GET":
            if not _has_password_set() or not _is_password_verification_enabled():
                return None
            if not _is_authenticated():
                return redirect("/")
            return None
        if request.path == "/faq" and request.method == "GET":
            if not _has_password_set() or not _is_password_verification_enabled():
                return None
            if not _is_authenticated():
                return redirect("/")
            return None
        if request.path.startswith("/support/img/") and request.method == "GET":
            if not _has_password_set() or not _is_password_verification_enabled():
                return None
            if not _is_authenticated():
                abort(403)
            return None
        if request.path not in PROTECTED_PATHS and not request.path.startswith(PROTECTED_PREFIXES):
            return None
        if not _has_password_set():
            return None
        if not _is_password_verification_enabled():
            return None
        if _is_authenticated():
            return None
        return jsonify({"ok": False, "message": "未登录或会话已过期，请重新输入密码。"}), 401

    @app.get("/api/auth/status")
    def auth_status():
        """无需登录即可访问。返回是否需要设置密码、是否需要登录、是否已认证。"""
        has_pw = _has_password_set()
        verification_enabled = _is_password_verification_enabled()
        authenticated = _is_authenticated()
        need_setup = not has_pw
        need_login = has_pw and verification_enabled and not authenticated
        return jsonify({
            "ok": True,
            "need_setup": need_setup,
            "need_login": need_login,
            "authenticated": authenticated,
        })

    @app.post("/api/auth/set-password")
    def auth_set_password():
        """首次设置密码（两次输入须一致）。"""
        if _has_password_set():
            return jsonify({"ok": False, "message": "已设置过密码，请使用登录。"}), 400
        payload = request.get_json(force=True, silent=True) or {}
        p1 = (payload.get("password") or "").strip()
        p2 = (payload.get("password_confirm") or "").strip()
        if not p1:
            return jsonify({"ok": False, "message": "请输入密码。"}), 400
        if len(p1) < 6:
            return jsonify({"ok": False, "message": "密码长度至少 6 位。"}), 400
        if p1 != p2:
            return jsonify({"ok": False, "message": "两次输入的密码不一致。"}), 400
        salt = secrets.token_hex(16)
        stored_hash = _hash_password(p1, bytes.fromhex(salt))
        raw = _load_raw_config()
        raw["web_password_salt"] = salt
        raw["web_password_hash"] = stored_hash
        try:
            _save_raw_config(raw)
        except Exception as e:
            return jsonify({"ok": False, "message": f"保存失败：{e}"}), 500
        session_id = _create_session()
        resp = jsonify({"ok": True, "message": "密码设置成功。"})
        resp.set_cookie(
            AUTH_COOKIE_NAME,
            session_id,
            max_age=SESSION_IDLE_SECONDS,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        return resp

    @app.post("/api/auth/login")
    def auth_login():
        """使用密码登录。"""
        if not _has_password_set():
            return jsonify({"ok": False, "message": "尚未设置密码。"}), 400
        payload = request.get_json(force=True, silent=True) or {}
        password = (payload.get("password") or "").strip()
        if not password:
            return jsonify({"ok": False, "message": "请输入密码。"}), 400
        raw = _load_raw_config()
        salt, stored_hash = _get_password_config(raw)
        if not salt or not stored_hash:
            return jsonify({"ok": False, "message": "密码配置无效，请重新设置密码。"}), 400
        if not _verify_password(password, salt, stored_hash):
            return jsonify({"ok": False, "message": "密码错误。"}), 401
        session_id = _create_session()
        resp = jsonify({"ok": True, "message": "登录成功。"})
        resp.set_cookie(
            AUTH_COOKIE_NAME,
            session_id,
            max_age=SESSION_IDLE_SECONDS,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        return resp

    @app.get("/api/config")
    def get_config():
        raw = _load_raw_config()

        # 迁移：旧版默认多勾了「应用启动/自启动失败、UPS 开启/关闭」或「应用生命周期」时，改为新默认并回写
        raw_events = raw.get("monitor_events")
        if isinstance(raw_events, list):
            raw_set = set(raw_events)
            new_default_set = set(DEFAULT_SELECTED_EVENTS)
            # 仅当配置恰好为「旧版默认（含启动失败/UPS 开关）」时迁移为新默认；不迁移「全选」（= 新默认+生命周期+额外），否则会覆盖用户的全选
            old_default_with_extra = new_default_set | OLD_DEFAULT_SELECTED_EVENTS_WITH_EXTRA
            old_full_default = new_default_set | APP_LIFECYCLE_EVENTS
            if raw_set == old_default_with_extra:
                raw["monitor_events"] = DEFAULT_SELECTED_EVENTS
                _save_raw_config(raw)
                monitor_events = DEFAULT_SELECTED_EVENTS
            elif raw_set == old_full_default:
                filtered = [e for e in raw_events if e not in APP_LIFECYCLE_EVENTS]
                raw["monitor_events"] = filtered
                _save_raw_config(raw)
                monitor_events = filtered
            else:
                monitor_events = raw_events
        else:
            monitor_events = DEFAULT_SELECTED_EVENTS

        channels = []
        for ch_type, key in [
            ("wechat", "wechat_webhook_url"),
            ("dingtalk", "dingtalk_webhook_url"),
            ("feishu", "feishu_webhook_url"),
            ("bark", "bark_url"),
            ("pushplus", "pushplus_params"),
            ("magic_push", "magic_push_params"),
            ("smtp", "smtp_params"),
        ]:
            for url in _split_urls(raw.get(key, "")):
                # 过滤掉模板中的 ${WECHAT_WEBHOOK_URL} 这类占位符
                if url.startswith("${") and url.endswith("}"):
                    continue
                channels.append({"type": ch_type, "url": url})

        data = {
            "title": "FnMessageBot",
            "subtitle": "飞牛日志消息推送机器人",
            "version": "2.2.0",
            "events_by_category": events_by_category,
            "selected_events": monitor_events,
            "channels": channels,
            "title_prefix": _title_prefix_from_dict(raw),
            "log_retention_days": int(raw.get("log_retention_days", raw.get("max_log_age", 7))),
            "logger_poll_interval": int(raw.get("logger_poll_interval", 3)),
            "dnd_enabled": _as_bool(raw.get("dnd_enabled", False), False),
            "dnd_start_time": (raw.get("dnd_start_time") or "22:00").strip(),
            "dnd_end_time": (raw.get("dnd_end_time") or "07:00").strip(),
            "web_password_enabled": _as_bool(raw.get("web_password_enabled", True), True),
            "poll_batch_summary_enabled": _as_bool(raw.get("poll_batch_summary_enabled", False), False),
            "minimal_push_enabled": _as_bool(raw.get("minimal_push_enabled", False), False),
            "channel_options": CHANNEL_OPTIONS,
        }
        db_access_warnings = _collect_external_db_access_warnings(raw, monitor_events)
        return jsonify({"ok": True, "data": data, "warnings": db_access_warnings})

    @app.post("/api/save-config")
    def save_config():
        payload = request.get_json(force=True, silent=True) or {}

        events = payload.get("events") or []
        # 只保留后端认可的事件 ID，避免写入非法值导致热加载或重启异常
        events = [e for e in events if e in valid_event_ids]
        channels = payload.get("channels") or []
        log_retention_days = payload.get("log_retention_days", 7)
        logger_poll_interval = payload.get("logger_poll_interval", 3)
        dnd_enabled = _as_bool(payload.get("dnd_enabled", False), False)
        dnd_start_time = (payload.get("dnd_start_time") or "22:00").strip()
        dnd_end_time = (payload.get("dnd_end_time") or "07:00").strip()
        web_password_enabled = _as_bool(payload.get("web_password_enabled", True), True)
        poll_batch_summary_enabled = _as_bool(payload.get("poll_batch_summary_enabled", False), False)
        minimal_push_enabled = _as_bool(payload.get("minimal_push_enabled", False), False)
        title_prefix = _title_prefix_from_dict(payload)
        if title_prefix and len(title_prefix) > 20:
            return jsonify({"ok": False, "message": "标题前缀过长（最多 20 个字符）。"}), 400

        if dnd_enabled:
            if not dnd_start_time or not dnd_end_time:
                return jsonify({"ok": False, "message": "开启勿扰模式时请填写开始时间和结束时间。"}), 400
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", dnd_start_time):
                return jsonify({"ok": False, "message": "勿扰开始时间格式不正确，请使用 HH:MM（如 22:00）。"}), 400
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", dnd_end_time):
                return jsonify({"ok": False, "message": "勿扰结束时间格式不正确，请使用 HH:MM（如 07:00）。"}), 400

        # 不允许选择内部保留事件
        if EVENT_IDS_HIDDEN_IN_UI & set(events):
            return jsonify({"ok": False, "message": "包含不可选的事件类型，请刷新页面重试。"}), 400

        # 基本校验
        if not events:
            return jsonify({"ok": False, "message": "请至少选择一个事件类型。"}), 400

        if not channels:
            return jsonify({"ok": False, "message": "请至少配置一个推送渠道。"}), 400

        for ch in channels:
            ch_type = ch.get("type")
            url = (ch.get("url") or "").strip()
            if ch_type not in {"wechat", "dingtalk", "feishu", "bark", "pushplus", "magic_push", "smtp"}:
                return jsonify({"ok": False, "message": "存在未知的推送渠道类型。"}), 400
            if not url:
                return jsonify({"ok": False, "message": "推送渠道地址不能为空。"}), 400
            if ch_type == "pushplus":
                try:
                    obj = json.loads(url)
                    if not isinstance(obj, dict) or "token" not in obj:
                        return jsonify({"ok": False, "message": "PushPlus 参数必须是包含 token 的 JSON 对象。"}), 400
                except json.JSONDecodeError as e:
                    return jsonify({"ok": False, "message": f"PushPlus 参数不是合法 JSON：{e}"}), 400
            elif ch_type == "magic_push":
                try:
                    obj = json.loads(url)
                    if not isinstance(obj, dict):
                        return jsonify({"ok": False, "message": "魔法推送配置须为 JSON 对象。"}), 400
                    base = (obj.get("base_url") or "").strip()
                    token = (obj.get("token") or "").strip()
                    if not base or not token:
                        return jsonify({"ok": False, "message": "魔法推送须填写基础 URL 与 Token。"}), 400
                    if not base.startswith("http"):
                        return jsonify({"ok": False, "message": "魔法推送基础 URL 须为 http(s) 地址。"}), 400
                except json.JSONDecodeError as e:
                    return jsonify({"ok": False, "message": f"魔法推送配置不是合法 JSON：{e}"}), 400
            elif ch_type == "smtp":
                try:
                    obj = json.loads(url)
                    if not isinstance(obj, dict):
                        return jsonify({"ok": False, "message": "SMTP 配置须为 JSON 对象。"}), 400
                    server = (obj.get("server") or "").strip()
                    username = (obj.get("username") or "").strip()
                    password = obj.get("password") or ""
                    to_raw = (obj.get("to") or "").strip()
                    if not server or not username or not password or not to_raw:
                        return jsonify({"ok": False, "message": "SMTP 须填写服务器、用户名、密码和收件人地址。"}), 400
                    try:
                        int(obj.get("port", 465))
                    except (TypeError, ValueError):
                        return jsonify({"ok": False, "message": "SMTP 端口必须是整数。"}), 400
                except json.JSONDecodeError as e:
                    return jsonify({"ok": False, "message": f"SMTP 配置不是合法 JSON：{e}"}), 400
            elif not url.startswith("http"):
                return (
                    jsonify({"ok": False, "message": f"推送地址格式不正确：{url}"}),
                    400,
                )

        if log_retention_days is None:
            log_retention_days = 7
        if logger_poll_interval is None:
            logger_poll_interval = 3
        try:
            log_retention_days = int(log_retention_days)
            logger_poll_interval = int(logger_poll_interval)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "日志缓存天数和轮询时间必须是整数。"}), 400

        if log_retention_days <= 0:
            return jsonify({"ok": False, "message": "日志缓存天数必须大于 0。"}), 400
        if logger_poll_interval <= 0:
            return jsonify({"ok": False, "message": "数据库轮询时间必须大于 0 秒。"}), 400

        # 归并渠道为每种类型一个以 '|' 分隔的字符串，兼容现有配置结构
        wechat_urls = []
        dingtalk_urls = []
        feishu_urls = []
        bark_urls = []
        pushplus_urls = []
        magic_push_urls = []
        smtp_urls = []
        for ch in channels:
            ch_type = ch.get("type")
            url = (ch.get("url") or "").strip()
            if ch_type == "wechat":
                wechat_urls.append(url)
            elif ch_type == "dingtalk":
                dingtalk_urls.append(url)
            elif ch_type == "feishu":
                feishu_urls.append(url)
            elif ch_type == "bark":
                bark_urls.append(url)
            elif ch_type == "pushplus":
                pushplus_urls.append(url)
            elif ch_type == "magic_push":
                magic_push_urls.append(url)
            elif ch_type == "smtp":
                smtp_urls.append(url)

        raw = _load_raw_config()
        raw.update(
            {
                "wechat_webhook_url": _join_urls(wechat_urls),
                "dingtalk_webhook_url": _join_urls(dingtalk_urls),
                "feishu_webhook_url": _join_urls(feishu_urls),
                "bark_url": _join_urls(bark_urls),
                "pushplus_params": _join_urls(pushplus_urls),
                "magic_push_params": _join_urls(magic_push_urls),
                "smtp_params": _join_urls(smtp_urls),
                "monitor_events": events,
                "log_retention_days": log_retention_days,
                "logger_poll_interval": logger_poll_interval,
                "dnd_enabled": dnd_enabled,
                "dnd_start_time": dnd_start_time,
                "dnd_end_time": dnd_end_time,
                "web_password_enabled": web_password_enabled,
                "poll_batch_summary_enabled": poll_batch_summary_enabled,
                "minimal_push_enabled": minimal_push_enabled,
                "title_prefix": title_prefix,
            }
        )

        try:
            _save_raw_config(raw)
        except Exception as e:
            return jsonify({"ok": False, "message": f"配置写入失败（{e}），请检查 config 目录是否可写。"}), 500

        db_access_warnings = _collect_external_db_access_warnings(raw, events)

        if callable(on_config_saved):
            try:
                on_config_saved()
            except Exception as e:
                return jsonify({
                    "ok": True,
                    "message": f"配置已保存，但热加载失败（{e}），请重启容器后生效。",
                    "warnings": db_access_warnings,
                }), 200

        return jsonify({
            "ok": True,
            "message": "配置已保存，监控已热加载生效，无需重启容器。",
            "warnings": db_access_warnings,
        })

    @app.post("/api/test")
    def test_push():
        try:
            payload = request.get_json(force=True, silent=True) or {}
            content = (payload.get("content") or "").strip()
            if not content:
                return jsonify({"ok": False, "message": "请输入要测试的内容。"}), 400

            raw = _load_raw_config()
            notifier = _build_notifier_from_raw(raw)

            out = notifier.send_system_notification(
                "TEST_PUSH",
                content,
                {
                    "hostname": socket.gethostname(),
                    "version": "2.2.0",
                },
            )
            ok = out.get("success", False) if isinstance(out, dict) else bool(out)
            if ok:
                return jsonify({"ok": True, "message": "测试消息已发送，请检查各渠道是否收到。"})
            return jsonify({"ok": False, "message": "所有渠道发送失败，请检查配置。"}), 500
        except Exception as e:
            return jsonify({"ok": False, "message": f"测试发送异常：{e}"}), 500

    @app.get("/api/push-stats")
    def get_push_stats():
        """推送数据汇总：总条数/成功/失败，当日条数/成功/失败（基于 SQLite push_history）。"""
        try:
            stats = get_push_history_stats(_load_raw_config)
            return jsonify({
                "ok": True,
                "data": stats,
            })
        except Exception:
            return jsonify({
                "ok": True,
                "data": {
                    "total": {"total": 0, "success": 0, "fail": 0},
                    "today": {"total": 0, "success": 0, "fail": 0},
                },
            })

    @app.get("/api/push-history")
    def get_push_history():
        """推送记录列表：分页，可选按成功/失败筛选。"""
        try:
            limit = min(100, max(1, request.args.get("limit", 50, type=int)))
            offset = max(0, request.args.get("offset", 0, type=int))
            success_filter = _parse_success_filter(request.args.get("success"))
            rows = list_push_history_records(
                _load_raw_config,
                limit=limit,
                offset=offset,
                success_filter=success_filter,
            )
            return jsonify({"ok": True, "data": rows})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.get("/api/push-history/<int:record_id>")
    def get_push_history_detail(record_id):
        """单条推送记录详情。"""
        try:
            row = get_push_history_record(_load_raw_config, record_id)
            if row is None:
                return jsonify({"ok": False, "message": "记录不存在"}), 404
            return jsonify({"ok": True, "data": row})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.get("/history")
    def history_page():
        """推送记录二级页：列表 + 筛选 + 加载更多 + 查看详情。"""
        return render_template_string(HISTORY_PAGE_TEMPLATE, favicon_url=favicon_url)

    @app.get("/support/img/<path:name>")
    def support_qr(name: str):
        """支持作者页收款码（仅允许白名单文件名）。"""
        safe = Path(name).name
        if safe not in SUPPORT_QR_FILENAMES:
            abort(404)
        if not SUPPORT_QR_DIR.is_dir():
            abort(404)
        target = SUPPORT_QR_DIR / safe
        if not target.is_file():
            abort(404)
        return send_from_directory(str(SUPPORT_QR_DIR), safe)

    @app.get("/support")
    def support_page():
        """支持作者：展示 README 中与捐赠说明一致的收款二维码。"""
        wechat_src = (
            "/support/img/wechat_pay.jpg"
            if SUPPORT_QR_DIR.is_dir() and (SUPPORT_QR_DIR / "wechat_pay.jpg").is_file()
            else ""
        )
        ali_src = (
            "/support/img/ali_pay.jpg"
            if SUPPORT_QR_DIR.is_dir() and (SUPPORT_QR_DIR / "ali_pay.jpg").is_file()
            else ""
        )
        return render_template_string(
            SUPPORT_PAGE_TEMPLATE,
            favicon_url=favicon_url,
            wechat_src=wechat_src,
            ali_src=ali_src,
        )

    @app.get("/faq")
    def faq_page():
        """常见问题页。"""
        return render_template_string(FAQ_PAGE_TEMPLATE, favicon_url=favicon_url)

    @app.get("/")
    def index():
        # 单页应用，使用简单的原生 JS
        return render_template_string(
            """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  {% if favicon_url %}
  <link rel="icon" href="{{ favicon_url }}" />
  {% endif %}
<title>FnMessageBot</title>
  <style>
    * { box-sizing: border-box; }
    :root { color-scheme: light; }
    html[data-theme="dark"] { color-scheme: dark; }
    body {
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", "PingFang SC", sans-serif;
      background: #eef3ff;
      color: #1f2933;
    }
    .page {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px 16px;
    }
    .card {
      width: 100%;
      max-width: 960px;
      background: rgba(255,255,255,0.9);
      border-radius: 16px;
      box-shadow: 0 18px 40px rgba(15,23,42,0.18);
      padding: 32px 40px 40px;
      border: 1px solid rgba(148,163,184,0.32);
      backdrop-filter: blur(10px);
    }
    .header {
      text-align: center;
      margin-bottom: 28px;
      position: relative;
    }
    .theme-switcher {
      position: absolute;
      top: 0;
      right: 0;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: #6b7280;
    }
    .theme-buttons {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .theme-btn {
      width: 30px;
      height: 30px;
      border-radius: 999px;
      border: 1px solid #d1d5db;
      background: #fff;
      color: #4b5563;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 14px;
      line-height: 1;
      padding: 0;
      transition: background-color .15s, border-color .15s, color .15s, transform .05s;
    }
    .theme-btn:hover {
      background: #f3f4f6;
      border-color: #9ca3af;
    }
    .theme-btn.active {
      background: #2563eb;
      border-color: #1d4ed8;
      color: #fff;
    }
    .header-brand {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      margin-bottom: 6px;
    }
    .app-icon {
      width: 34px;
      height: 34px;
      object-fit: cover;
      flex-shrink: 0;
    }
    .header-title {
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0.06em;
      color: #111827;
      margin-bottom: 0;
      display: block;
    }
    .header-sub {
      font-size: 14px;
      color: #6b7280;
      margin-bottom: 4px;
    }
    .header-ver {
      font-size: 13px;
      color: #9ca3af;
    }
    .stats-section { margin-bottom: 16px; }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 20px;
    }
    .stats-block {
      padding: 10px 14px;
      background: #fff;
      border-radius: 10px;
      border: 1px solid #e5e7eb;
    }
    .stats-label {
      font-size: 13px;
      font-weight: 600;
      color: #374151;
      margin-bottom: 6px;
    }
    .stats-row {
      display: flex;
      flex-wrap: wrap;
      gap: 12px 16px;
      font-size: 13px;
      color: #6b7280;
    }
    .stats-row .stats-total { color: #111827; }
    .stats-row .stats-ok { color: #059669; }
    .stats-row .stats-fail { color: #dc2626; }
    .section {
      border-radius: 12px;
      padding: 18px 20px 16px;
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      margin-bottom: 16px;
    }
    .section-title {
      font-size: 15px;
      font-weight: 600;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      color: #111827;
    }
    .section-title span {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .section-title small {
      font-weight: 400;
      font-size: 12px;
      color: #9ca3af;
    }
    .events-by-category { max-height: 420px; overflow: auto; padding-right: 4px; }
    .event-category { margin-bottom: 22px; }
    .event-category-title {
      font-size: 15px; font-weight: 650; color: #111827;
      margin-bottom: 8px; padding-bottom: 4px;
      border-bottom: 1px solid #e5e7eb;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .events-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 10px 14px;
      padding-right: 4px;
    }
    .event-item {
      font-size: 13px;
      display: flex;
      align-items: flex-start;
      gap: 8px;
      color: #374151;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid #e5e7eb;
      background: #fafafa;
      transition: background 0.15s, border-color 0.15s;
    }
    .event-item:hover {
      background: #f3f4f6;
      border-color: #d1d5db;
    }
    .event-item input {
      margin-top: 3px;
      flex-shrink: 0;
    }
    .event-item span {
      line-height: 1.4;
    }
    .event-item .field-helper {
      margin-top: 4px;
      margin-bottom: 0;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      background: #e0f2fe;
      color: #0369a1;
      border: 1px solid #bae6fd;
    }
    .channels-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }
    .add-btn {
      border-radius: 999px;
      border: none;
      background: #2563eb;
      color: #fff;
      font-size: 12px;
      padding: 4px 10px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
    }
    .add-btn span {
      font-size: 14px;
    }
    .add-btn:hover {
      background: #1d4ed8;
    }
    .channels-table {
      width: 100%;
      border-collapse: collapse;
    }
    .table-wrap {
      width: 100%;
      overflow-x: auto;
      border-radius: 8px;
    }
    .channels-table th,
    .channels-table td {
      padding: 6px 8px;
      font-size: 13px;
      text-align: left;
    }
    .channels-table thead th {
      color: #6b7280;
      font-weight: 500;
      border-bottom: 1px solid #e5e7eb;
    }
    .channels-table tbody tr:not(:last-child) td {
      border-bottom: 1px solid #f3f4f6;
    }
    select,
    input[type="text"],
    input[type="number"],
    textarea {
      width: 100%;
      padding: 7px 9px;
      border-radius: 8px;
      border: 1px solid #d1d5db;
      font-size: 13px;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s, background-color 0.15s;
      background-color: #ffffff;
    }
    select:focus,
    input:focus,
    textarea:focus {
      border-color: #2563eb;
      box-shadow: 0 0 0 1px rgba(37,99,235,0.25);
    }
    textarea {
      resize: vertical;
      min-height: 70px;
    }
    .btn {
      min-width: 96px;
      border-radius: 999px;
      padding: 8px 20px;
      border: none;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: background-color 0.15s, box-shadow 0.15s, transform 0.05s;
    }
    .btn-primary {
      background: linear-gradient(135deg,#2563eb,#1d4ed8);
      color: #fff;
      box-shadow: 0 12px 22px rgba(37,99,235,0.28);
    }
    .btn-primary:hover {
      background: linear-gradient(135deg,#1d4ed8,#1e40af);
      box-shadow: 0 14px 26px rgba(37,99,235,0.3);
      transform: translateY(-1px);
    }
    .btn-ghost {
      background: #fff;
      color: #111827;
      border: 1px solid #d1d5db;
    }
    .btn-ghost:hover {
      background: #f3f4f6;
    }
    .btn-danger {
      background: #fee2e2;
      color: #b91c1c;
      border-radius: 999px;
      border: none;
      padding: 4px 10px;
      font-size: 12px;
      cursor: pointer;
    }
    .btn-danger:hover {
      background: #fecaca;
    }
    .footer-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 16px;
    }
    .home-footer {
      margin-top: 24px;
      padding-top: 20px;
      border-top: 1px solid #e5e7eb;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: center;
      gap: 8px 14px;
      font-size: 13px;
      color: #6b7280;
    }
    .home-footer .footer-link {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: #4b5563;
      text-decoration: none;
    }
    .home-footer .footer-link:hover {
      color: #2563eb;
      text-decoration: underline;
    }
    .home-footer .footer-github-icon {
      width: 18px;
      height: 18px;
      flex-shrink: 0;
      display: block;
    }
    .home-footer .footer-sep {
      color: #d1d5db;
      user-select: none;
    }
    .system-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px 16px;
    }
    .field-label {
      font-size: 13px;
      color: #4b5563;
      margin-bottom: 4px;
    }
    .field-helper {
      font-size: 11px;
      color: #9ca3af;
      margin-top: 2px;
      /* 推送事件副标题：最多展示两行，超出打点省略 */
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .test-section {
      margin-top: 20px;
    }
    .status-bar {
      margin-top: 10px;
      font-size: 12px;
      min-height: 18px;
    }
    .status-bar span {
      padding: 3px 10px;
      border-radius: 999px;
    }
    .status-ok span {
      background: #dcfce7;
      color: #166534;
    }
    .status-error span {
      background: #fee2e2;
      color: #b91c1c;
    }
    .warning-panel {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #fecaca;
      background: #fef2f2;
      color: #991b1b;
      font-size: 12px;
      line-height: 1.6;
      display: none;
    }
    .warning-panel.show {
      display: block;
    }
    .warning-panel-title {
      font-weight: 700;
      margin-bottom: 4px;
    }
    .warning-panel-item {
      word-break: break-word;
      margin: 2px 0;
    }
    .toast-container {
      position: fixed;
      top: 20px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 9999;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      pointer-events: none;
    }
    .toast {
      padding: 12px 20px;
      border-radius: 10px;
      font-size: 14px;
      font-weight: 500;
      box-shadow: 0 10px 30px rgba(0,0,0,0.18);
      animation: toast-in 0.25s ease-out;
      pointer-events: auto;
    }
    .toast.toast-ok {
      background: #059669;
      color: #fff;
    }
    .toast.toast-error {
      background: #dc2626;
      color: #fff;
    }
    @keyframes toast-in {
      from {
        opacity: 0;
        transform: translateY(-12px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    @media (max-width: 768px) {
      .page {
        align-items: flex-start;
        padding: 12px;
      }
      .card {
        padding: 16px 12px 18px;
        border-radius: 12px;
        max-width: 100%;
      }
      .theme-switcher {
        position: absolute;
        top: -2px;
        right: 0;
        justify-content: flex-end;
        width: auto;
        margin-bottom: 0;
      }
      .theme-switcher span { display: none; }
      .theme-buttons { gap: 4px; }
      .theme-btn { width: 28px; height: 28px; font-size: 13px; }
      .header { margin-bottom: 16px; }
      .header-brand { gap: 8px; margin-bottom: 4px; padding-right: 74px; }
      .app-icon { width: 30px; height: 30px; }
      .header-title {
        font-size: 22px;
        letter-spacing: 0.02em;
        max-width: calc(100vw - 130px);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .header-sub { font-size: 13px; }
      .header-ver { font-size: 12px; }
      .section { padding: 12px; margin-bottom: 12px; }
      .section-title { font-size: 14px; margin-bottom: 8px; }
      .events-by-category { max-height: none; }
      .events-grid { grid-template-columns: 1fr; gap: 8px; padding-right: 0; }
      .event-item { padding: 8px 10px; }
      .channels-header { flex-wrap: wrap; gap: 8px; }
      .channels-table { min-width: 760px; }
      .footer-actions { justify-content: stretch; }
      .footer-actions .btn { width: 100%; min-width: 0; }
      .btn { min-width: 0; }
      .system-grid {
        grid-template-columns: 1fr;
        gap: 10px;
      }
      .stats-grid {
        grid-template-columns: 1fr;
      }
      textarea { min-height: 88px; }
      .home-footer { margin-top: 16px; padding-top: 16px; gap: 6px 10px; font-size: 12px; }
    }
    html[data-theme="dark"] .home-footer {
      border-top-color: #374151;
      color: #9ca3af;
    }
    html[data-theme="dark"] .home-footer .footer-link {
      color: #d1d5db;
    }
    html[data-theme="dark"] .home-footer .footer-github-icon {
      filter: invert(1) brightness(1.05);
    }
    .license-footer {
      margin: 0;
      padding-top: 12px;
      text-align: center;
      font-size: 11px;
      line-height: 1.5;
      color: #9ca3af;
    }
    html[data-theme="dark"] .license-footer {
      color: #6b7280;
    }
    html[data-theme="dark"] body {
      background: #111827;
      color: #e5e7eb;
    }
    html[data-theme="dark"] .card {
      background: rgba(17,24,39,0.9);
      border-color: rgba(75,85,99,0.55);
      box-shadow: 0 18px 40px rgba(0,0,0,0.45);
    }
    html[data-theme="dark"] .section {
      background: #111827;
      border-color: #374151;
    }
    html[data-theme="dark"] .header-title { color: #f9fafb; }
    html[data-theme="dark"] .section-title { color: #f9fafb; }
    html[data-theme="dark"] .section-title small { color: #9ca3af; }
    html[data-theme="dark"] .event-category-title {
      color: #e5e7eb;
      border-bottom-color: #374151;
    }
    html[data-theme="dark"] .stats-label { color: #d1d5db; }
    html[data-theme="dark"] .event-item span { color: #e5e7eb; }
    html[data-theme="dark"] .header-sub,
    html[data-theme="dark"] .header-ver,
    html[data-theme="dark"] .field-label,
    html[data-theme="dark"] .field-helper,
    html[data-theme="dark"] .theme-switcher {
      color: #9ca3af;
    }
    html[data-theme="dark"] .theme-btn {
      background: #111827;
      border-color: #4b5563;
      color: #d1d5db;
    }
    html[data-theme="dark"] .theme-btn:hover {
      background: #1f2937;
      border-color: #6b7280;
    }
    html[data-theme="dark"] .theme-btn.active {
      background: #2563eb;
      border-color: #1d4ed8;
      color: #fff;
    }
    html[data-theme="dark"] .stats-block {
      background: #0f172a;
      border-color: #374151;
    }
    html[data-theme="dark"] .stats-row .stats-total { color: #e5e7eb; }
    html[data-theme="dark"] .event-item {
      background: #0f172a;
      border-color: #374151;
      color: #d1d5db;
    }
    html[data-theme="dark"] .event-item:hover {
      background: #111827;
      border-color: #4b5563;
    }
    html[data-theme="dark"] .channels-table thead th {
      border-bottom-color: #374151;
      color: #9ca3af;
    }
    html[data-theme="dark"] .channels-table tbody tr:not(:last-child) td {
      border-bottom-color: #1f2937;
    }
    html[data-theme="dark"] select,
    html[data-theme="dark"] input[type="text"],
    html[data-theme="dark"] input[type="number"],
    html[data-theme="dark"] input[type="password"],
    html[data-theme="dark"] textarea {
      background-color: #111827;
      color: #e5e7eb;
      border-color: #4b5563;
    }
    html[data-theme="dark"] .btn-ghost {
      background: #111827;
      color: #e5e7eb;
      border-color: #4b5563;
    }
    html[data-theme="dark"] .btn-ghost:hover {
      background: #1f2937;
    }
    html[data-theme="dark"] .auth-page { background: #111827; }
    html[data-theme="dark"] .auth-card {
      background: rgba(17,24,39,0.95);
      border-color: rgba(75,85,99,0.55);
      box-shadow: 0 18px 40px rgba(0,0,0,0.45);
    }
    html[data-theme="dark"] .auth-title { color: #f9fafb; }
    html[data-theme="dark"] .auth-sub { color: #9ca3af; }
    .auth-page {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 32px 16px;
      background: #eef3ff;
      gap: 16px;
    }
    .auth-card {
      width: 100%;
      max-width: 420px;
      background: rgba(255,255,255,0.95);
      border-radius: 16px;
      box-shadow: 0 18px 40px rgba(15,23,42,0.18);
      padding: 32px 40px;
      border: 1px solid rgba(148,163,184,0.32);
    }
    .auth-title {
      font-size: 20px;
      font-weight: 600;
      color: #111827;
      margin-bottom: 8px;
      text-align: center;
    }
    .auth-sub {
      font-size: 13px;
      color: #6b7280;
      margin-bottom: 20px;
      text-align: center;
    }
    .auth-form .field-label { font-size: 13px; color: #4b5563; margin-bottom: 4px; }
    .auth-form .field-label + input { margin-bottom: 12px; }
    .auth-form .btn-block { width: 100%; margin-top: 16px; }
    .auth-msg {
      font-size: 13px;
      margin-top: 12px;
      min-height: 18px;
      text-align: center;
    }
    .auth-msg.error { color: #b91c1c; }
    .auth-msg.ok { color: #166534; }
    .auth-hint {
      font-size: 12px;
      color: #9ca3af;
      margin-top: 16px;
      text-align: center;
    }
    input[type="password"] {
      width: 100%;
      padding: 7px 9px;
      border-radius: 8px;
      border: 1px solid #d1d5db;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <div id="auth-gate" class="auth-page" style="display:none;">
    <div class="auth-card">
      <div id="auth-set-password" style="display:none;">
        <div class="auth-title">设置访问密码</div>
        <div class="auth-sub">首次使用或已清除密码后，请设置新密码（至少 6 位）</div>
        <form class="auth-form" id="form-set-password">
          <div class="field-label">密码</div>
          <input type="password" id="set-pw-password" placeholder="请输入密码" autocomplete="new-password" />
          <div class="field-label">确认密码</div>
          <input type="password" id="set-pw-confirm" placeholder="请再次输入密码" autocomplete="new-password" />
          <button type="submit" class="btn btn-primary btn-block">确认设置</button>
        </form>
        <div id="auth-set-msg" class="auth-msg"></div>
      </div>
      <div id="auth-login" style="display:none;">
        <div class="auth-title">输入访问密码</div>
        <div class="auth-sub">会话有效期为 5 分钟，关闭页面后若未超时无需重新输入</div>
        <form class="auth-form" id="form-login">
          <div class="field-label">密码</div>
          <input type="password" id="login-password" placeholder="请输入密码" autocomplete="current-password" />
          <button type="submit" class="btn btn-primary btn-block">登录</button>
        </form>
        <div id="auth-login-msg" class="auth-msg"></div>
      </div>
    </div>
    <p class="license-footer">© 2024 Sunanang · FnMessageBot · MIT License terms apply.</p>
  </div>
  <div id="app-main" class="page" style="display:none;">
    <div class="card">
      <div class="header">
        <div class="theme-switcher">
          <button type="button" class="theme-btn" id="theme-toggle-btn" title="切换到深色模式" aria-label="切换到深色模式">🌙</button>
        </div>
        <div class="header-brand">
          {% if icon_url %}
          <img class="app-icon" src="{{ icon_url }}" alt="FnMessageBot" />
          {% endif %}
          <div class="header-title" id="app-title">FnMessageBot</div>
        </div>
        <div class="header-sub" id="app-subtitle">飞牛日志消息推送机器人</div>
        <div class="header-ver" id="app-version">2.2.0</div>
      </div>

      <div class="section stats-section">
        <div class="channels-header">
          <div class="section-title">
            <span>推送数据汇总</span>
          </div>
          <button type="button" class="btn btn-ghost" onclick="window.location.href='/history'">查看推送记录</button>
        </div>
        <div class="stats-grid">
          <div class="stats-block">
            <div class="stats-label">总推送</div>
            <div class="stats-row">
              <span class="stats-total">共 <strong id="stat-total-total">0</strong> 条</span>
              <span class="stats-ok">成功 <strong id="stat-total-success">0</strong></span>
              <span class="stats-fail">失败 <strong id="stat-total-fail">0</strong></span>
            </div>
          </div>
          <div class="stats-block">
            <div class="stats-label">当日推送</div>
            <div class="stats-row">
              <span class="stats-total">共 <strong id="stat-today-total">0</strong> 条</span>
              <span class="stats-ok">成功 <strong id="stat-today-success">0</strong></span>
              <span class="stats-fail">失败 <strong id="stat-today-fail">0</strong></span>
            </div>
          </div>
        </div>
      </div>

      <div class="section">
        <div class="section-title">
          <span>事件选择 <small>请选择需要监控并推送的事件</small></span>
        </div>
        <div class="events-by-category" id="events-container"></div>
      </div>

      <div class="section">
        <div class="channels-header">
          <div class="section-title">
            <span>推送渠道 <small>支持为同一渠道配置多个 Webhook</small></span>
          </div>
          <button class="add-btn" type="button" id="add-channel-btn">
            <span>＋</span> 添加渠道
          </button>
        </div>
        <div class="table-wrap">
          <table class="channels-table">
            <thead>
            <tr>
              <th style="width: 120px;">渠道类型</th>
              <th>推送地址</th>
              <th style="width: 64px; text-align: right;">操作</th>
            </tr>
            </thead>
            <tbody id="channels-body"></tbody>
          </table>
        </div>
        <div class="field-helper" style="margin-top: 10px;">
          渠道配置教程：
          <a href="https://github.com/Sunanang/FNMessageBots/blob/main/docs/notification-channels.md" target="_blank" rel="noopener noreferrer">
            推送渠道配置教程
          </a>
        </div>
      </div>

      <div class="section">
        <div class="section-title">
          <span>系统设置 <small>影响日志缓存与轮询行为</small></span>
        </div>
        <div>
          <div class="field-label" style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
            <input type="checkbox" id="input-web-password-enabled" />
            <span>开启密码验证</span>
          </div>
          <div class="field-helper">关闭后无需输入密码即可访问配置页，本地密码仍保留，可随时重新开启。</div>
        </div>
        <div class="system-grid" style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e5e7eb;">
          <div>
            <div class="field-label">日志缓存天数 (day)</div>
            <input id="input-log-days" type="number" min="1" />
            <div class="field-helper">原始推送日志的保留时长。</div>
          </div>
          <div>
            <div class="field-label">数据库轮询时间 (s)</div>
            <input id="input-poll-interval" type="number" min="1" />
            <div class="field-helper">轮询日志数据库的间隔时间，过小会增加磁盘 IO。</div>
          </div>
          <div>
            <div class="field-label">事件标题前缀</div>
            <input id="input-title-prefix" type="text" placeholder="飞牛NAS" />
            <div class="field-helper">默认「飞牛NAS」，内容为空则无前缀。</div>
          </div>
          <div>
            <div class="field-label" style="display: flex; align-items: center; gap: 8px;">
              <input type="checkbox" id="input-poll-batch-summary" />
              <span>轮询汇总模式</span>
            </div>
            <div class="field-helper">开启后短时间内的多条事件合并为一条推送。若关闭，短时间内频繁推送可能触发渠道限流。</div>
          </div>
          <div>
            <div class="field-label" style="display: flex; align-items: center; gap: 8px;">
              <input type="checkbox" id="input-minimal-push-enabled" />
              <span>极简推送</span>
            </div>
            <div class="field-helper">开启后推送消息将压缩为一行显示，仅展示关键信息</div>
          </div>
        </div>
        <div class="dnd-section" style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e5e7eb;">
          <div class="field-label" style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
            <input type="checkbox" id="input-dnd-enabled" />
            <span>勿扰模式</span>
          </div>
          <div class="field-helper" style="margin-bottom: 10px;">开启后，在设定时段内不推送消息；结束后将本时段事件汇总为一条推送。</div>
          <div class="system-grid" style="grid-template-columns: 1fr 1fr;">
            <div>
              <div class="field-label">开始时间</div>
              <input id="input-dnd-start" type="time" value="22:00" />
              <div class="field-helper">如 22:00，该时刻起进入勿扰</div>
            </div>
            <div>
              <div class="field-label">结束时间</div>
              <input id="input-dnd-end" type="time" value="07:00" />
              <div class="field-helper">如 07:00，跨日则到次日该时刻结束</div>
            </div>
          </div>
        </div>
      </div>

      <div class="footer-actions">
        <button class="btn btn-primary" id="save-btn" type="button">保存配置</button>
      </div>
      <div id="warning-panel" class="warning-panel"></div>

      <div class="section test-section">
        <div class="section-title">
          <span>测试推送 <small>保存成功后，可发送测试消息验证渠道是否配置正确（PS:发送不成功，尝试一下保存配置）</small></span>
        </div>
        <textarea id="test-content" placeholder="请输入要发送的测试内容，例如：这是一条 FnMessageBot 配置测试消息。"></textarea>
        <div class="footer-actions" style="margin-top: 10px;">
          <button class="btn btn-ghost" id="test-btn" type="button" disabled>发送测试</button>
        </div>
        <div class="status-bar" id="status-bar"></div>
      </div>

      <div class="home-footer">
        <a class="footer-link" href="https://github.com/Sunanang/FNMessageBots" target="_blank" rel="noopener noreferrer">
          <img class="footer-github-icon" src="{{ github_icon_url }}" alt="" width="18" height="18" decoding="async" />
          开源地址
        </a>
        <span class="footer-sep" aria-hidden="true">·</span>
        <a class="footer-link" href="/support">支持作者</a>
        <span class="footer-sep" aria-hidden="true">·</span>
        <a class="footer-link" href="/faq">常见问题</a>
      </div>
      <p class="license-footer">© 2024 Sunanang · FnMessageBot · MIT License terms apply.</p>
    </div>
  </div>
  <div id="toast-container" class="toast-container"></div>

  <script>
    const THEME_STORAGE_KEY = "fnmb_theme";
    const themeToggleBtn = document.getElementById("theme-toggle-btn");

    function getStoredThemeMode() {
      const mode = localStorage.getItem(THEME_STORAGE_KEY);
      return (mode === "light" || mode === "dark") ? mode : "light";
    }

    function resolveTheme(mode) {
      return mode === "dark" ? "dark" : "light";
    }

    function applyTheme(mode) {
      const resolved = resolveTheme(mode);
      document.documentElement.setAttribute("data-theme", resolved);
      if (!themeToggleBtn) return;
      if (resolved === "dark") {
        themeToggleBtn.textContent = "☀";
        themeToggleBtn.title = "切换到浅色模式";
        themeToggleBtn.setAttribute("aria-label", "切换到浅色模式");
      } else {
        themeToggleBtn.textContent = "🌙";
        themeToggleBtn.title = "切换到深色模式";
        themeToggleBtn.setAttribute("aria-label", "切换到深色模式");
      }
    }

    function initTheme() {
      const mode = getStoredThemeMode();
      applyTheme(mode);
      if (themeToggleBtn) {
        themeToggleBtn.addEventListener("click", function() {
          const current = resolveTheme(getStoredThemeMode());
          const nextMode = current === "dark" ? "light" : "dark";
          localStorage.setItem(THEME_STORAGE_KEY, nextMode);
          applyTheme(nextMode);
        });
      }
    }

    const eventsContainer = document.getElementById("events-container");
    const channelsBody = document.getElementById("channels-body");
    const addChannelBtn = document.getElementById("add-channel-btn");
    const saveBtn = document.getElementById("save-btn");
    const testBtn = document.getElementById("test-btn");
    const statusBar = document.getElementById("status-bar");
    const warningPanel = document.getElementById("warning-panel");

    let channelOptions = [];
    const fetchOpts = { credentials: "include" };

    async function initAuth() {
      const res = await fetch("/api/auth/status", fetchOpts);
      const data = await res.json();
      const authGate = document.getElementById("auth-gate");
      const appMain = document.getElementById("app-main");
      // 已登录，或未开启密码验证（无需设置密码且无需登录）时直接进入配置页
      const canShowApp = data.authenticated || (!data.need_setup && !data.need_login);
      if (canShowApp) {
        authGate.style.display = "none";
        appMain.style.display = "flex";
        loadConfig();
        return;
      }
      authGate.style.display = "flex";
      appMain.style.display = "none";
      document.getElementById("auth-set-password").style.display = data.need_setup ? "block" : "none";
      document.getElementById("auth-login").style.display = data.need_login ? "block" : "none";
      document.getElementById("auth-set-msg").textContent = "";
      document.getElementById("auth-login-msg").textContent = "";
    }

    document.getElementById("form-set-password").addEventListener("submit", async function(e) {
      e.preventDefault();
      const msgEl = document.getElementById("auth-set-msg");
      const p1 = document.getElementById("set-pw-password").value.trim();
      const p2 = document.getElementById("set-pw-confirm").value.trim();
      msgEl.textContent = "";
      msgEl.className = "auth-msg";
      if (p1.length < 6) {
        msgEl.textContent = "密码长度至少 6 位";
        msgEl.className = "auth-msg error";
        return;
      }
      if (p1 !== p2) {
        msgEl.textContent = "两次输入的密码不一致";
        msgEl.className = "auth-msg error";
        return;
      }
      const res = await fetch("/api/auth/set-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ password: p1, password_confirm: p2 }),
      });
      const json = await res.json();
      if (json.ok) {
        msgEl.textContent = "设置成功，正在进入配置页…";
        msgEl.className = "auth-msg ok";
        initAuth();
      } else {
        msgEl.textContent = json.message || "设置失败";
        msgEl.className = "auth-msg error";
      }
    });

    document.getElementById("form-login").addEventListener("submit", async function(e) {
      e.preventDefault();
      const msgEl = document.getElementById("auth-login-msg");
      const password = document.getElementById("login-password").value.trim();
      msgEl.textContent = "";
      msgEl.className = "auth-msg";
      if (!password) {
        msgEl.textContent = "请输入密码";
        msgEl.className = "auth-msg error";
        return;
      }
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ password }),
      });
      const json = await res.json();
      if (json.ok) {
        msgEl.textContent = "登录成功，正在进入配置页…";
        msgEl.className = "auth-msg ok";
        initAuth();
      } else {
        msgEl.textContent = json.message || "登录失败";
        msgEl.className = "auth-msg error";
      }
    });

    function setStatus(ok, message) {
      statusBar.className = "status-bar " + (ok ? "status-ok" : "status-error");
      statusBar.innerHTML = message ? "<span>" + message + "</span>" : "";
    }

    function showToast(ok, message) {
      const container = document.getElementById("toast-container");
      const el = document.createElement("div");
      el.className = "toast " + (ok ? "toast-ok" : "toast-error");
      el.textContent = message || (ok ? "操作成功" : "操作失败");
      container.appendChild(el);
      setTimeout(() => {
        el.style.opacity = "0";
        el.style.transform = "translateY(-8px)";
        el.style.transition = "opacity 0.2s, transform 0.2s";
        setTimeout(() => el.remove(), 200);
      }, 3200);
    }

    function renderPersistentWarnings(warnings) {
      const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
      if (!list.length) {
        warningPanel.classList.remove("show");
        warningPanel.innerHTML = "";
        return;
      }
      warningPanel.classList.add("show");
      const lines = list.map(function(item) {
        return '<div class="warning-panel-item">- ' + item + "</div>";
      }).join("");
      warningPanel.innerHTML =
        '<div class="warning-panel-title">数据库路径权限告警（保存后检测）</div>' + lines;
    }

    const PUSHPLUS_PLACEHOLDER = '{"token":"你的token","title":"{title}","content":"消息内容","template":"html","channel":"wechat"}';

    function serializeSmtpFromInputs(wrap) {
      if (!wrap) return "";
      const server = (wrap.querySelector(".smtp-server") && wrap.querySelector(".smtp-server").value || "").trim();
      const portRaw = (wrap.querySelector(".smtp-port") && wrap.querySelector(".smtp-port").value || "").trim();
      const username = (wrap.querySelector(".smtp-username") && wrap.querySelector(".smtp-username").value || "").trim();
      const password = (wrap.querySelector(".smtp-password") && wrap.querySelector(".smtp-password").value || "").trim();
      const fromAddr = (wrap.querySelector(".smtp-from") && wrap.querySelector(".smtp-from").value || "").trim();
      const toAddr = (wrap.querySelector(".smtp-to") && wrap.querySelector(".smtp-to").value || "").trim();
      if (!server || !portRaw || !username || !password || !toAddr) return "";
      const parsedPort = Number.parseInt(portRaw, 10);
      if (!Number.isInteger(parsedPort) || parsedPort <= 0) return "";
      const o = {
        server: server,
        port: parsedPort,
        username: username,
        password: password,
        to: toAddr,
      };
      if (fromAddr) o.from = fromAddr;
      return JSON.stringify(o);
    }

    function serializeMagicPushFromInputs(wrap) {
      if (!wrap) return "";
      const base = (wrap.querySelector(".magic-push-base") && wrap.querySelector(".magic-push-base").value || "").trim();
      const token = (wrap.querySelector(".magic-push-token") && wrap.querySelector(".magic-push-token").value || "").trim();
      const titleEl = wrap.querySelector(".magic-push-title");
      const title = titleEl ? titleEl.value.trim() : "";
      if (!base || !token) return "";
      const o = { base_url: base, token: token };
      if (title) o.title = title;
      return JSON.stringify(o);
    }

    function createChannelRow(chType, url) {
      const tr = document.createElement("tr");

      const tdType = document.createElement("td");
      const sel = document.createElement("select");
      for (const opt of channelOptions) {
        const o = document.createElement("option");
        o.value = opt.id;
        o.textContent = opt.name;
        if (opt.id === chType) o.selected = true;
        sel.appendChild(o);
      }
      tdType.appendChild(sel);

      const tdUrl = document.createElement("td");
      let rowChannelType = chType;

      function readCurrentSerializedUrl() {
        if (rowChannelType === "pushplus") {
          const ta = tdUrl.querySelector("textarea");
          return ta ? ta.value.trim() : "";
        }
        if (rowChannelType === "magic_push") {
          const wrap = tdUrl.querySelector(".magic-push-fields");
          return serializeMagicPushFromInputs(wrap);
        }
        if (rowChannelType === "smtp") {
          const wrap = tdUrl.querySelector(".smtp-fields");
          return serializeSmtpFromInputs(wrap);
        }
        const inp = tdUrl.querySelector("input.channel-url-input");
        return inp ? inp.value.trim() : "";
      }

      function setChannelWidget(type, val) {
        rowChannelType = type;
        tdUrl.innerHTML = "";
        if (type === "pushplus") {
          const ta = document.createElement("textarea");
          ta.rows = 3;
          ta.placeholder = PUSHPLUS_PLACEHOLDER;
          ta.value = val || "";
          ta.style.minHeight = "60px";
          tdUrl.appendChild(ta);
        } else if (type === "magic_push") {
          let base = "", token = "", title = "";
          if (val) {
            try {
              const o = JSON.parse(val);
              if (o && typeof o === "object") {
                base = (o.base_url || "").trim();
                token = (o.token || "").trim();
                title = (o.title || "").trim();
              }
            } catch (e) { /* ignore */ }
          }
          const wrap = document.createElement("div");
          wrap.className = "magic-push-fields";
          wrap.style.display = "flex";
          wrap.style.flexDirection = "row";
          wrap.style.flexWrap = "wrap";
          wrap.style.alignItems = "center";
          wrap.style.gap = "8px";
          const inBase = document.createElement("input");
          inBase.type = "text";
          inBase.className = "magic-push-base channel-url-input";
          inBase.placeholder = "基础 URL（如 https://push.example.com，不含 /api/push）";
          inBase.value = base;
          inBase.style.flex = "2 1 200px";
          inBase.style.minWidth = "160px";
          const inTok = document.createElement("input");
          inTok.type = "text";
          inTok.className = "magic-push-token";
          inTok.placeholder = "Token";
          inTok.autocomplete = "off";
          inTok.value = token;
          inTok.style.flex = "1 1 140px";
          inTok.style.minWidth = "100px";
          const inTitle = document.createElement("input");
          inTitle.type = "text";
          inTitle.className = "magic-push-title";
          inTitle.placeholder = "标题（留空则使用事件标题）";
          inTitle.value = title;
          inTitle.style.flex = "1 1 160px";
          inTitle.style.minWidth = "120px";
          wrap.appendChild(inBase);
          wrap.appendChild(inTok);
          wrap.appendChild(inTitle);
          tdUrl.appendChild(wrap);
        } else if (type === "smtp") {
          let server = "", port = "", username = "", password = "", fromAddr = "", toAddr = "";
          if (val) {
            try {
              const o = JSON.parse(val);
              if (o && typeof o === "object") {
                server = (o.server || "").trim();
                port = String(o.port || "").trim();
                username = (o.username || "").trim();
                password = (o.password || "").trim();
                fromAddr = (o.from || "").trim();
                toAddr = (o.to || "").trim();
              }
            } catch (e) { /* ignore */ }
          }
          const wrap = document.createElement("div");
          wrap.className = "smtp-fields";
          wrap.style.display = "grid";
          wrap.style.gridTemplateColumns = "repeat(3, minmax(160px, 1fr))";
          wrap.style.gap = "8px";
          const inServer = document.createElement("input");
          inServer.type = "text";
          inServer.className = "smtp-server";
          inServer.placeholder = "SMTP服务器（如 smtp.qq.com）";
          inServer.value = server;
          const inPort = document.createElement("input");
          inPort.type = "number";
          inPort.className = "smtp-port";
          inPort.placeholder = "端口（465/587）";
          inPort.value = port;
          const inUser = document.createElement("input");
          inUser.type = "text";
          inUser.className = "smtp-username";
          inUser.placeholder = "用户名（邮箱）";
          inUser.value = username;
          const inPass = document.createElement("input");
          inPass.type = "password";
          inPass.className = "smtp-password";
          inPass.placeholder = "密码/授权码";
          inPass.autocomplete = "new-password";
          inPass.value = password;
          const inFrom = document.createElement("input");
          inFrom.type = "text";
          inFrom.className = "smtp-from";
          inFrom.placeholder = "发件人（可选，默认同用户名）";
          inFrom.value = fromAddr;
          const inTo = document.createElement("input");
          inTo.type = "text";
          inTo.className = "smtp-to";
          inTo.placeholder = "收件人（多个用英文逗号）";
          inTo.value = toAddr;
          wrap.appendChild(inServer);
          wrap.appendChild(inPort);
          wrap.appendChild(inUser);
          wrap.appendChild(inPass);
          wrap.appendChild(inFrom);
          wrap.appendChild(inTo);
          tdUrl.appendChild(wrap);
        } else {
          const inp = document.createElement("input");
          inp.type = "text";
          inp.className = "channel-url-input";
          inp.placeholder = "例如：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...";
          inp.value = val || "";
          tdUrl.appendChild(inp);
        }
      }
      setChannelWidget(chType, url || "");

      sel.addEventListener("change", function() {
        const prevVal = readCurrentSerializedUrl();
        setChannelWidget(sel.value, prevVal);
      });

      const tdOp = document.createElement("td");
      tdOp.style.textAlign = "right";
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn-danger";
      delBtn.textContent = "删除";
      delBtn.onclick = () => {
        channelsBody.removeChild(tr);
      };
      tdOp.appendChild(delBtn);

      tr.appendChild(tdType);
      tr.appendChild(tdUrl);
      tr.appendChild(tdOp);

      channelsBody.appendChild(tr);
    }

    async function loadConfig() {
      try {
        const res = await fetch("/api/config", fetchOpts);
        const json = await res.json();
        if (res.status === 401) {
          initAuth();
          return;
        }
        if (!json.ok) {
          setStatus(false, json.message || "加载配置失败");
          return;
        }
        renderPersistentWarnings(json.warnings || []);
        const data = json.data;
        document.getElementById("app-title").textContent = data.title || "FnMessageBot";
        document.getElementById("app-subtitle").textContent = data.subtitle || "";
        document.getElementById("app-version").textContent = data.version || "";

        channelOptions = data.channel_options || [];

        // 按分类渲染事件
        eventsContainer.innerHTML = "";
        const selected = new Set(data.selected_events || []);
        const categories = data.events_by_category || [];
        for (const cat of categories) {
          const catBlock = document.createElement("div");
          catBlock.className = "event-category";

          // 分类标题 + 全选
          const catHeader = document.createElement("div");
          catHeader.className = "event-category-title";
          const catTitleSpan = document.createElement("span");
          catTitleSpan.textContent = cat.name || "";
          catHeader.appendChild(catTitleSpan);

          const catToggleLabel = document.createElement("label");
          catToggleLabel.style.fontSize = "13px";
          catToggleLabel.style.cursor = "pointer";
          catToggleLabel.style.flexShrink = "0";
          const catToggle = document.createElement("input");
          catToggle.type = "checkbox";
          catToggle.style.marginRight = "4px";
          catToggleLabel.appendChild(catToggle);
          const catToggleText = document.createElement("span");
          catToggleText.textContent = "全选";
          catToggleLabel.appendChild(catToggleText);
          catHeader.appendChild(catToggleLabel);

          catBlock.appendChild(catHeader);

          const grid = document.createElement("div");
          grid.className = "events-grid";

          function updateCatToggle() {
            const boxes = grid.querySelectorAll("input[type=checkbox]");
            if (!boxes.length) {
              catToggle.checked = false;
              return;
            }
            catToggle.checked = Array.from(boxes).every(b => b.checked);
          }

          for (const ev of cat.events || []) {
            const div = document.createElement("div");
            div.className = "event-item";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.value = ev.id;
            if (selected.has(ev.id)) cb.checked = true;

            cb.addEventListener("change", () => {
              updateCatToggle();
            });

            const label = document.createElement("div");
            const title = document.createElement("span");
            title.textContent = ev.title || ev.id;
            label.appendChild(title);
            if (ev.note) {
              const helper = document.createElement("div");
              helper.className = "field-helper";
              helper.textContent = ev.note;
              label.appendChild(helper);
            }
            div.appendChild(cb);
            div.appendChild(label);
            grid.appendChild(div);
          }

          // 分类全选/反选：用 change 事件，让复选框先自然切换，再根据其新状态同步子项
          catToggle.addEventListener("change", () => {
            const boxes = grid.querySelectorAll("input[type=checkbox]");
            const target = catToggle.checked;
            boxes.forEach(b => { b.checked = target; });
          });

          updateCatToggle();

          catBlock.appendChild(grid);
          eventsContainer.appendChild(catBlock);
        }

        // 渲染渠道
        channelsBody.innerHTML = "";
        if (data.channels && data.channels.length) {
          for (const ch of data.channels) {
            createChannelRow(ch.type || "wechat", ch.url || "");
          }
        } else {
          // 默认只展示一行企业微信，需要其他渠道可点击「添加渠道」
          createChannelRow("wechat", "");
        }

        document.getElementById("input-web-password-enabled").checked = data.web_password_enabled !== false;
        document.getElementById("input-log-days").value = data.log_retention_days || 7;
        document.getElementById("input-poll-interval").value = data.logger_poll_interval || 5;
        document.getElementById("input-poll-batch-summary").checked = !!data.poll_batch_summary_enabled;
        document.getElementById("input-minimal-push-enabled").checked = !!data.minimal_push_enabled;
        document.getElementById("input-title-prefix").value =
          typeof data.title_prefix === "string" ? data.title_prefix : "飞牛NAS";
        const dndEnabled = !!data.dnd_enabled;
        document.getElementById("input-dnd-enabled").checked = dndEnabled;
        document.getElementById("input-dnd-start").value = data.dnd_start_time || "22:00";
        document.getElementById("input-dnd-end").value = data.dnd_end_time || "07:00";
        document.getElementById("input-dnd-start").disabled = !dndEnabled;
        document.getElementById("input-dnd-end").disabled = !dndEnabled;
        const dndToggle = document.getElementById("input-dnd-enabled");
        dndToggle.onchange = function() {
          const en = document.getElementById("input-dnd-enabled").checked;
          document.getElementById("input-dnd-start").disabled = !en;
          document.getElementById("input-dnd-end").disabled = !en;
        };

        // 配置已从服务端加载成功即可测推送，不必再次点「保存配置」。
        testBtn.disabled = false;
        setStatus(false, "");
        loadPushStats();
      } catch (e) {
        console.error(e);
        setStatus(false, "加载配置失败，请检查服务是否正常运行。");
      }
    }

    async function loadPushStats() {
      try {
        const res = await fetch("/api/push-stats", fetchOpts);
        const json = await res.json();
        if (!json.ok || !json.data) return;
        const t = json.data.total || {};
        const d = json.data.today || {};
        document.getElementById("stat-total-total").textContent = (t.total ?? 0);
        document.getElementById("stat-total-success").textContent = (t.success ?? 0);
        document.getElementById("stat-total-fail").textContent = (t.fail ?? 0);
        document.getElementById("stat-today-total").textContent = (d.total ?? 0);
        document.getElementById("stat-today-success").textContent = (d.success ?? 0);
        document.getElementById("stat-today-fail").textContent = (d.fail ?? 0);
      } catch (e) { /* ignore */ }
    }

    addChannelBtn.addEventListener("click", () => {
      const defaultType = channelOptions.length ? channelOptions[0].id : "wechat";
      createChannelRow(defaultType, "");
    });

    saveBtn.addEventListener("click", async () => {
      setStatus(false, "");
      const events = [];
      eventsContainer.querySelectorAll("input[type=checkbox]").forEach(cb => {
        if (cb.checked) events.push(cb.value);
      });

      const channels = [];
      channelsBody.querySelectorAll("tr").forEach(tr => {
        const sel = tr.querySelector("select");
        if (!sel) return;
        const type = sel.value;
        let url = "";
        if (type === "magic_push") {
          const wrap = tr.querySelector(".magic-push-fields");
          url = serializeMagicPushFromInputs(wrap);
          if (!url) return;
        } else if (type === "smtp") {
          const wrap = tr.querySelector(".smtp-fields");
          url = serializeSmtpFromInputs(wrap);
          if (!url) return;
        } else {
          const inp = tr.querySelector("input.channel-url-input");
          const ta = tr.querySelector("textarea");
          const urlEl = ta || inp;
          if (!urlEl) return;
          url = urlEl.value.trim();
          if (!url) return;
        }
        channels.push({ type, url });
      });

      const payload = {
        events,
        channels,
        log_retention_days: document.getElementById("input-log-days").value,
        logger_poll_interval: document.getElementById("input-poll-interval").value,
        title_prefix: (document.getElementById("input-title-prefix").value || "").trim(),
        web_password_enabled: document.getElementById("input-web-password-enabled").checked,
        poll_batch_summary_enabled: document.getElementById("input-poll-batch-summary").checked,
        minimal_push_enabled: document.getElementById("input-minimal-push-enabled").checked,
        dnd_enabled: document.getElementById("input-dnd-enabled").checked,
        dnd_start_time: document.getElementById("input-dnd-start").value || "22:00",
        dnd_end_time: document.getElementById("input-dnd-end").value || "07:00",
      };

      try {
        const res = await fetch("/api/save-config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify(payload),
        });
        const json = await res.json();
        if (res.ok && json.ok) {
          showToast(true, json.message || "配置已保存");
          if (Array.isArray(json.warnings) && json.warnings.length) {
            json.warnings.forEach(w => showToast(false, "权限告警：" + w));
          }
          renderPersistentWarnings(json.warnings || []);
          testBtn.disabled = false;
          loadConfig();
        } else {
          showToast(false, json.message || "保存失败");
        }
      } catch (e) {
        console.error(e);
        showToast(false, "保存失败，请检查网络或稍后再试");
      }
    });

    testBtn.addEventListener("click", async () => {
      const content = document.getElementById("test-content").value.trim();
      if (!content) {
        showToast(false, "请输入测试内容");
        return;
      }
      setStatus(false, "正在发送测试消息...");
      try {
        const res = await fetch("/api/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ content }),
        });
        const text = await res.text();
        let json = null;
        try { json = JSON.parse(text); } catch (e) { json = null; }
        if (res.ok && json.ok) {
          showToast(true, json.message || "测试消息已发送");
        } else {
          showToast(false, (json && json.message) ? json.message : ("测试发送失败：" + (text ? text.slice(0, 120) : "")));
        }
      } catch (e) {
        console.error(e);
        showToast(false, "测试发送失败，请稍后重试");
      }
      setStatus(false, "");
    });

    window.addEventListener("load", () => {
      initTheme();
      initAuth();
      setInterval(loadPushStats, 30000);
    });
  </script>
</body>
</html>
            """,
            icon_url=icon_url,
            favicon_url=favicon_url,
            github_icon_url=github_icon_url,
        )

    return app


def start_ui_server_in_background(on_config_saved=None):
    """在后台线程启动配置 UI 服务。on_config_saved: 保存配置成功后的回调（热加载用）。"""
    app = create_app(on_config_saved=on_config_saved)
    port = int(os.getenv("UI_PORT", "18080"))

    def _run():
        app.run(host="0.0.0.0", port=port, threaded=True)

    thread = threading.Thread(target=_run, name="FnMessageBots-UI", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    # 本地调试：只启动 UI，不启动监控（无需配置 Webhook 即可打开页面）
    repo_root = Path(__file__).resolve().parent.parent.parent
    os.chdir(repo_root)
    app = create_app()
    port = int(os.getenv("UI_PORT", "18080"))
    print(f"配置 UI: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
