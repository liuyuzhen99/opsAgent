from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from aiops_agent.agent.context import ContextCompressor
from aiops_agent.agent.controller import AgentController
from aiops_agent.agent.memory import LangMemSummaryStrategy, LegacySessionMemoryWriter, SessionMemoryManager
from aiops_agent.agent.runtime import LangGraphRuntime
from aiops_agent.cli import build_session_summary_strategy
from aiops_agent.config import LLMProviderConfig
from aiops_agent.sessions.models import AgentSession
from aiops_agent.tasks.models import Task


class _FakeAuditLogger:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


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


def _task(task_id: str, intent: str, *, data=None, status: str = "success") -> Task:
    task = Task(trace_id=f"trace-{task_id}", input=f"input {task_id}", id=task_id, session_id="session")
    task.intent = intent
    task.status = status
    task.result = {"success": status == "success", "data": data or {}}
    task.report = f"report {task_id}"
    return task


def test_session_memory_manager_syncs_web_namespace_and_trace():
    store = InMemoryStore()
    manager = SessionMemoryManager(store)
    session = AgentSession(id="session")
    task = _task(
        "web-1",
        "web_action",
        data={
            "last_observation": {"url": "http://demo.test/users", "title": "Users", "page_type": "table"},
            "session_state_path": "/tmp/browser-state.json",
            "canonical_action_trace": {
                "schema_version": "opsagent.web_action_trace.v1",
                "steps": [{"action": {"type": "type_password", "value": "secret"}, "result": "success"}],
            },
        },
    )
    task.entities = {"site_key": "demo"}
    session = ContextCompressor().compress(session, task)

    result = manager.sync(session, task)

    assert result["migrated_legacy"] is True
    web = store.get(("sessions", "session", "web"), "context").value
    assert web["browser_state_path"] == "/tmp/browser-state.json"
    assert web["last_url"] == "http://demo.test/users"
    assert web["last_success_task_id"] == "web-1"
    trace = store.get(("sessions", "session", "web"), "trace:web-1").value
    assert trace["canonical_action_trace"]["steps"][0]["action"]["value"] == "***"


def test_session_memory_manager_syncs_knowledge_namespace_and_retrieves_history():
    store = InMemoryStore()
    manager = SessionMemoryManager(store)
    session = AgentSession(id="session")
    task = _task(
        "qa-1",
        "ops_qa",
        data={
            "answer": {
                "answer": "重启步骤如下...",
                "sources": [{"title": "Runbook", "path": "runbooks/weblogic.md"}],
                "confidence": 0.9,
            }
        },
    )
    session = ContextCompressor().compress(session, task)

    manager.sync(session, task)
    memory = manager.retrieve(session, "ops_qa", "重启步骤", fallback={})

    knowledge = store.get(("sessions", "session", "knowledge"), "context").value
    assert knowledge["last_answer_summary"] == "重启步骤如下..."
    assert knowledge["recent_sources"][0]["title"] == "Runbook"
    assert memory["qa_memory"][0]["question"] == "input qa-1"


def test_controller_persist_audit_syncs_langgraph_memory_store():
    store = InMemoryStore()
    runtime = LangGraphRuntime(
        checkpointer=InMemorySaver(),
        store=store,
        checkpoint_backend="memory",
        store_backend="memory",
    )
    audit = _FakeAuditLogger()
    controller = AgentController(
        parser=None,
        task_manager=_FakeTaskManager(),
        tool_executor=None,
        summarizer=None,
        audit_logger=audit,
        session_store=_FakeSessionStore(),
        langgraph_runtime=runtime,
    )
    session = AgentSession(id="session")
    task = _task("qa-2", "ops_qa", data={"answer": {"answer": "A2", "sources": [], "confidence": 1.0}})

    controller._persist_audit_node({"task": task, "session": session, "progress_callback": None})

    knowledge = store.get(("sessions", "session", "knowledge"), "context").value
    assert knowledge["last_answer_summary"] == "A2"
    assert any(event.event_type == "memory.store.synced" for event in audit.events)


def test_legacy_session_writer_can_use_langmem_summary_strategy():
    model = RunnableLambda(lambda _input: AIMessage(content="langmem session summary"))
    writer = LegacySessionMemoryWriter(summary_strategy=LangMemSummaryStrategy(model))
    session = AgentSession(id="session")
    task = _task("qa-3", "ops_qa", data={"answer": {"answer": "A3"}})

    writer.sync(session, task)

    assert session.summary == "langmem session summary"
    assert session.rolling_summary


def test_langmem_summary_strategy_is_built_from_llm_config():
    class Provider:
        enabled = True

        def __init__(self):
            self.called = False

        def build_summary_model(self):
            self.called = True
            return RunnableLambda(lambda _input: AIMessage(content="configured langmem summary"))

    provider = Provider()
    config = LLMProviderConfig(
        enabled=True,
        api_key="secret",
        langmem_summary_enabled=True,
        langmem_max_tokens=777,
        langmem_max_summary_tokens=111,
    )

    strategy = build_session_summary_strategy(config, provider)

    assert isinstance(strategy, LangMemSummaryStrategy)
    assert strategy.max_tokens == 777
    assert strategy.max_summary_tokens == 111
    assert provider.called is True
