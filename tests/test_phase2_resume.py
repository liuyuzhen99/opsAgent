import json
import threading

from aiops_agent.audit.logger import FileAuditLogger
from aiops_agent.browser.agent import BrowserAgentTool
from aiops_agent.browser.models import ActionResult, BrowserObservation
from aiops_agent.cli import create_controller
from tests.test_agent_flow import _write_llm_config, _write_rpa_config


class ConfirmationFakeBrowser:
    seen_state_paths = []
    instance_count = 0
    close_calls = 0

    def __init__(self, *args, **kwargs):
        ConfirmationFakeBrowser.instance_count += 1
        self.session_state_path = kwargs.get("session_state_path")
        self.current = BrowserObservation(url="http://example.test/form", title="Form", page_type="form")
        ConfirmationFakeBrowser.seen_state_paths.append(str(self.session_state_path))

    def execute(self, action):
        if action.type == "open_url":
            self.current.url = action.value
            return ActionResult("success", self.current)
        if action.type == "click":
            self.current = BrowserObservation(url="http://example.test/done", title="Done", page_type="content", visible_messages=["完成"])
            return ActionResult("success", self.current)
        if action.type in {"observe_page", "extract_text", "save_artifact", "finish"}:
            return ActionResult("success", self.current)
        return ActionResult("terminal_failure", self.current, error=f"unexpected {action.type}")

    def observe(self, *, last_action_result="", force_artifact=False):
        self.current.last_action_result = last_action_result
        if force_artifact:
            self.current.screenshot_path = "/tmp/fake-shot.png"
            self.current.page_summary_path = "/tmp/fake-summary.txt"
        return self.current

    def save_session_state(self):
        if self.session_state_path:
            return str(self.session_state_path)
        return None

    def close(self):
        ConfirmationFakeBrowser.close_calls += 1
        self.save_session_state()


class ThreadBoundConfirmationFakeBrowser(ConfirmationFakeBrowser):
    thread_ids = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner_thread_id = threading.get_ident()
        ThreadBoundConfirmationFakeBrowser.thread_ids.append(self.owner_thread_id)

    def _assert_owner_thread(self):
        current_thread_id = threading.get_ident()
        ThreadBoundConfirmationFakeBrowser.thread_ids.append(current_thread_id)
        if current_thread_id != self.owner_thread_id:
            raise RuntimeError("Playwright browser used from a different thread")

    def execute(self, action):
        self._assert_owner_thread()
        return super().execute(action)

    def observe(self, *, last_action_result="", force_artifact=False):
        self._assert_owner_thread()
        return super().observe(last_action_result=last_action_result, force_artifact=force_artifact)

    def save_session_state(self):
        self._assert_owner_thread()
        return super().save_session_state()

    def close(self):
        self._assert_owner_thread()
        return super().close()


def test_browser_agent_records_session_state_path_for_same_session(tmp_path, monkeypatch):
    ConfirmationFakeBrowser.instance_count = 0
    ConfirmationFakeBrowser.close_calls = 0
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", ConfirmationFakeBrowser)
    audit = FileAuditLogger(tmp_path / "audit.jsonl")
    tool = BrowserAgentTool(audit_logger=audit, artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session-a",
            "start_url": "http://example.test/form",
            "user_goal": "读取页面",
            "auto_plan": True,
            "max_steps": 5,
        }
    )

    assert result.success is True
    assert result.data["session_state_path"].endswith("session-a/browser-state.json")
    assert ConfirmationFakeBrowser.seen_state_paths[-1].endswith("session-a/browser-state.json")


def test_controller_confirm_resumes_pending_browser_action(tmp_path, monkeypatch):
    ConfirmationFakeBrowser.instance_count = 0
    ConfirmationFakeBrowser.close_calls = 0
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", ConfirmationFakeBrowser)
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    events = []
    task = controller.run("请打开 http://example.test/form 并保存权限设置", progress_callback=events.append)
    stages = [event.stage for event in events]
    assert task.status == "awaiting_confirmation"
    assert task.result["data"]["pending_action_raw"]["type"] == "click"
    assert "web.action.proposed" in stages
    assert "web.action.executed" in stages
    assert "web.page.observed" in stages
    assert stages.index("web.action.proposed") < stages.index("interrupt.requested")
    interrupted_state = controller.get_state(task.id)
    assert not interrupted_state.interrupts
    web_state = controller.get_web_state(task.id)
    assert web_state.interrupts
    assert web_state.interrupts[0].value["confirmation_type"] == "web_action"
    assert web_state.interrupts[0].value["langgraph"]["node"] == "risk_gate"
    assert ConfirmationFakeBrowser.instance_count == 1
    assert ConfirmationFakeBrowser.close_calls == 0

    resume_events = []
    prior_step_count = len(task.result["data"]["steps"])
    resumed = controller.confirm(task.id, progress_callback=resume_events.append)

    assert resumed.status == "success"
    assert ConfirmationFakeBrowser.instance_count == 1
    assert ConfirmationFakeBrowser.close_calls == 1
    assert resumed.result["data"]["status"] == "completed"
    actions = [step["action"]["type"] for step in resumed.result["data"]["steps"]]
    assert actions[0] == "open_url"
    assert "click" in actions
    resumed_state = controller.get_state(task.id)
    assert not resumed_state.interrupts
    resumed_web_state = controller.get_web_state(task.id)
    assert not resumed_web_state.interrupts
    resumed_step_indexes = [
        event.details.get("step_index")
        for event in resume_events
        if event.stage == "web.action.executed"
    ]
    assert resumed_step_indexes
    assert all(index > prior_step_count for index in resumed_step_indexes)
    saved_task = json.loads((tmp_path / "storage" / "tasks" / f"{task.id}.json").read_text(encoding="utf-8"))
    assert saved_task["status"] == "success"


def test_controller_confirm_keeps_live_browser_on_original_thread(tmp_path, monkeypatch):
    ConfirmationFakeBrowser.instance_count = 0
    ConfirmationFakeBrowser.close_calls = 0
    ThreadBoundConfirmationFakeBrowser.thread_ids = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", ThreadBoundConfirmationFakeBrowser)
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("请打开 http://example.test/form 并保存权限设置")

    assert task.status == "awaiting_confirmation"

    resumed = controller.confirm(task.id)

    assert resumed.status == "success"
    assert ConfirmationFakeBrowser.instance_count == 1
    assert len(set(ThreadBoundConfirmationFakeBrowser.thread_ids)) == 1


def test_controller_confirm_crash_resumes_web_subgraph_from_checkpoint(tmp_path, monkeypatch):
    ConfirmationFakeBrowser.instance_count = 0
    ConfirmationFakeBrowser.close_calls = 0
    ConfirmationFakeBrowser.seen_state_paths = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", ConfirmationFakeBrowser)
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("请打开 http://example.test/form 并保存权限设置")
    state_path = task.result["data"]["session_state_path"]
    web_thread_id = task.result["data"]["web_thread_id"]

    restarted = create_controller(str(config_path), str(llm_config_path))
    resumed = restarted.confirm(task.id)

    assert resumed.status == "success"
    assert resumed.result["data"]["web_thread_id"] == web_thread_id
    assert ConfirmationFakeBrowser.instance_count == 2
    assert ConfirmationFakeBrowser.seen_state_paths[-1] == state_path
    assert [step["action"]["type"] for step in resumed.result["data"]["steps"]][0] == "open_url"
