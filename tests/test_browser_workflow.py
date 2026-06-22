from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

from aiops_agent.audit.logger import FileAuditLogger
from aiops_agent.browser.agent import BrowserAgentTool
from aiops_agent.browser.skills import WebSkillMatcher, WebSkillStore
from aiops_agent.agent.summarizer import ResultSummarizer
from aiops_agent.browser.models import ActionResult, BrowserAction, BrowserObservation, BrowserTaskSpec, InteractiveElement
from aiops_agent.browser.planner import BrowserPlanner
from aiops_agent.browser.playwright_tool import PlaywrightBrowserTool
from aiops_agent.tasks.models import Task


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


def test_playwright_tool_extends_navigation_click_timeout(tmp_path):
    tool = PlaywrightBrowserTool(session_id="session", task_id="task", artifact_root=tmp_path)

    assert tool._click_timeout_ms(
        BrowserAction(type="click", target_hint="财司系统", expected_outcome="进入财司系统主页面", timeout_ms=5000)
    ) == 15000
    assert tool._click_timeout_ms(
        BrowserAction(type="click", target_hint="查询", expected_outcome="提交查询", timeout_ms=5000)
    ) == 5000


def test_browser_agent_uses_fixed_web_subgraph_nodes(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    nodes = set(tool.subgraph.graph.nodes)

    assert tool.subgraph.graph.name == "web_agent"
    assert tool.subgraph.graph.checkpointer is False
    assert {
        "prepare_spec",
        "load_web_memory",
        "restore_browser_context",
        "plan_action",
        "stabilize_action",
        "risk_gate",
        "execute_action",
        "observe_page",
        "reflect",
        "route_next",
        "skill_fallback",
        "finalize",
    }.issubset(nodes)


def test_browser_agent_advances_fixed_skill_after_stabilized_query(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    actions = [
        BrowserAction(type="click", target_hint="Query", key="skill.org-query"),
        BrowserAction(type="type", target_hint="userName", value="U0002865", key="skill.username"),
        BrowserAction(type="click", target_hint="Query", key="skill.user-query"),
        BrowserAction(type="extract_text", target_hint="已分配岗位列表", key="skill.extract"),
    ]
    steps = [
        {
            "action": {
                "type": "click",
                "target_hint": "查询",
                "target_id": "aiops-frame-2-el-3",
                "key": "stabilized.expected_command_click",
            },
            "result": "success",
        }
    ]

    assert tool._next_fixed_action(actions, steps) == actions[1]

    steps.extend(
        [
            {
                "action": {
                    "type": "type",
                    "target_hint": "用户名",
                    "target_id": "aiops-frame-2-el-2",
                    "value": "U0002865",
                    "key": "stabilized.pending_explicit_input",
                },
                "result": "success",
            },
            {
                "action": {
                    "type": "click",
                    "target_hint": "查询",
                    "target_id": "aiops-frame-2-el-5",
                    "key": "stabilized.expected_click",
                },
                "result": "success",
            },
        ]
    )

    assert tool._next_fixed_action(actions, steps) == actions[3]


def test_browser_agent_matches_localized_fixed_navigation_action(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    action = BrowserAction(type="click", target_hint="财司系统", key="skill.business-center")
    steps = [
        {
            "action": {
                "type": "click",
                "target_hint": "Business Center",
                "key": "stabilized.business-center",
            },
            "result": "success",
        }
    ]

    assert tool._next_fixed_action([action], steps) is None


def test_browser_agent_runtime_login_secret_action_drops_llm_unsafe_risk(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    proposed = BrowserAction(
        type="type_password",
        target_hint="Password",
        value="secret",
        risk_level="unsafe_mutation",
        requires_confirmation=True,
    )
    action = tool._runtime_action(proposed)
    spec = BrowserTaskSpec(start_url="http://example.test", user_goal="登录网站", requires_login=True)

    tool._prepare_runtime_action_for_risk(spec, proposed, action)

    assert action.type == "type"
    assert action.risk_level == "safe_local_edit"
    assert action.requires_confirmation is False
    assert proposed.requires_confirmation is False


def test_browser_agent_runtime_confirmation_dialog_opener_drops_llm_unsafe_risk(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    proposed = BrowserAction(
        type="click",
        target_hint="复核",
        expected_outcome="点击复核按钮，仅弹出复核确认对话框，不提交复核操作",
        risk_level="unsafe_mutation",
        requires_confirmation=True,
    )
    runtime_action = tool._runtime_action(proposed)
    spec = BrowserTaskSpec(start_url="http://example.test", user_goal="点击复核按钮并确认复核")

    tool._prepare_runtime_action_for_risk(spec, proposed, runtime_action)

    assert runtime_action.risk_level == "safe_local_edit"
    assert runtime_action.requires_confirmation is False
    assert proposed.risk_level == "safe_local_edit"
    assert proposed.requires_confirmation is False


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


class SkillFallbackFakeBrowser:
    close_calls = 0
    executed = []

    def __init__(self, *args, **kwargs):
        self.session_state_path = kwargs.get("session_state_path")
        self.current = BrowserObservation(url="http://example.test", title="Skill", page_type="form")

    def execute(self, action):
        SkillFallbackFakeBrowser.executed.append(action)
        if action.target_hint == "broken":
            return ActionResult("terminal_failure", self.current, error="broken skill action")
        return ActionResult("success", self.current)

    def observe(self, *, last_action_result="", force_artifact=False):
        self.current.last_action_result = last_action_result
        return self.current

    def save_session_state(self):
        return str(self.session_state_path) if self.session_state_path else None

    def close(self):
        SkillFallbackFakeBrowser.close_calls += 1


class FlakyRetryFakeBrowser:
    executed = []

    def __init__(self, *args, **kwargs):
        self.session_state_path = kwargs.get("session_state_path")
        self.current = BrowserObservation(url="", title="Retry", page_type="unknown")

    def execute(self, action):
        FlakyRetryFakeBrowser.executed.append(action)
        if action.type == "open_url" and len([item for item in FlakyRetryFakeBrowser.executed if item.type == "open_url"]) == 1:
            self.current = BrowserObservation(url=action.value or "", title="Retry", page_type="unknown")
            return ActionResult("retryable_failure", self.current, error="Timeout waiting for navigation")
        if action.type == "open_url":
            self.current = BrowserObservation(url=action.value or "", title="Retry", page_type="form")
            return ActionResult("success", self.current)
        if action.type == "finish":
            return ActionResult("success", self.current)
        return ActionResult("terminal_failure", self.current, error=f"unexpected {action.type}")

    def observe(self, *, last_action_result="", force_artifact=False):
        self.current.last_action_result = last_action_result
        return self.current

    def save_session_state(self):
        return str(self.session_state_path) if self.session_state_path else None

    def close(self):
        return None


class FallbackPlanner:
    def next_action(self, spec, observation, steps):
        return BrowserAction(type="finish", expected_outcome="fallback completed")


def test_web_subgraph_falls_back_from_failed_skill_to_planner(tmp_path, monkeypatch):
    SkillFallbackFakeBrowser.close_calls = 0
    SkillFallbackFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", SkillFallbackFakeBrowser)
    tool = BrowserAgentTool(
        audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"),
        artifact_root=tmp_path / "artifacts",
        planner=FallbackPlanner(),
    )

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "点击 broken",
            "auto_plan": False,
            "actions": [{"type": "click", "target_hint": "broken"}],
            "skill_name": "demo-broken-skill",
            "skill_fallback_to_llm_once": True,
            "max_steps": 4,
        }
    )

    assert result.success is True
    assert result.data["status"] == "completed"
    assert result.data["skill_fallback"]["skill_name"] == "demo-broken-skill"
    assert result.data["skill_fallback"]["llm_fallback_used"] is True
    assert any(action.target_hint == "broken" for action in SkillFallbackFakeBrowser.executed)
    assert any(action.type == "finish" for action in SkillFallbackFakeBrowser.executed)
    assert SkillFallbackFakeBrowser.close_calls >= 2


def test_web_subgraph_retries_transient_read_action_without_polluting_steps(tmp_path, monkeypatch):
    FlakyRetryFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", FlakyRetryFakeBrowser)
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "打开页面后结束",
            "auto_plan": False,
            "actions": [
                {"type": "open_url", "value": "http://example.test"},
                {"type": "finish", "expected_outcome": "done"},
            ],
            "max_steps": 4,
        }
    )

    assert result.success is True
    assert [action.type for action in FlakyRetryFakeBrowser.executed].count("open_url") == 2
    open_url_steps = [step for step in result.data["steps"] if step["action"]["type"] == "open_url"]
    assert len(open_url_steps) == 1
    assert open_url_steps[0]["reflection"]["retry_attempts"] == 1


def test_web_subgraph_checkpoints_do_not_store_login_secrets(tmp_path, monkeypatch):
    class _CredentialStore:
        def get(self, ref):
            return SimpleNamespace(username="alice-secret-user", password="super-secret-password")

    WorkflowFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", WorkflowFakeBrowser)
    tool = BrowserAgentTool(
        audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"),
        artifact_root=tmp_path / "artifacts",
        credential_store=_CredentialStore(),
        langgraph_checkpointer=InMemorySaver(),
    )

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "user_goal": "登录后结束",
            "requires_login": True,
            "credential_ref": "demo",
            "auto_plan": False,
            "actions": [{"type": "finish", "expected_outcome": "done"}],
            "max_steps": 2,
        }
    )

    checkpoints = [snapshot.values for snapshot in tool.get_state_history(result.data["web_thread_id"])]
    checkpoint_text = repr(checkpoints)
    assert "super-secret-password" not in checkpoint_text
    assert "alice-secret-user" not in checkpoint_text


def test_browser_planner_switches_credentials_only_for_a_new_login_cycle():
    planner = BrowserPlanner()
    spec = BrowserTaskSpec(
        start_url=None,
        user_goal="先新增用户，再切换账号复核",
        requires_login=True,
        credential_pairs=[("check-user", "check-password"), ("init-user", "init-password")],
    )
    login_observation = BrowserObservation(page_type="login")
    first_login_steps = [
        {"action": {"type": "observe_page"}, "result": "success"},
        {"action": {"type": "type_username"}, "result": "success"},
        {"action": {"type": "type_password"}, "result": "success"},
        {"action": {"type": "login_submit"}, "result": "success"},
        {"action": {"type": "wait_for"}, "result": "success"},
    ]

    assert planner._login_credentials(spec, first_login_steps) == ("check-user", "check-password")

    second_login_steps = first_login_steps + [
        {"action": {"type": "click", "target_hint": "退出登录"}, "result": "success"},
    ]
    action = planner.next_action(spec, login_observation, second_login_steps)

    assert action.type == "type_username"
    assert action.value == "init-user"


def test_browser_planner_rejects_false_login_page_away_from_configured_login_url():
    planner = BrowserPlanner()
    spec = BrowserTaskSpec(
        start_url="http://example.test/login",
        user_goal="进入网银用户管理并点击新增",
        requires_login=True,
        credential_pairs=[("check-user", "check-password"), ("init-user", "init-password")],
        site_config={"login_url": "http://example.test/login"},
    )
    observation = BrowserObservation(
        url="http://example.test/?workMode=business",
        page_type="login",
        page_text="网银用户管理 用户列表 新增",
    )

    assert planner._is_login_observation(spec, observation) is False


def test_browser_planner_waits_when_submitted_login_has_not_redirected():
    planner = BrowserPlanner()
    spec = BrowserTaskSpec(
        start_url="http://example.test/login",
        user_goal="登录后进入系统",
        requires_login=True,
        credential_pairs=[("user", "password")],
        site_config={"login_url": "http://example.test/login"},
    )
    steps = [
        {"action": {"type": "open_url"}, "result": "success"},
        {"action": {"type": "observe_page"}, "result": "success"},
        {"action": {"type": "type_username"}, "result": "success"},
        {"action": {"type": "type_password"}, "result": "success"},
        {"action": {"type": "login_submit"}, "result": "success"},
    ]

    action = planner.next_action(spec, BrowserObservation(url="http://example.test/login", page_type="login"), steps)

    assert action.type == "wait_for"
    assert action.key == "login.wait_for_redirect"


def test_browser_agent_enters_business_center_after_each_login(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(start_url="http://example.test/login", user_goal="登录后点击网上银行管理")
    steps = [
        {"action": {"type": "login_submit"}, "result": "success"},
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {
                "page_type": "interactive",
                "interactive_elements": [
                    {"element_id": "business", "role": "button", "text": "财司系统"},
                ],
            },
        },
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="网上银行管理"), steps)

    assert action.target_hint == "财司系统"
    assert action.key == "stabilized.business_center_after_login"


def test_browser_agent_marks_review_confirmation_as_mutation(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(start_url="http://example.test", user_goal="点击复核按钮并确认复核")
    steps = [
        {
            "action": {"type": "click", "target_hint": "复核"},
            "result": "success",
            "observation": {"page_type": "form", "interactive_elements": []},
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="确定"), steps)

    assert action.risk_level == "unsafe_mutation"
    assert action.expected_outcome == "确认提交复核操作"


def test_review_workflow_cannot_finish_without_confirmation(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(start_url="http://example.test", user_goal="点击复核按钮并确认复核")
    steps = [{"action": {"type": "click", "target_hint": "复核"}, "result": "success"}]

    error = tool._compound_workflow_completion_error(
        spec,
        BrowserObservation(url="http://example.test/review", page_type="form"),
        steps,
    )

    assert error == "复合网页任务未完成：尚未确认复核。"


def test_compound_web_workflow_cannot_finish_without_required_stages(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test/login",
        user_goal=(
            "使用check-admin登录系统，在用户名称中填入吕婧，在登录名称中填入lvjing_1228，"
            "点击保存。之后使用init-admin登录系统，点击复核"
        ),
        requires_login=True,
        credential_refs=["check-admin", "init-admin"],
        site_config={"login_url": "http://example.test/login"},
    )
    steps = [
        {"action": {"type": "login_submit"}, "result": "success"},
        {"action": {"type": "finish"}, "result": "success"},
    ]

    error = tool._compound_workflow_completion_error(
        spec,
        BrowserObservation(url="http://example.test/login", page_type="login"),
        steps,
    )

    assert error == "复合网页任务未完成：需要切换登录 2 次，实际完成 1 次。"


def test_compound_web_workflow_completion_contract_accepts_all_required_stages(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test/login",
        user_goal=(
            "使用check-admin登录系统，在用户名称中填入吕婧，在登录名称中填入lvjing_1228，"
            "所属单位编号中输入“101-230051_内部客户”，点击保存。之后使用init-admin登录系统，点击复核"
        ),
        requires_login=True,
        credential_refs=["check-admin", "init-admin"],
        site_config={"login_url": "http://example.test/login"},
    )
    steps = [
        {"action": {"type": "login_submit"}, "result": "success"},
        {"action": {"type": "type", "target_hint": "用户名称", "value": "吕婧"}, "result": "success"},
        {"action": {"type": "type", "target_hint": "登录名称", "value": "lvjing_1228"}, "result": "success"},
        {
            "action": {"type": "type", "target_hint": "所属单位编号", "value": "101-230051_内部客户"},
            "result": "success",
        },
        {"action": {"type": "click", "target_hint": "保存"}, "result": "success"},
        {"action": {"type": "login_submit"}, "result": "success"},
        {"action": {"type": "click", "target_hint": "复核"}, "result": "success"},
    ]

    error = tool._compound_workflow_completion_error(
        spec,
        BrowserObservation(url="http://example.test/review", page_type="interactive"),
        steps,
    )

    assert error is None


def test_browser_agent_parses_field_input_without_leading_preposition(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    goal = "在用户名称中填入吕婧，在登录名称中填入lvjing_1228，所属单位编号中输入“101-230051_内部客户”然后回车"

    assert tool._explicit_input_requests(goal) == [
        ("用户名称", "吕婧"),
        ("登录名称", "lvjing_1228"),
        ("所属单位编号", "101-230051_内部客户"),
    ]


def test_browser_agent_preserves_explicit_enter_between_type_and_save(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="所属单位编号中输入“101-230051_内部客户”然后回车，点击保存",
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "所属单位编号", "value": "101-230051_内部客户"},
            "result": "success",
            "observation": {"page_type": "form", "interactive_elements": []},
        }
    ]

    press = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="保存"), steps)

    assert press.type == "press"
    assert press.value == "Enter"
    assert press.key == "stabilized.pending_explicit_press"

    steps.append(
        {
            "action": {"type": "press", "target_hint": "所属单位编号", "value": "Enter"},
            "result": "success",
            "observation": {"page_type": "form", "interactive_elements": []},
        }
    )
    save = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="保存"), steps)

    assert save.type == "click"
    assert save.target_hint == "保存"


def test_browser_agent_detects_required_field_validation_after_mutation(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    reason = tool._page_validation_error_reason(
        {"type": "click", "target_hint": "保存", "risk_level": "unsafe_mutation"},
        BrowserObservation(page_text="用户新增 保存 该输入项为必输项"),
    )

    assert reason == "表单提交失败：页面仍存在未填写或未完成联动的必填项。"


def test_browser_agent_selects_all_review_rows_before_unrequested_action(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="进入网银用户复核，选中所有需要复核的数据，点击复核按钮并确认复核",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "网银用户复核"},
            "result": "success",
            "observation": {
                "page_type": "form",
                "page_text": "用户列表 显示1到2,共2记录",
                "interactive_elements": [
                    {
                        "element_id": "all-rows",
                        "role": "input",
                        "input_type": "checkbox",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ],
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="查询"), steps)

    assert action.target_hint == "全选复选框"
    assert action.target_id == "all-rows"
    assert action.key == "stabilized.review_select_all"


def test_empty_query_does_not_reuse_input_from_previous_login_stage(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在所属单位编号中输入101-230051_内部客户，之后进入网银用户复核",
    )
    steps = [
        {"action": {"type": "type", "target_hint": "所属单位编号", "value": "101-230051_内部客户"}, "result": "success"},
        {"action": {"type": "login_submit"}, "result": "success"},
        {"action": {"type": "click", "target_hint": "网银用户复核"}, "result": "success"},
        {"action": {"type": "click", "target_hint": "查询"}, "result": "success"},
    ]

    reason = tool._page_missing_information_reason(
        spec,
        steps[-1]["action"],
        BrowserObservation(page_text="显示0到0,共0记录"),
        steps,
    )

    assert reason is None


def test_web_subgraph_matches_skill_inside_browser_tool(tmp_path, monkeypatch):
    WorkflowFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", WorkflowFakeBrowser)
    store = WebSkillStore(tmp_path / "web_skills")
    store.write(
        name="demo-inline-skill",
        frontmatter={
            "name": "demo-inline-skill",
            "description": "inline browser skill",
            "compatibility": ["opsAgent web_action"],
        },
        body="inline skill",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "demo-inline-skill",
            "site_key": "demo",
            "inputs": [],
            "match": {"keywords": ["执行 skill"], "fields": [], "answer_types": []},
            "execution": {"auto_plan": False, "requires_login": False, "fallback_to_llm_once": True},
            "actions": [{"type": "finish", "expected_outcome": "done"}],
        },
        notes="notes",
    )
    tool = BrowserAgentTool(
        audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"),
        artifact_root=tmp_path / "artifacts",
        web_skill_matcher=WebSkillMatcher(store),
    )

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "执行 skill",
            "site_key": "demo",
            "auto_plan": True,
            "actions": [],
            "max_steps": 4,
        }
    )

    assert result.success is True
    assert result.data["skill_execution"]["skill_name"] == "demo-inline-skill"
    assert result.data["skill_execution"]["score"] >= 0.75
    assert WorkflowFakeBrowser.executed[0].type == "finish"


def test_web_subgraph_renders_explicit_skill_name_without_prebuilt_actions(tmp_path, monkeypatch):
    WorkflowFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", WorkflowFakeBrowser)
    store = WebSkillStore(tmp_path / "web_skills")
    store.write(
        name="demo-explicit-skill",
        frontmatter={
            "name": "demo-explicit-skill",
            "description": "explicit browser skill",
            "compatibility": ["opsAgent web_action"],
        },
        body="explicit skill",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "demo-explicit-skill",
            "site_key": "demo",
            "inputs": [{"name": "username", "required": True}],
            "match": {"keywords": ["unlikely keyword"], "fields": [], "answer_types": []},
            "execution": {"auto_plan": False, "requires_login": False, "fallback_to_llm_once": True},
            "actions": [
                {"type": "type", "target_hint": "用户名", "value": "{{username}}"},
                {"type": "finish", "expected_outcome": "done"},
            ],
        },
        notes="notes",
    )
    tool = BrowserAgentTool(
        audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"),
        artifact_root=tmp_path / "artifacts",
        web_skill_matcher=WebSkillMatcher(store),
    )

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "user_goal": "直接执行指定 skill",
            "site_key": "demo",
            "auto_plan": True,
            "actions": [],
            "skill_name": "demo-explicit-skill",
            "skill_parameters": {"username": "bob"},
            "max_steps": 4,
        }
    )

    assert result.success is True
    assert result.data["skill_execution"]["skill_name"] == "demo-explicit-skill"
    assert any(action.type == "type" and action.value == "bob" for action in WorkflowFakeBrowser.executed)


class EarlyStopFakeBrowser:
    mode = ""
    executed = []

    def __init__(self, *args, **kwargs):
        self.session_state_path = kwargs.get("session_state_path")
        self.current = BrowserObservation(url="", title="EarlyStop", page_type="form")

    def execute(self, action):
        EarlyStopFakeBrowser.executed.append(action)
        if action.type == "open_url":
            self.current = BrowserObservation(url=action.value or "", title="Portal", page_type="interactive")
            return ActionResult("success", self.current)
        if EarlyStopFakeBrowser.mode == "missing_menu" and action.type == "click":
            self.current = BrowserObservation(
                url=self.current.url,
                title="Portal",
                page_type="interactive",
                page_text="网上银行管理 权限管理",
            )
            return ActionResult(
                "retryable_failure",
                self.current,
                error='Locator.click: Timeout 5000ms exceeded waiting for get_by_text("网银岗位分配")',
            )
        if EarlyStopFakeBrowser.mode == "missing_company" and action.type == "click":
            self.current = BrowserObservation(
                url=self.current.url,
                title="Portal",
                page_type="form",
                page_text="授权单位 伊利财务有限公司 北京分公司",
            )
            return ActionResult(
                "retryable_failure",
                self.current,
                error='Locator.click: Timeout 5000ms exceeded waiting for get_by_text("内蒙古伊家好奶酪有限责任公司")',
            )
        if EarlyStopFakeBrowser.mode == "empty_search":
            if action.type == "type":
                self.current = BrowserObservation(url=self.current.url, title="Portal", page_type="form", page_text="用户名 查询")
                return ActionResult("success", self.current)
            if action.type == "click" and action.target_hint == "查询":
                self.current = BrowserObservation(
                    url=self.current.url,
                    title="Portal",
                    page_type="form",
                    page_text="用户编号 登录名称 用户名 10 20 50 100 200 显示0到0,共0记录",
                )
                return ActionResult("success", self.current)
        self.current = BrowserObservation(url=self.current.url, title="Portal", page_type="form")
        return ActionResult("success", self.current)

    def observe(self, *, last_action_result="", force_artifact=False):
        self.current.last_action_result = last_action_result
        if force_artifact:
            self.current.screenshot_path = "/tmp/early-stop-shot.png"
            self.current.page_summary_path = "/tmp/early-stop-summary.txt"
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
    assert "create_user.submit" not in [action.key for action in WorkflowFakeBrowser.executed]

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
    assert all("reflection" in step for step in result.data["steps"])
    assert all(step["reflection"]["next_decision"] == "continue" for step in result.data["steps"])
    trace = result.data["canonical_action_trace"]
    assert trace["schema_version"] == "opsagent.web_action_trace.v1"
    assert trace["status"] == "completed"
    assert trace["step_count"] == len(result.data["steps"])


def test_browser_agent_stops_early_when_menu_is_missing(tmp_path, monkeypatch):
    EarlyStopFakeBrowser.mode = "missing_menu"
    EarlyStopFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", EarlyStopFakeBrowser)
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "在左侧侧边栏依次点击网上银行管理，权限管理，网银岗位分配进入对应菜单",
            "auto_plan": False,
            "actions": [
                {"type": "open_url", "value": "http://example.test"},
                {"type": "click", "target_hint": "网银岗位分配"},
            ],
            "max_steps": 8,
        }
    )

    assert result.success is False
    assert result.data["status"] == "blocked"
    assert result.error == "系统中没有找到对应菜单：网银岗位分配。"
    assert result.data["steps"][-1]["reflection"]["failure_category"] == "system_missing_information"
    assert result.data["steps"][-1]["reflection"]["next_decision"] == "stop"
    assert len(EarlyStopFakeBrowser.executed) == 2


def test_browser_agent_stops_early_when_company_option_is_missing(tmp_path, monkeypatch):
    EarlyStopFakeBrowser.mode = "missing_company"
    EarlyStopFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", EarlyStopFakeBrowser)
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "在授权单位展开授权单位下拉列表，输入“内蒙古伊家好奶酪有限责任公司”，之后点击搜索到的第一个公司",
            "auto_plan": False,
            "actions": [
                {"type": "open_url", "value": "http://example.test"},
                {"type": "click", "target_hint": "内蒙古伊家好奶酪有限责任公司"},
            ],
            "max_steps": 8,
        }
    )

    assert result.success is False
    assert result.data["status"] == "blocked"
    assert result.error == "系统中没有找到对应公司：内蒙古伊家好奶酪有限责任公司。"
    assert result.data["steps"][-1]["reflection"]["failure_category"] == "system_missing_information"
    assert len(EarlyStopFakeBrowser.executed) == 2


def test_browser_agent_stops_early_when_search_returns_empty_result(tmp_path, monkeypatch):
    EarlyStopFakeBrowser.mode = "empty_search"
    EarlyStopFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", EarlyStopFakeBrowser)
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "在用户名中输入不存在的用户，然后点击下方的查询按钮进行查询，之后选中查询后的第一条数据",
            "auto_plan": False,
            "actions": [
                {"type": "open_url", "value": "http://example.test"},
                {"type": "type", "target_hint": "用户名", "value": "不存在的用户"},
                {"type": "click", "target_hint": "查询"},
            ],
            "max_steps": 8,
        }
    )

    assert result.success is False
    assert result.data["status"] == "blocked"
    assert result.error == "系统中没有找到符合条件的信息：用户名=不存在的用户。"
    assert result.data["steps"][-1]["reflection"]["terminal"] is True
    assert result.data["steps"][-1]["reflection"]["failure_category"] == "system_missing_information"
    assert len(EarlyStopFakeBrowser.executed) == 3


def test_browser_agent_blocks_before_executing_action_that_conflicts_with_user_intent(tmp_path, monkeypatch):
    EarlyStopFakeBrowser.mode = ""
    EarlyStopFakeBrowser.executed = []
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", EarlyStopFakeBrowser)
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    result = tool.execute(
        {
            "trace_id": "trace",
            "task_id": "task",
            "session_id": "session",
            "start_url": "http://example.test",
            "user_goal": "在客户名称中输入华北公司，然后点击应用按钮",
            "auto_plan": False,
            "actions": [
                {"type": "open_url", "value": "http://example.test"},
                {"type": "type", "target_hint": "客户名称", "value": "华北公司"},
                {"type": "click", "target_hint": "查询"},
            ],
            "max_steps": 8,
        }
    )

    assert result.success is False
    assert result.data["status"] == "blocked"
    assert "规划动作与用户意图不一致" in result.error
    assert "点击 应用" in result.error
    assert len(EarlyStopFakeBrowser.executed) == 2


def test_browser_agent_respects_explicit_click_after_field_input(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在客户名称中输入华北公司,然后点击应用按钮,告诉我客户对应的账户编号",
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "客户名称", "value": "华北公司"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "open-search-button",
                        "role": "button",
                        "text": "查询",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "apply-button",
                        "role": "button",
                        "text": "应用",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ]
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="查询", target_id="open-search-button"), steps)

    assert action.target_id == "apply-button"
    assert action.target_hint == "应用"


def test_browser_agent_corrects_click_after_dropdown_search_to_first_result(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“内蒙古伊家好奶酪有限责任公司”，"
            "之后点击搜索到的第一个公司，使授权单位处显示为内蒙古伊家好奶酪有限责任公司"
        ),
    )
    steps = [
        {
            "action": {
                "type": "type",
                "target_hint": "授权单位搜索输入框",
                "value": "内蒙古伊家好奶酪有限责任公司",
            },
            "result": "success",
            "observation": {"interactive_elements": []},
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="岗位分配", target_id="wrong"), steps)

    assert action.target_hint == "内蒙古伊家好奶酪有限责任公司"
    assert action.target_id is None


def test_browser_agent_treats_matching_dropdown_candidate_as_first_result(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "点击“授权单位”下拉框，在弹出的下拉搜索框中输入“101-51013200_内部客户”，"
            "等待候选项出现后，点击弹层中第一个匹配候选项“101-51013200_内部客户”，"
            "不要再次点击原来的授权单位选择框。"
        ),
    )
    steps = [
        {
            "action": {
                "type": "type",
                "target_hint": "授权单位搜索输入框",
                "value": org_name,
            },
            "result": "success",
            "observation": {"interactive_elements": []},
        }
    ]

    expected_click = tool._expected_click_after_last_type(spec.user_goal, steps[-1]["action"])
    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="查询"), steps)
    aligned, _ = tool._action_intent_alignment(spec, BrowserAction(type="click", target_hint=org_name), steps)

    assert expected_click is not None
    assert tool._means_first_search_result(expected_click) is True
    assert action.target_hint == org_name
    assert action.key == "stabilized.first_search_result"
    assert aligned is True


def test_browser_agent_clicks_typed_dropdown_value_after_observe_before_first_result(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "授权单位", "value": org_name},
            "result": "success",
            "observation": {"interactive_elements": []},
        },
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {
                "page_text": (
                    "授权单位 101-130017_内部客户 30 results are available, "
                    "use up and down arrow keys to navigate. 加载结果中..."
                ),
                "interactive_elements": [],
            },
        },
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="第一个"), steps)

    assert action.target_hint == org_name
    assert action.key == "stabilized.first_search_result"


def test_browser_agent_selects_dropdown_result_before_query_when_mask_is_open(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "授权单位", "value": org_name},
            "result": "success",
            "observation": {
                "page_text": (
                    "Authorized Agency 101-130017_内部客户 One result is available, "
                    "press enter to select it. 101-51013200_内部客户"
                ),
                "interactive_elements": [],
            },
        },
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Authorized Agency 101-130017_内部客户 One result is available, "
                    "press enter to select it. 101-51013200_内部客户"
                ),
                "interactive_elements": [],
            },
        },
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(
            type="click",
            target_hint="查询",
            expected_outcome="Click the 查询 button after selecting the authorized agency.",
            key="llm.step.14",
        ),
        steps,
    )

    assert action.type == "click"
    assert action.target_hint == org_name
    assert action.key == "stabilized.first_search_result"


def test_browser_agent_does_not_redirect_dropdown_option_click_while_one_result_is_open(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "授权单位", "value": org_name},
            "result": "success",
            "observation": {
                "page_text": (
                    "Authorized Agency 101-130017_内部客户 One result is available, "
                    "press enter to select it. 101-51013200_内部客户"
                ),
                "interactive_elements": [],
            },
        },
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint=org_name, key="llm.step.14"),
        steps,
    )

    assert action.target_hint == org_name
    assert action.key == "stabilized.first_search_result"


def test_browser_agent_uses_latest_dropdown_state_before_old_table_values(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "网银岗位分配"},
            "result": "success",
            "observation": {
                "page_text": "User No User Name U0002865 101-51013200_内部客户 Displaying 1 to 10 of 1158 items",
                "interactive_elements": [],
            },
        },
        {
            "action": {"type": "type", "target_hint": "授权单位搜索输入框", "value": org_name},
            "result": "success",
            "observation": {
                "page_text": (
                    "Authorized Agency 101-130017_内部客户 One result is available, "
                    "press enter to select it. 101-51013200_内部客户"
                ),
                "interactive_elements": [],
            },
        },
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint=org_name, key="llm.step.14"),
        steps,
    )

    assert action.target_hint == org_name
    assert action.key == "stabilized.first_search_result"


def test_browser_agent_treats_enter_after_dropdown_input_as_first_result_selection(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "授权单位", "value": org_name},
            "result": "success",
            "observation": {"interactive_elements": []},
        },
        {
            "action": {"type": "press", "target_hint": "授权单位下拉选项", "value": "Enter"},
            "result": "success",
            "observation": {
                "page_text": f"Authorized Agency {org_name} * Duty Assign Query",
                "interactive_elements": [],
            },
        },
    ]

    pending = tool._pending_click_after_explicit_type(spec, steps)

    assert pending is None


def test_browser_agent_clicks_query_after_enter_selected_dropdown_result(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮，在用户名中输入U0002865"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "授权单位搜索输入框", "value": org_name},
            "result": "success",
            "observation": {"page_text": f"Authorized Agency {org_name}", "interactive_elements": []},
        },
        {
            "action": {"type": "press", "target_hint": "授权单位下拉列表搜索框", "value": "Enter"},
            "result": "success",
            "observation": {
                "page_text": f"Authorized Agency {org_name} Duty Assign Query Assign Job",
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-3",
                        "role": "button",
                        "text": "Query",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-2",
                        "role": "a",
                        "text": "Duty Assign",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ],
            },
        },
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint="Duty Assign", target_id="aiops-frame-2-el-2"),
        steps,
    )

    assert action.target_hint == "Query"
    assert action.target_id == "aiops-frame-2-el-3"
    assert action.key == "stabilized.after_first_search_result"


def test_browser_agent_prefers_direct_button_text_over_neighbor_context(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    observation = BrowserObservation(
        page_text="Duty Assign Query Assign Job Run",
        interactive_elements=[
            InteractiveElement(
                element_id="aiops-frame-2-el-2",
                role="a",
                text="Duty Assign",
                context="Duty Assign Query Assign Job Run",
                is_enabled=True,
                is_visible=True,
            ),
            InteractiveElement(
                element_id="aiops-frame-2-el-3",
                role="button",
                text="Query",
                context="Query Assign Job Run",
                is_enabled=True,
                is_visible=True,
            ),
        ],
    )

    element = tool._find_element(observation, set(tool._click_label_aliases("查询")))

    assert element is not None
    assert element.element_id == "aiops-frame-2-el-3"


def test_browser_agent_prefers_filter_query_button_after_username_input(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮，"
            "在用户名中输入U0002865，然后点击下方的查询按钮进行查询"
        ),
    )
    steps = [
        {
            "action": {
                "type": "type",
                "target_hint": "userName",
                "target_id": "aiops-frame-2-el-2",
                "value": "U0002865",
            },
            "result": "success",
            "observation": {
                "page_text": (
                    "Login Name Subordinate units User Name User Flag All Query Cancel "
                    "Authorized Agency 101-51013200_内部客户 Duty Assign Query Assign Job Run"
                ),
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-5",
                        "role": "button",
                        "text": "Query",
                        "context": "Query",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-10",
                        "role": "button",
                        "text": "Query",
                        "context": "Query Assign Job Run",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint="Query", target_id="aiops-frame-2-el-10"),
        steps,
    )

    assert action.target_hint == "Query"
    assert action.target_id == "aiops-frame-2-el-5"
    assert action.key == "stabilized.expected_click"


def test_browser_agent_redirects_redundant_dropdown_value_click_to_next_query(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "授权单位", "value": org_name},
            "result": "success",
            "observation": {"interactive_elements": []},
        },
        {
            "action": {"type": "press", "target_hint": "授权单位下拉选项", "value": "Enter"},
            "result": "success",
            "observation": {
                "page_text": f"Authorized Agency {org_name} * Duty Assign Query",
                "interactive_elements": [],
            },
        },
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint=org_name), steps)

    assert action.target_hint == "查询"
    assert action.key == "stabilized.after_first_search_result"


def test_browser_agent_does_not_repeat_matching_dropdown_candidate_click(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    org_name = "101-51013200_内部客户"
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位下拉搜索框中输入“101-51013200_内部客户”，"
            "之后点击弹层中第一个匹配候选项“101-51013200_内部客户”"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "授权单位搜索输入框", "value": org_name},
            "result": "success",
            "observation": {"interactive_elements": []},
        },
        {
            "action": {"type": "click", "target_hint": org_name},
            "result": "success",
            "observation": {"interactive_elements": []},
        },
    ]

    pending = tool._pending_click_after_explicit_type(spec, steps)

    assert pending is None


def test_browser_agent_uses_nearest_click_after_repeated_typed_value(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    goal = (
        "在授权单位展开授权单位下拉列表，输入“内蒙古伊家好奶酪有限责任公司”，"
        "之后点击搜索到的第一个公司，使授权单位处显示为内蒙古伊家好奶酪有限责任公司，"
        "之后点击查询按钮"
    )

    expected = tool._expected_click_after_last_type(
        goal,
        {"type": "type", "target_hint": "授权单位搜索输入框", "value": "内蒙古伊家好奶酪有限责任公司"},
    )

    assert expected == "搜索到的第一个公司"


def test_browser_agent_cleans_positional_click_label_after_input(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    goal = "在用户名中输入张越，然后点击下方的查询按钮进行查询"

    expected = tool._expected_click_after_last_type(
        goal,
        {"type": "type", "target_hint": "用户名", "value": "张越"},
    )

    assert expected == "查询"


def test_browser_agent_corrects_llm_type_target_from_explicit_goal_and_page_elements(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在用户名称中输入张越，然后点击下方的查询按钮进行查询",
    )
    steps = [
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "user-name",
                        "role": "input",
                        "name": "用户名称",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "login-name",
                        "role": "input",
                        "name": "登录名称",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ]
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="type", target_hint="登录名称", target_id="login-name", value="张越"),
        steps,
    )

    assert action.target_id == "user-name"
    assert action.target_hint == "用户名称"


def test_browser_agent_normalizes_generic_input_box_hint_to_explicit_field(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在用户名中输入张越，然后点击下方的查询按钮进行查询",
    )
    steps = [
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {"interactive_elements": []},
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="type", target_hint="用户名输入框", value="张越"),
        steps,
    )

    assert action.target_hint == "用户名"
    assert action.target_id is None


def test_browser_agent_selects_first_result_row_before_assigning_role(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在用户名中输入张越，然后点击下方的查询按钮进行查询，"
            "之后选中查询后的第一条数据，点击分配岗位"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "查询"},
            "result": "success",
            "observation": {
                "page_text": (
                    "岗位分配 查询 分配岗位 用户编号 登录名称 用户名 所属单位 "
                    "U0003684 31602X_zhangyue 张越 内蒙古伊家好奶酪有限责任公司 "
                    "显示1到1,共1记录"
                ),
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="分配岗位"), steps)

    assert action.target_hint == "第一条数据"
    assert action.key == "stabilized.first_table_row"


def test_browser_agent_selects_first_result_row_instead_of_user_number_link(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在用户名中输入U0002865，然后点击下方的查询按钮进行查询，"
            "之后选中查询后的第一条数据，点击分配岗位"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "Query"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Duty Assign Query Assign Job User No Login Name User Name Subordinate units "
                    "U0002865 5101320003 U0002865 101-51013200_内部客户 启用 业务用户 "
                    "Displaying 1 to 1 of 1 items"
                ),
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(
            type="click",
            target_hint="U0002865",
            expected_outcome="选中第一条数据行，页面可能高亮或出现分配岗位按钮",
        ),
        steps,
    )

    assert action.target_hint == "第一条数据"
    assert action.key == "stabilized.first_table_row"


def test_browser_agent_selects_first_result_row_for_plain_user_number_click(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在用户名中输入U0002865，然后点击下方的查询按钮进行查询，"
            "之后选中查询后的第一条数据，点击分配岗位"
        ),
    )
    steps = [
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Duty Assign Query Assign Job User No Login Name User Name Subordinate units "
                    "U0002865 5101320003 U0002865 101-51013200_内部客户 启用 业务用户 "
                    "Displaying 1 to 1 of 1 items"
                ),
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint="U0002865", target_id="aiops-frame-2-el-2"),
        steps,
    )

    assert action.target_hint == "第一条数据"
    assert action.target_id is None
    assert action.key == "stabilized.first_table_row"


def test_browser_agent_clicks_pending_query_before_plain_user_number_row_selection(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在用户名中输入U0002865，然后点击下方的查询按钮进行查询，"
            "之后选中查询后的第一条数据，点击分配岗位"
        ),
    )
    steps = [
        {
            "action": {"type": "type", "target_hint": "userName", "value": "U0002865"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Login Name User Name Query Cancel Duty Assign Query Assign Job "
                    "User No Login Name User Name U0002865 5101320003 U0002865 "
                    "Displaying 1 to 10 of 1158 items"
                ),
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-5",
                        "role": "button",
                        "text": "Query",
                        "context": "Query",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint="U0002865", target_id="aiops-frame-2-el-2"),
        steps,
    )

    assert action.target_hint == "Query"
    assert action.target_id == "aiops-frame-2-el-5"
    assert action.key == "stabilized.expected_click"


def test_browser_agent_does_not_repeat_first_result_row_selection(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="选中查询后的第一条数据，点击分配岗位",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "第一条数据", "key": "stabilized.first_table_row"},
            "result": "success",
            "observation": {
                "page_text": "用户编号 登录名称 用户名 U0003684 31602X_zhangyue 张越 显示1到1,共1记录",
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="分配岗位"), steps)

    assert action.target_hint == "分配岗位"


def test_browser_agent_does_not_treat_query_result_as_query_command(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    action = BrowserAction(
        type="click",
        target_hint="第一条数据",
        expected_outcome="按用户指令先选中查询结果中的第一条数据",
        key="stabilized.first_table_row",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "查询"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-3",
                        "role": "button",
                        "text": "查询",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ]
            },
        }
    ]

    assert tool._correct_command_click_from_expected_outcome(action, steps) is None

    result_action = BrowserAction(
        type="click",
        target_hint="U0002865",
        expected_outcome="点击查询结果中的U0002865",
    )
    assert tool._correct_command_click_from_expected_outcome(result_action, steps) is None

    navigation_action = BrowserAction(
        type="click",
        target_hint="网银用户管理",
        expected_outcome="进入网银用户管理页面，加载iframe和查询按钮",
    )
    assert tool._correct_command_click_from_expected_outcome(navigation_action, steps) is None


def test_browser_agent_detects_repeated_stabilized_query_aliases(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    steps = [
        {"action": {"type": "click", "target_hint": "Query", "target_id": "aiops-frame-2-el-1"}},
        {"action": {"type": "click", "target_hint": "Search", "target_id": "aiops-frame-2-el-4"}},
    ]

    assert tool._is_repeated_action(BrowserAction(type="click", target_hint="查询"), steps, threshold=2) is True


def test_browser_agent_parses_rendered_skill_goal_inputs_without_following_actions(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    goal = (
        "在授权单位展开下拉列表，输入101-51013200_内部客户，点击第一个匹配项；"
        "在用户名中输入U0002865，点击查询；选中查询结果中的第一条数据"
    )

    assert tool._explicit_input_requests(goal) == [
        ("授权单位", "101-51013200_内部客户"),
        ("用户名", "U0002865"),
    ]


def test_browser_agent_enforces_explicit_navigation_order(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "依次点击网上银行管理，权限管理，网银账户权限设置进入对应菜单，"
            "等待页面加载完成，然后点击查询按钮"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "网上银行管理"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "aiops-el-14",
                        "role": "a",
                        "text": "用户信息管理",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-el-17",
                        "role": "a",
                        "text": "权限管理",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ]
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="用户信息管理"), steps)

    assert action.target_hint == "权限管理"
    assert action.target_id == "aiops-el-17"
    assert action.key == "stabilized.explicit_navigation"


def test_browser_agent_clicks_query_immediately_after_explicit_navigation(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "依次点击网上银行管理，权限管理，网银账户权限设置进入对应菜单，"
            "等待页面加载完成，然后点击查询按钮，在用户名称中输入U0002865"
        ),
    )
    steps = [
        {"action": {"type": "click", "target_hint": "网上银行管理"}, "result": "success"},
        {"action": {"type": "click", "target_hint": "权限管理"}, "result": "success"},
        {
            "action": {"type": "click", "target_hint": "网银账户权限设置"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-1",
                        "role": "input",
                        "input_type": "button",
                        "text": "查 询",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ]
            },
        },
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="用户信息管理"), steps)

    assert action.target_hint == "查 询"
    assert action.key == "stabilized.after_explicit_navigation"


def test_browser_agent_waits_when_post_navigation_button_is_not_ready(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "依次点击网上银行管理，权限管理，网银账户权限设置进入对应菜单，"
            "等待页面加载完成，然后点击查询按钮"
        ),
    )
    steps = [
        {"action": {"type": "click", "target_hint": "网上银行管理"}, "result": "success"},
        {"action": {"type": "click", "target_hint": "权限管理"}, "result": "success"},
        {
            "action": {"type": "click", "target_hint": "网银账户权限设置"},
            "result": "success",
            "observation": {"page_text": "网银账户权限设置 页面加载中"},
        },
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="用户信息管理"), steps)

    assert action.type == "wait_for"
    assert action.key == "stabilized.wait_after_explicit_navigation"


def test_browser_agent_enforces_explicit_result_click_sequence(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="点击查询结果中的U0002865，之后点击已分配账户，告诉我账户信息",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "确定"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-3",
                        "role": "a",
                        "text": "U0002865",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ]
            },
        }
    ]

    result_action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="查询"), steps)
    assert result_action.target_hint == "U0002865"
    assert result_action.key == "stabilized.explicit_result_sequence"

    steps.append(
        {
            "action": {"type": "click", "target_hint": "U0002865"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-3-el-2",
                        "role": "a",
                        "text": "Assigned Account",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ]
            },
        }
    )

    assigned_action = tool._stabilize_action(spec, BrowserAction(type="extract_text"), steps)
    assert assigned_action.target_hint == "Assigned Account"
    assert assigned_action.key == "stabilized.explicit_result_sequence"


def test_browser_agent_finishes_with_assigned_account_membership_after_tab_click(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="点击查询结果中的U0002865，之后点击已分配账户，告诉我1011051015101是否在该用户的已分配账户中",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "U0002865"},
            "result": "success",
            "observation": {"page_text": "User U0002865"},
        },
        {
            "action": {"type": "click", "target_hint": "Assigned Account"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Distributive Account Assigned Account Account No Account Name "
                    "1011051015101 内部户 1011051013201 其他户 "
                    "Page of 1 Displaying 1 to 2 of 2 items"
                )
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="用户信息管理"), steps)

    assert action.type == "finish"
    assert action.value == "1011051015101在该用户的已分配账户中。"
    assert action.key == "stabilized.membership_answer"


def test_browser_agent_enforces_repeated_query_and_multi_field_filter_before_membership(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    goal = (
        "点击查询结果中的U0002865，之后点击已分配账户，点击查询，"
        "将1011051015101输入到“账户编号由”和”账户编号至“中，然后点击确定，"
        "告诉我1011051015101是否在该用户的已分配账户中"
    )
    spec = BrowserTaskSpec(start_url="http://example.test", user_goal=goal)
    loaded_table = (
        "Assigned Account Account No Account Name 1011051015101 内部户 "
        "Page of 1 Displaying 1 to 1 of 1 items"
    )
    steps = [
        {"action": {"type": "click", "target_hint": "Query"}, "result": "success"},
        {"action": {"type": "click", "target_hint": "U0002865"}, "result": "success"},
        {
            "action": {"type": "click", "target_hint": "Assigned Account"},
            "result": "success",
            "observation": {
                "page_text": loaded_table,
                "interactive_elements": [
                    {
                        "element_id": "query-assigned",
                        "role": "input",
                        "input_type": "button",
                        "name": "butQuery",
                        "text": "Query",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ],
            },
        },
    ]

    query_action = tool._stabilize_action(
        spec,
        BrowserAction(type="type", target_hint="账户编号由", value="1011051015101"),
        steps,
    )

    assert query_action.type == "click"
    assert query_action.target_id == "query-assigned"
    assert query_action.key == "stabilized.explicit_result_sequence"
    assert tool._answer_from_observation(spec, BrowserObservation(page_text=loaded_table), steps) == {}

    steps.append(
        {
            "action": {"type": "click", "target_hint": "Query", "target_id": "query-assigned"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "account-from",
                        "role": "input",
                        "input_type": "text",
                        "name": "startAccountNo",
                        "context": "",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "account-to",
                        "role": "input",
                        "input_type": "text",
                        "name": "endAccountNo",
                        "context": "",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ]
            },
        }
    )
    first_input = tool._stabilize_action(spec, BrowserAction(type="observe_page"), steps)
    assert first_input.type == "type"
    assert first_input.target_id == "account-from"

    steps.append(
        {
            "action": {"type": "type", "target_hint": "账户编号由", "value": "1011051015101"},
            "result": "success",
            "observation": steps[-1]["observation"],
        }
    )
    second_input = tool._stabilize_action(
        spec,
        BrowserAction(
            type="type",
            target_hint="startAccountNo",
            target_id="account-from",
            value="1011051015101",
        ),
        steps,
    )
    assert second_input.type == "type"
    assert second_input.target_id == "account-to"
    assert second_input.key == "stabilized.pending_explicit_input"
    direct_type_correction = tool._stabilize_type_action(
        spec,
        BrowserAction(
            type="type",
            target_hint="startAccountNo",
            target_id="account-from",
            value="1011051015101",
        ),
        steps,
    )
    assert direct_type_correction.target_id == "account-to"
    aligned, reason = tool._action_intent_alignment(
        spec,
        BrowserAction(
            type="type",
            target_hint="endAccountNo",
            target_id="account-to",
            value="1011051015101",
            key="llm.next-input",
        ),
        steps,
    )
    assert aligned is True, reason

    steps.append(
        {
            "action": {"type": "type", "target_hint": "账户编号至", "value": "1011051015101"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "confirm-filter",
                        "role": "input",
                        "input_type": "button",
                        "text": "Search",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ]
            },
        }
    )
    confirm_action = tool._stabilize_action(spec, BrowserAction(type="observe_page"), steps)
    assert confirm_action.type == "click"
    assert confirm_action.target_id == "confirm-filter"
    assert confirm_action.key == "stabilized.pending_expected_click"


def test_browser_agent_recognizes_value_first_multi_field_input_syntax(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")

    requests = tool._explicit_input_requests(
        "将1011051015101输入到“账户编号由”和”账户编号至“中，然后点击确定"
    )

    assert requests == [("账户编号由", "1011051015101"), ("账户编号至", "1011051015101")]


def test_browser_agent_does_not_apply_business_field_contract_on_login_page(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test/login",
        user_goal="在用户名称中输入U0002865，然后点击确定",
        requires_login=True,
    )
    steps = [
        {
            "action": {"type": "open_url"},
            "result": "success",
            "observation": {
                "page_type": "login",
                "interactive_elements": [
                    {
                        "element_id": "login-user",
                        "role": "input",
                        "input_type": "text",
                        "name": "username",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ],
            },
        }
    ]
    planned = BrowserAction(type="observe_page")

    assert tool._stabilize_action(spec, planned, steps) is planned
    login_type_steps = [
        {
            "action": {"type": "type", "target_hint": "username", "value": "U0002865"},
            "result": "success",
            "observation": {"page_type": "login"},
        }
    ]
    assert tool._explicit_input_already_done("用户名称", "U0002865", login_type_steps) is False


def test_browser_agent_waits_for_first_business_target_after_login_redirect(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(start_url="http://example.test/login", user_goal="登录后点击财司系统")
    steps = [
        {
            "action": {"type": "login_submit"},
            "result": "success",
            "observation": {"url": "http://example.test/portal/skip.jsp", "page_type": "interactive"},
        }
    ]
    action = BrowserAction(type="click", target_hint="财司系统")

    waiting = tool._stabilize_action(spec, action, steps)

    assert waiting.type == "wait_for"
    assert waiting.key == "stabilized.wait_after_login"

    steps.append(
        {
            "action": {"type": "wait_for", "key": "stabilized.wait_after_login"},
            "result": "success",
            "observation": {
                "page_type": "interactive",
                "interactive_elements": [
                    {
                        "element_id": "business-center",
                        "role": "a",
                        "text": "Business Center",
                        "is_enabled": True,
                        "is_visible": True,
                    }
                ],
            },
        }
    )

    assert tool._stabilize_action(spec, action, steps) is action


def test_browser_agent_matches_query_button_text_separately_from_control_name(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    observation = BrowserObservation(
        interactive_elements=[
            InteractiveElement(
                element_id="query-button",
                role="input",
                input_type="button",
                name="butQuery",
                text="Query",
            )
        ]
    )

    assert tool._find_query_button(observation).element_id == "query-button"


def test_browser_agent_does_not_answer_assigned_account_membership_before_tab_click(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="告诉我1011051015101是否在该用户的已分配账户中",
    )
    observation = BrowserObservation(
        page_text=(
            "Distributive Account Assigned Account Account No Account Name "
            "1011051015101 内部户 Page of 1 Displaying 1 to 1 of 1 items"
        )
    )

    assert tool._answer_from_observation(spec, observation, []) == {}


def test_browser_agent_answers_value_absent_from_loaded_assigned_account_table(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="告诉我1011051015101是否在该用户的已分配账户中",
    )
    observation = BrowserObservation(
        page_text=(
            "Distributive Account Assigned Account Account No Account Name "
            "1011051015201 内部户 Page of 1 Displaying 1 to 1 of 1 items"
        )
    )
    steps = [{"action": {"type": "click", "target_hint": "Assigned Account"}, "result": "success"}]

    assert tool._answer_from_observation(spec, observation, steps) == {
        "answer": "1011051015101不在该用户的已分配账户中。",
        "target": "1011051015101",
        "present": False,
        "list": "已分配账户",
    }


def test_browser_agent_corrects_duty_assign_tab_to_assign_job_button(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="选中查询后的第一条数据，点击分配岗位",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "第一条数据", "key": "stabilized.first_table_row"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Duty Assign Query Assign Job User No Login Name User Name "
                    "U0002865 5101320003 U0002865 Displaying 1 to 1 of 1 items"
                ),
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-2",
                        "role": "a",
                        "text": "Duty Assign",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-4",
                        "role": "button",
                        "text": "Assign Job",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(
            type="click",
            target_hint="Duty Assign",
            expected_outcome="Click the '分配岗位' button to open the assignment dialog/modal.",
        ),
        steps,
    )

    assert action.target_hint == "Assign Job"
    assert action.target_id == "aiops-frame-2-el-4"
    assert action.key == "stabilized.expected_command_click"


def test_browser_agent_corrects_assigned_role_tab_to_assigned_position(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="点击弹出内容中的已分配岗位",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "Assign Job"},
            "result": "success",
            "observation": {
                "page_text": "Distributive Duty Assigned position Duty List Assign duty Name",
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-3-el-4",
                        "role": "a",
                        "text": "Assigned position",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(
            type="click",
            target_hint="已分配岗位",
            expected_outcome="点击弹出内容中的已分配岗位",
        ),
        steps,
    )

    assert action.target_hint == "Assigned position"
    assert action.target_id == "aiops-frame-3-el-4"
    assert action.key == "stabilized.expected_command_click"


def test_browser_agent_infers_dropdown_field_for_explicit_value_from_goal(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在授权单位展开授权单位下拉列表，输入“内蒙古伊家好奶酪有限责任公司”，之后点击搜索到的第一个公司",
    )

    field = tool._explicit_input_field_for_value(spec.user_goal, "内蒙古伊家好奶酪有限责任公司")

    assert field == "授权单位"


def test_browser_agent_does_not_treat_sidebar_navigation_as_input_field(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    goal = (
        "使用ifinance-check-admin登录ifinance，在左侧侧边栏依次点击网上银行管理，权限管理，"
        "网银岗位分配进入对应菜单，等待页面加载完成，然后在授权单位展开授权单位下拉列表，"
        "输入“101-51013200_内部客户”，之后点击下方高亮的第一个内容，之后点击查询按钮，"
        "在用户名中输入U0002865"
    )

    requests = tool._explicit_input_requests(goal)

    assert ("授权单位", "101-51013200_内部客户") in requests
    assert ("用户名", "U0002865") in requests
    assert all(field != "左侧侧边栏依次" for field, _ in requests)


def test_browser_agent_corrects_username_target_id_when_hint_is_right_but_id_is_login_name(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在用户名中输入U0002865，然后点击下方的查询按钮进行查询",
    )
    steps = [
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-0",
                        "role": "input",
                        "input_type": "text",
                        "name": "loginName",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-2",
                        "role": "input",
                        "input_type": "text",
                        "name": "userName",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ]
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="type", target_hint="用户名", target_id="aiops-frame-2-el-0", value="U0002865"),
        steps,
    )

    assert action.target_id == "aiops-frame-2-el-2"
    assert action.target_hint == "userName"
    assert action.key == "stabilized.explicit_input"


def test_browser_agent_types_dropdown_value_after_selected_item_click(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "授权单位下拉列表"},
            "result": "success",
            "observation": {
                "page_text": (
                    "授权单位 101-130017_内部客户 30 results are available, "
                    "use up and down arrow keys to navigate. 加载结果中..."
                ),
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(
            type="click",
            target_hint="授权单位下拉列表当前选中项",
            expected_outcome="下拉列表搜索框获得焦点，可以输入文本",
        ),
        steps,
    )

    assert action.type == "type"
    assert action.target_hint == "授权单位搜索输入框"
    assert action.value == "101-51013200_内部客户"
    assert action.key == "stabilized.pending_dropdown_input"


def test_browser_agent_types_dropdown_value_when_llm_reclicks_authorized_agency_dropdown(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "Authorized Agency"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Authorized Agency 101-130017_内部客户 30 results are available, "
                    "use up and down arrow keys to navigate. Loading more results..."
                ),
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(
            type="click",
            target_hint="Authorized Agency dropdown",
            target_id="aiops-frame-2-el-0",
            expected_outcome="The authorized agency dropdown opens.",
        ),
        steps,
    )

    assert action.type == "type"
    assert action.target_hint == "授权单位搜索输入框"
    assert action.target_id is None
    assert action.value == "101-51013200_内部客户"
    assert action.key == "stabilized.pending_dropdown_input"


def test_browser_agent_types_dropdown_value_when_open_dropdown_is_searching(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "授权单位下拉列表"},
            "result": "success",
            "observation": {
                "page_text": "Authorized Agency 101-130017_内部客户 Duty Assign Query Searching... Searching...",
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint="授权单位下拉列表"),
        steps,
    )

    assert action.type == "type"
    assert action.target_hint == "授权单位搜索输入框"
    assert action.value == "101-51013200_内部客户"
    assert action.key == "stabilized.pending_dropdown_input"


def test_browser_agent_types_dropdown_value_when_open_dropdown_is_searching_in_chinese(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "授权单位"},
            "result": "success",
            "observation": {
                "page_text": "授权单位 101-130017_内部客户 岗位分配 查询 搜索中... 搜索中...",
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint="授权单位下拉列表", target_id="aiops-frame-2-el-0"),
        steps,
    )

    assert action.type == "type"
    assert action.target_hint == "授权单位搜索输入框"
    assert action.value == "101-51013200_内部客户"
    assert action.key == "stabilized.pending_dropdown_input"


def test_browser_agent_types_pending_dropdown_value_before_later_username(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”，"
            "之后点击下方高亮的第一个内容，之后点击查询按钮，在用户名中输入U0002865"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "授权单位下拉列表"},
            "result": "success",
            "observation": {
                "page_text": (
                    "Authorized Agency 101-130017_内部客户 30 results are available, "
                    "use up and down arrow keys to navigate. Loading more results..."
                ),
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="type", target_hint="用户名", value="U0002865"),
        steps,
    )

    assert action.type == "type"
    assert action.target_hint == "授权单位搜索输入框"
    assert action.value == "101-51013200_内部客户"
    assert action.key == "stabilized.pending_dropdown_input"


def test_browser_agent_does_not_type_dropdown_value_before_dropdown_is_open(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "网银岗位分配"},
            "result": "success",
            "observation": {
                "page_text": "授权单位 101-130017_内部客户 岗位分配 查询",
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="click", target_hint="授权单位下拉列表"),
        steps,
    )

    assert action.type == "click"
    assert action.target_hint == "授权单位下拉列表"


def test_browser_agent_opens_dropdown_before_typing_when_dropdown_is_closed(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在授权单位展开授权单位下拉列表，输入“101-51013200_内部客户”",
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "Authorized Agency"},
            "result": "success",
            "observation": {
                "page_text": "Authorized Agency 101-130017_内部客户 * Duty Assign Query",
                "interactive_elements": [],
            },
        }
    ]

    action = tool._stabilize_action(
        spec,
        BrowserAction(type="type", target_hint="授权单位", value="101-51013200_内部客户"),
        steps,
    )

    assert action.type == "click"
    assert action.target_hint == "授权单位下拉列表"
    assert action.key == "stabilized.open_dropdown_before_type"


def test_browser_agent_stabilizes_click_to_pending_explicit_input(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "查询用户pen_test2的信息：用ifinance-check-admin登录ifinance,"
            "在左侧侧边栏依次点击网上银行管理，用户信息管理，网银用户管理进入对应菜单,"
            "在弹出的内容中找到用户名称,在用户名称中输入pen_test2,然后点击确定按钮"
        ),
    )
    steps = [
        {
            "action": {"type": "click", "target_hint": "Query"},
            "result": "success",
            "observation": {
                "page_text": "User No: User Name: Search Cancel",
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-0",
                        "role": "input",
                        "input_type": "text",
                        "name": "userNo",
                        "text": "",
                        "title": "",
                        "href": "",
                        "placeholder": "",
                        "context": "",
                        "locator_strategy": "data-aiops-id",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-1",
                        "role": "input",
                        "input_type": "text",
                        "name": "userName",
                        "text": "",
                        "title": "",
                        "href": "",
                        "placeholder": "",
                        "context": "",
                        "locator_strategy": "data-aiops-id",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ],
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="click", target_hint="查询"), steps)

    assert action.type == "type"
    assert action.target_id == "aiops-frame-2-el-1"
    assert action.value == "pen_test2"
    assert action.key == "stabilized.pending_explicit_input"


def test_browser_agent_stabilizes_extract_to_expected_click_after_type(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "查询用户pen_test2的信息：用ifinance-check-admin登录ifinance,"
            "在弹出的内容中找到用户名称,在用户名称中输入pen_test2,然后点击确定按钮"
        ),
    )
    steps = [
        {
            "action": {
                "type": "type",
                "target_hint": "userName",
                "target_id": "aiops-frame-2-el-1",
                "value": "pen_test2",
                "expected_outcome": "按用户指令在字段 用户名称 中输入 pen_test2",
            },
            "result": "success",
            "observation": {
                "page_text": "用户编号: 用户名称: 确定 取消 用户列表",
                "interactive_elements": [
                    {
                        "element_id": "aiops-frame-2-el-1",
                        "role": "input",
                        "input_type": "text",
                        "name": "userName",
                        "text": "",
                        "title": "",
                        "href": "",
                        "placeholder": "",
                        "context": "",
                        "locator_strategy": "data-aiops-id",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-8",
                        "role": "button",
                        "input_type": "",
                        "name": "",
                        "text": "确定",
                        "title": "",
                        "href": "",
                        "placeholder": "",
                        "context": "确定 取消",
                        "locator_strategy": "data-aiops-id",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ],
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="extract_text", target_hint="用户列表"), steps)

    assert action.type == "click"
    assert action.target_id == "aiops-frame-2-el-8"
    assert action.target_hint == "确定"
    assert action.key == "stabilized.pending_expected_click"

    expected_click = tool._expected_click_after_last_type(
        spec.user_goal,
        {"type": "type", "target_hint": "userName", "value": "pen_test2"},
    )
    assert expected_click == "确定"


def test_browser_agent_expected_click_prefers_clickable_search_alias(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在用户名称中输入pen_test2,然后点击确定按钮",
    )
    steps = [
        {
            "action": {
                "type": "type",
                "target_hint": "userName",
                "target_id": "aiops-frame-2-el-1",
                "value": "pen_test2",
                "expected_outcome": "按用户指令在字段 用户名称 中输入 pen_test2",
            },
            "result": "success",
            "observation": {
                "page_text": "User Name Search Cancel",
                "interactive_elements": [
                    {
                        "element_id": "aiops-el-4",
                        "role": "input",
                        "input_type": "text",
                        "name": "searchInput",
                        "text": "",
                        "title": "",
                        "href": "",
                        "placeholder": "",
                        "context": "",
                        "locator_strategy": "data-aiops-id",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-1",
                        "role": "input",
                        "input_type": "text",
                        "name": "userName",
                        "text": "",
                        "title": "",
                        "href": "",
                        "placeholder": "",
                        "context": "",
                        "locator_strategy": "data-aiops-id",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                    {
                        "element_id": "aiops-frame-2-el-8",
                        "role": "button",
                        "input_type": "",
                        "name": "",
                        "text": "Search",
                        "title": "",
                        "href": "",
                        "placeholder": "",
                        "context": "Search Cancel",
                        "locator_strategy": "data-aiops-id",
                        "is_enabled": True,
                        "is_visible": True,
                    },
                ],
            },
        }
    ]

    action = tool._stabilize_action(spec, BrowserAction(type="extract_text", target_hint="用户列表"), steps)

    assert action.type == "click"
    assert action.target_id == "aiops-frame-2-el-8"
    assert action.target_hint == "Search"
    assert action.key == "stabilized.pending_expected_click"
    assert not tool._click_target_matches_label("searchInput aiops-el-4", "确定")

    aligned, _ = tool._action_intent_alignment(spec, BrowserAction(type="click", target_hint="Search"), steps)
    assert aligned is True
    aligned, reason = tool._action_intent_alignment(spec, BrowserAction(type="click", target_hint="searchInput"), steps)
    assert aligned is False
    assert "用户要求下一步点击 确定" in reason


def test_browser_agent_extracts_login_name_answer_and_summarizer_prints_it(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在用户名称中输入高斌,然后点击确定按钮,告诉我用户对应的登录名称",
    )
    observation = BrowserObservation(
        page_text=(
            "用户列表 用户编号 用户名称 登录名称 所属单位 "
            "U00005582 网银调试-高斌 gaobin 内蒙古伊利实业集团股份有限公司 "
            "U00003668 高斌 lilei1 伊利财务有限公司"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer["query_value"] == "高斌"
    assert answer["matches"][0]["output_value"] == "gaobin"
    assert answer["matches"][1]["output_value"] == "lilei1"
    assert answer["answer"].startswith("高斌 对应的登录名称是 lilei1")

    task = Task(trace_id="trace", input=spec.user_goal, intent="web_action", status="success")
    report = ResultSummarizer().summarize(task, {"data": {"answer": answer}})

    assert report == answer["answer"]


def test_browser_agent_extracts_full_user_info_for_broad_info_request(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "查询用户pen_test2的信息：用ifinance-check-admin登录ifinance,"
            "在用户名称中输入pen_test2,然后点击确定按钮,告诉我用户对应的信息"
        ),
    )
    observation = BrowserObservation(
        page_text=(
            "网银用户管理 用户编号 用户名称 登录名称 所属单位 岗位信息 录入人 录入日期 "
            "复核人 复核日期 活动状态 复核状态 "
            "U0003085 pen_test2 pen_test2 101-51011000_内部客户 无 U0000003 2026-06-12 "
            "U0000004 2026-06-16 活动中 已复核 显示1到1,共1记录"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer["answer"] == (
        "pen_test2的信息：用户编号U0003085，用户名称pen_test2，登录名称pen_test2，"
        "所属单位101-51011000_内部客户，岗位信息无，录入人U0000003，录入日期2026-06-12，"
        "复核人U0000004，复核日期2026-06-16，活动状态活动中，复核状态已复核。"
    )
    assert answer["answer_type"] == "table_detail"
    assert "对应的信息是 pen_test2" not in answer["answer"]

    task = Task(trace_id="trace", input=spec.user_goal, intent="web_action", status="success")
    report = ResultSummarizer().summarize(task, {"data": {"answer": answer}})

    assert report == answer["answer"]


def test_browser_agent_extracts_user_info_from_english_table_headers(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal=(
            "查询用户pen_test2的信息：用ifinance-check-admin登录ifinance,"
            "在用户名称中输入pen_test2,然后点击确定按钮,告诉我用户对应的信息"
        ),
    )
    observation = BrowserObservation(
        page_text=(
            "User List User No User Name Login Name Subordinate Units Duty Name Input User Name "
            "Modify Name Check User Name Check Time Status Check State "
            "U0003085 pen_test2 pen_test2 101-51011000_内部客户 U0000003 2026-06-12 "
            "U0000004 2026-06-12 Activity in Has been reviewed "
            "10 20 50 100 200 Page of 1 Displaying 1 to 1 of 1 items"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer["answer"] == (
        "pen_test2的信息：用户编号U0003085，用户名称pen_test2，登录名称pen_test2，"
        "所属单位101-51011000_内部客户，录入人U0000003，录入日期2026-06-12，"
        "复核人U0000004，复核日期2026-06-12，活动状态活动中，复核状态已复核。"
    )
    assert answer["rows"] == [
        {
            "User No": "U0003085",
            "User Name": "pen_test2",
            "Login Name": "pen_test2",
            "Subordinate Units": "101-51011000_内部客户",
            "Duty Name": "",
            "Input User Name": "U0000003",
            "Modify Name": "2026-06-12",
            "Check User Name": "U0000004",
            "Check Time": "2026-06-12",
            "Status": "Activity in",
            "Check State": "Has been reviewed",
        }
    ]


def test_browser_agent_extracts_generic_table_detail_for_broad_info_request(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在账户名称中输入基本户,然后点击确定按钮,告诉我账户对应的信息",
    )
    observation = BrowserObservation(
        page_text=(
            "账户管理 账户编号 账户名称 开户行 状态 更新日期 "
            "AC001 基本户 招商银行 启用 2026-06-16 显示1到1,共1记录"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer["answer"] == "基本户的信息：账户编号AC001，账户名称基本户，开户行招商银行，状态启用，更新日期2026-06-16。"
    assert answer["rows"] == [
        {"账户编号": "AC001", "账户名称": "基本户", "开户行": "招商银行", "状态": "启用", "更新日期": "2026-06-16"}
    ]


def test_browser_agent_extracts_generic_table_column_answer(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在账户名称中输入基本户,然后点击确定按钮,告诉我账户对应的状态",
    )
    observation = BrowserObservation(
        page_text=(
            "账户管理 账户编号 账户名称 开户行 状态 "
            "AC001 基本户 招商银行 启用 显示1到1,共1记录"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer["answer"] == "基本户 对应的状态是 启用。"
    assert answer["matches"][0]["row_id"] == "AC001"


def test_browser_agent_answers_empty_assigned_role_names(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="点击弹出内容中的已分配岗位，告诉我当前已分配岗位中的岗位名称",
    )
    observation = BrowserObservation(
        page_text=(
            "查询条件 查询 岗位名称 可分配岗位 已分配岗位 岗位列表 取消分配 "
            "岗位名称 10 20 50 100 200 第 共1页 显示0到0,共0记录"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer == {"answer": "当前已分配岗位中没有岗位名称。", "role_names": []}


def test_browser_agent_extracts_assigned_role_names(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="点击弹出内容中的已分配岗位，告诉我当前已分配岗位中的岗位名称",
    )
    observation = BrowserObservation(
        page_text=(
            "可分配岗位 已分配岗位 岗位列表 取消分配 岗位名称 "
            "复核岗 经办岗 10 20 50 100 200 第 共1页 显示1到2,共2记录"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer == {"answer": "当前已分配岗位中的岗位名称：复核岗、经办岗。", "role_names": ["复核岗", "经办岗"]}


def test_browser_agent_extracts_assigned_role_names_from_english_ifinance_headers(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="点击弹出内容中的已分配岗位，告诉我当前已分配岗位中的岗位名称",
    )
    observation = BrowserObservation(
        page_text=(
            "Distributive Duty Assigned position Duty List CancelAssign duty Name "
            "锦乔生物科技有限公司经办人 10 20 50 100 200 Page of 1 Displaying 1 to 1 of 1 items"
        )
    )

    answer = tool._answer_from_observation(spec, observation)

    assert answer == {
        "answer": "当前已分配岗位中的岗位名称：锦乔生物科技有限公司经办人。",
        "role_names": ["锦乔生物科技有限公司经办人"],
    }
