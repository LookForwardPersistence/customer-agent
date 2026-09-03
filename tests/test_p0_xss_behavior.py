"""P0-2 行为级 XSS 测试：在 node DOM shim 中执行真实 app.js，
注入恶意 reply / pending_action / handoff / trace，断言：
- innerHTML 只出现静态白名单（typing 指示器、客户欢迎语）；
- 恶意 payload 只以文本形式渲染（不进入可执行节点）；
- 事件处理器是函数而非字符串。

node 不可用时跳过（CI 有 node；纯 Python 环境退化为 test_p0_xss.py 静态检查）。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
HARNESS = Path(__file__).parent / "dom_harness.js"
APP_JS = ROOT / "app" / "static" / "app.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def test_malicious_payload_never_executes_in_dom():
    proc = subprocess.run(
        [NODE, str(HARNESS), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 2, f"harness crashed: {proc.stderr}"

    verdict = json.loads(proc.stdout)
    assert verdict["violations"] == [], f"XSS violations: {verdict['violations']}"
    # 恶意内容确实被渲染了（作为文本）——证明不是“没渲染”而是“安全渲染”。
    assert verdict["sawEvilAsText"] is True

    # 所有 innerHTML 赋值都必须落在静态白名单内。
    for html in verdict["innerHTMLAssignments"]:
        static = 'class="typing"' in html or "当前演示账号为" in html
        assert static, f"non-static innerHTML: {html[:120]}"
