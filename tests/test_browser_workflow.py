from aiops_agent.audit.logger import FileAuditLogger
from aiops_agent.browser.agent import BrowserAgentTool
from aiops_agent.agent.summarizer import ResultSummarizer
from aiops_agent.browser.models import ActionResult, BrowserAction, BrowserObservation, BrowserTaskSpec, InteractiveElement
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


def test_browser_agent_infers_dropdown_field_for_explicit_value_from_goal(tmp_path):
    tool = BrowserAgentTool(audit_logger=FileAuditLogger(tmp_path / "audit.jsonl"), artifact_root=tmp_path / "artifacts")
    spec = BrowserTaskSpec(
        start_url="http://example.test",
        user_goal="在授权单位展开授权单位下拉列表，输入“内蒙古伊家好奶酪有限责任公司”，之后点击搜索到的第一个公司",
    )

    field = tool._explicit_input_field_for_value(spec.user_goal, "内蒙古伊家好奶酪有限责任公司")

    assert field == "授权单位"


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
