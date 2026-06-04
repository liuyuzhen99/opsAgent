from aiops_agent.browser.action_trace import build_canonical_action_trace, legacy_steps_from_canonical_trace


def test_canonical_action_trace_redacts_secret_values_and_keeps_replay_shape():
    steps = [
        {
            "step_index": 1,
            "action": {
                "type": "type_password",
                "target_hint": "密码",
                "value": "secret-pass",
                "requires_confirmation": False,
                "key": "login.password",
            },
            "result": "success",
            "observation": {
                "url": "http://example.test/login",
                "title": "Login",
                "page_type": "login",
                "screenshot_path": "/tmp/login.png",
                "interactive_elements": [{"role": "input"}],
            },
            "reflection": {"next_decision": "continue"},
        }
    ]

    trace = build_canonical_action_trace(steps, status="completed", task_id="task", session_id="session")

    assert trace["schema_version"] == "opsagent.web_action_trace.v1"
    assert trace["steps"][0]["action"]["value"] == "***"
    assert trace["steps"][0]["requires_confirmation"] is False
    assert trace["steps"][0]["observation"]["element_count"] == 1
    assert trace["artifact_paths"] == ["/tmp/login.png"]

    legacy_steps = legacy_steps_from_canonical_trace(trace)
    assert legacy_steps[0]["action"]["value"] == "***"
    assert legacy_steps[0]["reflection"]["next_decision"] == "continue"
