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


def test_authorization_unit_filter_is_not_treated_as_remote_mutation():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="授权单位",
        expected_outcome="授权单位下拉列表展开，显示可选单位列表",
    )

    assert evaluator.classify(action) == "safe_local_edit"
    assert evaluator.requires_confirmation(action) is False


def test_press_escape_is_safe_even_when_expected_outcome_mentions_remove_overlay():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="press",
        value="Escape",
        expected_outcome="Close the select2 dropdown and remove the overlay mask",
    )

    assert evaluator.classify(action) == "safe_local_edit"
    assert evaluator.requires_confirmation(action) is False


def test_create_date_field_input_is_not_treated_as_remote_mutation():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="type",
        target_hint="transCreateDateStart",
        value="2026-05-13",
        expected_outcome="Start date field filled with 2026-05-13",
    )

    assert evaluator.classify(action) == "safe_local_edit"
    assert evaluator.requires_confirmation(action) is False


def test_click_create_date_field_is_not_treated_as_remote_mutation():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="指令创建日期： * 至： *",
        expected_outcome="点击开始日期输入框，使其获得焦点，准备选择日期。",
    )

    assert evaluator.classify(action) == "safe_local_edit"
    assert evaluator.requires_confirmation(action) is False


def test_click_save_still_requires_confirmation():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="保存",
        expected_outcome="保存权限设置",
    )

    assert evaluator.classify(action) == "unsafe_mutation"
    assert evaluator.requires_confirmation(action) is True
