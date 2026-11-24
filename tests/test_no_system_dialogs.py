"""Web 前端不得使用浏览器原生 alert/confirm/prompt。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "src" / "web"
ALLOW_FILE = "app-dialog.js"
# 直接调用 window.alert/confirm/prompt（覆盖赋值不匹配）
WINDOW_CALL = re.compile(r"\bwindow\.(?:alert|confirm|prompt)\s*\(")


def test_web_sources_do_not_call_native_dialogs():
    offenders = []
    for path in list(WEB_ROOT.glob("templates/*.html")) + list(WEB_ROOT.glob("static/*.js")):
        if path.name == ALLOW_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        for match in WINDOW_CALL.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0).strip()}")
    assert not offenders, "发现系统弹窗调用，请改用 fnmbDialog：\n" + "\n".join(offenders)


def test_app_pages_include_app_dialog_script():
    for name in ("index.html", "faq.html", "history.html", "support.html"):
        html = (WEB_ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "app_dialog_js" in html or "app-dialog.js" in html, f"{name} 未引入 app-dialog.js"


def test_app_dialog_overrides_native_dialogs():
    code = (WEB_ROOT / "static" / "app-dialog.js").read_text(encoding="utf-8")
    assert "window.fnmbDialog" in code
    assert re.search(r"window\.alert\s*=", code)
    assert re.search(r"window\.confirm\s*=", code)
    assert re.search(r"window\.prompt\s*=", code)
