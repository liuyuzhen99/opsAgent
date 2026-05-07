import json

from aiops_agent.audit.logger import FileAuditLogger
from aiops_agent.browser.agent import BrowserAgentTool
from aiops_agent.browser.credentials import CredentialStore
from aiops_agent.browser.models import ActionResult, BrowserObservation, InteractiveElement


def _credential_store(tmp_path, password="secret"):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"credentials": {"demo": {"username": "alice", "password": password}}}),
        encoding="utf-8",
    )
    return CredentialStore(path)


def _login_observation(message="登录页"):
    return BrowserObservation(
        url="http://example.test/login",
        title="Login",
        page_type="login",
        visible_messages=[message],
        interactive_elements=[
            InteractiveElement(element_id="user", role="input", name="用户名", input_type="text"),
            InteractiveElement(element_id="pass", role="input", name="密码", input_type="password"),
            InteractiveElement(element_id="login", role="button", text="登录"),
        ],
    )


def _content_observation():
    return BrowserObservation(
        url="http://example.test/dashboard",
        title="Dashboard",
        page_type="content",
        visible_messages=["登录成功"],
    )


class FakePlaywrightTool:
    def __init__(self, *args, **kwargs):
        self.username = ""
        self.password = ""
        self.mode = kwargs.get("allowed_domains", ["success"])[0] if kwargs.get("allowed_domains") else "success"
        self.current = _login_observation()

    def execute(self, action):
        if action.type == "open_url":
            self.current = _login_observation()
            return ActionResult("success", self.current)
        if action.type == "observe_page":
            return ActionResult("success", self.current)
        if action.type == "type":
            if action.target_hint == "用户名":
                self.username = action.value or ""
            if action.target_hint == "密码":
                self.password = action.value or ""
            return ActionResult("success", self.current)
        if action.type == "login_submit":
            if self.mode == "mfa":
                self.current = BrowserObservation(page_type="verification", title="MFA", visible_messages=["MFA required"])
            elif self.password == "secret":
                self.current = _content_observation()
            else:
                self.current = _login_observation("密码错误")
            return ActionResult("success", self.current)
        if action.type in {"extract_text", "save_artifact", "finish"}:
            return ActionResult("success", self.current)
        return ActionResult("terminal_failure", self.current, error=f"unexpected action {action.type}")

    def observe(self, *, last_action_result="", force_artifact=False):
        self.current.last_action_result = last_action_result
        if force_artifact:
            self.current.screenshot_path = "/tmp/fake-screenshot.png"
            self.current.page_summary_path = "/tmp/fake-summary.txt"
        return self.current

    def close(self):
        return None


def _run_login(tmp_path, monkeypatch, *, password="secret", mode="success"):
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", FakePlaywrightTool)
    audit = FileAuditLogger(tmp_path / "audit.jsonl")
    tool = BrowserAgentTool(audit_logger=audit, credential_store=_credential_store(tmp_path, password=password))
    result = tool.execute(
        {
            "trace_id": "trace-login",
            "task_id": "task-login",
            "session_id": "session-login",
            "start_url": "http://example.test/login",
            "user_goal": "登录后读取页面",
            "allowed_domains": [mode],
            "requires_login": True,
            "credential_ref": "demo",
            "auto_plan": True,
            "max_steps": 8,
        }
    )
    return result, audit.path.read_text(encoding="utf-8")


def test_browser_agent_login_success_does_not_persist_password(tmp_path, monkeypatch):
    result, audit_text = _run_login(tmp_path, monkeypatch)

    assert result.success is True
    assert result.data["status"] == "completed"
    assert [step["action"]["type"] for step in result.data["steps"]][:5] == [
        "open_url",
        "observe_page",
        "type_username",
        "type_password",
        "login_submit",
    ]
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "secret" not in serialized
    assert "alice" not in serialized
    assert "secret" not in audit_text
    assert "alice" not in audit_text
    assert '"value": "***"' in audit_text


def test_browser_agent_login_bad_password_blocks_with_artifact(tmp_path, monkeypatch):
    result, _audit_text = _run_login(tmp_path, monkeypatch, password="wrong")

    assert result.success is False
    assert result.data["status"] == "blocked"
    assert "登录失败" in result.error
    assert result.artifacts


def test_browser_agent_login_mfa_blocks(tmp_path, monkeypatch):
    result, _audit_text = _run_login(tmp_path, monkeypatch, mode="mfa")

    assert result.success is False
    assert result.data["status"] == "blocked"
    assert "验证码" in result.error or "MFA" in result.error
