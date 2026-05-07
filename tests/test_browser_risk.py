from aiops_agent.browser.models import BrowserAction
from aiops_agent.browser.risk import RiskEvaluator


def test_save_artifact_is_safe_even_when_expected_outcome_mentions_save():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="save_artifact",
        expected_outcome="保存最终截图和页面摘要",
        risk_level="safe_read",
    )

    assert evaluator.classify(action) == "safe_read"
    assert evaluator.requires_confirmation(action) is False
