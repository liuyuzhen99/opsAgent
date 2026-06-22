from __future__ import annotations

import json
from types import SimpleNamespace

from aiops_agent.agent.controller import AgentController
import pytest

from aiops_agent.browser.action_trace import build_canonical_action_trace
from aiops_agent.browser.models import ActionResult, BrowserObservation
from aiops_agent.browser.skills import (
    WebSkillGenerationError,
    WebSkillGenerator,
    WebSkillInvocationService,
    WebSkillMatcher,
    WebSkillStore,
    WebSkillValidationError,
)
from aiops_agent.browser.skills.renderer import WebSkillRenderer
from aiops_agent.chat import ChatOptions, ChatRunner
from aiops_agent.cli import create_controller
from aiops_agent.planning import PlanningService
from aiops_agent.tasks.models import Task, ToolCallSpec
from tests.test_agent_flow import _write_llm_config, _write_rpa_config


def _successful_web_task() -> Task:
    task = Task(trace_id="trace", input="查询用户 alice，告诉我岗位名称", id="task-1", session_id="session-1")
    task.intent = "web_action"
    task.status = "success"
    task.entities = {"site_key": "demo", "workflow_fields": {"username": "alice"}}
    task.tool_calls = [
        ToolCallSpec(
            tool_name="browser_agent",
            action="run_browser_task",
            params={"site_key": "demo", "requires_login": True},
        )
    ]
    reflection = {
        "intent_aligned": True,
        "intent_reason": "ok",
        "failure_category": "none",
        "failure_reason": "",
        "terminal": False,
        "terminal_reason": None,
        "next_decision": "continue",
    }
    steps = [
        {"action": {"type": "open_url", "value": "http://example.test", "key": "site.open"}, "result": "success", "reflection": reflection},
        {"action": {"type": "click", "target_hint": "用户管理", "key": "nav.users"}, "result": "success", "reflection": reflection},
        {
            "action": {"type": "type", "target_hint": "用户名", "value": "alice", "key": "field.username"},
            "result": "success",
            "reflection": reflection,
        },
        {"action": {"type": "click", "target_hint": "查询", "key": "search.submit"}, "result": "success", "reflection": reflection},
        {"action": {"type": "extract_text", "target_hint": "用户查询结果", "key": "extract"}, "result": "success", "reflection": reflection},
        {"action": {"type": "save_artifact", "key": "artifact"}, "result": "success", "reflection": reflection},
        {"action": {"type": "finish", "key": "finish"}, "result": "success", "reflection": reflection},
    ]
    task.result = {
        "success": True,
        "data": {
            "status": "completed",
            "answer": {"answer": "当前已分配岗位中的岗位名称：只读。"},
            "steps": steps,
        },
    }
    return task


def _successful_date_range_task() -> Task:
    task = Task(
        trace_id="trace",
        input="登录财司系统，将时间范围设置为2026-05-13到2026-05-28，然后点击查询，告诉我查询结果",
        id="date-task",
        session_id="session-1",
    )
    task.intent = "web_action"
    task.status = "success"
    task.entities = {"site_key": "ifinance"}
    task.tool_calls = [
        ToolCallSpec(
            tool_name="browser_agent",
            action="run_browser_task",
            params={"site_key": "ifinance", "requires_login": True},
        )
    ]
    reflection = {
        "intent_aligned": True,
        "intent_reason": "ok",
        "failure_category": "none",
        "failure_reason": "",
        "terminal": False,
        "terminal_reason": None,
        "next_decision": "continue",
    }
    steps = [
        {"action": {"type": "open_url", "value": "http://example.test", "key": "site.open"}, "result": "success", "reflection": reflection},
        {"action": {"type": "click", "target_hint": "银企平台", "key": "nav.bank"}, "result": "success", "reflection": reflection},
        {
            "action": {
                "type": "type",
                "target_hint": "指令创建日期： * 至： *",
                "target_id": "transCreateDateStart",
                "value": "2026-05-13",
                "expected_outcome": "Start date field filled with 2026-05-13",
                "key": "field.start",
            },
            "result": "success",
            "reflection": reflection,
        },
        {
            "action": {
                "type": "type",
                "target_hint": "至：",
                "target_id": "transCreateDateEnd",
                "value": "2026-05-28",
                "expected_outcome": "End date field filled with 2026-05-28",
                "key": "field.end",
            },
            "result": "success",
            "reflection": reflection,
        },
        {"action": {"type": "click", "target_hint": "查询", "key": "search.submit"}, "result": "success", "reflection": reflection},
        {"action": {"type": "extract_text", "target_hint": "对私支付指令查询结果表格", "key": "extract"}, "result": "success", "reflection": reflection},
        {"action": {"type": "save_artifact", "key": "artifact"}, "result": "success", "reflection": reflection},
        {
            "action": {
                "type": "finish",
                "value": "查询结果（2026-05-13至2026-05-28）如下：旧结果",
                "key": "finish",
            },
            "result": "success",
            "reflection": reflection,
        },
    ]
    task.result = {
        "success": True,
        "data": {
            "status": "completed",
            "answer": {"answer": "查询结果摘要"},
            "steps": steps,
        },
    }
    return task


def _successful_user_search_without_captured_type_task() -> Task:
    task = Task(
        trace_id="trace",
        input=(
            "查询用户pen_test2的信息：进入网银用户管理,等待页面加载完成，然后点击查询,"
            "在弹出的内容中找到用户名称,在用户名称中输入pen_test2,然后点击确定按钮,告诉我用户对应的信息"
        ),
        id="missing-type-task",
        session_id="session-1",
    )
    task.intent = "web_action"
    task.status = "success"
    task.entities = {"site_key": "ifinance"}
    task.tool_calls = [
        ToolCallSpec(
            tool_name="browser_agent",
            action="run_browser_task",
            params={"site_key": "ifinance", "requires_login": True},
        )
    ]
    reflection = {
        "intent_aligned": True,
        "intent_reason": "ok",
        "failure_category": "none",
        "failure_reason": "",
        "terminal": False,
        "terminal_reason": None,
        "next_decision": "continue",
    }
    steps = [
        {"action": {"type": "open_url", "value": "http://example.test", "key": "site.open"}, "result": "success", "reflection": reflection},
        {"action": {"type": "click", "target_hint": "网上银行管理", "key": "nav.bank"}, "result": "success", "reflection": reflection},
        {"action": {"type": "click", "target_hint": "用户信息管理", "key": "nav.users"}, "result": "success", "reflection": reflection},
        {"action": {"type": "wait_for", "expected_outcome": "页面加载完成", "key": "wait"}, "result": "success", "reflection": reflection},
        {
            "action": {
                "type": "extract_text",
                "target_hint": "用户列表",
                "value": "用户编号 U0003085 用户名称 pen_test2 登录名称 pen_test2",
                "expected_outcome": "提取pen_test2的完整信息",
                "key": "extract",
            },
            "result": "success",
            "reflection": reflection,
        },
        {
            "action": {
                "type": "finish",
                "value": "用户pen_test2的信息：用户编号U0003085。",
                "key": "finish",
            },
            "result": "success",
            "reflection": reflection,
        },
    ]
    task.result = {
        "success": True,
        "data": {
            "status": "completed",
            "answer": {"answer": "用户pen_test2的信息：用户编号U0003085。"},
            "steps": steps,
        },
    }
    return task


def _successful_ifinance_assigned_role_task() -> Task:
    task = Task(
        trace_id="trace",
        input=(
            "使用ifinance-check-admin登录ifinance，在左侧侧边栏依次点击网上银行管理，权限管理，"
            "网银岗位分配进入对应菜单，等待页面加载完成，然后在授权单位展开授权单位下拉列表，"
            "输入“101-51013200_内部客户”，之后点击下方高亮的第一个内容，之后点击查询按钮，"
            "在用户名中输入U0002865，然后点击下方的查询按钮进行查询，告诉我当前已分配岗位中的岗位名称"
        ),
        id="ifinance-assigned-role-task",
        session_id="session-1",
    )
    task.intent = "web_action"
    task.status = "success"
    task.entities = {"site_key": "ifinance"}
    task.tool_calls = [
        ToolCallSpec(
            tool_name="browser_agent",
            action="run_browser_task",
            params={"site_key": "ifinance", "requires_login": True},
        )
    ]
    reflection = {
        "intent_aligned": True,
        "failure_category": "none",
        "terminal": False,
        "next_decision": "continue",
    }
    steps = [
        {
            "action": {
                "type": "type",
                "target_hint": "授权单位搜索输入框",
                "value": "101-51013200_内部客户",
                "expected_outcome": "按用户指令在 授权单位 下拉搜索框中输入 101-51013200_内部客户",
                "key": "field.company",
            },
            "result": "success",
            "reflection": reflection,
        },
        {
            "action": {
                "type": "click",
                "target_hint": "101-51013200_内部客户",
                "key": "company.option",
            },
            "result": "success",
            "reflection": reflection,
        },
        {
            "action": {
                "type": "type",
                "target_hint": "userName",
                "value": "U0002865",
                "expected_outcome": "按用户指令先在字段 用户名 中输入 U0002865",
                "key": "field.username",
            },
            "result": "success",
            "reflection": reflection,
        },
        {
            "action": {"type": "click", "target_hint": "Query", "key": "search.submit"},
            "result": "success",
            "reflection": reflection,
        },
        {
            "action": {"type": "extract_text", "target_hint": "已分配岗位列表", "key": "extract"},
            "result": "success",
            "reflection": reflection,
        },
        {
            "action": {"type": "finish", "value": "锦乔生物科技有限公司经办人", "key": "finish"},
            "result": "success",
            "reflection": reflection,
        },
    ]
    task.result = {
        "success": True,
        "data": {
            "status": "completed",
            "answer": {"answer": "当前已分配岗位中的岗位名称：锦乔生物科技有限公司经办人。"},
            "steps": steps,
        },
    }
    return task


def _successful_ifinance_assigned_account_task(*, filter_account: bool = False) -> Task:
    account_filter = (
        "点击查询，将1011051015101输入到“账户编号由”和”账户编号至“中，然后点击确定，"
        if filter_account
        else ""
    )
    goal = (
        "使用ifinance-check-admin登录ifinance，在左侧侧边栏依次点击网上银行管理，权限管理，"
        "网银账户权限设置进入对应菜单，等待页面加载完成，然后点击查询按钮，在用户名称中输入U0002865，"
        "点击确定，然后点击查询结果中的U0002865，之后点击已分配账户，"
        f"{account_filter}告诉我1011051015101是否在该用户的已分配账户中"
    )
    task = Task(trace_id="trace", input=goal, id="assigned-account-task", session_id="session-1")
    task.intent = "web_action"
    task.status = "success"
    task.entities = {"site_key": "ifinance"}
    task.tool_calls = [
        ToolCallSpec(
            tool_name="browser_agent",
            action="run_browser_task",
            params={"site_key": "ifinance", "requires_login": True},
        )
    ]
    reflection = {
        "intent_aligned": True,
        "failure_category": "none",
        "terminal": False,
        "next_decision": "continue",
    }
    raw_actions = [
        {"type": "open_url", "value": "http://example.test", "key": "open"},
        {"type": "click", "target_hint": "财司系统", "expected_outcome": "进入财司系统主页面", "key": "system"},
        {"type": "click", "target_hint": "网上银行管理", "key": "nav.bank"},
        {"type": "click", "target_hint": "权限管理", "key": "nav.permission"},
        {
            "type": "click",
            "target_hint": "查询",
            "expected_outcome": "按动作意图点击命令按钮: 查询",
            "key": "stabilized.wrong-query",
        },
        {"type": "click", "target_hint": "网银账户权限设置", "key": "nav.account-permission"},
        {"type": "click", "target_hint": "查 询", "key": "query"},
        {"type": "type", "target_hint": "用户名称", "value": "U0002865", "key": "user"},
        {"type": "click", "target_hint": "确 定", "key": "search"},
        {"type": "click", "target_hint": "U0002865", "key": "result"},
        {"type": "click", "target_hint": "已分配账户", "key": "assigned"},
    ]
    if filter_account:
        raw_actions.extend(
            [
                {"type": "click", "target_hint": "查 询", "key": "account-query"},
                {
                    "type": "type",
                    "target_hint": "startAccountNo",
                    "value": "1011051015101",
                    "expected_outcome": "按用户指令在字段 账户编号由 中输入 1011051015101",
                    "key": "account-from",
                },
                {
                    "type": "type",
                    "target_hint": "endAccountNo",
                    "value": "1011051015101",
                    "expected_outcome": "Input 1011051015101 into the 账户编号至 field",
                    "key": "account-to",
                },
                {"type": "click", "target_hint": "确 定", "key": "account-search"},
            ]
        )
    raw_actions.append(
        {
            "type": "finish",
            "value": "1011051015101在该用户的已分配账户中。",
            "expected_outcome": "已从用户指定列表中判断目标值是否存在",
            "key": "finish",
        }
    )
    task.result = {
        "success": True,
        "data": {
            "status": "completed",
            "answer": {"answer": "1011051015101在该用户的已分配账户中。"},
            "steps": [
                {"action": action, "result": "success", "reflection": reflection}
                for action in raw_actions
            ],
        },
    }
    return task


def test_web_skill_generator_writes_agentskills_layout_and_parameterizes_values(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    result = WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")

    assert (result.path / "SKILL.md").exists()
    assert (result.path / "assets" / "workflow.json").exists()
    assert (result.path / "references" / "notes.md").exists()
    assert result.inputs == ["username"]
    assert result.action_count == 7

    skill = store.load("demo-search-user")
    assert skill.frontmatter["name"] == "demo-search-user"
    assert skill.frontmatter["metadata"]["opsagent_site_key"] == "demo"
    workflow_text = (result.path / "assets" / "workflow.json").read_text(encoding="utf-8")
    workflow = json.loads(workflow_text)
    assert workflow["actions"][2]["value"] == "{{username}}"
    assert workflow["inputs"][0]["type"] == "text"
    assert workflow["inputs"][0]["examples"] == ["alice"]
    assert "password" not in workflow_text.lower()


def test_web_skill_generator_prefers_canonical_action_trace(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    task = _successful_web_task()
    data = task.result["data"]
    data["canonical_action_trace"] = build_canonical_action_trace(
        data["steps"],
        status="completed",
        task_id=task.id,
        session_id=task.session_id or "",
    )
    del data["steps"]

    result = WebSkillGenerator(store).generate_from_task(task, name="demo-canonical-search-user")
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))

    assert result.inputs == ["username"]
    assert workflow["actions"][2]["value"] == "{{username}}"


def test_web_skill_generator_parameterizes_date_range_and_excludes_finish_value(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    result = WebSkillGenerator(store).generate_from_task(
        _successful_date_range_task(),
        name="ifinance-person-payment-search-bydate",
    )
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))

    assert result.inputs == ["start_date", "end_date"]
    assert workflow["inputs"][:2] == [
        {
            "name": "start_date",
            "type": "date",
            "required": True,
            "source": "user_goal",
            "aliases": ["开始日期", "起始日期", "开始时间", "起始时间", "from", "start", "start_date"],
            "examples": ["2026-05-13"],
            "original_value": "2026-05-13",
        },
        {
            "name": "end_date",
            "type": "date",
            "required": True,
            "source": "user_goal",
            "aliases": ["结束日期", "截止日期", "结束时间", "截止时间", "to", "end", "end_date"],
            "examples": ["2026-05-28"],
            "original_value": "2026-05-28",
        },
    ]
    assert workflow["actions"][2]["value"] == "{{start_date}}"
    assert workflow["actions"][3]["value"] == "{{end_date}}"
    assert "旧结果" not in json.dumps(workflow, ensure_ascii=False)
    assert workflow["actions"][-1]["type"] == "finish"
    assert "value" not in workflow["actions"][-1]
    assert any(
        item["decision"] == "dynamic_result/excluded" and item["action_key"] == "finish"
        for item in workflow["parameterization_decisions"]
    )


def test_web_skill_generator_synthesizes_missing_query_input_from_goal(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    result = WebSkillGenerator(store).generate_from_task(
        _successful_user_search_without_captured_type_task(),
        name="ifinance-search-user",
    )
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))
    workflow_text = json.dumps(workflow, ensure_ascii=False)

    assert result.inputs == ["user_name"]
    assert workflow["inputs"][0]["name"] == "user_name"
    assert workflow["inputs"][0]["examples"] == ["pen_test2"]
    assert "用户编号 U0003085" not in workflow_text
    assert "用户pen_test2的信息" not in workflow_text
    type_actions = [action for action in workflow["actions"] if action.get("type") == "type"]
    assert type_actions == [
        {
            "type": "type",
            "target_hint": "用户名称",
            "value": "{{user_name}}",
            "expected_outcome": "填写用户名称",
            "risk_level": "safe_local_edit",
            "key": "skill.synthetic.user_name.type",
        }
    ]
    assert any(
        action.get("type") == "click" and action.get("target_hint") == "查询"
        for action in workflow["actions"]
    )
    assert any(
        action.get("type") == "click" and action.get("target_hint") == "确定"
        for action in workflow["actions"]
    )
    assert any(
        item["decision"] == "variable"
        and item["param_name"] == "user_name"
        and item["reason"] == "synthesized from explicit user input in original goal"
        for item in workflow["parameterization_decisions"]
    )
    assert any(
        item["decision"] == "dynamic_result/excluded" and item["action_key"] == "extract"
        for item in workflow["parameterization_decisions"]
    )


def test_web_skill_generator_keeps_authorization_unit_and_username_separate(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    result = WebSkillGenerator(store).generate_from_task(
        _successful_ifinance_assigned_role_task(),
        name="ifinance-assigned-role",
    )
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))

    assert result.inputs == ["company_name", "username"]
    assert workflow["inputs"][0]["examples"] == ["101-51013200_内部客户"]
    assert workflow["inputs"][1]["examples"] == ["U0002865"]
    type_actions = [action for action in workflow["actions"] if action.get("type") == "type"]
    assert type_actions[0]["value"] == "{{company_name}}"
    assert type_actions[1]["value"] == "{{username}}"
    assert type_actions[1]["target_hint"] == "用户名"
    assert "{{company_name}}" in workflow["goal_template"]
    assert "{{username}}" in workflow["goal_template"]
    assert workflow["goal_template"].endswith("岗位名称")
    assert workflow["match"]["navigation"] == ["网上银行管理", "权限管理", "网银岗位分配"]

    match = WebSkillMatcher(store).match(
        _successful_ifinance_assigned_role_task().input,
        {"site_key": "ifinance"},
    )
    assert match is not None
    assert match.parameters == {
        "company_name": "101-51013200_内部客户",
        "username": "U0002865",
    }
    rendered_types = [action for action in match.actions if action.type == "type"]
    assert rendered_types[0].value == "101-51013200_内部客户"
    assert rendered_types[1].value == "U0002865"


def test_web_skill_generator_preserves_membership_contract_and_navigation_order(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    result = WebSkillGenerator(store).generate_from_task(
        _successful_ifinance_assigned_account_task(),
        name="ifinance-check-assigned-account",
    )
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))

    assert result.inputs == ["user_name", "account_no"]
    assert workflow["match"]["answer_types"] == ["membership"]
    assert workflow["match"]["navigation"] == ["网上银行管理", "权限管理", "网银账户权限设置"]
    assert "在用户名称中输入{{user_name}}" in workflow["goal_template"]
    assert "告诉我{{account_no}}是否在该用户的已分配账户中" in workflow["goal_template"]
    click_targets = [action.get("target_hint") for action in workflow["actions"] if action.get("type") == "click"]
    assert click_targets == [
        "财司系统",
        "网上银行管理",
        "权限管理",
        "网银账户权限设置",
        "查 询",
        "确 定",
        "{{user_name}}",
        "已分配账户",
    ]
    assert workflow["actions"][-1]["type"] == "finish"
    assert "value" not in workflow["actions"][-1]

    match = WebSkillMatcher(store).match_by_name(
        "ifinance-check-assigned-account",
        {"user_name": "U0003000", "account_no": "1011051015999"},
        {"site_key": "ifinance"},
    )
    rendered_goal = WebSkillRenderer().render_goal(workflow, match.parameters)
    assert "在用户名称中输入U0003000" in rendered_goal
    assert "1011051015999是否在该用户的已分配账户中" in rendered_goal

    automatic = WebSkillMatcher(store).match(
        _successful_ifinance_assigned_account_task().input.replace("U0002865", "U0003000").replace(
            "1011051015101", "1011051015999"
        ),
        {"site_key": "ifinance"},
    )
    assert automatic is not None
    assert automatic.skill.name == "ifinance-check-assigned-account"
    assert automatic.parameters == {"user_name": "U0003000", "account_no": "1011051015999"}


def test_web_skill_generator_preserves_shared_value_for_multiple_account_fields(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    result = WebSkillGenerator(store).generate_from_task(
        _successful_ifinance_assigned_account_task(filter_account=True),
        name="ifinance-check-assigned-account",
    )
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))

    assert result.inputs == ["user_name", "account_no"]
    assert "{{account_no}}输入到“账户编号由”和”账户编号至“中" in workflow["goal_template"]
    assert "{{{{" not in json.dumps(workflow, ensure_ascii=False)
    assert not any(action.get("key", "").startswith("skill.synthetic") for action in workflow["actions"])

    type_actions = [action for action in workflow["actions"] if action.get("type") == "type"]
    assert [(action["target_hint"], action["value"]) for action in type_actions] == [
        ("用户名称", "{{user_name}}"),
        ("账户编号由", "{{account_no}}"),
        ("账户编号至", "{{account_no}}"),
    ]

    match = WebSkillMatcher(store).match_by_name(
        "ifinance-check-assigned-account",
        {"user_name": "U0003000", "account_no": "1011051015999"},
        {"site_key": "ifinance"},
    )
    rendered_types = [action for action in match.actions if action.type == "type"]
    assert [(action.target_hint, action.value) for action in rendered_types] == [
        ("用户名称", "U0003000"),
        ("账户编号由", "1011051015999"),
        ("账户编号至", "1011051015999"),
    ]


def test_web_skill_generator_drops_runtime_element_ids(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    task = _successful_ifinance_assigned_role_task()
    source_actions = [step["action"] for step in task.result["data"]["steps"]]
    source_actions[0]["target_id"] = "aiops-frame-2-el-1"
    source_actions[2]["target_id"] = "aiops-frame-2-el-2"
    source_actions[3]["target_id"] = "aiops-frame-2-el-5"

    result = WebSkillGenerator(store).generate_from_task(task, name="ifinance-assigned-role")
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))

    assert all(not str(action.get("target_id") or "").startswith("aiops-") for action in workflow["actions"])


def test_web_skill_renderer_drops_runtime_ids_from_legacy_skills_but_keeps_stable_ids():
    workflow = {
        "inputs": [],
        "actions": [
            {"type": "click", "target_hint": "网上银行管理", "target_id": "aiops-el-12"},
            {"type": "type", "target_hint": "开始日期", "target_id": "transCreateDateStart", "value": "2026-05-11"},
        ],
    }

    actions = WebSkillRenderer().render_actions(workflow, {}, {})

    assert actions[0].target_id is None
    assert actions[1].target_id == "transCreateDateStart"


def test_web_skill_renderer_drops_captured_finish_answer_from_legacy_skill():
    workflow = {
        "inputs": [],
        "actions": [
            {
                "type": "finish",
                "value": "查询结果（2026-05-13至2026-05-28）共204条",
                "expected_outcome": "返回查询结果",
            }
        ],
    }

    actions = WebSkillRenderer().render_actions(workflow, {}, {})

    assert actions[0].value is None


def test_web_skill_renderer_infers_date_range_for_legacy_input_names():
    workflow = {
        "inputs": [
            {"name": "input_value", "required": True, "source": "user_goal"},
            {"name": "input_value_2", "required": True, "source": "user_goal"},
        ]
    }

    parameters = WebSkillRenderer().infer_parameters(
        workflow,
        "将时间范围设置为2026-05-11到2026-05-28",
        {},
    )

    assert parameters == {"input_value": "2026-05-11", "input_value_2": "2026-05-28"}


def test_web_skill_generator_replaces_captured_dropdown_value_with_field_target(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    task = _successful_ifinance_assigned_role_task()
    task.result["data"]["steps"].insert(
        0,
        {
            "action": {
                "type": "click",
                "target_hint": "101-130017_内部客户",
                "expected_outcome": "授权单位下拉列表展开，显示可选项或搜索输入框",
                "key": "field.company.open",
            },
            "result": "success",
            "reflection": {"terminal": False, "next_decision": "continue"},
        },
    )

    result = WebSkillGenerator(store).generate_from_task(task, name="ifinance-assigned-role")
    workflow = json.loads((result.path / "assets" / "workflow.json").read_text(encoding="utf-8"))

    assert workflow["actions"][0]["target_hint"] == "授权单位"


def test_web_skill_generator_rejects_invalid_name_and_non_successful_task(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    task = _successful_web_task()

    with pytest.raises(WebSkillValidationError, match="consecutive hyphens"):
        WebSkillGenerator(store).generate_from_task(task, name="bad--name")

    task.status = "blocked"
    with pytest.raises(WebSkillGenerationError, match="未成功完成"):
        WebSkillGenerator(store).generate_from_task(task, name="demo-search-user")


def test_web_skill_store_delete_removes_skill_directory(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    skill_path = store.root / "demo-search-user"

    deleted_path = store.delete("demo-search-user")

    assert deleted_path == skill_path
    assert not skill_path.exists()
    with pytest.raises(WebSkillValidationError, match="skill not found: demo-search-user"):
        store.load("demo-search-user")


def test_web_skill_store_rename_updates_internal_identity_and_preserves_files(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    result = WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    extra_asset = result.path / "assets" / "selectors.json"
    extra_asset.write_text('{"query": "#search"}\n', encoding="utf-8")

    renamed_path = store.rename("demo-search-user", "demo-find-user")

    assert renamed_path == store.root / "demo-find-user"
    assert not (store.root / "demo-search-user").exists()
    assert (renamed_path / "assets" / "selectors.json").read_text(encoding="utf-8") == '{"query": "#search"}\n'
    renamed = store.load("demo-find-user")
    assert renamed.name == "demo-find-user"
    assert renamed.frontmatter["name"] == "demo-find-user"
    assert renamed.workflow["skill_name"] == "demo-find-user"


def test_web_skill_store_rename_rejects_existing_target_without_changing_source(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-find-user")

    with pytest.raises(WebSkillValidationError, match="skill already exists: demo-find-user"):
        store.rename("demo-search-user", "demo-find-user")

    assert store.load("demo-search-user").name == "demo-search-user"
    assert store.load("demo-find-user").name == "demo-find-user"


def test_web_skill_matcher_requires_same_site_and_renders_actions(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    matcher = WebSkillMatcher(store)

    match = matcher.match("查询用户 bob，告诉我岗位名称", {"site_key": "demo", "workflow_fields": {"username": "bob"}})

    assert match is not None
    assert match.score >= 0.75
    assert match.parameters == {"username": "bob"}
    assert any(action.type == "type" and action.value == "bob" for action in match.actions)
    assert matcher.match("查询用户 bob，告诉我岗位名称", {"site_key": "other", "workflow_fields": {"username": "bob"}}) is None


def test_web_skill_matcher_rejects_conflicting_explicit_navigation_path(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    store.write(
        name="ifinance-search-user",
        frontmatter={"name": "ifinance-search-user", "description": "search user"},
        body="search user",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "ifinance-search-user",
            "site_key": "ifinance",
            "inputs": [],
            "match": {"keywords": ["查询"], "fields": ["用户名称"], "answer_types": []},
            "execution": {"auto_plan": False, "requires_login": True},
            "actions": [
                {"type": "click", "target_hint": "财司系统"},
                {"type": "click", "target_hint": "网上银行管理"},
                {"type": "click", "target_hint": "用户信息管理"},
                {"type": "click", "target_hint": "网银用户管理"},
                {"type": "click", "target_hint": "查询"},
            ],
        },
        notes="notes",
    )
    matcher = WebSkillMatcher(store)

    conflicting = matcher.match(
        "依次点击网上银行管理，权限管理，网银账户权限设置进入对应菜单，然后查询用户名称",
        {"site_key": "ifinance"},
    )
    compatible = matcher.match(
        "依次点击网上银行管理，用户信息管理，网银用户管理进入对应菜单，然后查询用户名称",
        {"site_key": "ifinance"},
    )

    assert conflicting is None
    assert compatible is not None


def test_web_skill_matcher_rejects_compound_multi_login_workflow(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    store.write(
        name="ifinance-search-user",
        frontmatter={"name": "ifinance-search-user", "description": "search user"},
        body="search user",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "ifinance-search-user",
            "site_key": "ifinance",
            "inputs": [],
            "match": {"keywords": ["网银用户管理", "用户名称"], "fields": ["用户名称"]},
            "execution": {"auto_plan": False, "requires_login": True},
            "actions": [{"type": "finish", "expected_outcome": "done"}],
        },
        notes="notes",
    )
    goal = (
        "使用ifinance-check-admin登录ifinance，在左侧侧边栏依次点击网上银行管理，用户信息管理，"
        "网银用户管理，点击新增，在用户名称中填入吕婧，在登录名称中填入lvjing_1228,"
        "所属单位编号中输入“101-230051_内部客户”然后回车，点击保存。之后使用ifinance-init-admin"
        "登录ifinance, 在左侧侧边栏依次点击网上银行管理，用户信息管理，网银用户复核，"
        "选中所有需要复核的数据，点击复核按钮并确认复核"
    )

    match = WebSkillMatcher(store).match(goal, {"site_key": "ifinance"})

    assert match is None


def test_web_skill_matcher_accepts_compatible_compound_multi_login_workflow(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    goal_template = (
        "使用ifinance-check-admin登录ifinance，在左侧侧边栏依次点击网上银行管理，用户信息管理，"
        "网银用户管理，点击新增，在用户名称中填入{{user_name}}，在登录名称中填入{{login_name}},"
        "所属单位编号中输入“{{company_name}}”然后回车，点击保存。之后使用ifinance-init-admin登录"
        "ifinance, 在左侧侧边栏依次点击网上银行管理，用户信息管理，网银用户复核，选中所有需要"
        "复核的数据，点击复核按钮并确认复核"
    )
    store.write(
        name="ifinance-create-review-user",
        frontmatter={"name": "ifinance-create-review-user", "description": "create and review user"},
        body="create and review user",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "ifinance-create-review-user",
            "site_key": "ifinance",
            "goal_template": goal_template,
            "inputs": [
                {"name": "user_name", "required": True, "aliases": ["用户名称"], "examples": ["吕婧1"]},
                {"name": "login_name", "required": True, "aliases": ["登录名称"], "examples": ["lvjing_12281"]},
                {
                    "name": "company_name",
                    "required": True,
                    "aliases": ["所属单位编号"],
                    "examples": ["101-230051_内部客户"],
                },
            ],
            "match": {
                "keywords": ["网银用户管理", "网银用户复核", "复核"],
                "fields": ["用户名称", "登录名称", "所属单位编号"],
                "navigation": [
                    "网上银行管理", "用户信息管理", "网银用户管理",
                    "网上银行管理", "用户信息管理", "网银用户复核",
                ],
            },
            "execution": {
                "auto_plan": False,
                "requires_login": True,
                "credential_refs": ["ifinance-check-admin", "ifinance-init-admin"],
            },
            "actions": [
                {"type": "type", "target_hint": "用户名称", "value": "{{user_name}}"},
                {"type": "type", "target_hint": "登录名称", "value": "{{login_name}}"},
                {"type": "type", "target_hint": "所属单位编号", "value": "{{company_name}}"},
                {"type": "click", "target_hint": "保存", "risk_level": "unsafe_mutation"},
                {"type": "click", "target_hint": "复核", "risk_level": "unsafe_mutation"},
                {"type": "finish", "expected_outcome": "done"},
            ],
        },
        notes="notes",
    )
    goal = (
        "使用ifinance-check-admin登录ifinance，在左侧侧边栏依次点击网上银行管理，用户信息管理，"
        "网银用户管理，点击新增，在用户名称中填入吕婧1，在登录名称中填入lvjing_12281,"
        "所属单位编号中输入“101-230051_内部客户”然后回车，点击保存。之后使用ifinance-init-admin"
        "登录ifinance, 在左侧侧边栏依次点击网上银行管理，用户信息管理，网银用户复核，"
        "选中所有需要复核的数据，点击复核按钮并确认复核"
    )
    entities = {
        "site_key": "ifinance",
        "credential_refs": ["ifinance-check-admin", "ifinance-init-admin"],
    }
    matcher = WebSkillMatcher(store)

    match = matcher.match(goal, entities)
    plan = PlanningService(web_skill_matcher=matcher).plan(goal, "web_action", {**entities, "raw_text": goal})

    assert match is not None
    assert match.skill.name == "ifinance-create-review-user"
    assert match.parameters == {
        "user_name": "吕婧1",
        "login_name": "lvjing_12281",
        "company_name": "101-230051_内部客户",
    }
    params = plan.tool_calls[0].params
    assert params["skill_name"] == "ifinance-create-review-user"
    assert params["auto_plan"] is False
    assert params["credential_refs"] == ["ifinance-check-admin", "ifinance-init-admin"]


def test_web_skill_generator_preserves_ordered_multi_login_credentials(tmp_path):
    task = _successful_web_task()
    task.tool_calls[0].params["credential_ref"] = "demo-check-admin"
    task.tool_calls[0].params["credential_refs"] = ["demo-check-admin", "demo-init-admin"]
    task.result["data"]["steps"].insert(
        -3,
        {
            "action": {
                "type": "press",
                "target_hint": "所属单位编号",
                "value": "Enter",
                "key": "field.company.enter",
            },
            "result": "success",
            "reflection": {"terminal": False, "next_decision": "continue"},
        },
    )
    store = WebSkillStore(tmp_path / "web_skills")

    WebSkillGenerator(store).generate_from_task(task, name="demo-multi-login")
    workflow = store.load("demo-multi-login").workflow

    assert workflow["execution"]["credential_refs"] == ["demo-check-admin", "demo-init-admin"]
    assert not any(
        item.get("original_value") == "Enter"
        for item in workflow["parameterization_decisions"]
    )


def test_web_skill_generator_keeps_actions_between_compound_navigation_stages(tmp_path):
    generator = WebSkillGenerator(WebSkillStore(tmp_path / "web_skills"))
    goal = (
        "依次点击网上银行管理，用户信息管理，网银用户管理，点击新增并保存。之后"
        "依次点击网上银行管理，用户信息管理，网银用户复核，选中所有数据并复核"
    )
    actions = [
        {"type": "click", "target_hint": "网上银行管理"},
        {"type": "click", "target_hint": "用户信息管理"},
        {"type": "click", "target_hint": "网银用户管理"},
        {"type": "click", "target_hint": "新增"},
        {"type": "click", "target_hint": "保存"},
        {"type": "click", "target_hint": "确定"},
        {"type": "click", "target_hint": "退出系统"},
        {"type": "click", "target_hint": "财司系统"},
        {"type": "click", "target_hint": "网上银行管理"},
        {"type": "click", "target_hint": "用户信息管理"},
        {"type": "click", "target_hint": "网银用户复核"},
        {"type": "click", "target_hint": "全选复选框"},
        {"type": "click", "target_hint": "复核"},
    ]

    pruned = generator._prune_clicks_during_explicit_navigation(actions, goal)
    targets = [action["target_hint"] for action in pruned]

    assert targets == [action["target_hint"] for action in actions]


def test_web_skill_generator_reapplies_current_risk_policy(tmp_path):
    task = _successful_web_task()
    task.result["data"]["steps"].insert(
        -3,
        {
            "action": {
                "type": "click",
                "target_hint": "复核",
                "expected_outcome": "触发复核操作，可能弹出确认对话框",
                "risk_level": "safe_local_edit",
                "requires_confirmation": False,
                "key": "review.submit",
            },
            "result": "success",
            "reflection": {"terminal": False, "next_decision": "continue"},
        },
    )
    store = WebSkillStore(tmp_path / "web_skills")

    WebSkillGenerator(store).generate_from_task(task, name="demo-risk-normalized")
    review = next(
        action
        for action in store.load("demo-risk-normalized").workflow["actions"]
        if action.get("key") == "review.submit"
    )

    assert review["risk_level"] == "unsafe_mutation"
    assert review["requires_confirmation"] is True


def test_web_skill_invocation_replays_saved_multi_login_credential_order(tmp_path):
    task = _successful_web_task()
    task.tool_calls[0].params["credential_ref"] = "demo-check-admin"
    task.tool_calls[0].params["credential_refs"] = ["demo-check-admin", "demo-init-admin"]
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(task, name="demo-multi-login")
    service = WebSkillInvocationService(WebSkillMatcher(store))

    invocation = service.prepare_invocation("demo-multi-login", {"username": "bob"})

    assert invocation.credential_ref == "demo-check-admin"
    assert invocation.entities["credential_refs"] == ["demo-check-admin", "demo-init-admin"]
    assert invocation.call_spec.params["credential_refs"] == ["demo-check-admin", "demo-init-admin"]
    [summary] = service.list_skills()
    assert summary["runtime_inputs"][1]["default"] == "demo-check-admin,demo-init-admin"


def test_planning_service_passes_all_web_credentials_to_browser_agent():
    service = PlanningService()
    plan = service.plan(
        "先经办再复核",
        "web_action",
        {
            "raw_text": "先经办再复核",
            "credential_ref": "ifinance-check-admin",
            "credential_refs": ["ifinance-check-admin", "ifinance-init-admin"],
            "requires_login": True,
        },
    )

    params = plan.tool_calls[0].params
    assert params["credential_ref"] == "ifinance-check-admin"
    assert params["credential_refs"] == ["ifinance-check-admin", "ifinance-init-admin"]


def test_web_skill_matcher_renders_explicit_skill_by_name(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    matcher = WebSkillMatcher(store)

    match = matcher.match_by_name("demo-search-user", {"username": "bob"}, {"site_key": "demo"})

    assert match.score == 1.0
    assert match.parameters == {"username": "bob"}
    assert any(action.type == "type" and action.value == "bob" for action in match.actions)


def test_web_skill_matcher_explicit_skill_requires_parameters(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    matcher = WebSkillMatcher(store)

    with pytest.raises(WebSkillValidationError, match="missing required skill parameters: username"):
        matcher.match_by_name("demo-search-user", {}, {"site_key": "demo"})


def test_web_skill_matcher_extracts_date_range_parameters(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(
        _successful_date_range_task(),
        name="ifinance-person-payment-search-bydate",
    )
    matcher = WebSkillMatcher(store)

    match = matcher.match(
        "登录财司系统，点击银企平台->对私指令查询，将时间范围设置为2026-05-11到2026-05-28，然后点击查询",
        {"site_key": "ifinance"},
    )

    assert match is not None
    assert match.parameters == {"start_date": "2026-05-11", "end_date": "2026-05-28"}
    assert any(action.type == "type" and action.value == "2026-05-11" for action in match.actions)
    assert any(action.type == "type" and action.value == "2026-05-28" for action in match.actions)


def test_web_skill_matcher_does_not_match_when_required_parameters_are_missing(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(
        _successful_date_range_task(),
        name="ifinance-person-payment-search-bydate",
    )
    matcher = WebSkillMatcher(store)

    match = matcher.match(
        "登录财司系统，点击银企平台->对私指令查询，然后点击查询",
        {"site_key": "ifinance"},
    )

    assert match is None


def test_web_skill_matcher_keeps_legacy_input_value_skill_compatible(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    store.write(
        name="legacy-search",
        frontmatter={
            "name": "legacy-search",
            "description": "legacy input value skill",
            "compatibility": ["opsAgent web_action"],
        },
        body="legacy",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "legacy-search",
            "site_key": "demo",
            "inputs": [{"name": "input_value", "required": True, "source": "user_goal", "examples": ["alice"]}],
            "match": {"keywords": ["查询用户"], "fields": ["用户名"], "answer_types": []},
            "execution": {"auto_plan": False, "requires_login": False, "fallback_to_llm_once": True},
            "actions": [
                {"type": "type", "target_hint": "用户名", "value": "{{input_value}}"},
                {"type": "finish", "expected_outcome": "done"},
            ],
        },
        notes="legacy notes",
    )
    matcher = WebSkillMatcher(store)

    match = matcher.match("查询用户 alice", {"site_key": "demo"})

    assert match is not None
    assert match.parameters == {"input_value": "alice"}


def test_web_skill_matcher_infers_user_name_parameter(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    store.write(
        name="demo-user-name-search",
        frontmatter={
            "name": "demo-user-name-search",
            "description": "search user by user_name",
            "compatibility": ["opsAgent web_action"],
        },
        body="search user",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "demo-user-name-search",
            "site_key": "demo",
            "inputs": [{"name": "user_name", "required": True, "type": "text"}],
            "match": {"keywords": ["查询用户"], "fields": ["用户名称"], "answer_types": []},
            "execution": {"auto_plan": False, "requires_login": False},
            "actions": [
                {"type": "type", "target_hint": "用户名称", "value": "{{user_name}}"},
                {"type": "click", "target_hint": "查询"},
            ],
        },
        notes="notes",
    )
    matcher = WebSkillMatcher(store)

    match = matcher.match("在用户名称中输入 bob，然后查询用户", {"site_key": "demo"})

    assert match is not None
    assert match.parameters == {"user_name": "bob"}
    assert match.actions[0].value == "bob"


def test_web_skill_matcher_supports_workflow_v2_steps(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    store.write(
        name="v2-search",
        frontmatter={
            "name": "v2-search",
            "description": "workflow v2 skill",
            "compatibility": ["opsAgent web_action"],
        },
        body="v2",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v2",
            "skill_name": "v2-search",
            "site_key": "demo",
            "inputs": [{"name": "username", "required": True}],
            "match": {"keywords": ["查询用户"], "fields": ["用户名"], "answer_types": []},
            "execution": {"auto_plan": False, "requires_login": False, "fallback_to_llm_once": True},
            "steps": [
                {"type": "type", "target_hint": "用户名", "value": "{{username}}"},
                {"type": "finish", "expected_outcome": "done"},
            ],
        },
        notes="v2 notes",
    )
    matcher = WebSkillMatcher(store)

    match = matcher.match("查询用户 alice", {"site_key": "demo", "workflow_fields": {"username": "alice"}})

    assert match is not None
    assert match.actions[0].value == "alice"


def test_planning_service_uses_matched_web_skill_as_fixed_actions(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    service = PlanningService(web_skill_matcher=WebSkillMatcher(store))

    plan = service.plan(
        "查询用户 bob，告诉我岗位名称",
        "web_action",
        {"raw_text": "查询用户 bob，告诉我岗位名称", "site_key": "demo", "workflow_fields": {"username": "bob"}},
    )
    params = plan.tool_calls[0].params

    assert params["auto_plan"] is False
    assert params["skill_name"] == "demo-search-user"
    assert any(action["type"] == "type" and action["value"] == "bob" for action in params["actions"])


def test_create_controller_wires_saved_web_skills_into_planning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = WebSkillStore(tmp_path / "storage" / "web_skills")
    WebSkillGenerator(store).generate_from_task(
        _successful_ifinance_assigned_role_task(),
        name="ifinance-assigned-role",
    )
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    controller = create_controller(str(config_path), str(llm_config_path))
    task_input = _successful_ifinance_assigned_role_task().input

    plan = controller.planning_service.plan(
        task_input,
        "web_action",
        {"raw_text": task_input, "site_key": "ifinance", "workflow_fields": {}},
    )
    params = plan.tool_calls[0].params

    assert params["auto_plan"] is False
    assert params["skill_name"] == "ifinance-assigned-role"
    assert params["skill_parameters"] == {
        "company_name": "101-51013200_内部客户",
        "username": "U0002865",
    }


class ExplicitSkillFakeBrowser:
    instance_count = 0
    executed = []

    def __init__(self, *args, **kwargs):
        ExplicitSkillFakeBrowser.instance_count += 1
        self.session_state_path = kwargs.get("session_state_path")
        self.current = BrowserObservation(url="http://example.test", title="Skill", page_type="form")

    def execute(self, action):
        ExplicitSkillFakeBrowser.executed.append(action)
        if action.type in {"type", "click", "finish"}:
            return ActionResult("success", self.current)
        return ActionResult("terminal_failure", self.current, error=f"unexpected {action.type}")

    def observe(self, *, last_action_result="", force_artifact=False):
        self.current.last_action_result = last_action_result
        if force_artifact:
            self.current.screenshot_path = "/tmp/explicit-skill-shot.png"
            self.current.page_summary_path = "/tmp/explicit-skill-summary.txt"
        return self.current

    def save_session_state(self):
        return str(self.session_state_path) if self.session_state_path else None

    def close(self):
        return None


def _write_demo_search_skill(root, *, requires_login=False):
    store = WebSkillStore(root / "storage" / "web_skills")
    store.write(
        name="demo-search-user",
        frontmatter={
            "name": "demo-search-user",
            "description": "search user",
            "compatibility": ["opsAgent web_action"],
        },
        body="search user",
        workflow={
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": "demo-search-user",
            "site_key": "demo",
            "goal_template": "在用户名中输入 {{username}}",
            "inputs": [{"name": "username", "required": True}],
            "match": {"keywords": ["查询用户"], "fields": ["用户名"], "answer_types": []},
            "execution": {"auto_plan": False, "requires_login": requires_login, "fallback_to_llm_once": True},
            "actions": [
                {"type": "type", "target_hint": "用户名", "value": "{{username}}"},
                {"type": "finish", "expected_outcome": "done"},
            ],
        },
        notes="notes",
    )


def test_controller_run_web_skill_executes_named_skill(tmp_path, monkeypatch):
    ExplicitSkillFakeBrowser.instance_count = 0
    ExplicitSkillFakeBrowser.executed = []
    _write_demo_search_skill(tmp_path)
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", ExplicitSkillFakeBrowser)
    controller = create_controller(str(config_path), str(llm_config_path))

    task = controller.run_web_skill("demo-search-user", {"username": "bob"}, max_steps=5)

    assert task.status == "success"
    assert ExplicitSkillFakeBrowser.instance_count == 1
    assert any(action.type == "type" and action.value == "bob" for action in ExplicitSkillFakeBrowser.executed)
    assert task.tool_calls[0].params["skill_name"] == "demo-search-user"


def test_controller_run_web_skill_rejects_missing_required_input_before_browser_start(tmp_path, monkeypatch):
    ExplicitSkillFakeBrowser.instance_count = 0
    _write_demo_search_skill(tmp_path)
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", ExplicitSkillFakeBrowser)
    controller = create_controller(str(config_path), str(llm_config_path))

    with pytest.raises(ValueError, match="missing required skill parameters: username"):
        controller.run_web_skill("demo-search-user", {})

    assert ExplicitSkillFakeBrowser.instance_count == 0


def test_controller_run_web_skill_rejects_missing_runtime_credential_before_browser_start(tmp_path, monkeypatch):
    ExplicitSkillFakeBrowser.instance_count = 0
    _write_demo_search_skill(tmp_path, requires_login=True)
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("aiops_agent.browser.agent.PlaywrightBrowserTool", ExplicitSkillFakeBrowser)
    controller = create_controller(str(config_path), str(llm_config_path))

    with pytest.raises(ValueError, match="user"):
        controller.run_web_skill("demo-search-user", {"username": "bob"})

    assert ExplicitSkillFakeBrowser.instance_count == 0


def test_web_skill_invocation_service_prepares_browser_call(tmp_path):
    _write_demo_search_skill(tmp_path)
    store = WebSkillStore(tmp_path / "storage" / "web_skills")
    service = WebSkillInvocationService(WebSkillMatcher(store))

    invocation = service.prepare_invocation(
        "demo-search-user",
        {"username": "bob", "credential_ref": "demo-admin"},
        allowed_domains=["extra.example"],
        max_steps=7,
        browser_trace=True,
    )

    assert invocation.task_input == "/skill demo-search-user credential_ref=demo-admin username=bob"
    assert invocation.call_spec.params["user_goal"] == "在用户名中输入 bob"
    assert invocation.skill_parameters == {"username": "bob"}
    assert invocation.credential_ref == "demo-admin"
    assert invocation.risk_level == "safe_read"
    assert invocation.plan.selected_tools == ["browser_agent"]
    assert invocation.call_spec.tool_name == "browser_agent"
    assert invocation.call_spec.params["auto_plan"] is False
    assert invocation.call_spec.params["credential_ref"] == "demo-admin"
    assert invocation.call_spec.params["allowed_domains"] == ["extra.example"]
    assert invocation.call_spec.params["trace_enabled"] is True
    assert invocation.call_spec.params["max_steps"] == 7
    assert any(action["type"] == "type" and action["value"] == "bob" for action in invocation.call_spec.params["actions"])


def test_web_skill_invocation_service_lists_site_user_runtime_inputs(tmp_path):
    _write_demo_search_skill(tmp_path, requires_login=True)
    store = WebSkillStore(tmp_path / "storage" / "web_skills")
    service = WebSkillInvocationService(WebSkillMatcher(store))

    [summary] = service.list_skills()

    assert summary["runtime_inputs"] == [
        {
            "name": "site_key",
            "required": False,
            "type": "site_key",
            "description": "站点标识，对应 credentials.local.json 的 sites.<site_key>。",
            "examples": ["demo"],
            "default": "demo",
        },
        {
            "name": "user",
            "required": True,
            "type": "credential_user",
            "description": "站点下的登录用户，对应 credentials.local.json 的 sites.<site_key>.users.<user>。",
            "examples": [],
        },
    ]


def test_web_skill_invocation_service_marks_default_runtime_user_optional(tmp_path):
    _write_demo_search_skill(tmp_path, requires_login=True)
    store = WebSkillStore(tmp_path / "storage" / "web_skills")
    service = WebSkillInvocationService(
        WebSkillMatcher(store),
        credential_user_resolver=lambda site_key: "admin" if site_key == "demo" else None,
    )

    [summary] = service.list_skills()

    assert summary["runtime_inputs"][1] == {
        "name": "user",
        "required": False,
        "type": "credential_user",
        "description": "站点下的登录用户，对应 credentials.local.json 的 sites.<site_key>.users.<user>。",
        "examples": ["admin"],
        "default": "admin",
    }


def test_web_skill_invocation_service_rejects_missing_runtime_user(tmp_path):
    _write_demo_search_skill(tmp_path, requires_login=True)
    store = WebSkillStore(tmp_path / "storage" / "web_skills")
    service = WebSkillInvocationService(WebSkillMatcher(store))

    with pytest.raises(WebSkillValidationError, match="user"):
        service.prepare_invocation("demo-search-user", {"username": "bob"})


def test_web_skill_invocation_service_resolves_credential_from_site_user(tmp_path):
    _write_demo_search_skill(tmp_path, requires_login=True)
    store = WebSkillStore(tmp_path / "storage" / "web_skills")
    service = WebSkillInvocationService(
        WebSkillMatcher(store),
        credential_ref_for_site_user=lambda site_key, user: "demo:admin" if site_key == "demo" and user == "admin" else None,
    )

    invocation = service.prepare_invocation("demo-search-user", {"username": "bob", "user": "admin"})

    assert invocation.credential_ref == "demo:admin"
    assert invocation.entities["credential_user"] == "admin"
    assert invocation.call_spec.params["credential_ref"] == "demo:admin"
    assert invocation.call_spec.params["credential_user"] == "admin"


def test_web_skill_invocation_service_uses_default_runtime_user(tmp_path):
    _write_demo_search_skill(tmp_path, requires_login=True)
    store = WebSkillStore(tmp_path / "storage" / "web_skills")
    service = WebSkillInvocationService(
        WebSkillMatcher(store),
        credential_user_resolver=lambda site_key: "admin" if site_key == "demo" else None,
        credential_ref_for_site_user=lambda site_key, user: "demo:admin" if site_key == "demo" and user == "admin" else None,
    )

    invocation = service.prepare_invocation("demo-search-user", {"username": "bob"})

    assert invocation.credential_ref == "demo:admin"
    assert invocation.entities["credential_user"] == "admin"


class _FakeSessionStore:
    def __init__(self, session):
        self.session = session

    def load(self, session_id):
        return self.session if session_id == self.session.id else None


class _FakeTaskManager:
    def __init__(self, task):
        self.task = task

    def load(self, task_id):
        return self.task if task_id == self.task.id else None


class _SaveSkillController:
    def __init__(self, result):
        self.result = result

    def save_web_skill(self, session_id, name=None):
        assert session_id == "session-1"
        assert name == "demo-search-user"
        return self.result


def test_controller_save_web_skill_uses_session_last_success_task(tmp_path):
    task = _successful_web_task()
    session = SimpleNamespace(id="session-1", metadata={"browser_last_success_task_id": task.id})
    controller = AgentController(
        parser=None,
        task_manager=_FakeTaskManager(task),
        tool_executor=None,
        summarizer=None,
        audit_logger=None,
        session_store=_FakeSessionStore(session),
        web_skill_generator=WebSkillGenerator(WebSkillStore(tmp_path / "web_skills")),
    )

    result = controller.save_web_skill("session-1", name="demo-search-user")

    assert result.name == "demo-search-user"
    assert result.path.name == "demo-search-user"


def test_controller_save_web_skill_reports_invalid_name_as_value_error(tmp_path):
    task = _successful_web_task()
    session = SimpleNamespace(id="session-1", metadata={"browser_last_success_task_id": task.id})
    controller = AgentController(
        parser=None,
        task_manager=_FakeTaskManager(task),
        tool_executor=None,
        summarizer=None,
        audit_logger=None,
        session_store=_FakeSessionStore(session),
        web_skill_generator=WebSkillGenerator(WebSkillStore(tmp_path / "web_skills")),
    )

    with pytest.raises(ValueError, match="lowercase letters, numbers, and hyphens"):
        controller.save_web_skill("session-1", name="bad_name")


def test_controller_rename_web_skill_reports_store_errors_as_value_error(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-search-user")
    WebSkillGenerator(store).generate_from_task(_successful_web_task(), name="demo-find-user")
    controller = AgentController(
        parser=None,
        task_manager=_FakeTaskManager(_successful_web_task()),
        tool_executor=None,
        summarizer=None,
        audit_logger=None,
        session_store=_FakeSessionStore(None),
        web_skill_generator=WebSkillGenerator(store),
    )

    with pytest.raises(ValueError, match="skill already exists: demo-find-user"):
        controller.rename_web_skill("demo-search-user", "demo-find-user")


def test_chat_save_skill_command_prints_summary(tmp_path):
    result = SimpleNamespace(
        path=tmp_path / "web_skills" / "demo-search-user",
        inputs=["username"],
        action_count=7,
        matched_keywords=["查询", "用户名"],
        parameterization_decisions=[
            {
                "decision": "variable",
                "param_name": "username",
                "param_type": "text",
                "original_value": "alice",
            },
            {
                "decision": "constant",
                "field_hint": "查询",
                "original_value": "查询",
                "confidence": 0.35,
            },
        ],
    )
    inputs = iter(["/save-skill demo-search-user", "/exit"])
    def fake_input(_prompt):
        return next(inputs)

    from io import StringIO

    output = StringIO()
    runner = ChatRunner(_SaveSkillController(result), ChatOptions(session_id="session-1"), input_func=fake_input, output=output)

    assert runner.run() == 0
    text = output.getvalue()
    assert "已生成 skill" in text
    assert "参数: username" in text
    assert "动作数: 7" in text
    assert "参数预览:" in text
    assert "username text 原值=alice" in text
    assert "固定值:" in text
