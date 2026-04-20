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

    assert permission_result.intent == "permission_change"
    assert qa_result.intent == "ops_qa"


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
