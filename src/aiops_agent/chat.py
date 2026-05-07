from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

from aiops_agent.agent.controller import AgentController
from aiops_agent.agent.progress import ProgressEvent
from aiops_agent.tasks.models import Task


@dataclass(slots=True)
class ChatOptions:
    session_id: str | None = None
    llm_profile: str | None = None
    max_steps: int = 20
    require_confirmation: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    credential_ref: str | None = None
    browser_trace: bool = False
    browser_video: bool = False
    browser_site: str | None = None
    browser_channel: str | None = None
    browser_slow_mo_ms: int = 0


class ChatRunner:
    def __init__(
        self,
        controller: AgentController,
        options: ChatOptions,
        *,
        input_func: Callable[[str], str] = input,
        output: TextIO | None = None,
    ):
        self.controller = controller
        self.options = options
        self.current_session_id = options.session_id
        self.input_func = input_func
        self.output = output

    def run(self) -> int:
        self._print("进入 opsAgent chat 模式。输入 /exit 退出，/session 查看会话，/new 开启新会话。")
        while True:
            try:
                user_input = self.input_func("opsAgent> ")
            except EOFError:
                self._print("")
                return 0
            except KeyboardInterrupt:
                self._print("\n已退出 chat。")
                return 130

            text = user_input.strip()
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                self._print("已退出 chat。")
                return 0
            if text == "/session":
                self._show_session()
                continue
            if text == "/new":
                self.current_session_id = None
                self._print("已开启新会话，下一条任务会创建新的 session。")
                continue

            task = self._run_task(text)
            self.current_session_id = task.session_id or self.current_session_id
            self._print_task_result(task)
            if task.status == "awaiting_confirmation":
                self._handle_confirmation(task)

    def _run_task(self, text: str) -> Task:
        return self.controller.run(
            text,
            session_id=self.current_session_id,
            llm_profile=self.options.llm_profile,
            max_steps=self.options.max_steps,
            require_confirmation=self.options.require_confirmation,
            allowed_domains=self.options.allowed_domains,
            credential_ref=self.options.credential_ref,
            browser_trace=self.options.browser_trace,
            browser_video=self.options.browser_video,
            browser_site=self.options.browser_site,
            browser_channel=self.options.browser_channel,
            browser_slow_mo_ms=self.options.browser_slow_mo_ms,
            progress_callback=self._print_progress,
        )

    def _handle_confirmation(self, task: Task) -> None:
        self._print_confirmation(task)
        try:
            answer = self.input_func("确认继续执行? [y/N] ").strip().lower()
        except EOFError:
            self._print("")
            return
        except KeyboardInterrupt:
            self._print("\n已取消确认。")
            return

        if answer not in {"y", "yes"}:
            self._print("已跳过确认，任务保持等待确认状态。")
            return

        try:
            resumed = self.controller.confirm(task.id, progress_callback=self._print_progress)
        except ValueError as exc:
            self._print(f"确认恢复失败: {exc}")
            return
        self.current_session_id = resumed.session_id or self.current_session_id
        self._print_task_result(resumed)

    def _print_confirmation(self, task: Task) -> None:
        data = (task.result or {}).get("data") or {}
        summary = data.get("confirmation_summary") or {}
        self._print("需要人工确认后才能继续。")
        self._print(f"任务 ID: {task.id}")
        self._print(f"风险等级: {task.risk_level}")
        if summary:
            page = summary.get("current_page") or summary.get("current_url") or "-"
            action = summary.get("prepared_action") or "-"
            target = summary.get("target") or "-"
            expected = summary.get("expected_outcome") or "-"
            self._print(f"当前页面: {page}")
            self._print(f"待执行动作: {action} -> {target}")
            self._print(f"预期结果: {expected}")

    def _print_task_result(self, task: Task) -> None:
        self._print("")
        self._print(task.report or "")
        self._print(f"任务 ID: {task.id}")
        self._print(f"执行状态: {task.status}")
        self._print(f"会话 ID: {task.session_id or '-'}")
        self._print("")

    def _show_session(self) -> None:
        if not self.current_session_id:
            self._print("当前还没有 active session。")
            return
        session = self.controller.session_store.load(self.current_session_id)
        if session is None:
            self._print(f"当前 session 尚未持久化: {self.current_session_id}")
            return
        self._print(f"{session.id}\t{session.status}\t{session.last_task_id or '-'}\t{session.summary}")

    def _print_progress(self, event: ProgressEvent) -> None:
        self._print(f"[{event.stage}] {event.message}")

    def _print(self, text: str) -> None:
        print(text, file=self.output)

