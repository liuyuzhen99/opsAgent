import json

from pydantic import ValidationError

from aiops_agent.browser.llm_planner import BrowserPlannerOutput
from aiops_agent.browser.site_config import BrowserSitesConfig, load_browser_sites_config


def test_browser_sites_config_loads_and_defaults_allowed_domain(tmp_path):
    path = tmp_path / "browser_sites.json"
    path.write_text(
        json.dumps(
            {
                "sites": {
                    "demo": {
                        "site_key": "demo",
                        "base_url": "http://example.test",
                        "workflows": {
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
