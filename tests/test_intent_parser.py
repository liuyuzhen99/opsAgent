from aiops_agent.agent.parser import IntentParser
from aiops_agent.config import InspectionConfig, RPAConfig
from aiops_agent.llm.base import IntentClassification
from aiops_agent.llm.client import LLMError


def test_parse_inspection_entities_from_chinese_text():
    parser = IntentParser(
        rpa_config=RPAConfig(
            inspection=InspectionConfig(default_system="WebLogic", default_env="prod")
        )
    )

    result = parser.parse("巡检生产环境 WebLogic")

    assert result.intent == "inspection"
    assert result.entities["system"] == "WebLogic"
    assert result.entities["env"] == "prod"


def test_parse_non_inspection_as_permission_or_qa():
    parser = IntentParser()

    permission_result = parser.parse("给张三开通生产权限")
    qa_result = parser.parse("如何处理 WebLogic 连接池告警")
    chat_result = parser.parse("hello")

    assert permission_result.intent == "permission_change"
    assert qa_result.intent == "ops_qa"
    assert chat_result.intent == "general_chat"


def test_parser_routes_account_role_request_to_web_action():
    parser = IntentParser()

    result = parser.parse("创建账号 alice，分配只读权限")

    assert result.intent == "web_action"
    assert result.entities["workflow"] == "create_user_and_assign_role"
    assert result.entities["workflow_fields"]["username"] == "alice"
    assert result.entities["workflow_fields"]["role"] == "只读权限"


def test_parser_routes_search_user_request_to_read_workflow():
    parser = IntentParser()

    result = parser.parse("查询用户 alice")

    assert result.intent == "web_action"
    assert result.entities["workflow"] == "search_user"
    assert result.entities["workflow_fields"]["username"] == "alice"
    assert result.entities["has_side_effect"] is False


def test_parse_with_llm_when_available():
    class FakeProvider:
        def classify_intent(self, text, defaults):
            assert text == "帮我巡检生产环境 WebLogic"
            assert defaults == {"system": "WebLogic", "env": "prod"}
            return IntentClassification(
                intent="inspection",
                entities={"system": "WebLogic", "env": "prod"},
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                request_id="msg_123",
            )

    parser = IntentParser(
        rpa_config=RPAConfig(
            inspection=InspectionConfig(default_system="WebLogic", default_env="prod")
        ),
        llm_provider=FakeProvider(),
    )

    result = parser.parse("帮我巡检生产环境 WebLogic")

    assert result.intent == "inspection"
    assert result.entities["system"] == "WebLogic"
    assert result.entities["raw_text"] == "帮我巡检生产环境 WebLogic"
    assert result.entities["llm_provider"] == "anthropic"
    assert result.entities["llm_request_id"] == "msg_123"


def test_parse_general_chat_with_llm_when_available():
    class FakeProvider:
        def classify_intent(self, text, defaults):
            assert text == "hello"
            return IntentClassification(
                intent="general_chat",
                entities={},
                provider="openai",
                model="deepseek-chat",
                request_id=None,
            )

    parser = IntentParser(llm_provider=FakeProvider())

    result = parser.parse("hello")

    assert result.intent == "general_chat"
    assert result.entities["raw_text"] == "hello"
    assert result.entities["llm_provider"] == "openai"


def test_parse_falls_back_to_rules_when_llm_fails():
    class BrokenProvider:
        def classify_intent(self, text, defaults):
            raise LLMError("network error")

    parser = IntentParser(
        rpa_config=RPAConfig(
            inspection=InspectionConfig(default_system="WebLogic", default_env="prod")
        ),
        llm_provider=BrokenProvider(),
    )

    result = parser.parse("巡检生产环境 WebLogic")

    assert result.intent == "inspection"
    assert result.entities["system"] == "WebLogic"
    assert result.entities["llm_fallback_used"] is True
    assert result.entities["llm_fallback_error"] == "network error"


# ---------------------------------------------------------------------------
# ops_qa intent routing and reverse-filter tests
# ---------------------------------------------------------------------------

def _rule_parser() -> IntentParser:
    """Parser with no LLM, uses rule fallback only."""
    return IntentParser(
        rpa_config=RPAConfig(
            inspection=InspectionConfig(default_system="WebLogic", default_env="prod")
        )
    )


def test_ops_qa_keywords_route_to_ops_qa():
    parser = _rule_parser()
    cases = [
        "WebLogic OOM 如何排查",
        "排查 JVM 堆满的原因",
        "部署回滚的步骤是什么",
        "WebLogic OOM 怎么处理",
        "告警触发了怎么解决",
        "runbook 在哪里",
    ]
    for text in cases:
        result = parser.parse(text)
        assert result.intent == "ops_qa", f"Expected ops_qa for: {text!r}, got {result.intent}"


def test_ops_qa_reverse_filter_web_action_keywords():
    """Phrases containing both QA keywords and web_action keywords should NOT be ops_qa."""
    parser = _rule_parser()
    cases = [
        "如何访问监控控制台",
        "怎么打开运维页面",
        "如何登录 WebLogic 控制台",
    ]
    for text in cases:
        result = parser.parse(text)
        assert result.intent != "ops_qa", f"Expected non-ops_qa for: {text!r}, got {result.intent}"


def test_ops_qa_extended_keywords():
    parser = _rule_parser()
    assert parser.parse("故障处理手册在哪").intent == "ops_qa"
    assert parser.parse("最佳实践是什么").intent == "ops_qa"
    assert parser.parse("incident 复盘报告").intent == "ops_qa"
