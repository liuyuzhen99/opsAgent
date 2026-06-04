from types import SimpleNamespace

from langchain_core.documents import Document

from aiops_agent.agent.knowledge_subgraph import KnowledgeSubgraph
from aiops_agent.agent.runtime import LangGraphRuntime, LangGraphRuntimeConfig
from aiops_agent.cli import create_controller
from aiops_agent.storage.langgraph_store import FileBackedStore
from aiops_agent.support.trace import set_trace_id
from aiops_agent.tasks.models import Task, ToolCallSpec, ToolExecutionResult
from aiops_agent.tools.knowledge import KnowledgeAnswer, KnowledgeSource
from tests.test_agent_flow import _write_llm_config, _write_rpa_config


def test_file_backed_store_persists_and_searches_items(tmp_path):
    store = FileBackedStore(tmp_path / "store")
    namespace = ("sessions", "session-1", "web")

    store.put(namespace, "browser", {"last_url": "http://example.test/users", "site_key": "demo"})
    reloaded = FileBackedStore(tmp_path / "store")

    item = reloaded.get(namespace, "browser")
    assert item is not None
    assert item.value["last_url"] == "http://example.test/users"

    results = reloaded.search(("sessions", "session-1"), query="users")
    assert results[0].namespace == namespace
    assert results[0].key == "browser"

    namespaces = reloaded.list_namespaces(prefix=("sessions",), max_depth=2)
    assert namespaces == [("sessions", "session-1")]


def test_langgraph_runtime_uses_file_store_with_checkpoint_fallback(tmp_path):
    runtime = LangGraphRuntime.from_config(
        LangGraphRuntimeConfig(
            checkpoint_path=tmp_path / "checkpoints.sqlite",
            store_path=tmp_path / "store",
        )
    )

    assert runtime.store_backend == "file"
    assert runtime.checkpoint_backend in {"memory", "sqlite"}


def test_controller_stream_run_emits_events_and_keeps_state(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    set_trace_id("trace-stream-test")
    events = list(controller.stream_run("给张三开通生产权限"))

    stages = [event.stage for event in events]
    assert stages[:3] == ["graph.started", "session.created", "task.created"]
    assert "intent.parsed" in stages
    assert "plan.generated" in stages
    assert "graph.interrupted" in stages
    assert stages[-1] == "task.completed"
    assert events[-1].details["graph"] == "main"
    assert events[-1].details["node"] == "persist_audit"
    assert events[-1].details["trace_id"] == "trace-stream-test"

    task_id = events[-1].task_id
    task = controller.task_manager.load(task_id)
    assert task.status == "awaiting_confirmation"
    assert task.id == task_id

    state = controller.get_state(task_id)
    assert state.config["configurable"]["thread_id"] == task_id
    assert state.interrupts
    assert state.interrupts[0].value["confirmation_type"] == "plan"
    assert state.interrupts[0].value["resume_node"] == "policy_check"


def test_plan_confirmation_resume_clears_langgraph_interrupt(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    task = controller.run("给张三开通生产权限")

    interrupted_state = controller.get_state(task.id)
    assert interrupted_state.interrupts

    confirmed = controller.confirm(task.id)

    assert confirmed.status == "blocked"
    assert confirmed.result["data"]["block_reason"] == "confirmed_without_executable_tool"
    resumed_state = controller.get_state(task.id)
    assert not resumed_state.interrupts
    assert resumed_state.next == ()


def test_controller_stream_run_emits_knowledge_events_before_summary(tmp_path, monkeypatch):
    config_path = tmp_path / "rpa.json"
    llm_config_path = tmp_path / "llm.json"
    _write_rpa_config(config_path)
    _write_llm_config(llm_config_path, enabled=False)
    monkeypatch.chdir(tmp_path)

    controller = create_controller(str(config_path), str(llm_config_path))
    events = list(controller.stream_run("如何处理 WebLogic 连接池告警"))
    stages = [event.stage for event in events]

    assert stages.index("knowledge.sources.ready") < stages.index("knowledge.answer.ready")
    assert stages.index("knowledge.answer.ready") < stages.index("summary.ready")
    answer_event = events[stages.index("knowledge.answer.ready")]
    assert answer_event.details["node"] == "route_execution"
    assert answer_event.details["missing_info"] == ["knowledge.vault_path"]

    task_id = events[-1].task_id
    subgraph_state = controller.knowledge_subgraph.get_state(task_id)
    assert subgraph_state.config["configurable"]["thread_id"] == f"{task_id}:knowledge"
    assert subgraph_state.values["branch"] == "qa"
    assert subgraph_state.values["rewritten_query"] == "如何处理 WebLogic 连接池告警"


def test_knowledge_subgraph_retries_retryable_qa_result():
    class _Executor:
        def __init__(self):
            self.calls = 0

        def execute(self, call_spec):
            self.calls += 1
            if self.calls == 1:
                return ToolExecutionResult(success=False, data={"status": "retryable_failure"}, error="temporary", retryable=True)
            return ToolExecutionResult(
                success=True,
                data={"answer": {"answer": "A", "sources": [], "confidence": 1.0}},
            )

    runtime = LangGraphRuntime.from_config(LangGraphRuntimeConfig(in_memory_checkpointer=True, in_memory_store=True))
    executor = _Executor()
    subgraph = KnowledgeSubgraph(executor, runtime)
    task = Task(trace_id="trace", input="Q", id="qa", session_id="session")
    task.intent = "ops_qa"

    result = subgraph.run(task, ToolCallSpec(tool_name="knowledge", action="query", params={"question": "Q"}))

    assert result.success is True
    assert result.data["retry_attempts"] == 1
    assert executor.calls == 2


def test_knowledge_subgraph_does_not_retry_write_result():
    class _Executor:
        def __init__(self):
            self.calls = 0

        def execute(self, call_spec):
            self.calls += 1
            return ToolExecutionResult(success=False, data={"status": "retryable_failure"}, error="write failed", retryable=True)

    runtime = LangGraphRuntime.from_config(LangGraphRuntimeConfig(in_memory_checkpointer=True, in_memory_store=True))
    executor = _Executor()
    subgraph = KnowledgeSubgraph(executor, runtime)
    task = Task(trace_id="trace", input="write", id="write", session_id="session")
    task.intent = "knowledge_write"

    result = subgraph.run(task, ToolCallSpec(tool_name="knowledge_writer", action="write", params={"instruction": "write"}))

    assert result.success is False
    assert executor.calls == 1


def test_knowledge_subgraph_native_qa_retries_retrieve_stage(tmp_path):
    class _Engine:
        def __init__(self):
            self.config = SimpleNamespace(enable_eval=False)
            self.retrieve_calls = 0

        def rewrite_query(self, question, history):
            return f"rewritten {question}"

        def retrieve_documents(self, question):
            self.retrieve_calls += 1
            if self.retrieve_calls == 1:
                raise RuntimeError("temporary retrieve failure")
            return [
                Document(
                    page_content="restart weblogic by running the runbook",
                    metadata={"title": "Runbook", "source": str(tmp_path / "runbook.md"), "rel_path": "runbooks/runbook.md"},
                )
            ]

        def synthesize_answer(self, question, docs, history):
            return KnowledgeAnswer(
                answer="Use the runbook.",
                sources=[KnowledgeSource(title="Runbook", path=str(tmp_path / "runbook.md"))],
                confidence=1.0,
            )

    class _Tool:
        def __init__(self):
            self.config = SimpleNamespace(vault_path=str(tmp_path))
            self._llm_config = SimpleNamespace(enabled=True)
            self.engine = _Engine()

    class _Registry:
        def __init__(self, tool):
            self.tool = tool

        def get(self, name):
            return SimpleNamespace(tool=self.tool)

    class _Executor:
        def __init__(self, tool):
            self.registry = _Registry(tool)
            self.calls = 0

        def execute(self, call_spec):
            self.calls += 1
            raise AssertionError("native QA path should not call legacy executor")

    tool = _Tool()
    runtime = LangGraphRuntime.from_config(LangGraphRuntimeConfig(in_memory_checkpointer=True, in_memory_store=True))
    subgraph = KnowledgeSubgraph(_Executor(tool), runtime)
    task = Task(trace_id="trace", input="Q", id="native-qa", session_id="session")
    task.intent = "ops_qa"

    result = subgraph.run(task, ToolCallSpec(tool_name="knowledge", action="query", params={"question": "Q"}))

    assert result.success is True
    assert result.data["rewritten_query"] == "rewritten Q"
    assert result.data["answer"]["answer"] == "Use the runbook."
    assert result.data["retry_state"]["retrieve"]["attempts"] == 1
    assert tool.engine.retrieve_calls == 2
