from urllib.parse import quote

import pytest

from aiops_agent.browser.models import BrowserAction
from aiops_agent.browser.playwright_tool import PlaywrightBrowserTool


def test_playwright_tool_observes_and_interacts_with_iframe_elements(tmp_path):
    iframe_html = """
      <html>
        <body>
          <label>用户名称<input id="username" /></label>
          <button id="search" onclick="document.body.append(' 已查询 ' + document.querySelector('#username').value)">查询</button>
        </body>
      </html>
    """
    page_html = f"""
      <html>
        <body>
          <button>主页面按钮</button>
          <iframe src="data:text/html;charset=utf-8,{quote(iframe_html)}"></iframe>
        </body>
      </html>
    """
    tool = PlaywrightBrowserTool(
        session_id="session",
        task_id="task",
        artifact_root=tmp_path,
        headless=True,
    )

    try:
        try:
            tool.execute(
                BrowserAction(
                    type="open_url",
                    value=f"data:text/html;charset=utf-8,{quote(page_html)}",
                )
            )
        except Exception as exc:
            if "BrowserType.launch" in str(exc) and "Permission denied" in str(exc):
                pytest.skip("Playwright browser launch is blocked by the local macOS sandbox")
            raise
        waited = tool.execute(BrowserAction(type="wait_for", timeout_ms=1000))
        iframe_search = next(
            element
            for element in waited.observation.interactive_elements
            if element.text == "查询"
        )

        assert iframe_search.element_id.startswith("aiops-frame-")

        filled = tool.execute(BrowserAction(type="type", target_hint="用户名称", value="高斌"))
        assert filled.status == "success"
        clicked = tool.execute(BrowserAction(type="click", target_id=iframe_search.element_id))

        assert clicked.status == "success"
        assert "已查询 高斌" in clicked.observation.page_text
    finally:
        tool.close()
