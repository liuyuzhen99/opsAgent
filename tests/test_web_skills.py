from __future__ import annotations

import json
from types import SimpleNamespace

from aiops_agent.agent.controller import AgentController
import pytest

from aiops_agent.browser.skills import (
    WebSkillGenerationError,
    WebSkillGenerator,
    WebSkillMatcher,
    WebSkillStore,
    WebSkillValidationError,
)
from aiops_agent.chat import ChatOptions, ChatRunner
from aiops_agent.planning import PlanningService
from aiops_agent.tasks.models import Task, ToolCallSpec


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
    assert "alice" not in workflow_text
    assert "password" not in workflow_text.lower()


def test_web_skill_generator_rejects_invalid_name_and_non_successful_task(tmp_path):
    store = WebSkillStore(tmp_path / "web_skills")
    task = _successful_web_task()

    with pytest.raises(WebSkillValidationError, match="consecutive hyphens"):
        WebSkillGenerator(store).generate_from_task(task, name="bad--name")

    task.status = "blocked"
    with pytest.raises(WebSkillGenerationError, match="未成功完成"):
        WebSkillGenerator(store).generate_from_task(task, name="demo-search-user")


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


def test_chat_save_skill_command_prints_summary(tmp_path):
    result = SimpleNamespace(
        path=tmp_path / "web_skills" / "demo-search-user",
        inputs=["username"],
        action_count=7,
        matched_keywords=["查询", "用户名"],
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
