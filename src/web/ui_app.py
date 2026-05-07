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

from flask import Flask, jsonify, request, render_template, send_from_directory, abort, redirect

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

# 配置页密码：会话空闲超时（秒），超时后需重新输入密码
SESSION_IDLE_SECONDS = 300
AUTH_COOKIE_NAME = "fnmb_session"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _session_cookie_secure() -> bool:
    """HTTPS 反代场景：设置 SESSION_COOKIE_SECURE=1/true 或为 Cookie 加 Secure。"""
    v = (os.environ.get("SESSION_COOKIE_SECURE") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return (os.environ.get("HTTPS") or "").strip() == "1"


def _auth_cookie_kwargs() -> dict:
    return {
        "max_age": SESSION_IDLE_SECONDS,
        "httponly": True,
        "samesite": "Lax",
        "path": "/",
        "secure": _session_cookie_secure(),
    }


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

    backup_events = {"BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS"}
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

    scheduler_events = {"SCHEDULER_TASK_SUCCESS", "SCHEDULER_TASK_FAILED", "SCHEDULER_TASK_CONDITION_FAILED"}
    if monitor_events & scheduler_events:
        issue = _check_db_access_issue(
            (raw_cfg.get("scheduler_db_path") or "").strip(),
            "SELECT id FROM task_results LIMIT 1",
        )
        if issue:
            warnings.append(f"任务计划库: {issue}")

    return warnings


def create_app(on_config_saved=None) -> Flask:
    """创建 Flask 应用。on_config_saved: 保存配置成功后的回调（用于热加载，无需重启）。"""
    app = Flask(__name__, template_folder=str(_TEMPLATE_DIR))
    _sk = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
    app.secret_key = _sk if (_sk and _sk.strip()) else secrets.token_hex(32)
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
    logger_db_path = (raw_cfg.get("logger_db_path") or "").strip()
    backup_db_path = (raw_cfg.get("backup_db_path") or "").strip()
    trim_media_db_path = (raw_cfg.get("trim_media_db_path") or "").strip()
    trim_activity_db_path = (raw_cfg.get("trim_activity_db_path") or "").strip()
    photo_db_path = (raw_cfg.get("photo_db_path") or "").strip()
    scheduler_db_path = (raw_cfg.get("scheduler_db_path") or "").strip()
    events_by_category, valid_event_ids, discovered_vm_event_ids = build_events_for_ui(
        logger_db_path=logger_db_path,
        backup_db_path=backup_db_path,
        trim_media_db_path=trim_media_db_path,
        trim_activity_db_path=trim_activity_db_path,
        photo_db_path=photo_db_path,
        scheduler_db_path=scheduler_db_path,
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
        resp.set_cookie(AUTH_COOKIE_NAME, session_id, **_auth_cookie_kwargs())
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
        resp.set_cookie(AUTH_COOKIE_NAME, session_id, **_auth_cookie_kwargs())
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
            "bark_icon": (raw.get("bark_icon") or "").strip(),
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
        bark_icon = (payload.get("bark_icon") or "").strip()

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
                "bark_icon": bark_icon,
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
        return render_template("history.html", favicon_url=favicon_url)

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
        return render_template(
            "support.html",
            favicon_url=favicon_url,
            wechat_src=wechat_src,
            ali_src=ali_src,
        )

    @app.get("/faq")
    def faq_page():
        """常见问题页。"""
        return render_template("faq.html", favicon_url=favicon_url)

    @app.get("/")
    def index():
        """单页应用（模板见 templates/index.html）。"""
        return render_template(
            "index.html",
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
