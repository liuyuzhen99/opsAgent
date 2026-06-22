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


def test_hover_is_safe_local_interaction():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="hover",
        target_hint="删除菜单",
        expected_outcome="展开更多操作菜单",
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


def test_login_secret_actions_stay_safe_even_if_llm_marks_unsafe():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="type_password",
        target_hint="Password",
        value="secret",
        risk_level="unsafe_mutation",
        requires_confirmation=True,
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


def test_click_review_still_requires_confirmation():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="复核",
        expected_outcome="确认复核所有待复核数据",
    )

    assert evaluator.classify(action) == "unsafe_mutation"
    assert evaluator.requires_confirmation(action) is True


def test_click_review_that_only_opens_confirmation_dialog_is_safe_preflight():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="复核",
        expected_outcome="点击复核按钮，仅弹出复核确认对话框，不提交复核操作",
        risk_level="unsafe_mutation",
        requires_confirmation=True,
    )

    assert evaluator.classify(action) == "safe_local_edit"
    assert evaluator.requires_confirmation(action) is False


def test_speculative_confirmation_dialog_does_not_downgrade_mutation():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="复核",
        expected_outcome="点击复核按钮，触发复核操作，可能弹出确认对话框",
        risk_level="unsafe_mutation",
    )

    assert evaluator.classify(action) == "unsafe_mutation"
    assert evaluator.requires_confirmation(action) is True


def test_final_review_confirmation_still_requires_confirmation():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="确定",
        expected_outcome="确认提交复核操作",
        risk_level="unsafe_mutation",
    )

    assert evaluator.classify(action) == "unsafe_mutation"
    assert evaluator.requires_confirmation(action) is True


def test_parent_navigation_with_review_only_in_expected_outcome_is_safe():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="用户信息管理",
        expected_outcome="展开用户信息管理子菜单，显示网银用户复核等子项",
    )

    assert evaluator.classify(action) == "safe_local_edit"
    assert evaluator.requires_confirmation(action) is False


def test_review_menu_navigation_is_safe():
    evaluator = RiskEvaluator()
    action = BrowserAction(
        type="click",
        target_hint="网银用户复核",
        expected_outcome="进入网银用户复核列表页面",
    )

    assert evaluator.classify(action) == "safe_local_edit"
    assert evaluator.requires_confirmation(action) is False
