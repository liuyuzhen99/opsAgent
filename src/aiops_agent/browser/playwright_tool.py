from __future__ import annotations

import html
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
                locator.click(timeout=action.timeout_ms)
            elif action.type == "type":
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
            context_kwargs["storage_state"] = str(self.session_state_path)
        if self.video_enabled:
            context_kwargs["record_video_dir"] = str(self.artifact_dir / "video")
        self._context = self._browser.new_context(**context_kwargs)
        if self.trace_enabled:
            self._context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self._page = self._context.new_page()
        return self._page

    def _resolve_locator(self, page, action: BrowserAction):
        if action.target_id:
            return self._first_usable_locator(
                *[frame.locator(f"[data-aiops-id='{action.target_id}']") for frame in page.frames]
            )
        hint = action.target_hint or ""
        if hint.startswith("css="):
            selector = hint.removeprefix("css=")
            return self._first_usable_locator(*[frame.locator(selector) for frame in page.frames])
        if hint.startswith("xpath="):
            return self._first_usable_locator(*[frame.locator(hint) for frame in page.frames])
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
            exact_locators = []
            fallback_locators = []
            for frame in page.frames:
                exact_locators.append(frame.get_by_label(hint, exact=True).or_(frame.get_by_placeholder(hint, exact=True)))
                fallback_locators.append(frame.get_by_label(hint).or_(frame.get_by_placeholder(hint)))
            return self._first_usable_locator(*exact_locators, *fallback_locators)
        if action.type == "click":
            button_locators = [frame.get_by_role("button", name=hint) for frame in page.frames]
            if any(self._locator_has_candidates(locator) for locator in button_locators):
                return self._first_usable_locator(*button_locators)
            exact_text = [frame.get_by_text(hint, exact=True) for frame in page.frames]
            fallback_text = [frame.get_by_text(hint) for frame in page.frames]
            return self._first_usable_locator(*exact_text, *fallback_text)
        exact_text = [frame.get_by_text(hint, exact=True) for frame in page.frames]
        fallback_text = [frame.get_by_text(hint) for frame in page.frames]
        return self._first_usable_locator(*exact_text, *fallback_text)

    def _locator_has_candidates(self, locator) -> bool:
        try:
            return locator.count() > 0
        except Exception:
            return False

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
