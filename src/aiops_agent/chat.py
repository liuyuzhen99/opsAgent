from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import sys
from typing import TextIO

from aiops_agent.agent.controller import AgentController
from aiops_agent.agent.progress import ProgressEvent
from aiops_agent.tasks.models import Task


@dataclass(slots=True)
class ChatOptions:
    session_id: str | None = None
    llm_profile: str | None = None
    max_steps: int = 40
    require_confirmation: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    credential_ref: str | None = None
    browser_trace: bool = False
    browser_video: bool = False
    browser_site: str | None = None
    browser_channel: str | None = None
    browser_slow_mo_ms: int = 800


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
        self.last_note_source: str | None = None
        self._prompt_session = self._build_prompt_session()

    def run(self) -> int:
        self._print(
            "进入 opsAgent chat 模式。输入 /exit 退出，/session 查看会话，/new 开启新会话，"
            "/save-note 保存上一条输入，/note 进入多行记录，/save-skill 保存最近成功 web_action。"
        )
        while True:
            try:
                user_input = self._read_input("opsAgent> ")
            except EOFError:
                self._print("")
                return 0
            except KeyboardInterrupt:
                self._print("\n已退出 chat。")
                return 130

            text = user_input.strip()
            original_text = text
            save_note_command = False
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
            if text in {"/note", "/paste"}:
                text = self._read_block("将多行内容粘贴到下方，单独输入 /end 结束。")
                if not text:
                    continue
            if text == "/save-skill" or text.startswith("/save-skill "):
                name = text.split(maxsplit=1)[1].strip() if " " in text else None
                self._save_skill(name or None)
                continue
            if text == "/save-note" or text.startswith("/save-note "):
                save_note_command = True
                if " " in text:
                    instruction = text.split(maxsplit=1)[1].strip()
                elif self.last_note_source:
                    instruction = f"请将以下内容整理成知识库笔记：\n\n{self.last_note_source}"
                else:
                    instruction = "把上一条问答记录到知识库"
                text = f"记录到知识库：{instruction}"

            task = self._run_task(text)
            self.current_session_id = task.session_id or self.current_session_id
            self._print_task_result(task)
            if not save_note_command:
                self.last_note_source = original_text
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
        current = task
        while current.status == "awaiting_confirmation":
            self._print_confirmation(current)
            try:
                answer = self._read_input("确认继续执行? [y/N] ").strip().lower()
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
                current = self.controller.confirm(current.id, progress_callback=self._print_progress)
            except ValueError as exc:
                self._print(f"确认恢复失败: {exc}")
                return
            self.current_session_id = current.session_id or self.current_session_id
            self._print_task_result(current)

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

    def _save_skill(self, name: str | None) -> None:
        try:
            result = self.controller.save_web_skill(self.current_session_id, name=name)
        except (AttributeError, ValueError) as exc:
            self._print(f"保存 skill 失败: {exc}")
            return
        self._print(f"已生成 skill: {result.path}")
        self._print(f"参数: {', '.join(result.inputs) or '-'}")
        self._print(f"动作数: {result.action_count}")
        self._print(f"匹配关键词: {', '.join(result.matched_keywords) or '-'}")
        decisions = getattr(result, "parameterization_decisions", []) or []
        variable_decisions = [item for item in decisions if item.get("decision") == "variable"]
        fixed_decisions = [item for item in decisions if item.get("decision") == "constant"]
        if variable_decisions:
            self._print("参数预览:")
            for item in variable_decisions:
                self._print(
                    f"- {item.get('param_name')} {item.get('param_type') or 'text'} "
                    f"原值={item.get('original_value')}"
                )
        if fixed_decisions:
            self._print("固定值:")
            for item in fixed_decisions[:10]:
                self._print(
                    f"- {item.get('field_hint') or '-'}={item.get('original_value')} "
                    f"confidence={item.get('confidence')}"
                )

    def _print_progress(self, event: ProgressEvent) -> None:
        self._print(f"[{event.stage}] {event.message}")

    def _print(self, text: str) -> None:
        print(text, file=self.output)

    def _read_input(self, prompt: str) -> str:
        if self._prompt_session is not None:
            return self._prompt_session.prompt(prompt, in_thread=self._has_running_event_loop())
        return self.input_func(prompt)

    @staticmethod
    def _has_running_event_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def _read_block(self, message: str) -> str:
        self._print(message)
        lines: list[str] = []
        while True:
            line = self.input_func("... ")
            if line.strip() == "/end":
                break
            lines.append(line)
        return "\n".join(lines).strip()

    def _build_prompt_session(self):
        if self.output is not None or self.input_func is not input or not sys.stdin.isatty():
            return None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.key_binding import KeyBindings
        except ImportError:
            return None

        bindings = KeyBindings()

        def insert_newline(event):
            event.current_buffer.insert_text("\n")

        def submit(event):
            event.current_buffer.validate_and_handle()

        bindings.add("enter")(submit)
        bindings.add("escape", "enter")(insert_newline)
        for keys in (("s-enter",), ("shift-enter",)):
            try:
                bindings.add(*keys)(insert_newline)
            except (TypeError, ValueError):
                pass

        return PromptSession(
            multiline=True,
            key_bindings=bindings,
            enable_history_search=True,
        )
