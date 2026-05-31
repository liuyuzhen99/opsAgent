from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

from aiops_agent.agent.progress import ProgressEvent
from aiops_agent.chat import ChatOptions, ChatRunner
from aiops_agent.cli import build_parser
from aiops_agent.tasks.models import Task


def _input_script(values):
    items = iter(values)

    def fake_input(prompt):
        return next(items)

    return fake_input


def _task(text: str, *, task_id: str, session_id: str, status: str = "success") -> Task:
    task = Task(trace_id=f"trace-{task_id}", input=text, id=task_id, session_id=session_id)
    task.status = status
    task.risk_level = "read_only"
    task.report = f"report for {text}"
    task.result = {"success": status == "success", "data": {}}
    return task


class FakeSessionStore:
    def __init__(self):
        self.sessions = {}

    def load(self, session_id):
        return self.sessions.get(session_id)


class FakeController:
    def __init__(self, statuses=None, confirm_statuses=None):
        self.statuses = list(statuses or [])
        self.confirm_statuses = list(confirm_statuses or [])
        self.run_calls = []
        self.confirm_calls = []
        self.session_store = FakeSessionStore()

    def run(self, text, **kwargs):
        self.run_calls.append((text, kwargs))
        callback = kwargs.get("progress_callback")
        session_id = kwargs.get("session_id") or f"session-{len(self.run_calls)}"
        if callback:
            callback(ProgressEvent(stage="intent.parsed", message="已识别意图。", session_id=session_id))
        status = self.statuses.pop(0) if self.statuses else "success"
        task = _task(text, task_id=f"task-{len(self.run_calls)}", session_id=session_id, status=status)
        if status == "awaiting_confirmation":
            task.risk_level = "controlled_browser"
            task.result = {
                "success": False,
                "data": {
                    "confirmation_summary": {
                        "current_page": "Users",
                        "prepared_action": "click",
                        "target": "Save",
                        "expected_outcome": "保存权限",
                    }
                },
            }
        self.session_store.sessions[session_id] = SimpleNamespace(
            id=session_id,
            status="active",
            last_task_id=task.id,
            summary=f"last={task.input}",
        )
        return task

    def confirm(self, task_id, **kwargs):
        self.confirm_calls.append((task_id, kwargs))
        callback = kwargs.get("progress_callback")
        if callback:
            callback(ProgressEvent(stage="tool.running", message="正在恢复执行。", task_id=task_id))
        status = self.confirm_statuses.pop(0) if self.confirm_statuses else "success"
        task = _task("confirmed", task_id=task_id, session_id="session-1", status=status)
        if status == "awaiting_confirmation":
            task.risk_level = "controlled_browser"
            task.result = {
                "success": False,
                "data": {
                    "confirmation_summary": {
                        "current_page": "Users",
                        "prepared_action": "click",
                        "target": "Assign",
                        "expected_outcome": "分配岗位",
                    }
                },
            }
        return task


def test_chat_parser_accepts_chat_command():
    args = build_parser().parse_args(
        [
            "chat",
            "--config",
            "configs/rpa.json",
            "--llm-config",
            "configs/llm.json",
            "--session-id",
            "session-1",
            "--max-steps",
            "3",
            "--allowed-domains",
            "localhost,127.0.0.1",
            "--headed",
        ]
    )

    assert args.command == "chat"
    assert args.session_id == "session-1"
    assert args.max_steps == 3
    assert args.headed is True


def test_chat_parser_uses_web_agent_defaults_without_flags():
    args = build_parser().parse_args(["chat"])

    assert args.command == "chat"
    assert args.max_steps == 40
    assert args.headed is True
    assert args.browser_slow_mo_ms == 800


def test_chat_parser_can_opt_back_into_headless_mode():
    args = build_parser().parse_args(["chat", "--headless"])

    assert args.command == "chat"
    assert args.headed is False


def test_chat_runner_reuses_session_and_prints_progress():
    controller = FakeController()
    output = StringIO()
    runner = ChatRunner(
        controller,
        ChatOptions(max_steps=7),
        input_func=_input_script(["巡检生产环境 WebLogic", "再看一次", "/exit"]),
        output=output,
    )

    assert runner.run() == 0

    assert controller.run_calls[0][1]["session_id"] is None
    assert controller.run_calls[1][1]["session_id"] == "session-1"
    assert controller.run_calls[0][1]["max_steps"] == 7
    text = output.getvalue()
    assert "[intent.parsed]" in text
    assert "report for 巡检生产环境 WebLogic" in text
    assert "会话 ID: session-1" in text


def test_chat_runner_new_command_starts_new_session():
    controller = FakeController()
    runner = ChatRunner(
        controller,
        ChatOptions(),
        input_func=_input_script(["first", "/new", "second", "/exit"]),
        output=StringIO(),
    )

    assert runner.run() == 0

    assert controller.run_calls[0][1]["session_id"] is None
    assert controller.run_calls[1][1]["session_id"] is None


def test_chat_runner_save_note_command_routes_to_controller():
    controller = FakeController()
    runner = ChatRunner(
        controller,
        ChatOptions(),
        input_func=_input_script(["/save-note 记录 WebLogic OOM 步骤", "/exit"]),
        output=StringIO(),
    )

    assert runner.run() == 0

    assert controller.run_calls[0][0] == "记录到知识库：记录 WebLogic OOM 步骤"


def test_chat_runner_save_note_without_args_uses_previous_input():
    controller = FakeController()
    runner = ChatRunner(
        controller,
        ChatOptions(),
        input_func=_input_script(["WebLogic OOM：先 jmap，再重启 Managed Server", "/save-note", "/exit"]),
        output=StringIO(),
    )

    assert runner.run() == 0

    assert controller.run_calls[0][0] == "WebLogic OOM：先 jmap，再重启 Managed Server"
    assert controller.run_calls[1][0] == (
        "记录到知识库：请将以下内容整理成知识库笔记：\n\n"
        "WebLogic OOM：先 jmap，再重启 Managed Server"
    )


def test_chat_runner_note_block_collects_multiline_content():
    controller = FakeController()
    runner = ChatRunner(
        controller,
        ChatOptions(),
        input_func=_input_script([
            "/note",
            "将以下内容整理到知识库:# 接口",
            "调服务url：http://10.60.143.160:8000/FrontEnd/FrontEndServlet",
            "sp.Finance.interface.ip: 172.16.222.52",
            "/end",
            "/exit",
        ]),
        output=StringIO(),
    )

    assert runner.run() == 0

    assert controller.run_calls[0][0] == (
        "将以下内容整理到知识库:# 接口\n"
        "调服务url：http://10.60.143.160:8000/FrontEnd/FrontEndServlet\n"
        "sp.Finance.interface.ip: 172.16.222.52"
    )


def test_chat_runner_confirmation_no_does_not_resume():
    controller = FakeController(statuses=["awaiting_confirmation"])
    output = StringIO()
    runner = ChatRunner(
        controller,
        ChatOptions(),
        input_func=_input_script(["保存权限", "n", "/exit"]),
        output=output,
    )

    assert runner.run() == 0

    assert controller.confirm_calls == []
    assert "已跳过确认" in output.getvalue()


def test_chat_runner_prompt_session_uses_thread_inside_running_event_loop():
    calls = []

    class FakePromptSession:
        def prompt(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            if not kwargs.get("in_thread"):
                raise RuntimeError("asyncio.run() cannot be called from a running event loop")
            return "yes"

    runner = ChatRunner(FakeController(), ChatOptions(), output=StringIO())
    runner._prompt_session = FakePromptSession()

    async def read_input():
        return runner._read_input("确认继续执行? [y/N] ")

    assert asyncio.run(read_input()) == "yes"
    assert calls == [("确认继续执行? [y/N] ", {"in_thread": True})]


def test_chat_runner_confirmation_yes_resumes():
    controller = FakeController(statuses=["awaiting_confirmation"])
    output = StringIO()
    runner = ChatRunner(
        controller,
        ChatOptions(),
        input_func=_input_script(["保存权限", "yes", "/exit"]),
        output=output,
    )

    assert runner.run() == 0

    assert controller.confirm_calls[0][0] == "task-1"
    text = output.getvalue()
    assert "需要人工确认后才能继续" in text
    assert "[tool.running]" in text


def test_chat_runner_prompts_again_when_confirm_returns_awaiting_confirmation():
    controller = FakeController(statuses=["awaiting_confirmation"], confirm_statuses=["awaiting_confirmation", "success"])
    output = StringIO()
    runner = ChatRunner(
        controller,
        ChatOptions(),
        input_func=_input_script(["保存权限", "yes", "y", "/exit"]),
        output=output,
    )

    assert runner.run() == 0

    assert [call[0] for call in controller.confirm_calls] == ["task-1", "task-1"]
    text = output.getvalue()
    assert text.count("需要人工确认后才能继续。") == 2
    assert "click -> Assign" in text
