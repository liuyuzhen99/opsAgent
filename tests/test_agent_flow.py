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
                "rpa_actions": {
                    "targets": {
                        "120.13": {
                            "ssh": "ssh-flow-12013",
                            "sftp": "sftp-flow-12013",
                        },
                        "120.11": {
                            "ssh": "ssh-flow-12011",
                            "sftp": "sftp-flow-12011",
                            "db": "db-flow-12011",
                        },
                    }
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

    monkeypatch.setattr("aiops_agent.tools.rpa_runner.request.urlopen", fake_urlopen)
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


def test_rpa_action_login_calls_configured_flow_api(tmp_path, monkeypatch):
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
        assert req.full_url == "http://rpa.example.com/api/v1/flows/ssh-flow-12013/run"
        assert timeout == 5
        body = json.loads(req.data.decode("utf-8"))
        assert body["target"] == "120.13"
        assert body["capability"] == "ssh"
        assert body["operation"] == "login"
        return FakeResponse(
            {
                "success": True,
                "result": "completed",
                "operation_log": ["ssh login launched"],
            }
        )

    monkeypatch.setattr("aiops_agent.tools.rpa_runner.request.urlopen", fake_urlopen)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("登录 120.13 ssh")

    assert task.status == "success"
    assert task.intent == "rpa_action"
    assert task.result["data"]["target"] == "120.13"
    assert task.result["data"]["capability"] == "ssh"
    assert task.result["data"]["flow_id"] == "ssh-flow-12013"
    assert "已启动目标 120.13 的 ssh 登录 RPA" in (task.report or "")


def test_rpa_action_missing_capability_returns_clear_error(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("登录 120.13 数据库")

    assert task.status == "failed"
    assert task.result["error"] == "配置缺失: 未配置 120.13 的 db 登录 RPA"
    assert "RPA 登录启动失败" in (task.report or "")


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

    monkeypatch.setattr("aiops_agent.tools.rpa_runner.request.urlopen", fake_rpa_urlopen)
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

    monkeypatch.setattr("aiops_agent.tools.rpa_runner.platform.system", lambda: "Windows")
    monkeypatch.setattr("aiops_agent.tools.rpa_runner.subprocess.run", fake_run)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("巡检生产环境 WebLogic")


def test_rpa_action_shadowbot_local_uses_target_flow_uuid(tmp_path, monkeypatch):
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
                    "flow_map": {"WebLogic": "inspection-flow"},
                },
                "rpa_actions": {
                    "targets": {
                        "120.13": {
                            "ssh": "ssh-flow-12013",
                            "sftp": "sftp-flow-12013",
                        }
                    }
                },
                "shadowbot": {
                    "executable_path": "D:\\Program Files\\ShadowBot\\ShadowBot.exe",
                    "robot_uuid": "global-robot-uuid",
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
            "shadowbot:Run?robot-uuid=sftp-flow-12013",
        ]
        assert check is True
        assert capture_output is True
        assert text is True
        assert timeout == 10
        return FakeCompletedProcess()

    monkeypatch.setattr("aiops_agent.tools.rpa_runner.platform.system", lambda: "Windows")
    monkeypatch.setattr("aiops_agent.tools.rpa_runner.subprocess.run", fake_run)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("打开 120.13 的 sftp")

    assert task.status == "success"
    assert task.result["data"]["action_result"] == "launched"
    assert task.result["data"]["flow_id"] == "sftp-flow-12013"


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
    assert task.result["data"]["status"] == "awaiting_confirmation"
    assert task.result["data"]["confirmation_type"] == "plan"
    assert task.result["data"]["confirmation_summary"]["prepared_action"]
    assert task.result["data"]["pending_tool_calls"] == []
    assert "人工确认" in (task.report or "")
    session_files = list((tmp_path / "storage" / "sessions").glob("*.json"))
    assert len(session_files) == 1


def test_permission_change_confirm_without_tool_becomes_blocked(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("给张三开通生产权限")
    confirmed = controller.confirm(task.id)

    assert confirmed.status == "blocked"
    assert confirmed.result["data"]["block_reason"] == "confirmed_without_executable_tool"
    assert confirmed.result["data"]["confirmation"]["confirmed"] is True
    assert "当前任务没有可执行工具" in (confirmed.report or "")
    audit_lines = (tmp_path / "storage" / "audit" / "events.jsonl").read_text(encoding="utf-8")
    assert "confirmation.confirmed" in audit_lines
    assert "task_completed" in audit_lines


def test_require_confirmation_resumes_tool_execution_after_confirm(tmp_path, monkeypatch):
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
        return FakeResponse(
            {
                "success": True,
                "result": "healthy",
                "anomalies": [],
                "operation_log": ["inspection completed"],
            }
        )

    monkeypatch.setattr("aiops_agent.tools.rpa_runner.request.urlopen", fake_urlopen)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("巡检生产环境 WebLogic", require_confirmation=True)

    assert task.status == "awaiting_confirmation"
    assert task.result["data"]["confirmation_type"] == "plan"
    assert len(task.result["data"]["pending_tool_calls"]) == 1

    confirmed = controller.confirm(task.id)

    assert confirmed.status == "success"
    assert confirmed.result["data"]["inspection_result"] == "healthy"


def test_web_action_side_effect_enters_confirmation_state(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("帮我在网页里保存权限设置")

    assert task.status == "awaiting_confirmation"
    assert "人工确认" in (task.report or "")
    assert task.result["data"]["status"] == "awaiting_confirmation"
    audit_lines = (tmp_path / "storage" / "audit" / "events.jsonl").read_text(encoding="utf-8")
    assert "action.blocked_for_confirmation" in audit_lines


def test_ops_qa_uses_knowledge_contract(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("如何处理 WebLogic 连接池告警")

    assert task.status == "success"
    assert task.result["data"]["answer"]["missing_info"] == ["knowledge.vault_path"]


def test_knowledge_write_runs_full_controller_flow_and_redacts_audit(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _write_rpa_config(config_path)
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["knowledge"] = {
        "vault_path": str(vault_path),
        "index_mode": "keyword",
        "exclude_patterns": [".obsidian/**", "archive/**", "secrets/**"],
    }
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    _write_llm_config(llm_config_path, enabled=True)
    monkeypatch.chdir(tmp_path)

    def fake_draft(self, instruction, history, *, default_system=None, default_env=None):
        return {
            "title": "连接池告警处理",
            "aliases": ["连接池告警"],
            "system": default_system or "WebLogic",
            "type": "runbooks",
            "env": default_env or "prod",
            "severity": "P3",
            "tags": ["weblogic"],
            "summary": "WebLogic 连接池告警处理方法",
            "body": "## 处理步骤\n\n1. 查看连接池状态\n2. 释放异常连接",
            "related_links": [],
        }

    monkeypatch.setattr("aiops_agent.knowledge.writer.KnowledgeNoteWriter._draft_with_llm", fake_draft)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("请把刚才的 WebLogic 连接池告警处理方法记录到知识库")

    assert task.status == "success"
    assert task.intent == "knowledge_write"
    assert "知识笔记已写入" in (task.report or "")
    assert (vault_path / "runbooks" / "WebLogic - 连接池告警处理.md").exists()
    audit_text = (tmp_path / "storage" / "audit" / "events.jsonl").read_text(encoding="utf-8")
    assert "knowledge_write.completed" in audit_text
    assert "刚才的 WebLogic 连接池告警处理方法" not in audit_text
    assert "查看连接池状态" not in audit_text


def test_general_chat_uses_chat_tool(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=True)
    monkeypatch.chdir(tmp_path)

    class FakeProvider:
        enabled = True

        def classify_intent(self, text, defaults):
            assert text == "hello"
            return IntentClassification(
                intent="general_chat",
                entities={},
                provider="openai",
                model="deepseek-chat",
            )

        def generate_chat_reply(self, text, context=None):
            assert text == "hello"
            assert context["current_date"]
            assert context["timezone"] == "Asia/Shanghai"
            return "你好，我是 opsAgent。"

    controller = create_controller(str(config_path), str(llm_config_path), llm_provider=FakeProvider())
    task = controller.run("hello")

    assert task.status == "success"
    assert task.intent == "general_chat"
    assert task.result["data"]["reply"] == "你好，我是 opsAgent。"
    assert task.report == "你好，我是 opsAgent。"
