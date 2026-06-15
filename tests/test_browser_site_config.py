import json
from types import SimpleNamespace

from aiops_agent.agent.controller import AgentController
from aiops_agent.agent.parser import IntentParser
from aiops_agent.llm.base import IntentClassification
from pydantic import ValidationError

from aiops_agent.browser.llm_planner import BrowserPlannerDecision, BrowserPlannerOutput
from aiops_agent.browser.site_config import BrowserSitesConfig, load_browser_sites_config
from aiops_agent.tasks.models import Task


def test_browser_sites_config_loads_and_defaults_allowed_domain(tmp_path):
    path = tmp_path / "browser_sites.json"
    path.write_text(
        json.dumps(
            {
                "sites": {
                    "demo": {
                        "site_key": "demo",
                        "aliases": ["演示系统"],
                        "base_url": "http://example.test",
                        "workflows": {
                            "search_user": {
                                "entry_url": "/users",
                                "navigation": ["用户管理"],
                                "submit_button": "查询",
                                "fields": {"username": "用户名"},
                            },
                            "create_user": {
                                "entry_url": "/users",
                                "submit_button": "保存",
                                "fields": {"username": "用户名"},
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_browser_sites_config(path)

    assert config.get("demo").allowed_domains == ["example.test"]
    assert config.get("demo").aliases == ["演示系统"]
    assert config.get("demo").workflow_config("search_user").navigation == ["用户管理"]
    assert config.get("demo").workflow_config("create_user").submit_button == "保存"


def test_browser_sites_config_rejects_invalid_url():
    try:
        BrowserSitesConfig.model_validate(
            {
                "sites": {
                    "demo": {
                        "site_key": "demo",
                        "base_url": "example.test",
                        "workflows": {},
                    }
                }
            }
        )
    except ValidationError:
        return

    raise AssertionError("expected validation error")


def test_llm_planner_output_rejects_unknown_action_and_bad_shape():
    for payload in (
        {"type": "eval_js", "value": "alert(1)"},
        {"type": "type", "value": "alice"},
        {"type": "hover"},
        {"type": "open_url", "value": "/relative"},
    ):
        try:
            BrowserPlannerOutput.model_validate(payload)
        except ValidationError:
            continue
        raise AssertionError(f"expected validation error for {payload}")

    action = BrowserPlannerOutput.model_validate(
        {"type": "type", "target_hint": "用户名", "value": "alice"}
    ).to_action()
    assert action.type == "type"
    assert action.value == "alice"

    hover_action = BrowserPlannerOutput.model_validate(
        {"type": "hover", "target_hint": "更多操作"}
    ).to_action()
    assert hover_action.type == "hover"
    assert hover_action.target_hint == "更多操作"


def test_llm_planner_decision_accepts_react_thought_and_action():
    decision = BrowserPlannerDecision.model_validate(
        {
            "thought": "当前页面有用户名筛选框，下一步输入查询条件。",
            "action": {"type": "type", "target_id": "user-filter", "value": "alice"},
        }
    )

    assert decision.action.to_action().target_id == "user-filter"


class _FakeAuditLogger:
    def record(self, event):
        return None


def test_controller_infers_browser_site_and_credential_from_natural_language():
    sites = BrowserSitesConfig.model_validate(
        {
            "sites": {
                "ifinance": {
                    "site_key": "ifinance",
                    "base_url": "http://ifinance.test",
                    "login_url": "http://ifinance.test/login",
                    "allowed_domains": ["ifinance.test"],
                    "login_fields": {"username": "用户名", "password": "密码", "submit": "登录"},
                    "workflows": {},
                }
            }
        }
    )
    controller = AgentController(
        parser=IntentParser(),
        task_manager=None,
        tool_executor=None,
        summarizer=None,
        audit_logger=_FakeAuditLogger(),
        session_store=None,
        browser_sites_config=sites,
        credential_ref_resolver=lambda site_key: "ifinance_admin" if site_key == "ifinance" else None,
    )
    task = Task(trace_id="trace", input="登录ifinance网站", id="task", session_id="session")

    state = controller._intent_parse_node(
        {
            "task": task,
            "session": SimpleNamespace(id="session"),
            "allowed_domains": [],
            "credential_ref": "",
            "browser_trace": False,
            "browser_video": False,
            "browser_site": "",
            "browser_channel": "",
            "browser_slow_mo_ms": 0,
            "progress_callback": None,
        }
    )

    parsed = state["task"]
    assert parsed.intent == "web_action"
    assert parsed.entities["site_key"] == "ifinance"
    assert parsed.entities["credential_ref"] == "ifinance_admin"
    assert parsed.entities["start_url"] == "http://ifinance.test/login"
    assert parsed.entities["requires_login"] is True
    assert parsed.entities["allowed_domains"] == ["ifinance.test"]


def test_controller_alias_overrides_llm_rpa_for_finance_system_login():
    class MisclassifyingProvider:
        def classify_intent(self, text, defaults):
            return IntentClassification(
                intent="rpa_action",
                entities={},
                provider="fake",
                model="fake-model",
                request_id=None,
            )

    sites = BrowserSitesConfig.model_validate(
        {
            "sites": {
                "ifinance": {
                    "site_key": "ifinance",
                    "aliases": ["财司", "财司系统"],
                    "base_url": "http://ifinance.test",
                    "login_url": "http://ifinance.test/login",
                    "allowed_domains": ["ifinance.test"],
                    "login_fields": {"username": "用户名", "password": "密码", "submit": "登录"},
                    "workflows": {},
                }
            }
        }
    )
    controller = AgentController(
        parser=IntentParser(llm_provider=MisclassifyingProvider()),
        task_manager=None,
        tool_executor=None,
        summarizer=None,
        audit_logger=_FakeAuditLogger(),
        session_store=None,
        browser_sites_config=sites,
        credential_ref_resolver=lambda site_key: "ifinance_admin" if site_key == "ifinance" else None,
    )
    task = Task(trace_id="trace", input="登录财司系统", id="task", session_id="session")

    state = controller._intent_parse_node(
        {
            "task": task,
            "session": SimpleNamespace(id="session"),
            "allowed_domains": [],
            "credential_ref": "",
            "browser_trace": False,
            "browser_video": False,
            "browser_site": "",
            "browser_channel": "",
            "browser_slow_mo_ms": 0,
            "progress_callback": None,
        }
    )

    parsed = state["task"]
    assert parsed.intent == "web_action"
    assert parsed.entities["site_key"] == "ifinance"
    assert parsed.entities["credential_ref"] == "ifinance_admin"
    assert parsed.entities["start_url"] == "http://ifinance.test/login"
    assert parsed.entities["allowed_domains"] == ["ifinance.test"]


def test_controller_alias_does_not_override_explicit_database_rpa():
    sites = BrowserSitesConfig.model_validate(
        {
            "sites": {
                "ifinance": {
                    "site_key": "ifinance",
                    "aliases": ["财司", "财司系统"],
                    "base_url": "http://ifinance.test",
                    "login_url": "http://ifinance.test/login",
                    "allowed_domains": ["ifinance.test"],
                    "workflows": {},
                }
            }
        }
    )
    controller = AgentController(
        parser=IntentParser(),
        task_manager=None,
        tool_executor=None,
        summarizer=None,
        audit_logger=_FakeAuditLogger(),
        session_store=None,
        browser_sites_config=sites,
    )
    task = Task(trace_id="trace", input="登录财司系统数据库", id="task", session_id="session")

    state = controller._intent_parse_node(
        {
            "task": task,
            "session": SimpleNamespace(id="session"),
            "allowed_domains": [],
            "credential_ref": "",
            "browser_trace": False,
            "browser_video": False,
            "browser_site": "",
            "browser_channel": "",
            "browser_slow_mo_ms": 0,
            "progress_callback": None,
        }
    )

    parsed = state["task"]
    assert parsed.intent == "rpa_action"
    assert parsed.entities["capability"] == "db"
    assert "site_key" not in parsed.entities
