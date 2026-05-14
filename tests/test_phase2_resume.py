import json

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
    task = controller.run("请打开 http://example.test/form 并保存权限设置")
    assert task.status == "awaiting_confirmation"
    assert task.result["data"]["pending_action_raw"]["type"] == "click"
    assert ConfirmationFakeBrowser.instance_count == 1
    assert ConfirmationFakeBrowser.close_calls == 0

    resumed = controller.confirm(task.id)

    assert resumed.status == "success"
    assert ConfirmationFakeBrowser.instance_count == 1
    assert ConfirmationFakeBrowser.close_calls == 1
    assert resumed.result["data"]["status"] == "completed"
    actions = [step["action"]["type"] for step in resumed.result["data"]["steps"]]
    assert actions[0] == "open_url"
    assert "click" in actions
    saved_task = json.loads((tmp_path / "storage" / "tasks" / f"{task.id}.json").read_text(encoding="utf-8"))
    assert saved_task["status"] == "success"
