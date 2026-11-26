"""消息模板：优先级、空行删除、黑名单、极简、normalize、别名。"""

from __future__ import annotations

from unittest.mock import patch

from utils.message_template import (
    empty_templates,
    family_id_for_event,
    is_template_allowed,
    normalize_message_templates,
    render_message,
    render_template,
    resolve_template,
    build_context,
)
from notifier.multi_platform_notifier import MultiPlatformNotifier


def test_priority_event_over_family_over_builtin():
    templates = {
        "version": 1,
        "families": {
            "login": {"title": "族标题 {{user}}", "body": "族正文"},
        },
        "events": {
            "LoginSucc": {"title": "单事件 {{user}}", "body": "单事件正文"},
        },
    }
    scope, title, body = resolve_template("LoginSucc", templates)
    assert scope == "event"
    assert "单事件" in title
    rendered = render_message(
        "LoginSucc",
        {"user": "alice", "IP": "1.2.3.4"},
        timestamp="t",
        prefix="飞牛NAS",
        templates=templates,
        official_title="✅ 飞牛NAS-登录成功",
    )
    assert rendered is not None
    assert rendered[0] == "event"
    assert rendered[1] == "单事件 alice"
    assert rendered[2] == "单事件正文"

    del templates["events"]["LoginSucc"]
    scope2, _, _ = resolve_template("LoginSucc", templates)
    assert scope2 == "family"
    rendered2 = render_message(
        "LoginSucc",
        {"user": "bob"},
        templates=templates,
        official_title="✅ 飞牛NAS-登录成功",
    )
    assert rendered2 is not None
    assert rendered2[0] == "family"
    assert rendered2[1] == "族标题 bob"

    empty = empty_templates()
    scope3, _, _ = resolve_template("LoginSucc", empty)
    assert scope3 == "builtin"
    assert render_message("LoginSucc", {"user": "x"}, templates=empty) is None


def test_empty_line_omit_and_label_colon():
    ctx = {"user": "a", "ip": "", "time": "12:00", "note": ""}
    text = "用户: {{user}}\nIP: {{ip}}\n{{time}}\n{{note}}\n尾"
    out = render_template(text, ctx)
    assert "用户: a" in out
    assert "IP:" not in out
    assert "12:00" in out
    assert "尾" in out
    assert out.count("\n\n") == 0


def test_event_icon_and_prefix_in_title():
    rendered = render_message(
        "LoginSucc",
        {"user": "u"},
        timestamp="t",
        prefix="我家NAS",
        templates={
            "families": {
                "login": {
                    "title": "{{event_icon}} {{prefix}}-{{event_title}}",
                    "body": "{{user}}",
                }
            },
            "events": {},
        },
        official_title="✅ 飞牛NAS-登录成功",
    )
    assert rendered is not None
    assert rendered[1].startswith("✅")
    assert "我家NAS-登录成功" in rendered[1]
    assert rendered[2] == "u"


def test_parse_misc_technical_and_geometric_icons():
    """⏰▶️⏹️ 等不在常见 emoji 区块，须能拆出 event_icon。"""
    from utils.message_template import parse_official_title

    for raw, expect_icon_prefix in (
        ("▶️ 飞牛NAS-Docker容器已启动", "▶"),
        ("⏹️ 飞牛NAS-Docker容器已停止", "⏹"),
        ("⏰ 飞牛NAS-SSH开启超时提醒", "⏰"),
        ("⏱️ 飞牛NAS-相册分享过期", "⏱"),
    ):
        icon, short = parse_official_title(raw)
        assert icon.startswith(expect_icon_prefix), (raw, icon)
        assert "飞牛NAS" not in short
        assert not short.startswith(expect_icon_prefix)

    n = MultiPlatformNotifier(
        title_prefix="飞牛NAS",
        message_templates={
            "events": {
                "DOCKER_CONTAINER_START": {
                    "title": "{{event_icon}} {{prefix}}-{{event_title}}",
                    "body": "{{container_name}}",
                }
            },
            "families": {},
        },
    )
    msg = n._build_message(
        "DOCKER_CONTAINER_START",
        {"container_name": "nginx"},
        "t",
        "",
    )
    assert msg.title.startswith("▶")
    assert msg.title.index("飞牛NAS") < msg.title.index("Docker")
    assert "飞牛NAS-▶" not in msg.title


def test_blacklist_not_customizable():
    assert not is_template_allowed("TEST_PUSH")
    assert not is_template_allowed("DND_SUMMARY")
    assert family_id_for_event("NAS_PATROL_REPORT") is None
    assert render_message(
        "TEST_PUSH",
        {},
        templates={"events": {"TEST_PUSH": {"title": "x", "body": "y"}}},
    ) is None


def test_normalize_drops_unknown_and_bad():
    raw = {
        "version": 9,
        "families": {
            "login": {"title": "ok", "body": "b"},
            "nope": {"title": "x", "body": "y"},
            "ssh": "bad",
        },
        "events": {
            "LoginSucc": {"title": "e", "body": ""},
            "TEST_PUSH": {"title": "no", "body": "no"},
            "LoginFail": {},
        },
        "extra": 1,
    }
    out = normalize_message_templates(raw)
    assert out["version"] == 1
    assert "login" in out["families"]
    assert "nope" not in out["families"]
    assert "ssh" not in out["families"]
    assert "LoginSucc" in out["events"]
    assert "TEST_PUSH" not in out["events"]
    assert "LoginFail" not in out["events"]
    assert "extra" not in out


def test_alias_ip_to_ip():
    ctx = build_context(
        "LoginSucc",
        {"IP": "10.0.0.1", "USER_NAME": "neo"},
        prefix="飞牛NAS",
        timestamp="t",
        official_title="✅ 飞牛NAS-登录成功",
    )
    assert ctx["ip"] == "10.0.0.1"
    assert ctx["user"] == "neo"
    assert ctx["event_icon"] == "✅"


def test_minimal_push_skips_custom_templates():
    n = MultiPlatformNotifier(
        title_prefix="飞牛NAS",
        minimal_push_enabled=True,
        poll_batch_summary_enabled=False,
        message_templates={
            "families": {
                "login": {"title": "自定义标题", "body": "自定义正文"},
            },
            "events": {},
        },
    )
    msg = n._build_message("LoginSucc", {"user": "a", "IP": "1.1.1.1"}, "t", "")
    # 极简：一行标题，无正文
    assert msg.content == ""
    assert "自定义标题" not in (msg.title or "")
    assert "自定义正文" not in (msg.title or "")


def test_custom_path_no_double_prefix():
    n = MultiPlatformNotifier(
        title_prefix="飞牛NAS",
        minimal_push_enabled=False,
        message_templates={
            "events": {
                "LoginSucc": {
                    "title": "{{event_icon}} {{prefix}}-{{event_title}}",
                    "body": "用户 {{user}}",
                }
            },
            "families": {},
        },
    )
    msg = n._build_message("LoginSucc", {"user": "a"}, "2026-01-01 00:00:00", "")
    assert "飞牛NAS-飞牛NAS" not in msg.title
    assert "用户 a" == msg.content
    # 自定义路径不应再套一层官方前缀替换导致重复
    assert msg.title.count("飞牛NAS") == 1


def test_dual_empty_falls_back_to_official():
    n = MultiPlatformNotifier(
        title_prefix="飞牛NAS",
        message_templates={
            "events": {
                "LoginSucc": {"title": "{{missing}}", "body": "{{also_missing}}"},
            },
            "families": {},
        },
    )
    with patch.object(n, "_build_content", return_value="官方正文") as mock_content:
        msg = n._build_message("LoginSucc", {}, "t", "")
        mock_content.assert_called()
        assert "官方正文" in msg.content
        assert "飞牛NAS" in msg.title or "登录" in msg.title


def test_empty_title_keeps_body_with_official_title_fallback():
    n = MultiPlatformNotifier(
        title_prefix="飞牛NAS",
        message_templates={
            "events": {
                "LoginSucc": {"title": "{{missing}}", "body": "自定义正文 {{user}}"},
            },
            "families": {},
        },
    )
    msg = n._build_message("LoginSucc", {"user": "alice"}, "t", "")
    assert msg.content == "自定义正文 alice"
    assert msg.title
    assert "登录" in msg.title or "飞牛NAS" in msg.title


def test_category_header_drawer_locks_family_scope():
    """组标题入口只能编辑本类：不得出现「仅本事件」，保存/恢复/试发都按本类处理。"""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "src/web/templates/index.html").read_text(encoding="utf-8")
    # 组标题按钮：打开时带 lockScope，且 eventType 只是示例事件
    header_open = html[html.index('catTplBtn.addEventListener("click"'):]
    header_open = header_open[:header_open.index("});")]
    assert 'scope: "family"' in header_open and "lockScope: true" in header_open
    # 抽屉逻辑：锁定时隐藏范围切换与复制按钮
    assert 'scopeRowEl.style.display = lockScope ? "none" : ""' in html
    assert 'copyFamilyBtn.style.display = lockScope ? "none" : ""' in html
    # 保存/恢复/试发统一经 currentTplScope()，锁定时不读取单选框
    assert html.count("const scope = currentTplScope();") == 3
    assert 'if (tplDrawerState.lockScope) return "family";' in html
