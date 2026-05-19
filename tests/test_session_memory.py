from __future__ import annotations

import json
from types import SimpleNamespace

from aiops_agent.agent.context import ContextCompressor
from aiops_agent.agent.controller import AgentController
from aiops_agent.sessions.models import AgentSession, BrowserMemory, PageMemory, QATurn
from aiops_agent.storage.session_store import FileSessionStore
from aiops_agent.tasks.models import ExecutionPlan, Task


class _FakeAuditLogger:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


class _FakePlanningService:
    def __init__(self):
        self.entities = None

    def plan(self, text, intent, entities):
        self.entities = dict(entities)
        return ExecutionPlan(goal=text, steps=["done"], selected_tools=[])


def _task(
    task_id: str,
    intent: str,
    status: str = "success",
    *,
    text: str | None = None,
    entities: dict | None = None,
    data: dict | None = None,
    report: str | None = None,
) -> Task:
    task = Task(trace_id=f"trace-{task_id}", input=text or f"input {task_id}", id=task_id)
    task.intent = intent
    task.status = status
    task.entities = entities or {}
    task.result = {"success": status == "success", "data": data or {}}
    task.report = report or f"report {task_id}"
    return task


def test_session_store_migrates_legacy_memory_and_round_trips(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    qa_turns = [{"question": "Q1", "answer": "A1"}]
    (root / "legacy.json").write_text(
        json.dumps(
            {
                "id": "legacy",
                "task_ids": ["t1"],
                "metadata": {
                    "qa_turns": json.dumps(qa_turns, ensure_ascii=False),
                    "last_url": "http://example.test/last",
                    "last_page_type": "form",
                    "browser_state_path": "/tmp/browser-state.json",
                    "browser_last_success_task_id": "web-1",
                },
                "recent_observations": [
                    {"task_id": "web-1", "url": "http://example.test", "title": "Home"},
                    {"task_id": "web-2"},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = FileSessionStore(root)

    session = store.load("legacy")

    assert isinstance(session, AgentSession)
    assert isinstance(session.qa_memory[0], QATurn)
    assert session.qa_memory[0].question == "Q1"
    assert isinstance(session.browser_memory.recent_pages[0], PageMemory)
    assert session.browser_memory.recent_pages[0].page_type == ""
    assert session.browser_memory.recent_pages[1].url == ""
    assert session.browser_memory.last_url == "http://example.test/last"
    assert session.browser_memory.last_page_type == "form"
    assert session.browser_memory.state_path == "/tmp/browser-state.json"
    assert session.browser_memory.last_success_task_id == "web-1"

    store.save(session)
    reloaded = store.load("legacy")

    assert isinstance(reloaded.browser_memory, BrowserMemory)
    assert isinstance(reloaded.browser_memory.recent_pages[0], PageMemory)
    assert isinstance(reloaded.qa_memory[0], QATurn)


def test_session_store_ignores_bad_memory_shapes(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "bad.json").write_text(
        json.dumps(
            {
                "id": "bad",
                "metadata": {"qa_turns": "{not json"},
                "recent_observations": [{"url": "http://example.test"}],
                "short_term": "wrong",
                "browser_memory": "wrong",
                "qa_memory": "wrong",
                "task_index": "wrong",
            }
        ),
        encoding="utf-8",
    )

    session = FileSessionStore(root).load("bad")

    assert session.qa_memory == []
    assert session.short_term == []
    assert session.task_index == []
    assert isinstance(session.browser_memory, BrowserMemory)
    assert session.browser_memory.recent_pages[0].task_id == ""
    assert session.browser_memory.recent_pages[0].page_type == ""


def test_context_compressor_updates_structured_memory_and_legacy_fields():
    class SummaryProvider:
        enabled = True

        def summarize_session_memory(self, memory_facts):
            dumped = json.dumps(memory_facts, ensure_ascii=False)
            assert "state_path" not in dumped
            assert "/tmp/browser-state.json" not in dumped
            assert "secret-token" not in dumped
            return "LLM session summary"

    session = AgentSession()
    task = _task(
        "web-1",
        "web_action",
        entities={"site_key": "demo"},
        data={
            "last_observation": {"url": "http://demo.test/users", "title": "Users", "page_type": "table"},
            "session_state_path": "/tmp/browser-state.json",
            "steps": [{"action": {"type": "click"}, "result": "success"}],
        },
        report="opened users page token=secret-token",
    )

    session = ContextCompressor(llm_provider=SummaryProvider()).compress(session, task)

    assert session.summary == "LLM session summary"
    assert session.short_term[-1].task_id == "web-1"
    assert session.task_index[-1].site_key == "demo"
    assert session.browser_memory.recent_pages[-1].title == "Users"
    assert session.browser_memory.state_path == "/tmp/browser-state.json"
    assert session.browser_memory.last_success_task_id == "web-1"
    assert session.browser_memory.last_success_site_key == "demo"
    assert session.recent_observations[-1]["url"] == "http://demo.test/users"
    assert session.metadata["last_url"] == "http://demo.test/users"
    assert session.metadata["browser_state_path"] == "/tmp/browser-state.json"
    assert session.metadata["browser_last_success_task_id"] == "web-1"
    assert "最近任务=web_action:success" in session.rolling_summary


def test_context_compressor_caps_memory_and_updates_qa_only_on_success():
    session = AgentSession()
    compressor = ContextCompressor()

    for index in range(12):
        session = compressor.compress(session, _task(f"task-{index}", "general_chat"))
    assert len(session.short_term) == 10
    assert session.short_term[0].task_id == "task-2"

    for index in range(55):
        session = compressor.compress(session, _task(f"idx-{index}", "inspection"))
    assert len(session.task_index) == 50
    assert session.task_index[0].task_id == "idx-5"

    failed = _task(
        "qa-fail",
        "ops_qa",
        status="failed",
        data={"answer": {"answer": "should not persist"}},
    )
    session = compressor.compress(session, failed)
    assert all(turn.task_id != "qa-fail" for turn in session.qa_memory)

    for index in range(6):
        session = compressor.compress(
            session,
            _task(
                f"qa-{index}",
                "ops_qa",
                text=f"Q{index}",
                data={"answer": {"answer": f"A{index}"}},
            ),
        )
    assert len(session.qa_memory) == 5
    assert session.qa_memory[0].question == "Q1"
    legacy_turns = json.loads(session.metadata["qa_turns"])
    assert len(legacy_turns) == 5
    assert legacy_turns[-1]["answer"] == "A5"


def test_context_compressor_summary_falls_back_when_llm_unavailable():
    providers = [
        SimpleNamespace(enabled=False, summarize_session_memory=lambda facts: "unused"),
        SimpleNamespace(enabled=True),
        SimpleNamespace(enabled=True, summarize_session_memory=lambda facts: "   "),
    ]

    class RaisingProvider:
        enabled = True

        def summarize_session_memory(self, facts):
            raise RuntimeError("boom")

    providers.append(RaisingProvider())

    for index, provider in enumerate(providers):
        session = ContextCompressor(llm_provider=provider).compress(
            AgentSession(),
            _task(f"fallback-{index}", "inspection", text="巡检 WebLogic"),
        )
        assert "最近任务是 inspection" in session.summary
        assert session.short_term[-1].task_id == f"fallback-{index}"


def test_context_retrieve_ranks_current_session_memory_only():
    compressor = ContextCompressor()
    session = AgentSession()
    session = compressor.compress(
        session,
        _task("inspect-1", "inspection", text="检查 WebLogic prod", entities={"system": "WebLogic", "env": "prod"}),
    )
    session = compressor.compress(
        session,
        _task("inspect-2", "inspection", text="检查 Tomcat test", entities={"system": "Tomcat", "env": "test"}),
    )
    session = compressor.compress(
        session,
        _task(
            "web-1",
            "web_action",
            text="查询 demo 用户",
            entities={"site_key": "demo"},
            data={"last_observation": {"url": "http://demo.test", "title": "Demo", "page_type": "content"}},
        ),
    )

    inspection = compressor.retrieve(session, "inspection", "WebLogic prod 状态", limit=2)
    web = compressor.retrieve(session, "web_action", "demo 用户", limit=2)

    assert inspection["task_matches"][0]["task_id"] == "inspect-1"
    assert web["task_matches"][0]["task_id"] == "web-1"
    assert web["browser_memory"]["last_success_task_id"] == "web-1"
    assert "summary" in web and "rolling_summary" in web


def test_task_plan_prefers_qa_memory_and_falls_back_to_legacy_metadata():
    planning = _FakePlanningService()
    controller = AgentController(
        parser=None,
        task_manager=None,
        tool_executor=None,
        summarizer=None,
        audit_logger=_FakeAuditLogger(),
        session_store=None,
        planning_service=planning,
    )
    task = _task("qa-plan", "ops_qa")
    session = SimpleNamespace(
        id="session",
        qa_memory=[QATurn(task_id="new", question="new Q", answer="new A", created_at="now")],
        metadata={"qa_turns": json.dumps([{"question": "old Q", "answer": "old A"}])},
    )

    controller._task_plan_node({"task": task, "session": session, "progress_callback": None})

    assert planning.entities["conversation_history"] == [{"question": "new Q", "answer": "new A"}]

    legacy_planning = _FakePlanningService()
    controller.planning_service = legacy_planning
    task = _task("qa-plan-2", "knowledge_write")
    session.qa_memory = []

    controller._task_plan_node({"task": task, "session": session, "progress_callback": None})

    assert legacy_planning.entities["conversation_history"] == [{"question": "old Q", "answer": "old A"}]


def test_persist_audit_delegates_qa_memory_write_to_compressor():
    class _FakeTaskManager:
        def __init__(self):
            self.persisted = []

        def persist(self, task):
            self.persisted.append(task.id)

    class _FakeSessionStore:
        def __init__(self):
            self.saved = []

        def save(self, session):
            self.saved.append(session.id)

    class _FakeCompressor:
        def compress(self, session, task):
            assert "qa_turns" not in session.metadata
            session.metadata["compressed"] = "yes"
            return session

    task_manager = _FakeTaskManager()
    session_store = _FakeSessionStore()
    controller = AgentController(
        parser=None,
        task_manager=task_manager,
        tool_executor=None,
        summarizer=None,
        audit_logger=_FakeAuditLogger(),
        session_store=session_store,
        context_compressor=_FakeCompressor(),
    )
    session = AgentSession(id="session")
    task = _task("qa-done", "ops_qa", data={"answer": {"answer": "A"}})

    controller._persist_audit_node({"task": task, "session": session, "progress_callback": None})

    assert session.metadata["compressed"] == "yes"
    assert task_manager.persisted == ["qa-done"]
    assert session_store.saved == ["session"]


def test_save_web_skill_prefers_browser_memory_over_legacy_metadata():
    class _FakeTaskManager:
        def __init__(self, tasks):
            self.tasks = tasks

        def load(self, task_id):
            return self.tasks.get(task_id)

    class _FakeSessionStore:
        def __init__(self, session):
            self.session = session

        def load(self, session_id):
            return self.session

    class _FakeGenerator:
        def generate_from_task(self, task, name=None):
            return SimpleNamespace(task_id=task.id, name=name)

    preferred = _task("preferred", "web_action")
    legacy = _task("legacy", "web_action")
    session = SimpleNamespace(
        id="session",
        browser_memory=BrowserMemory(last_success_task_id="preferred"),
        metadata={"browser_last_success_task_id": "legacy"},
    )
    controller = AgentController(
        parser=None,
        task_manager=_FakeTaskManager({"preferred": preferred, "legacy": legacy}),
        tool_executor=None,
        summarizer=None,
        audit_logger=_FakeAuditLogger(),
        session_store=_FakeSessionStore(session),
        web_skill_generator=_FakeGenerator(),
    )

    result = controller.save_web_skill("session", name="skill")

    assert result.task_id == "preferred"
    assert result.name == "skill"
