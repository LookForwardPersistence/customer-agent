"""P2-6: accessibility — live region, labels, pressed state, contrast."""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_message_log_is_live_region():
    # New bot replies / action cards / handoff banners must be announced.
    assert 'id="messages" role="log" aria-live="polite"' in HTML


def test_input_has_accessible_label_and_limit():
    assert 'aria-label="输入您的问题"' in HTML
    # client-side mirror of the server-side schema cap (2000 chars)
    assert 'maxlength="2000"' in HTML


def test_customer_selector_labelled():
    assert 'aria-label="切换演示客户"' in HTML


def test_trace_toggle_has_pressed_state():
    assert 'aria-pressed="false"' in HTML  # initial state in markup
    assert "aria-pressed" in JS            # JS keeps it in sync on click


def test_trace_panel_is_labelled_region():
    assert 'role="region" aria-label="执行轨迹面板"' in HTML


def test_typing_indicator_hidden_from_screen_readers():
    assert 'aria-hidden' in JS


def test_handoff_is_assertive_alert():
    assert "setAttribute('role', 'alert')" in JS


def test_small_text_colors_meet_aa_contrast():
    """Chip text (10px) and handoff body (13px) need >= 4.5:1 on their soft backgrounds."""
    # luminance-based contrast check, no external deps
    def srgb_to_lin(c: int) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def lum(hex_color: str) -> float:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)

    def contrast(fg: str, bg: str) -> float:
        l1, l2 = sorted((lum(fg), lum(bg)), reverse=True)
        return (l1 + 0.05) / (l2 + 0.05)

    # green chip on green-soft background
    assert contrast("#047857", "#ecfdf5") >= 4.5
    # handoff body on red-soft background
    assert contrast("#b91c1c", "#fef2f2") >= 4.5
    # and the palette actually uses these values
    assert "--green: #047857" in HTML
    assert "--red: #b91c1c" in HTML


def test_confirm_buttons_are_real_buttons():
    # Action cards must use <button> elements (keyboard-focusable by default).
    assert "createElement('button')" in JS
