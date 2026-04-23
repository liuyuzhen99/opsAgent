import json

from aiops_agent.cli import create_controller
from aiops_agent.llm.base import IntentClassification


def _write_rpa_config(path, platform_url="http://rpa.example.com", token="secret"):
    path.write_text(
        json.dumps(
            {
                "provider": "yidao",
                "execution_mode": "api",
                "platform_url": platform_url,
                "timeout_seconds": 5,
                "auth": {"type": "bearer", "token": token},
                "inspection": {
                    "default_system": "WebLogic",
                    "default_env": "prod",
                    "flow_map": {"WebLogic": "flow-001"},
                },
                "shadowbot": {
                    "executable_path": "",
                    "robot_uuid": "",
                    "command_timeout_seconds": 10,
                    "result_file": "",
                },
            }
        ),
        encoding="utf-8",
    )


def _write_llm_config(path, enabled=False):
    path.write_text(
        json.dumps(
            {
                "provider": "anthropic",
                "enabled": enabled,
                "base_url": "https://api.anthropic.com",
                "api_key": "llm-secret",
                "model": "claude-sonnet-4-20250514",
                "api_version": "2023-06-01",
                "timeout_seconds": 10,
                "max_retries": 2,
                "max_tokens": 512,
            }
        ),
        encoding="utf-8",
    )


def test_agent_run_success_flow(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout):
        assert req.full_url == "http://rpa.example.com/api/v1/flows/flow-001/run"
        assert timeout == 5
        body = json.loads(req.data.decode("utf-8"))
        assert body["system"] == "WebLogic"
        assert body["env"] == "prod"
        return FakeResponse(
            {
                "success": True,
                "result": "healthy",
                "anomalies": [],
                "operation_log": ["inspection completed"],
            }
        )

    monkeypatch.setattr("aiops_agent.tools.inspection.request.urlopen", fake_urlopen)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("巡检生产环境 WebLogic")

    assert task.status == "success"
    assert task.result["data"]["inspection_result"] == "healthy"
    assert "执行状态：success" in task.report
    assert task.trace_id
    saved_files = list((tmp_path / "storage" / "tasks").glob("*.json"))
    assert len(saved_files) >= 1
    audit_lines = (tmp_path / "storage" / "audit" / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) >= 3


def test_agent_run_config_failure(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path, platform_url="", token="")
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    from aiops_agent.config import ConfigError

    try:
        create_controller(str(config_path), str(llm_config_path))
    except ConfigError as exc:
        assert "RPA platform_url 未设置" in str(exc)
        assert "RPA bearer token 未设置" in str(exc)
        return

    raise AssertionError("expected startup config validation to fail")


def test_agent_uses_llm_parser_before_rule_fallback(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=True)

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeProvider:
        enabled = True

        def classify_intent(self, text, defaults):
            assert text == "帮我看下线上 WebLogic 有没有问题"
            return IntentClassification(
                intent="inspection",
                entities={"system": "WebLogic", "env": "prod"},
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                request_id="msg_test",
            )

    def fake_rpa_urlopen(req, timeout):
        assert req.full_url == "http://rpa.example.com/api/v1/flows/flow-001/run"
        return FakeResponse(
            {
                "success": True,
                "result": "healthy",
                "anomalies": [],
                "operation_log": ["inspection completed"],
            }
        )

    monkeypatch.setattr("aiops_agent.tools.inspection.request.urlopen", fake_rpa_urlopen)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(
        str(config_path),
        str(llm_config_path),
        llm_provider=FakeProvider(),
    )
    task = controller.run("帮我看下线上 WebLogic 有没有问题")

    assert task.status == "success"
    assert task.result["data"]["system"] == "WebLogic"


def test_startup_validation_rejects_invalid_llm_config(tmp_path):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    llm_config_path.write_text(
        json.dumps(
            {
                "provider": "anthropic",
                "enabled": True,
                "api_key": "",
                "model": "",
                "timeout_seconds": 10,
                "max_retries": 2,
                "max_tokens": 512,
            }
        ),
        encoding="utf-8",
    )

    from aiops_agent.config import ConfigError

    try:
        create_controller(str(config_path), str(llm_config_path))
    except ConfigError as exc:
        assert "ANTHROPIC_API_KEY 未设置" in str(exc)
        return

    raise AssertionError("expected startup config validation to fail")


def test_shadowbot_local_mode_launches_on_windows(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "yidao",
                "execution_mode": "shadowbot_local",
                "platform_url": "",
                "timeout_seconds": 5,
                "auth": {"type": "bearer", "token": ""},
                "inspection": {
                    "default_system": "WebLogic",
                    "default_env": "prod",
                    "flow_map": {"WebLogic": "robot-uuid-001"},
                },
                "shadowbot": {
                    "executable_path": "D:\\Program Files\\ShadowBot\\ShadowBot.exe",
                    "robot_uuid": "robot-uuid-001",
                    "command_timeout_seconds": 10,
                    "result_file": "",
                },
            }
        ),
        encoding="utf-8",
    )
    _write_llm_config(llm_config_path, enabled=False)

    class FakeCompletedProcess:
        stdout = ""
        stderr = ""

    def fake_run(command, check, capture_output, text, timeout):
        assert command == [
            "cmd",
            "/c",
            "start",
            "",
            "D:\\Program Files\\ShadowBot\\ShadowBot.exe",
            "shadowbot:Run?robot-uuid=robot-uuid-001",
        ]
        assert check is True
        assert capture_output is True
        assert text is True
        assert timeout == 10
        return FakeCompletedProcess()

    monkeypatch.setattr("aiops_agent.tools.inspection.platform.system", lambda: "Windows")
    monkeypatch.setattr("aiops_agent.tools.inspection.subprocess.run", fake_run)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("巡检生产环境 WebLogic")


def test_permission_change_enters_confirmation_state(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("给张三开通生产权限")

    assert task.status == "awaiting_confirmation"
    assert task.risk_level == "high_risk_change"
    assert "人工确认" in (task.report or "")
    session_files = list((tmp_path / "storage" / "sessions").glob("*.json"))
    assert len(session_files) == 1


def test_web_action_is_policy_blocked(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("帮我做一个网页自动化登录流程")

    assert task.status == "blocked"
    assert "尚未开放自动执行" in task.result["error"]
    audit_lines = (tmp_path / "storage" / "audit" / "events.jsonl").read_text(encoding="utf-8")
    assert "policy_blocked" in audit_lines
