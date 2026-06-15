from __future__ import annotations

import html
import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from aiops_agent.browser.models import (
    ActionResult,
    BrowserAction,
    BrowserObservation,
    InteractiveElement,
)


class PlaywrightBrowserTool:
    def __init__(
        self,
        *,
        session_id: str,
        task_id: str,
        artifact_root: str | Path = "storage/artifacts",
        headless: bool = True,
        allowed_domains: list[str] | None = None,
        session_state_path: str | Path | None = None,
        trace_enabled: bool = False,
        video_enabled: bool = False,
        browser_channel: str | None = None,
        slow_mo_ms: int = 0,
    ):
        self.session_id = session_id
        self.task_id = task_id
        self.artifact_dir = Path(artifact_root) / session_id / task_id
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.allowed_domains = allowed_domains or []
        self.session_state_path = Path(session_state_path) if session_state_path else None
        self.trace_enabled = trace_enabled
        self.video_enabled = video_enabled
        self.browser_channel = browser_channel
        self.slow_mo_ms = slow_mo_ms
        self.trace_path = self.artifact_dir / "trace.zip"
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def execute(self, action: BrowserAction) -> ActionResult:
        if action.type == "open_url":
            return self._open_url(action)
        if action.type == "observe_page":
            return ActionResult("success", self.observe(last_action_result="observed"))
        if action.type == "extract_text":
            observation = self.observe(last_action_result="text extracted")
            return ActionResult("success", observation)
        if action.type == "save_artifact":
            observation = self.observe(last_action_result="artifact saved", force_artifact=True)
            artifacts = [path for path in (observation.screenshot_path, observation.page_summary_path) if path]
            return ActionResult("success", observation, artifacts=artifacts)
        if action.type == "finish":
            return ActionResult("success", self.observe(last_action_result="finished"))
        if action.type == "login_submit":
            return self._login_submit(action)
        return self._interact(action)

    def observe(self, *, last_action_result: str = "", force_artifact: bool = False) -> BrowserObservation:
        page = self._ensure_page()
        title = self._safe_eval(lambda: page.title(), "")
        url = getattr(page, "url", "")
        elements = self._collect_interactive_elements(page)
        messages = self._collect_visible_messages(page)
        page_text = self._collect_page_text(page)
        observation = BrowserObservation(
            url=url,
            title=title,
            page_type=self._classify_page(elements, messages),
            interactive_elements=elements,
            forms=self._collect_forms(elements),
            visible_messages=messages,
            page_text=page_text,
            last_action_result=last_action_result,
            done_signals=self._done_signals(title, url, messages),
        )
        if force_artifact:
            self._save_artifacts(observation)
        return observation

    def close(self) -> None:
        self.save_session_state()
        self.save_trace()
        for resource in (self._context, self._browser, self._playwright):
            if resource is None:
                continue
            close = getattr(resource, "close", None) or getattr(resource, "stop", None)
            if close:
                close()

    def save_session_state(self) -> str | None:
        if self._context is None or self.session_state_path is None:
            return None
        self.session_state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Playwright storage_state 主要保存 cookies/localStorage 等登录态；
            # 它不是页面 DOM/弹窗/未提交表单的完整快照。
            self._context.storage_state(path=str(self.session_state_path))
        except Exception:
            return None
        return str(self.session_state_path)

    def save_trace(self) -> str | None:
        if self._context is None or not self.trace_enabled:
            return None
        try:
            self._context.tracing.stop(path=str(self.trace_path))
        except Exception:
            return None
        return str(self.trace_path)

    def _open_url(self, action: BrowserAction) -> ActionResult:
        url = action.value or action.target_hint
        if not url:
            return ActionResult("terminal_failure", BrowserObservation(), error="open_url 缺少 URL")
        if not self._domain_allowed(url):
            observation = BrowserObservation(url=url, blocking_reason="跳转到非允许域名")
            self._save_artifacts(observation)
            return ActionResult(
                "terminal_failure",
                observation,
                error=f"URL 不在允许域名范围内: {urlparse(url).netloc}",
            )
        page = self._ensure_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=action.timeout_ms)
        except Exception as exc:
            observation = self.observe(last_action_result="open_url failed", force_artifact=True)
            return ActionResult("retryable_failure", observation, error=str(exc))
        return ActionResult("success", self.observe(last_action_result=f"opened {url}"))

    def _interact(self, action: BrowserAction) -> ActionResult:
        page = self._ensure_page()
        try:
            locator = self._resolve_locator(page, action)
            if action.type == "click":
                self._click_with_popup_fallback(page, action, locator)
            elif action.type == "hover":
                locator.hover(timeout=action.timeout_ms)
            elif action.type == "type":
                if self._is_date_type_action(action):
                    self._set_date_field(page, action, locator)
                else:
                    locator.fill(action.value or "", timeout=action.timeout_ms)
            elif action.type == "select":
                locator.select_option(action.value or "", timeout=action.timeout_ms)
            elif action.type == "press":
                page.keyboard.press(action.value or action.target_hint or "Enter")
            elif action.type == "wait_for":
                page.wait_for_timeout(action.timeout_ms)
            else:
                observation = self.observe(last_action_result=f"unsupported action {action.type}", force_artifact=True)
                return ActionResult("terminal_failure", observation, error=f"不支持的浏览器动作: {action.type}")
        except Exception as exc:
            observation = self.observe(last_action_result=f"{action.type} failed", force_artifact=True)
            return ActionResult("retryable_failure", observation, error=str(exc))
        return ActionResult("success", self.observe(last_action_result=f"{action.type} executed"))

    def _click_with_popup_fallback(self, page, action: BrowserAction, locator) -> None:
        try:
            locator.click(timeout=action.timeout_ms)
            return
        except Exception as first_error:
            if self._means_first_result(action.target_hint or ""):
                try:
                    locator.dispatch_event("click", timeout=action.timeout_ms)
                    return
                except Exception:
                    pass
            fallback = self._popup_click_locator(page, action)
            if fallback is None:
                raise first_error
            fallback.click(timeout=action.timeout_ms)

    def _login_submit(self, action: BrowserAction) -> ActionResult:
        page = self._ensure_page()
        login_wait_ms = max(action.timeout_ms, 30000)
        click_action = BrowserAction(
            type="click",
            target_hint=action.target_hint or "登录",
            target_id=action.target_id,
            expected_outcome=action.expected_outcome,
            risk_level="safe_local_edit",
            timeout_ms=login_wait_ms,
        )
        try:
            before_url = page.url
            locator = self._resolve_locator(page, click_action)
            locator.click(timeout=login_wait_ms)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=login_wait_ms)
            except Exception:
                pass
            try:
                page.wait_for_function(
                    """
                    (beforeUrl) => {
                      const visiblePasswordInputs = Array.from(document.querySelectorAll('input[type="password"]'))
                        .filter((el) => {
                          const style = window.getComputedStyle(el);
                          const rect = el.getBoundingClientRect();
                          return style && style.visibility !== 'hidden' && style.display !== 'none'
                            && rect.width > 0 && rect.height > 0 && el.getClientRects().length > 0;
                        });
                      return window.location.href !== beforeUrl || visiblePasswordInputs.length === 0;
                    }
                    """,
                    arg=before_url,
                    timeout=login_wait_ms,
                )
            except Exception:
                page.wait_for_timeout(1000)
        except Exception as exc:
            observation = self.observe(last_action_result="login_submit failed", force_artifact=True)
            return ActionResult("retryable_failure", observation, error=str(exc))
        return ActionResult("success", self.observe(last_action_result="login submitted"))

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Playwright 未安装，请执行 playwright install chromium 并安装 Python 依赖。") from exc
        self._playwright = sync_playwright().start()
        launch_kwargs = {"headless": self.headless}
        if self.browser_channel and self.browser_channel != "chromium":
            launch_kwargs["channel"] = self.browser_channel
        if self.slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = self.slow_mo_ms
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        context_kwargs = {}
        if self.session_state_path and self.session_state_path.exists():
            # crash resume 时用上次保存的 storage_state 创建新 context，尽量恢复登录会话。
            context_kwargs["storage_state"] = str(self.session_state_path)
        if self.video_enabled:
            context_kwargs["record_video_dir"] = str(self.artifact_dir / "video")
        self._context = self._browser.new_context(**context_kwargs)
        if self.trace_enabled:
            self._context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self._page = self._context.new_page()
        return self._page

    def _resolve_locator(self, page, action: BrowserAction):
        hint = action.target_hint or ""
        if action.type == "click" and self._is_command_hint(hint):
            command_locator = self._command_click_locator(page, hint)
            if command_locator is not None:
                return command_locator
        if action.target_id:
            target_locator = self._first_existing_usable_locator(
                *[
                    locator
                    for frame in page.frames
                    for locator in self._target_id_locators(frame, action.target_id)
                ]
            )
            if target_locator is not None:
                return target_locator
        if action.type == "type" and (hint == "__password__" or hint in {"密码", "password", "Password"}):
            return self._first_usable_locator(
                *[
                    frame.locator(
                        "input[type='password']"
                        ":not([name*='pin' i])"
                        ":not([id*='pin' i])"
                        ":not([placeholder*='证书'])"
                        ":not([placeholder*='pin' i])"
                    )
                    for frame in page.frames
                ]
            )
        if action.type == "type" and hint == "__username__":
            return self._first_usable_locator(
                *[
                    frame.locator("input:not([type='hidden']):not([type='password'])")
                    for frame in page.frames
                ]
            )
        if action.type == "type":
            selector_locators = []
            exact_locators = []
            fallback_locators = []
            field_locators = []
            dropdown_locators = []
            for frame in page.frames:
                selector_locators.extend(frame.locator(selector) for selector in self._selector_hints(hint))
                exact_locators.append(frame.get_by_label(hint, exact=True).or_(frame.get_by_placeholder(hint, exact=True)))
                fallback_locators.append(frame.get_by_label(hint).or_(frame.get_by_placeholder(hint)))
                field_locators.extend(self._field_locators_for_label(frame, hint))
                dropdown_locators.extend(self._dropdown_search_locators(frame))
            return self._first_usable_locator(
                *selector_locators,
                *exact_locators,
                *fallback_locators,
                *field_locators,
                *dropdown_locators,
            )
        if hint.startswith("css="):
            selector = hint.removeprefix("css=")
            return self._first_usable_locator(*[frame.locator(selector) for frame in page.frames])
        if self._looks_like_css_selector(hint):
            return self._first_usable_locator(*[frame.locator(hint) for frame in page.frames])
        if hint.startswith("xpath="):
            return self._first_usable_locator(*[frame.locator(hint) for frame in page.frames])
        if action.type == "click":
            date_locator = self._date_click_locator(page, action)
            if date_locator is not None:
                return date_locator
            popup_locator = self._popup_click_locator(page, action)
            if popup_locator is not None:
                return popup_locator
            first_row_locator = self._first_row_click_locator(page, action)
            if first_row_locator is not None:
                return first_row_locator
            button_locators = [frame.get_by_role("button", name=hint) for frame in page.frames]
            if any(self._locator_has_candidates(locator) for locator in button_locators):
                return self._first_usable_locator(*button_locators)
            exact_text = [frame.get_by_text(hint, exact=True) for frame in page.frames]
            fallback_text = [frame.get_by_text(hint) for frame in page.frames]
            return self._first_usable_locator(*exact_text, *fallback_text)
        exact_text = [frame.get_by_text(hint, exact=True) for frame in page.frames]
        fallback_text = [frame.get_by_text(hint) for frame in page.frames]
        return self._first_usable_locator(*exact_text, *fallback_text)

    def _target_id_locators(self, frame, target_id: str):
        escaped = self._css_attr_value(target_id)
        return [
            frame.locator(f"[data-aiops-id=\"{escaped}\"]"),
            frame.locator(f"[id=\"{escaped}\"]"),
            frame.locator(f"[name=\"{escaped}\"]"),
            frame.locator(f"[aria-label=\"{escaped}\"]"),
        ]

    def _css_attr_value(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _is_date_type_action(self, action: BrowserAction) -> bool:
        value = (action.value or "").strip()
        if not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
            return False
        searchable = " ".join(item for item in (action.target_hint, action.target_id or "", action.expected_outcome) if item).lower()
        return any(token in searchable for token in ("date", "日期", "时间"))

    def _date_click_locator(self, page, action: BrowserAction):
        hint_text = " ".join(item for item in (action.target_hint, action.expected_outcome) if item)
        if not any(token in hint_text.lower() for token in ("date", "日期", "时间")):
            return None
        prefer_end = any(token in hint_text for token in ("结束", "截止", "至", "end", "End"))
        locators = []
        for frame in page.frames:
            preferred = self._date_input_selectors(prefer_end=prefer_end)
            fallback = self._date_input_selectors(prefer_end=not prefer_end)
            locators.extend(frame.locator(selector) for selector in preferred + fallback)
        return self._first_existing_usable_locator(*locators)

    def _date_input_selectors(self, *, prefer_end: bool) -> list[str]:
        specific = (
            [
                "input[name*='End' i]",
                "input[id*='End' i]",
                "input[name*='To' i]",
                "input[id*='To' i]",
            ]
            if prefer_end
            else [
                "input[name*='Start' i]",
                "input[id*='Start' i]",
                "input[name*='Begin' i]",
                "input[id*='Begin' i]",
                "input[name*='From' i]",
                "input[id*='From' i]",
            ]
        )
        return [
            *specific,
            "input[name*='date' i]:not([type='hidden'])",
            "input[id*='date' i]:not([type='hidden'])",
            "input[placeholder*='yyyy' i]:not([type='hidden'])",
        ]

    def _set_date_field(self, page, action: BrowserAction, locator) -> None:
        value = (action.value or "").strip()
        if self._locator_value_equals(locator, value):
            return
        try:
            locator.click(timeout=action.timeout_ms)
        except Exception:
            pass
        if self._select_visible_calendar_date(page, value, action.timeout_ms):
            return
        try:
            locator.fill(value, timeout=action.timeout_ms)
            self._dispatch_input_events(locator)
            return
        except Exception as fill_error:
            try:
                locator.evaluate(
                    """
                    (el, value) => {
                      const oldReadonly = el.getAttribute('readonly');
                      const oldDisabled = el.getAttribute('disabled');
                      el.removeAttribute('readonly');
                      el.removeAttribute('disabled');
                      el.focus();
                      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                      const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                      if (descriptor && descriptor.set) {
                        descriptor.set.call(el, value);
                      } else {
                        el.value = value;
                      }
                      el.dispatchEvent(new Event('input', { bubbles: true }));
                      el.dispatchEvent(new Event('change', { bubbles: true }));
                      el.blur();
                      el.dispatchEvent(new Event('blur', { bubbles: true }));
                      if (oldReadonly !== null) el.setAttribute('readonly', oldReadonly);
                      if (oldDisabled !== null) el.setAttribute('disabled', oldDisabled);
                    }
                    """,
                    value,
                )
                return
            except Exception:
                raise fill_error

    def _locator_value_equals(self, locator, value: str) -> bool:
        try:
            return str(locator.input_value()).strip() == value
        except Exception:
            return False

    def _dispatch_input_events(self, locator) -> None:
        try:
            locator.evaluate(
                """
                (el) => {
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                  el.blur();
                  el.dispatchEvent(new Event('blur', { bubbles: true }));
                }
                """
            )
        except Exception:
            pass

    def _select_visible_calendar_date(self, page, value: str, timeout_ms: int) -> bool:
        day_text = str(int(value.rsplit("-", 1)[1]))
        locators = []
        for frame in page.frames:
            for popup in self._date_popup_selectors():
                popup_locator = frame.locator(f"{popup}:visible")
                locators.extend(
                    [
                        popup_locator.get_by_text(value, exact=True),
                        popup_locator.get_by_text(day_text, exact=True),
                    ]
                )
        option = self._first_existing_usable_locator(*locators)
        if option is None:
            return False
        try:
            option.click(timeout=timeout_ms)
        except Exception:
            return False
        return True

    def _locator_has_candidates(self, locator) -> bool:
        try:
            return locator.count() > 0
        except Exception:
            return False

    def _is_command_hint(self, hint: str) -> bool:
        return hint in {"查询", "取消", "分配岗位", "已分配岗位", "启用/停用", "确定", "关闭"}

    def _command_click_locator(self, page, hint: str):
        locators = []
        for frame in page.frames:
            locators.extend(
                [
                    frame.get_by_role("button", name=hint),
                    frame.locator(f"input[type='button'][value='{hint}']"),
                    frame.locator(f"input[type='submit'][value='{hint}']"),
                    frame.locator(f"a:has-text('{hint}')"),
                    frame.get_by_text(hint, exact=True),
                ]
            )
        return self._first_existing_usable_locator(*locators)

    def _looks_like_css_selector(self, hint: str) -> bool:
        stripped = hint.strip()
        if not stripped:
            return False
        def starts_with_tag(tag: str) -> bool:
            return stripped == tag or stripped.startswith(
                (f"{tag}#", f"{tag}.", f"{tag}[", f"{tag}:", f"{tag} ", f"{tag}>", f"{tag}+")
            )

        return (
            stripped.startswith(("#", ".", "["))
            or "[" in stripped and "]" in stripped
            or any(starts_with_tag(tag) for tag in ("input", "select", "textarea", "button", "a", "div", "span"))
        )

    def _selector_hints(self, hint: str) -> list[str]:
        stripped = hint.strip()
        if not stripped:
            return []
        if stripped.startswith("css="):
            return [stripped.removeprefix("css=")]
        if stripped.startswith("xpath="):
            return [stripped]
        if not self._looks_like_css_selector(stripped):
            return []
        selectors = [stripped]
        if stripped.startswith("select2-"):
            selectors.insert(0, f".{stripped}")
        return list(dict.fromkeys(selectors))

    def _field_locators_for_label(self, frame, label: str):
        locators = [
            frame.locator(
                "xpath="
                f"//*[normalize-space(.)='{label}']"
                "/following::input[not(@type='hidden') and not(@disabled) and not(@readonly)][1]"
            ),
            frame.locator(
                "xpath="
                f"//*[normalize-space(.)='{label}']"
                "/following::textarea[not(@disabled) and not(@readonly)][1]"
            ),
            frame.locator(
                "xpath="
                f"//*[contains(normalize-space(.), '{label}')]"
                "/following::input[not(@type='hidden') and not(@disabled) and not(@readonly)][1]"
            ),
        ]
        semantic_selectors = self._semantic_field_selectors(label)
        locators.extend(frame.locator(selector) for selector in semantic_selectors)
        return locators

    def _semantic_field_selectors(self, label: str) -> list[str]:
        if label in {"用户名", "用户名称"}:
            return [
                "input[name='userName']",
                "input[id='userName']",
                "input[name*='user' i]:not([type='hidden']):not([type='password'])",
                "input[id*='user' i]:not([type='hidden']):not([type='password'])",
            ]
        if label == "登录名称":
            return [
                "input[name*='login' i]:not([type='hidden']):not([type='password'])",
                "input[id*='login' i]:not([type='hidden']):not([type='password'])",
            ]
        return []

    def _popup_click_locator(self, page, action: BrowserAction):
        hint = action.target_hint or ""
        locators = []
        for frame in page.frames:
            locators.extend(self._popup_option_locators(frame, hint))
        return self._first_existing_usable_locator(*locators)

    def _first_row_click_locator(self, page, action: BrowserAction):
        hint = action.target_hint or ""
        if not self._means_first_result(hint):
            return None
        locators = []
        data_row_selectors = [
            ".ui-jqgrid-btable tr.jqgrow",
            "tr.jqgrow",
            "tbody tr:has(td a)",
            "table tr:has(td a)",
            "tbody tr:has(td):not(:has(th))",
            "tbody tr",
        ]
        for frame in page.frames:
            for row_selector in data_row_selectors:
                locators.extend(
                    [
                        frame.locator(f"{row_selector} input[type='checkbox']").first,
                        frame.locator(f"{row_selector} input[type='radio']").first,
                        frame.locator(f"{row_selector} [role='checkbox']").first,
                        frame.locator(f"{row_selector} [role='radio']").first,
                    ]
                )
            locators.extend(frame.locator(row_selector).first for row_selector in data_row_selectors)
        return self._first_existing_usable_locator(*locators)

    def _popup_option_locators(self, frame, hint: str):
        popup_selectors = self._popup_selectors()
        option_selectors = [
            "[role='option']",
            "li",
            "tr",
            ".select2-result-label",
            ".select2-results li",
            ".el-select-dropdown__item",
            ".ant-select-item-option",
        ]
        if self._means_first_result(hint):
            return [
                frame.locator(f"{popup}:visible {option}").first
                for popup in popup_selectors
                for option in option_selectors
            ]
        text_locators = []
        if hint:
            for popup in popup_selectors:
                popup_locator = frame.locator(f"{popup}:visible")
                text_locators.append(popup_locator.get_by_text(hint, exact=True))
                text_locators.append(popup_locator.get_by_text(hint))
        return text_locators

    def _dropdown_search_locators(self, frame):
        editable_selectors = [
            "input:not([type='hidden']):not([type='password']):not([disabled]):not([readonly])",
            "textarea:not([disabled]):not([readonly])",
            "[contenteditable='true']",
        ]
        popup_selectors = self._popup_selectors()
        focused_editable = ",".join(f"{selector}:focus" for selector in editable_selectors)
        return [
            frame.locator(focused_editable),
            *[
                frame.locator(",".join(f"{container}:visible {selector}" for selector in editable_selectors))
                for container in popup_selectors
            ],
        ]

    def _popup_selectors(self) -> list[str]:
        return [
            "[role='listbox']",
            "[role='dialog']",
            "[class*='select-dropdown']",
            "[class*='dropdown']",
            "[class*='popover']",
            "[class*='popup']",
            ".select2-drop",
            ".select2-drop-active",
            ".el-select-dropdown",
            ".ant-select-dropdown",
            ".layui-layer",
        ]

    def _date_popup_selectors(self) -> list[str]:
        return [
            ".WdateDiv",
            "#_my97DP",
            ".ui-datepicker",
            ".datepicker",
            ".date-picker",
            ".calendar",
            "[class*='datepicker']",
            "[class*='date-picker']",
            "[class*='calendar']",
            *self._popup_selectors(),
        ]

    def _means_first_result(self, hint: str) -> bool:
        lowered = hint.lower()
        return any(token in lowered for token in ("第一个", "第一条", "首个", "first"))

    def _first_usable_locator(self, *locators):
        for locator in locators:
            try:
                count = min(locator.count(), 20)
            except Exception:
                count = 0
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
        for locator in locators:
            return locator.first
        raise RuntimeError("无法定位可交互元素")

    def _first_existing_usable_locator(self, *locators):
        for locator in locators:
            try:
                count = min(locator.count(), 20)
            except Exception:
                count = 0
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
        return None

    def _collect_interactive_elements(self, page) -> list[InteractiveElement]:
        script = """
        ({ idPrefix }) => {
          document.querySelectorAll('[data-aiops-id]').forEach((el) => el.removeAttribute('data-aiops-id'));
          const elements = Array.from(document.querySelectorAll('a,button,input,select,textarea,[role="button"]'))
          .filter((el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0 && el.getClientRects().length > 0;
          })
          .slice(0, 80);
          elements.forEach((el, index) => el.setAttribute('data-aiops-id', `${idPrefix}${index}`));
          return elements.map((el) => ({
            element_id: el.getAttribute('data-aiops-id'),
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            input_type: el.getAttribute('type') || '',
            name: el.getAttribute('aria-label') || el.getAttribute('name') || el.getAttribute('placeholder') || el.getAttribute('title') || '',
            title: el.getAttribute('title') || '',
            href: el.getAttribute('href') || '',
            placeholder: el.getAttribute('placeholder') || '',
            context: ((el.closest('form,li,tr,td,th,div,section,nav') || el.parentElement || el).innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 240),
            text: ((() => {
              const inputType = (el.getAttribute('type') || '').toLowerCase();
              const name = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('placeholder') || ''}`.toLowerCase();
              if (inputType === 'password' || name.includes('password') || name.includes('用户名') || name.includes('账号') || name.includes('user')) return '';
              return el.innerText || el.value || '';
            })()).trim().slice(0, 120),
            locator_strategy: el.getAttribute('data-aiops-id') ? 'data-aiops-id' : 'semantic',
            is_enabled: !el.disabled,
            is_visible: true
          }));
        }
        """
        elements: list[InteractiveElement] = []
        for frame_index, frame in enumerate(page.frames):
            id_prefix = "aiops-el-" if frame == page.main_frame else f"aiops-frame-{frame_index}-el-"
            try:
                elements.extend(InteractiveElement(**item) for item in frame.evaluate(script, {"idPrefix": id_prefix}))
            except Exception:
                continue
        return elements[:200]

    def _collect_page_text(self, page) -> str:
        script = """
        () => {
          const text = (document.body && document.body.innerText) ? document.body.innerText : '';
          return text.replace(/\\s+/g, ' ').trim().slice(0, 6000);
        }
        """
        parts = []
        for frame in page.frames:
            try:
                text = str(frame.evaluate(script))
            except Exception:
                continue
            if text:
                parts.append(text)
        return " ".join(parts).strip()[:6000]

    def _collect_visible_messages(self, page) -> list[str]:
        script = """
        () => {
          const selectors = [
            '[role="alert"]', '.error', '.message', '.toast', '.tips', '.tip',
            '.layui-layer-content', '.el-message', '.ant-message',
            '[class*="error"]', '[class*="msg"]', '[class*="warn"]',
            'h1', 'h2'
          ];
          const candidates = Array.from(document.querySelectorAll(selectors.join(',')));
          return candidates
            .filter((el) => {
              const style = window.getComputedStyle(el);
              const rect = el.getBoundingClientRect();
              return style && style.visibility !== 'hidden' && style.display !== 'none'
                && rect.width > 0 && rect.height > 0;
            })
            .map((el) => (el.innerText || el.textContent || '').trim())
            .filter(Boolean)
            .slice(0, 30);
        }
        """
        messages = []
        for frame in page.frames:
            try:
                messages.extend(str(item) for item in frame.evaluate(script))
            except Exception:
                continue
        return messages[:30]

    def _collect_forms(self, elements: list[InteractiveElement]) -> list[dict[str, str]]:
        fields = [element for element in elements if element.role in {"input", "select", "textarea"}]
        if not fields:
            return []
        return [{"fields": [field.name or field.text or field.element_id for field in fields]}]

    def _classify_page(self, elements: list[InteractiveElement], messages: list[str]) -> str:
        roles = {element.role for element in elements}
        text = " ".join(messages).lower()
        if "captcha" in text or "验证码" in text or "mfa" in text or "otp" in text or "二次验证" in text or "短信" in text:
            return "verification"
        if "input" in roles and any(
            element.input_type.lower() == "password" or "password" in (element.name or "").lower() or "密码" in element.name
            for element in elements
        ):
            return "login"
        if "input" in roles or "textarea" in roles:
            return "form"
        if elements:
            return "interactive"
        return "content"

    def _done_signals(self, title: str, url: str, messages: list[str]) -> list[str]:
        signals = []
        joined = " ".join(messages)
        for keyword in ("完成", "成功", "success", "done"):
            if keyword.lower() in joined.lower() or keyword.lower() in title.lower() or keyword.lower() in url.lower():
                signals.append(keyword)
        return signals

    def _save_artifacts(self, observation: BrowserObservation) -> None:
        page = self._page
        summary_path = self.artifact_dir / "page-summary.txt"
        lines = [
            f"title={observation.title}",
            f"url={observation.url}",
            f"page_type={observation.page_type}",
            f"blocking_reason={observation.blocking_reason or ''}",
            "messages=" + " | ".join(observation.visible_messages),
            "page_text=" + observation.page_text[:2000],
            "elements=" + " | ".join(
                html.escape(element.name or element.text or element.element_id)
                for element in observation.interactive_elements[:20]
            ),
        ]
        summary_path.write_text("\n".join(lines), encoding="utf-8")
        observation.page_summary_path = str(summary_path)
        if page is not None:
            screenshot_path = self.artifact_dir / "screenshot.png"
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
                observation.screenshot_path = str(screenshot_path)
            except Exception:
                pass

    def _domain_allowed(self, url: str) -> bool:
        if not self.allowed_domains:
            return True
        parsed = urlparse(url)
        host = (parsed.hostname or parsed.netloc).lower()
        netloc = parsed.netloc.lower()
        return any(
            host == domain.lower()
            or netloc == domain.lower()
            or host.endswith("." + domain.lower())
            for domain in self.allowed_domains
        )

    def _safe_eval(self, func, default):
        try:
            return func()
        except Exception:
            return default

    def observation_to_dict(self, observation: BrowserObservation) -> dict:
        return asdict(observation)
