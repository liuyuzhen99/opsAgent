from __future__ import annotations

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
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [])
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
        return _task("confirmed", task_id=task_id, session_id="session-1", status="success")


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
