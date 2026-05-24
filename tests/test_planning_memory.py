from __future__ import annotations

from aiops_agent.planning import PlanningService
from aiops_agent.tools.chat import ChatTool


def test_planning_uses_session_memory_to_fill_inspection_entities():
    plan = PlanningService().plan(
        "再看一下生产环境",
        "inspection",
        {
            "raw_text": "再看一下生产环境",
            "session_memory": {
                "task_matches": [
                    {
                        "task_id": "inspect-1",
                        "intent": "inspection",
                        "system": "WebLogic",
                        "env": "prod",
                    }
                ]
            },
        },
    )

    params = plan.tool_calls[0].params

    assert params["system"] == "WebLogic"
    assert params["env"] == "prod"


def test_planning_builds_rpa_action_login_call():
    plan = PlanningService().plan(
        "登录 120.13 ssh",
        "rpa_action",
        {
            "raw_text": "登录 120.13 ssh",
            "target": "120.13",
            "capability": "ssh",
            "operation": "login",
        },
    )

    call = plan.tool_calls[0]

    assert call.tool_name == "rpa_action"
    assert call.action == "login"
    assert call.risk_level == "controlled_rpa_login"
    assert call.params["target"] == "120.13"
    assert call.params["capability"] == "ssh"


def test_planning_uses_session_memory_to_fill_rpa_action_target():
    plan = PlanningService().plan(
        "再打开这台机器的 sftp",
        "rpa_action",
        {
            "raw_text": "再打开这台机器的 sftp",
            "capability": "sftp",
            "session_memory": {
                "task_matches": [
                    {
                        "task_id": "rpa-1",
                        "intent": "rpa_action",
                        "target": "120.13",
                        "capability": "ssh",
                    }
                ]
            },
        },
    )

    params = plan.tool_calls[0].params

    assert params["target"] == "120.13"
    assert params["capability"] == "sftp"


def test_planning_uses_session_memory_for_web_resume_params():
    plan = PlanningService().plan(
        "继续查 bob",
        "web_action",
        {
            "raw_text": "继续查 bob",
            "session_memory": {
                "browser_memory": {
                    "last_url": "http://demo.test/users",
                    "state_path": "storage/artifacts/session/browser-state.json",
                    "last_success_site_key": "demo",
                }
            },
        },
    )

    params = plan.tool_calls[0].params

    assert params["start_url"] == "http://demo.test/users"
    assert params["session_state_path"] == "storage/artifacts/session/browser-state.json"
    assert params["site_key"] == "demo"
    assert params["allowed_domains"] == ["demo.test"]


def test_planning_passes_session_memory_to_chat_tool():
    memory = {"summary": "刚才巡检了 WebLogic", "short_term": [{"task_id": "t1"}]}
    plan = PlanningService().plan(
        "刚才做了什么",
        "general_chat",
        {"raw_text": "刚才做了什么", "session_memory": memory},
    )

    assert plan.tool_calls[0].params["session_memory"] == memory


def test_chat_tool_includes_session_memory_in_llm_context():
    class _Provider:
        enabled = True

        def __init__(self):
            self.context = None

        def generate_chat_reply(self, text, context=None):
            self.context = context
            return "刚才巡检了 WebLogic。"

    provider = _Provider()
    result = ChatTool(provider).execute(
        {
            "message": "刚才做了什么",
            "session_memory": {"summary": "刚才巡检了 WebLogic"},
        }
    )

    assert result.success is True
    assert provider.context["session_memory"]["summary"] == "刚才巡检了 WebLogic"
