from aiops_agent.audit.logger import FileAuditLogger
from aiops_agent.browser.agent import BrowserAgentTool
from aiops_agent.browser.models import ActionResult, BrowserObservation


SITE_CONFIG = {
    "site_key": "demo",
    "base_url": "http://example.test",
    "allowed_domains": ["example.test"],
    "workflows": {
        "create_user": {
            "entry_url": "/users",
            "navigation": ["用户管理"],
            "open_button": "新建用户",
            "submit_button": "保存用户",
            "fields": {"username": "用户名"},
        },
        "search_user": {
            "entry_url": "/users",
            "navigation": ["用户管理"],
            "submit_button": "查询",
            "fields": {"username": "用户名"},
        },
        "assign_role": {
            "entry_url": "/users/{username}/roles",
            "submit_button": "保存权限",
            "fields": {"role": "角色"},
        },
    },
}


class WorkflowFakeBrowser:
    executed = []

    def __init__(self, *args, **kwargs):
        self.session_state_path = kwargs.get("session_state_path")
        self.current = BrowserObservation(url="", title="Workflow", page_type="form")

    def execute(self, action):
        WorkflowFakeBrowser.executed.append(action)
        if action.type == "open_url":
            self.current = BrowserObservation(url=action.value or "", title="Workflow", page_type="form")
            return ActionResult("success", self.current)
        if action.type in {"click", "type", "select", "observe_page"}:
            self.current = BrowserObservation(url=self.current.url, title="Workflow", page_type="form")
            return ActionResult("success", self.current)
        if action.type in {"extract_text", "save_artifact", "finish"}:
            return ActionResult("success", self.current)
        return ActionResult("terminal_failure", self.current, error=f"unexpected {action.type}")

    def observe(self, *, last_action_result="", force_artifact=False):
        self.current.last_action_result = last_action_result
        if force_artifact:
            self.current.screenshot_path = "/tmp/workflow-shot.png"
            self.current.page_summary_path = "/tmp/workflow-summary.txt"
        return self.current

    def save_session_state(self):
        if self.session_state_path:
            return str(self.session_state_path)
        return None

    def close(self):
        return None


def test_workflow_blocks_before_each_remote_mutation_and_replays_safe_actions(tmp_path, monkeypatch):
    WorkflowFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", WorkflowFakeBrowser)
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    params = {
        "trace_id": "trace",
        "task_id": "task",
        "session_id": "session",
        "start_url": "http://example.test",
        "user_goal": "创建账号 alice，分配只读权限",
        "allowed_domains": ["example.test"],
        "auto_plan": True,
        "site_key": "demo",
        "workflow": "create_user_and_assign_role",
        "workflow_fields": {"username": "alice", "role": "只读权限"},
        "site_config": SITE_CONFIG,
        "max_steps": 12,
    }

    first = tool.execute(params)

    assert first.success is False
    assert first.data["status"] == "awaiting_confirmation"
    assert first.data["pending_action_raw"]["key"] == "create_user.submit"
    assert any(action["type"] == "type" and action["value"] == "alice" for action in first.data["replay_actions"])

    second = tool.execute(
        {
            **params,
            "confirmed_action": first.data["pending_action_raw"],
            "replay_actions": first.data["replay_actions"],
            "completed_action_keys": first.data["completed_action_keys"],
        }
    )

    assert second.success is False
    assert second.data["status"] == "awaiting_confirmation"
    assert second.data["pending_action_raw"]["key"] == "assign_role.submit"
    executed_keys = [action.key for action in WorkflowFakeBrowser.executed]
    assert "create_user.field.username" in executed_keys
    assert "create_user.submit" in executed_keys
    assert "assign_role.field.role" in executed_keys


def test_workflow_blocks_without_required_fields(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "user_goal": "分配只读权限",
            "site_key": "demo",
            "workflow": "assign_role",
            "workflow_fields": {"role": "只读权限"},
            "site_config": SITE_CONFIG,
        }
    )

    assert result.success is False
    assert result.data["status"] == "blocked"
    assert "username" in result.error


def test_search_user_workflow_is_read_only_and_completes(tmp_path, monkeypatch):
    WorkflowFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", WorkflowFakeBrowser)
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "查询用户 alice",
            "allowed_domains": ["example.test"],
            "auto_plan": True,
            "site_key": "demo",
            "workflow": "search_user",
            "workflow_fields": {"username": "alice"},
            "site_config": SITE_CONFIG,
            "max_steps": 12,
        }
    )

    assert result.success is True
    executed = [(action.key, action.type, action.target_hint, action.value) for action in WorkflowFakeBrowser.executed]
    assert ("search_user.nav.1", "click", "用户管理", None) in executed
    assert ("search_user.field.username", "type", "用户名", "alice") in executed
    assert ("search_user.submit", "click", "查询", None) in executed
