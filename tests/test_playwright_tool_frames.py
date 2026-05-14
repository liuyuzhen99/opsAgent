from urllib.parse import quote

import pytest

from aiops_agent.browser.models import BrowserAction
from aiops_agent.browser.playwright_tool import PlaywrightBrowserTool


class _FakeLocator:
    def __init__(self, count=0, *, visible=True, enabled=True):
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self.first = self

    def count(self):
        return self._count

    def nth(self, _index):
        return self

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def get_by_text(self, _text, exact=False):
        return _FakeLocator(0)


class _FakeFrame:
    def locator(self, _selector):
        return _FakeLocator(1)


class _FakePage:
    frames = [_FakeFrame()]


def test_popup_locator_does_not_claim_unmatched_visible_popup(tmp_path):
    tool = PlaywrightBrowserTool(
        session_id="session",
        task_id="task",
        artifact_root=tmp_path,
        headless=True,
    )

    locator = tool._popup_click_locator(_FakePage(), BrowserAction(type="click", target_hint="查询"))

    assert locator is None


def test_playwright_tool_recognizes_bare_css_selector_hints(tmp_path):
    tool = PlaywrightBrowserTool(
        session_id="session",
        task_id="task",
        artifact_root=tmp_path,
        headless=True,
    )

    assert tool._looks_like_css_selector("input[name='userName']") is True
    assert tool._looks_like_css_selector("select2-search input") is False
    assert tool._looks_like_css_selector("用户名") is False


def test_command_hints_are_recognized_for_semantic_lookup(tmp_path):
    tool = PlaywrightBrowserTool(
        session_id="session",
        task_id="task",
        artifact_root=tmp_path,
        headless=True,
    )

    assert tool._is_command_hint("查询") is True
    assert tool._is_command_hint("授权单位") is False


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


def test_playwright_tool_types_into_open_dropdown_search_input(tmp_path):
    page_html = """
      <html>
        <body>
          <button
            aria-label="授权单位"
            onclick="
              document.querySelector('#dropdown').style.display = 'block';
              document.querySelector('#companySearch').focus();
            "
          >授权单位</button>
          <div id="dropdown" class="el-select-dropdown" style="display:none">
            <input id="companySearch" />
          </div>
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

        opened = tool.execute(BrowserAction(type="click", target_hint="授权单位"))
        assert opened.status == "success"

        typed = tool.execute(
            BrowserAction(
                type="type",
                target_hint="select2-search input",
                value="内蒙古伊家好奶酪有限责任公司",
            )
        )

        assert typed.status == "success"
        assert "内蒙古伊家好奶酪有限责任公司" in typed.observation.page_text
    finally:
        tool.close()


def test_playwright_tool_clicks_matching_popup_option_instead_of_masked_select(tmp_path):
    page_html = """
      <html>
        <body>
          <a
            class="select2-choice"
            data-aiops-id="company-select"
            href="javascript:void(0)"
            onclick="
              document.querySelector('#mask').style.display = 'block';
              document.querySelector('#drop').style.display = 'block';
            "
          >伊利财务有限公司</a>
          <div id="mask" class="select2-drop-mask" style="display:none; position:fixed; inset:0"></div>
          <div id="drop" class="select2-drop" style="display:none">
            <ul class="select2-results">
              <li onclick="document.body.setAttribute('data-selected-company', this.innerText)">内蒙古伊家好奶酪有限责任公司</li>
            </ul>
          </div>
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

        opened = tool.execute(BrowserAction(type="click", target_id="company-select", target_hint="授权单位"))
        assert opened.status == "success"

        selected = tool.execute(
            BrowserAction(
                type="click",
                target_id="company-select",
                target_hint="内蒙古伊家好奶酪有限责任公司",
            )
        )

        assert selected.status == "success"
        assert selected.observation.page_text.count("内蒙古伊家好奶酪有限责任公司") == 1
    finally:
        tool.close()


def test_playwright_tool_selects_first_table_row_checkbox(tmp_path):
    page_html = """
      <html>
        <body>
          <table>
            <tbody>
              <tr><td><input type="checkbox" onclick="document.body.setAttribute('data-selected-row', '1')" /></td><td>张越</td></tr>
              <tr><td><input type="checkbox" onclick="document.body.setAttribute('data-selected-row', '2')" /></td><td>张三</td></tr>
            </tbody>
          </table>
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

        selected = tool.execute(BrowserAction(type="click", target_hint="第一条数据"))

        assert selected.status == "success"
        assert selected.observation.page_text.startswith("张越")
    finally:
        tool.close()


def test_playwright_tool_selects_first_jqgrid_data_row_not_header_row(tmp_path):
    page_html = """
      <html>
        <body>
          <table>
            <tbody>
              <tr onclick="document.body.setAttribute('data-selected-row', 'header')">
                <td>用户编号</td><td>登录名称</td><td>用户名</td>
              </tr>
            </tbody>
          </table>
          <table class="ui-jqgrid-btable">
            <tbody>
              <tr class="jqgrow" onclick="document.body.setAttribute('data-selected-row', '1')">
                <td><a href="#">U0003684</a></td><td>31602X_zhangyue</td><td>张越</td>
              </tr>
            </tbody>
          </table>
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

        selected = tool.execute(BrowserAction(type="click", target_hint="第一条数据"))

        assert selected.status == "success"
        assert selected.observation.page_text.startswith("用户编号")
        assert tool._page.locator("body").get_attribute("data-selected-row") == "1"
    finally:
        tool.close()


def test_playwright_tool_dispatches_first_row_click_when_overlay_intercepts_pointer(tmp_path):
    page_html = """
      <html>
        <body>
          <div style="position:relative">
            <table style="position:absolute; top:0; left:0">
              <tbody>
                <tr onclick="document.body.setAttribute('data-selected-row', '1')">
                  <td><a href="#">U0003684</a></td><td>31602X_zhangyue</td><td>张越</td>
                </tr>
              </tbody>
            </table>
            <label style="position:absolute; top:0; left:0; width:260px; height:40px; background:white">登录名称</label>
          </div>
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

        selected = tool.execute(BrowserAction(type="click", target_hint="第一条数据"))

        assert selected.status == "success"
        assert tool._page.locator("body").get_attribute("data-selected-row") == "1"
    finally:
        tool.close()
