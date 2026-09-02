"""P0-4 DOM XSS：验证前端不将动态内容交给 innerHTML。"""

import re
from pathlib import Path

HTML = Path(__file__).parent.parent / "app" / "static" / "index.html"
JS = Path(__file__).parent.parent / "app" / "static" / "app.js"
SOURCE = HTML.read_text(encoding="utf-8")
SCRIPT = JS.read_text(encoding="utf-8")


def test_csp_header_present():
    assert "Content-Security-Policy" in SOURCE
    assert "script-src 'self'" in SOURCE
    # CSP forbids inline scripts: the JS must live in an external file.
    assert 'src="/static/app.js"' in SOURCE
    assert not re.search(r"<script(?![^>]*src=)[^>]*>", SOURCE), \
        "inline <script> is blocked by CSP — keep JS in app.js"


def test_dynamic_fields_use_text_content():
    # 所有来自模型/后端/用户的动态字段必须经 textContent 渲染
    dynamic_setters = [
        "b.textContent = text",          # addBubble
        "chip.textContent =",             # sources
        "v.textContent =",                # action card detail
        "head.textContent =",             # handoff header
        "reason.textContent =",           # handoff reason
        "ctx.textContent =",              # handoff context
        "d.textContent =",                # trace detail
        "err.message" in SCRIPT and "textContent" in SCRIPT,
    ]
    for stmt in dynamic_setters[:-1]:
        assert stmt in SCRIPT, f"missing safe setter: {stmt}"


def test_no_innerhtml_with_user_content():
    # innerHTML 只允许出现在两处静态内容：typing 指示器与客户欢迎语（静态 map），
    # 不得用于任何来自用户/模型/后端的动态数据。
    matches = list(re.finditer(r"\.innerHTML\s*=", SCRIPT))
    assert len(matches) == 2, f"expected exactly 2 static innerHTML uses, found {len(matches)}"
    for m in matches:
        start = SCRIPT.rfind("\n", 0, m.start()) + 1
        end = SCRIPT.find("\n", m.end())
        line = SCRIPT[start:end]
        assert "typing" in line or "CUSTOMER_ORDERS" in line, \
            f"innerHTML used with non-static content: {line}"


def test_mask_sensitive_neutralizes_markup():
    assert "replace(/</g, '‹')" in SCRIPT


def test_trace_masking_by_default():
    assert "state.rawTrace" in SCRIPT
    assert "maskSensitive(t.detail)" in SCRIPT
