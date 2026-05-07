from aiops_agent.browser.models import BrowserObservation, BrowserTaskSpec, InteractiveElement
from aiops_agent.browser.planner import BrowserPlanner


def test_planner_opens_then_observes_then_extracts_from_latest_observation():
    planner = BrowserPlanner()
    spec = BrowserTaskSpec(
        start_url="http://example.test/users",
        user_goal="读取用户权限信息",
        max_steps=5,
    )

    first = planner.next_action(spec, None, [])
    assert first.type == "open_url"
    assert first.value == "http://example.test/users"

    opened_steps = [
        {
            "action": {"type": "open_url"},
            "result": "success",
            "observation": {"url": spec.start_url, "title": "Users", "page_type": "interactive"},
        }
    ]
    second = planner.next_action(spec, BrowserObservation(url=spec.start_url), opened_steps)
    assert second.type == "observe_page"

    observed_steps = opened_steps + [
        {
            "action": {"type": "observe_page"},
            "result": "success",
            "observation": {"url": spec.start_url, "title": "Users", "page_type": "content"},
        }
    ]
    third = planner.next_action(spec, BrowserObservation(page_type="content"), observed_steps)
    assert third.type == "extract_text"


def test_planner_fills_local_draft_field_before_extracting_when_goal_requests_input():
    planner = BrowserPlanner()
    spec = BrowserTaskSpec(
        start_url="http://example.test/form",
        user_goal="填写用户名为zhangsan后读取页面",
    )
    observation = BrowserObservation(
        url=spec.start_url,
        page_type="form",
        interactive_elements=[InteractiveElement(element_id="user", role="input", name="用户名")],
    )
    steps = [
        {"action": {"type": "open_url"}, "result": "success", "observation": {}},
        {"action": {"type": "observe_page"}, "result": "success", "observation": {}},
    ]

    action = planner.next_action(spec, observation, steps)

    assert action.type == "type"
    assert action.target_hint == "用户名"
    assert action.value == "zhangsan"
    assert action.risk_level == "safe_local_edit"


def test_planner_blocks_remote_mutation_without_opening_browser_when_site_is_missing():
    planner = BrowserPlanner()
    spec = BrowserTaskSpec(
        start_url=None,
        user_goal="保存权限设置",
        requires_remote_mutation=True,
    )

    action = planner.next_action(spec, None, [])

    assert action.type == "click"
    assert action.requires_confirmation is True
    assert action.risk_level == "unsafe_mutation"


def test_planner_generates_login_steps_from_login_observation():
    planner = BrowserPlanner()
    spec = BrowserTaskSpec(
        start_url="http://example.test/login",
        user_goal="登录后读取页面",
        requires_login=True,
        credential_username="alice",
        credential_password="secret",
    )
    observation = BrowserObservation(
        page_type="login",
        interactive_elements=[
            InteractiveElement(element_id="user", role="input", name="用户名", input_type="text"),
            InteractiveElement(element_id="pass", role="input", name="密码", input_type="password"),
            InteractiveElement(element_id="login", role="button", text="登录"),
        ],
    )
    steps = [
        {"action": {"type": "open_url"}, "result": "success", "observation": {}},
        {"action": {"type": "observe_page"}, "result": "success", "observation": {}},
    ]

    username_action = planner.next_action(spec, observation, steps)
    password_action = planner.next_action(
        spec,
        observation,
        steps + [{"action": {"type": "type_username"}, "result": "success", "observation": {}}],
    )
    submit_action = planner.next_action(
        spec,
        observation,
        steps
        + [
            {"action": {"type": "type_username"}, "result": "success", "observation": {}},
            {"action": {"type": "type_password"}, "result": "success", "observation": {}},
        ],
    )

    assert username_action.type == "type_username"
    assert username_action.value == "alice"
    assert password_action.type == "type_password"
    assert password_action.value == "secret"
    assert submit_action.type == "login_submit"
