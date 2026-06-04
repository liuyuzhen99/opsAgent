from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, TypedDict

from langchain_core.documents import Document
from langgraph.graph import END, StateGraph

from aiops_agent.agent.runtime import LangGraphRuntime
from aiops_agent.agent.state_codec import (
    task_from_state,
    task_to_state,
    tool_call_from_state,
    tool_call_to_state,
    tool_result_to_state,
    to_plain,
)
from aiops_agent.tasks.models import Task, TaskArtifact, ToolCallSpec, ToolExecutionResult
from aiops_agent.tools.executor import ToolExecutor
from aiops_agent.tools.knowledge import KnowledgeAnswer, KnowledgeSource

MAX_KNOWLEDGE_RETRIES = 2
_MISSING = object()


class KnowledgeSubgraphState(TypedDict, total=False):
    task: Task
    call_spec: ToolCallSpec
    branch: str
    native_qa: bool
    question: str
    instruction: str
    conversation_messages: list[dict]
    rewritten_query: str
    retrieved_docs: list[dict]
    sources: list[dict]
    answer: dict
    evaluation: dict
    write_result: dict
    knowledge_memory_context: dict
    running_summary: str
    tool_result: dict
    events: list[dict]
    retry_state: dict


class KnowledgeSubgraph:
    def __init__(self, tool_executor: ToolExecutor, runtime: LangGraphRuntime):
        self.tool_executor = tool_executor
        self.runtime = runtime
        self.graph = self._build_graph()

    def run(self, task: Task, call_spec: ToolCallSpec) -> ToolExecutionResult:
        result = self.graph.invoke(
            {
                "task": task_to_state(task),
                "call_spec": tool_call_to_state(call_spec),
                "events": [],
            },
            config=self._graph_config(task.id, task.session_id),
        )
        result = self._runtime_state(result) if isinstance(result, dict) else result
        return self._result_from_dict(result.get("tool_result") or {})

    def get_state(self, task_id: str):
        return self.graph.get_state(self._graph_config(task_id))

    def get_state_history(self, task_id: str):
        return list(self.graph.get_state_history(self._graph_config(task_id)))

    def _graph_config(self, task_id: str, session_id: str | None = None) -> dict:
        configurable = {
            "thread_id": f"{task_id}:knowledge",
            "task_id": task_id,
            "subgraph": "knowledge",
        }
        if session_id:
            configurable["session_id"] = session_id
        return {"configurable": configurable}

    def _build_graph(self):
        graph = StateGraph(KnowledgeSubgraphState)
        graph.add_node("validate_config", self._checkpointed_node(self._validate_config_node))
        graph.add_node("load_knowledge_memory", self._checkpointed_node(self._load_knowledge_memory_node))
        graph.add_node("rewrite_query", self._checkpointed_node(self._rewrite_query_node))
        graph.add_node("retrieve", self._checkpointed_node(self._retrieve_node))
        graph.add_node("emit_sources", self._checkpointed_node(self._emit_sources_node))
        graph.add_node("synthesize", self._checkpointed_node(self._synthesize_node))
        graph.add_node("evaluate_or_skip", self._checkpointed_node(self._evaluate_or_skip_node))
        graph.add_node("prepare_note", self._checkpointed_node(self._prepare_note_node))
        graph.add_node("write_note", self._checkpointed_node(self._write_note_node))
        graph.add_node("reindex_or_skip", self._checkpointed_node(self._reindex_or_skip_node))
        graph.add_node("finalize", self._checkpointed_node(self._finalize_node))
        graph.set_entry_point("validate_config")
        graph.add_edge("validate_config", "load_knowledge_memory")
        graph.add_conditional_edges(
            "load_knowledge_memory",
            self._route_branch,
            {"qa": "rewrite_query", "write": "prepare_note"},
        )
        graph.add_edge("rewrite_query", "retrieve")
        graph.add_edge("retrieve", "emit_sources")
        graph.add_edge("emit_sources", "synthesize")
        graph.add_edge("synthesize", "evaluate_or_skip")
        graph.add_edge("evaluate_or_skip", "finalize")
        graph.add_edge("prepare_note", "write_note")
        graph.add_edge("write_note", "reindex_or_skip")
        graph.add_edge("reindex_or_skip", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self.runtime.checkpointer, store=self.runtime.store)

    def _checkpointed_node(self, func):
        def wrapped(state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
            return self._checkpoint_state(func(self._runtime_state(state)))

        return wrapped

    def _runtime_state(self, state: dict) -> dict:
        runtime = dict(state)
        if "task" in runtime and isinstance(runtime["task"], dict):
            runtime["task"] = task_from_state(runtime["task"])
        if "call_spec" in runtime and isinstance(runtime["call_spec"], dict):
            runtime["call_spec"] = tool_call_from_state(runtime["call_spec"])
        return runtime

    def _checkpoint_state(self, state: dict | None) -> dict:
        if not state:
            return {}
        checkpoint = dict(state)
        if "task" in checkpoint:
            checkpoint["task"] = task_to_state(checkpoint["task"])
        if "call_spec" in checkpoint:
            checkpoint["call_spec"] = tool_call_to_state(checkpoint["call_spec"])
        if "tool_result" in checkpoint and isinstance(checkpoint["tool_result"], ToolExecutionResult):
            checkpoint["tool_result"] = tool_result_to_state(checkpoint["tool_result"])
        return to_plain(checkpoint)

    def _validate_config_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        task = state["task"]
        call_spec = state["call_spec"]
        params = call_spec.params or {}
        branch = "write" if task.intent == "knowledge_write" else "qa"
        return {
            "branch": branch,
            "native_qa": branch == "qa" and self._native_qa_tool(call_spec) is not None,
            "question": str(params.get("question") or task.input),
            "instruction": str(params.get("instruction") or task.input),
            "conversation_messages": list(params.get("conversation_history") or []),
            "retry_state": {},
        }

    def _load_knowledge_memory_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        task = state["task"]
        session_memory = dict((task.entities or {}).get("session_memory") or {})
        return {"knowledge_memory_context": {"qa_memory": list(session_memory.get("qa_memory") or [])}}

    def _route_branch(self, state: KnowledgeSubgraphState) -> str:
        return state.get("branch", "qa")

    def _rewrite_query_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        question = state.get("question", "")
        if not state.get("native_qa"):
            return {
                "rewritten_query": question,
                "retry_state": self._with_retry_state(
                    state,
                    "rewrite_query",
                    {"status": "skipped", "attempts": 0, "fallback": "legacy_tool"},
                ),
            }
        tool = self._native_qa_tool(state["call_spec"])
        if tool is None:
            return {"rewritten_query": question}
        rewritten, retry = self._execute_stage_with_retry(
            "rewrite_query",
            lambda: tool.engine.rewrite_query(question, list(state.get("conversation_messages") or [])),
            fallback=question,
        )
        return {
            "rewritten_query": str(rewritten or question),
            "retry_state": self._with_retry_state(state, "rewrite_query", retry),
        }

    def _retrieve_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        if state.get("native_qa"):
            return self._native_retrieve_node(state)
        result = self._execute_with_retry(state["call_spec"])
        result_dict = self._result_to_dict(result)
        answer = (result.data or {}).get("answer") if isinstance((result.data or {}).get("answer"), dict) else {}
        sources = list(answer.get("sources") or []) if answer else []
        return {
            "tool_result": result_dict,
            "answer": answer,
            "sources": sources,
            "retrieved_docs": sources,
        }

    def _native_retrieve_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        tool = self._native_qa_tool(state["call_spec"])
        if tool is None:
            return {"retrieved_docs": [], "sources": []}
        docs, retry = self._execute_stage_with_retry(
            "retrieve",
            lambda: tool.engine.retrieve_documents(state.get("rewritten_query") or state.get("question", "")),
        )
        retry_state = self._with_retry_state(state, "retrieve", retry)
        if retry.get("status") == "failed":
            result = ToolExecutionResult(
                success=False,
                error=str(retry.get("error") or "知识库检索失败"),
                retryable=False,
                data={"status": "failed", "question": state.get("question", ""), "retry_state": retry_state},
            )
            return {
                "retrieved_docs": [],
                "sources": [],
                "retry_state": retry_state,
                "tool_result": self._result_to_dict(result),
            }
        retrieved_docs = [self._doc_to_dict(doc) for doc in (docs or [])]
        return {
            "retrieved_docs": retrieved_docs,
            "sources": [self._source_from_doc(doc) for doc in retrieved_docs],
            "retry_state": retry_state,
        }

    def _emit_sources_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        events = list(state.get("events") or [])
        events.append({"stage": "knowledge.sources.ready", "source_count": len(state.get("sources") or [])})
        return {"events": events}

    def _synthesize_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        events = list(state.get("events") or [])
        update: KnowledgeSubgraphState = {}
        if state.get("native_qa") and not (state.get("tool_result") or {}).get("error"):
            update = self._native_synthesize(state)
            answer = update.get("answer") or {}
        else:
            answer = state.get("answer") or {}
        events.append(
            {
                "stage": "knowledge.answer.ready",
                "has_answer": bool(answer.get("answer")),
                "confidence": answer.get("confidence"),
            }
        )
        update["events"] = events
        return update

    def _native_synthesize(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        tool = self._native_qa_tool(state["call_spec"])
        if tool is None:
            return {}
        docs = self._docs_from_state(state.get("retrieved_docs") or [])
        answer, retry = self._execute_stage_with_retry(
            "synthesize",
            lambda: tool.engine.synthesize_answer(
                state.get("rewritten_query") or state.get("question", ""),
                docs,
                list(state.get("conversation_messages") or []),
            ),
        )
        retry_state = self._with_retry_state(state, "synthesize", retry)
        if retry.get("status") == "failed":
            result = ToolExecutionResult(
                success=False,
                error=str(retry.get("error") or "知识库答案合成失败"),
                retryable=False,
                data={"status": "failed", "question": state.get("question", ""), "retry_state": retry_state},
            )
            return {"retry_state": retry_state, "tool_result": self._result_to_dict(result)}
        answer_dict = self._answer_to_dict(answer)
        return {
            "answer": answer_dict,
            "sources": list(answer_dict.get("sources") or state.get("sources") or []),
            "retry_state": retry_state,
        }

    def _evaluate_or_skip_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        answer = state.get("answer") or {}
        if state.get("native_qa") and answer and not (state.get("tool_result") or {}).get("error"):
            return self._native_evaluate_or_skip(state)
        return {"evaluation": dict(answer.get("evaluation") or {})}

    def _native_evaluate_or_skip(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        tool = self._native_qa_tool(state["call_spec"])
        engine = getattr(tool, "engine", None) if tool is not None else None
        config = getattr(engine, "config", None)
        if tool is None or engine is None or config is None or not getattr(config, "enable_eval", False):
            return {"evaluation": dict((state.get("answer") or {}).get("evaluation") or {})}
        docs = self._docs_from_state(state.get("retrieved_docs") or [])
        if not docs:
            return {"evaluation": dict((state.get("answer") or {}).get("evaluation") or {})}
        answer = self._answer_from_dict(state.get("answer") or {})
        evaluated, retry = self._execute_stage_with_retry(
            "evaluate",
            lambda: tool.engine.evaluate_answer(state.get("rewritten_query") or state.get("question", ""), answer, docs),
            fallback=answer,
        )
        answer_dict = self._answer_to_dict(evaluated)
        return {
            "answer": answer_dict,
            "evaluation": dict(answer_dict.get("evaluation") or {}),
            "retry_state": self._with_retry_state(state, "evaluate", retry),
        }

    def _prepare_note_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        return {"running_summary": state.get("instruction", "")[:500]}

    def _write_note_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        result = self.tool_executor.execute(state["call_spec"])
        return {"tool_result": self._result_to_dict(result), "write_result": dict(result.data or {})}

    def _execute_with_retry(self, call_spec: ToolCallSpec) -> ToolExecutionResult:
        attempts = 0
        while True:
            result = self.tool_executor.execute(call_spec)
            if not self._is_retryable_result(result) or attempts >= MAX_KNOWLEDGE_RETRIES:
                result.data = dict(result.data or {})
                result.data["retry_attempts"] = attempts
                return result
            attempts += 1

    def _is_retryable_result(self, result: ToolExecutionResult) -> bool:
        status = (result.data or {}).get("status")
        return bool(result.retryable or status == "retryable_failure")

    def _reindex_or_skip_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        events = list(state.get("events") or [])
        write_result = state.get("write_result") or {}
        events.append(
            {
                "stage": "knowledge.write.completed",
                "success": bool((state.get("tool_result") or {}).get("success")),
                "reindex_status": write_result.get("reindex_status"),
            }
        )
        return {"events": events}

    def _finalize_node(self, state: KnowledgeSubgraphState) -> KnowledgeSubgraphState:
        if state.get("native_qa") and not state.get("tool_result"):
            return {"tool_result": self._native_tool_result(state)}
        return {"tool_result": state.get("tool_result") or {"success": False, "data": {}, "error": "知识子图未产生结果"}}

    def _native_tool_result(self, state: KnowledgeSubgraphState) -> dict:
        data = {
            "question": state.get("question", ""),
            "rewritten_query": state.get("rewritten_query") or state.get("question", ""),
            "answer": state.get("answer") or {},
            "retry_state": state.get("retry_state") or {},
        }
        return self._result_to_dict(ToolExecutionResult(success=True, data=data))

    def _result_to_dict(self, result: ToolExecutionResult) -> dict:
        return {
            "success": result.success,
            "data": result.data,
            "error": result.error,
            "retryable": result.retryable,
            "artifacts": [asdict(artifact) for artifact in result.artifacts],
        }

    def _result_from_dict(self, result: dict) -> ToolExecutionResult:
        artifacts = [TaskArtifact(**item) for item in result.get("artifacts") or []]
        return ToolExecutionResult(
            success=bool(result.get("success")),
            data=dict(result.get("data") or {}),
            error=result.get("error"),
            retryable=bool(result.get("retryable", False)),
            artifacts=artifacts,
        )

    def _native_qa_tool(self, call_spec: ToolCallSpec):
        if call_spec.tool_name != "knowledge":
            return None
        registry = getattr(self.tool_executor, "registry", None)
        if registry is None:
            return None
        try:
            tool = registry.get(call_spec.tool_name).tool
        except Exception:
            return None
        config = getattr(tool, "config", None)
        llm_config = getattr(tool, "_llm_config", None)
        vault_path = str(getattr(config, "vault_path", "") or "")
        if not vault_path or not Path(vault_path).exists():
            return None
        if llm_config is None or not getattr(llm_config, "enabled", False):
            return None
        if not hasattr(tool, "engine"):
            return None
        return tool

    def _execute_stage_with_retry(
        self,
        stage: str,
        func: Callable[[], Any],
        *,
        fallback: Any = _MISSING,
    ) -> tuple[Any, dict[str, Any]]:
        attempts = 0
        while True:
            try:
                return func(), {"status": "success", "attempts": attempts}
            except Exception as exc:
                if attempts >= MAX_KNOWLEDGE_RETRIES:
                    retry = {"status": "failed", "attempts": attempts, "error": str(exc), "stage": stage}
                    if fallback is not _MISSING:
                        retry["status"] = "fallback"
                        return fallback, retry
                    return None, retry
                attempts += 1

    def _with_retry_state(self, state: KnowledgeSubgraphState, stage: str, retry: dict[str, Any]) -> dict[str, Any]:
        retry_state = dict(state.get("retry_state") or {})
        retry_state[stage] = retry
        return retry_state

    def _doc_to_dict(self, doc) -> dict[str, Any]:
        if isinstance(doc, dict):
            return {"page_content": str(doc.get("page_content", "")), "metadata": dict(doc.get("metadata") or {})}
        return {"page_content": str(getattr(doc, "page_content", "")), "metadata": dict(getattr(doc, "metadata", {}) or {})}

    def _docs_from_state(self, docs: list[dict]) -> list[Document]:
        return [Document(page_content=str(item.get("page_content", "")), metadata=dict(item.get("metadata") or {})) for item in docs]

    def _source_from_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(doc.get("metadata") or {})
        return {
            "title": str(metadata.get("title", metadata.get("rel_path", ""))),
            "path": str(metadata.get("source", "")),
            "section": str(metadata.get("rel_path", "")),
            "matched_text": str(doc.get("page_content", ""))[:200],
            "relation": str(metadata.get("relation", "direct")),
            "related_to": str(metadata.get("related_to", "")),
        }

    def _answer_to_dict(self, answer) -> dict[str, Any]:
        if isinstance(answer, dict):
            return dict(answer)
        return {
            "answer": str(getattr(answer, "answer", "")),
            "sources": [asdict(source) if not isinstance(source, dict) else dict(source) for source in getattr(answer, "sources", [])],
            "confidence": float(getattr(answer, "confidence", 0.0) or 0.0),
            "missing_info": list(getattr(answer, "missing_info", []) or []),
            "evaluation": getattr(answer, "evaluation", None),
        }

    def _answer_from_dict(self, raw: dict[str, Any]) -> KnowledgeAnswer:
        return KnowledgeAnswer(
            answer=str(raw.get("answer", "")),
            sources=[
                source if isinstance(source, KnowledgeSource) else KnowledgeSource(**dict(source))
                for source in raw.get("sources") or []
            ],
            confidence=float(raw.get("confidence", 0.0) or 0.0),
            missing_info=list(raw.get("missing_info") or []),
            evaluation=raw.get("evaluation"),
        )
