from __future__ import annotations

import logging
import queue
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from aiops_agent.audit.models import AuditEvent
from aiops_agent.agent.knowledge_subgraph import KnowledgeSubgraph
from aiops_agent.agent.memory import LegacySessionMemoryWriter, SessionMemoryManager
from aiops_agent.agent.progress import ProgressEvent
from aiops_agent.agent.runtime import LangGraphRuntime, LangGraphRuntimeConfig
from aiops_agent.agent.state_codec import session_from_state, session_to_state, task_from_state, task_to_state, to_plain
from aiops_agent.browser.skills import (
    WebSkillGenerationError,
    WebSkillGenerator,
    WebSkillInvocationService,
    WebSkillSaveResult,
    WebSkillValidationError,
)
from aiops_agent.support.logging import log_kv
from aiops_agent.support.trace import get_trace_id, set_trace_id
from aiops_agent.tasks.models import Task, ToolCallSpec, ToolExecutionResult
from aiops_agent.tasks.manager import TaskManager
from aiops_agent.tools.base import ToolError
from aiops_agent.tools.executor import ToolExecutor
from aiops_agent.policy import PolicyEngine
from aiops_agent.planning import PlanningService
from aiops_agent.browser.site_config import BrowserSitesConfig, BrowserSiteConfigError


class OrchestrationState(TypedDict, total=False):
    task_input: str
    requested_session_id: str | None
    trace_id: str
    task_id: str
    llm_profile: str | None
    max_steps: int
    require_confirmation: bool
    task: Task
    session: object
    next_node: str
    allowed_domains: list[str]
    credential_ref: str
    browser_trace: bool
    browser_video: bool
    browser_site: str
    browser_channel: str
    browser_slow_mo_ms: int
    progress_callback: Callable[[ProgressEvent], None] | None


class AgentController:
    def __init__(
        self,
        parser,
        task_manager: TaskManager,
        tool_executor: ToolExecutor,
        summarizer,
        audit_logger,
        session_store,
        planning_service: PlanningService | None = None,
        policy_engine: PolicyEngine | None = None,
        context_compressor: object | None = None,
        browser_sites_config: BrowserSitesConfig | None = None,
        web_skill_generator: WebSkillGenerator | None = None,
        credential_ref_resolver: Callable[[str], str | None] | None = None,
        credential_ref_detector: Callable[[str], str | None] | None = None,
        credential_user_resolver: Callable[[str | None], str | None] | None = None,
        credential_ref_for_site_user: Callable[[str | None, str | None], str | None] | None = None,
        credential_site_resolver: Callable[[str | None], str | None] | None = None,
        langgraph_runtime: LangGraphRuntime | None = None,
        langgraph_runtime_config: LangGraphRuntimeConfig | None = None,
        session_memory_manager: SessionMemoryManager | None = None,
        session_summary_strategy=None,
        logger=None,
    ):
        self.parser = parser
        self.task_manager = task_manager
        self.tool_executor = tool_executor
        self.summarizer = summarizer
        self.audit_logger = audit_logger
        self.session_store = session_store
        self.planning_service = planning_service or PlanningService()
        self.policy_engine = policy_engine or PolicyEngine()
        self.legacy_session_memory_writer = LegacySessionMemoryWriter(summary_strategy=session_summary_strategy)
        self.browser_sites_config = browser_sites_config or BrowserSitesConfig()
        self.web_skill_generator = web_skill_generator
        self.credential_ref_resolver = credential_ref_resolver
        self.credential_ref_detector = credential_ref_detector
        self.credential_user_resolver = credential_user_resolver
        self.credential_ref_for_site_user = credential_ref_for_site_user
        self.credential_site_resolver = credential_site_resolver
        self.langgraph_runtime = langgraph_runtime or LangGraphRuntime.from_config(langgraph_runtime_config)
        self.session_memory_manager = session_memory_manager or SessionMemoryManager(self.langgraph_runtime.store)
        self.knowledge_subgraph = KnowledgeSubgraph(self.tool_executor, self.langgraph_runtime)
        self.logger = logger or logging.getLogger(__name__)
        self._runtime_context = threading.local()
        self._configure_tool_runtimes()
        self.graph = self._build_graph()

    def _configure_tool_runtimes(self) -> None:
        registry = getattr(self.tool_executor, "registry", None)
        if registry is None:
            return
        try:
            browser_definition = registry.get("browser_agent")
        except Exception:
            return
        configure = getattr(browser_definition.tool, "configure_langgraph_runtime", None)
        if configure is not None:
            configure(checkpointer=self.langgraph_runtime.checkpointer, store=self.langgraph_runtime.store)

    def run(
        self,
        task_input: str,
        *,
        session_id: str | None = None,
        llm_profile: str | None = None,
        max_steps: int = 20,
        require_confirmation: bool = False,
        allowed_domains: list[str] | None = None,
        credential_ref: str | None = None,
        browser_trace: bool = False,
        browser_video: bool = False,
        browser_site: str | None = None,
        browser_channel: str | None = None,
        browser_slow_mo_ms: int = 0,
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> Task:
        task_id: str | None = None

        def publish(event: ProgressEvent) -> None:
            nonlocal task_id
            if event.task_id:
                task_id = event.task_id
            if progress_callback is not None:
                progress_callback(event)

        result = self._run_graph(
            task_input,
            trace_id=get_trace_id(),
            session_id=session_id,
            llm_profile=llm_profile,
            max_steps=max_steps,
            require_confirmation=require_confirmation,
            allowed_domains=allowed_domains,
            credential_ref=credential_ref,
            browser_trace=browser_trace,
            browser_video=browser_video,
            browser_site=browser_site,
            browser_channel=browser_channel,
            browser_slow_mo_ms=browser_slow_mo_ms,
            progress_callback=publish,
        )
        result = self._runtime_state(result) if isinstance(result, dict) else result
        task = result.get("task") if isinstance(result, dict) else None
        if not isinstance(task, Task):
            if not task_id:
                raise RuntimeError("任务流结束但没有产生 task_id")
            task = self.task_manager.load(task_id)
        if task is None:
            raise RuntimeError(f"任务流结束但无法加载任务: {task_id or '-'}")
        return self.task_manager.load(task.id) or task

    def stream_run(
        self,
        task_input: str,
        *,
        session_id: str | None = None,
        llm_profile: str | None = None,
        max_steps: int = 20,
        require_confirmation: bool = False,
        allowed_domains: list[str] | None = None,
        credential_ref: str | None = None,
        browser_trace: bool = False,
        browser_video: bool = False,
        browser_site: str | None = None,
        browser_channel: str | None = None,
        browser_slow_mo_ms: int = 0,
    ) -> Iterator[ProgressEvent]:
        events: queue.Queue[ProgressEvent | object] = queue.Queue()
        sentinel = object()
        errors: list[BaseException] = []
        trace_id = get_trace_id()

        def publish(event: ProgressEvent) -> None:
            events.put(event)

        def worker() -> None:
            try:
                self._run_graph(
                    task_input,
                    trace_id=trace_id,
                    session_id=session_id,
                    llm_profile=llm_profile,
                    max_steps=max_steps,
                    require_confirmation=require_confirmation,
                    allowed_domains=allowed_domains,
                    credential_ref=credential_ref,
                    browser_trace=browser_trace,
                    browser_video=browser_video,
                    browser_site=browser_site,
                    browser_channel=browser_channel,
                    browser_slow_mo_ms=browser_slow_mo_ms,
                    progress_callback=publish,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                events.put(sentinel)

        thread = threading.Thread(target=worker, name="aiops-agent-stream-run", daemon=True)
        thread.start()
        while True:
            item = events.get()
            if item is sentinel:
                break
            yield item
        thread.join()
        if errors:
            raise errors[0]

    def _run_graph(
        self,
        task_input: str,
        *,
        trace_id: str,
        session_id: str | None,
        llm_profile: str | None,
        max_steps: int,
        require_confirmation: bool,
        allowed_domains: list[str] | None,
        credential_ref: str | None,
        browser_trace: bool,
        browser_video: bool,
        browser_site: str | None,
        browser_channel: str | None,
        browser_slow_mo_ms: int,
        progress_callback: Callable[[ProgressEvent], None],
    ) -> OrchestrationState:
        task_id = str(uuid4())
        set_trace_id(trace_id)
        log_kv(self.logger, logging.INFO, "Received task input", trace_id=trace_id, task_id=task_id)
        self._runtime_context.progress_callback = progress_callback
        self._runtime_context.trace_id = trace_id
        try:
            result = self.graph.invoke(
                {
                    "task_input": task_input,
                    "requested_session_id": session_id,
                    "trace_id": trace_id,
                    "task_id": task_id,
                    "llm_profile": llm_profile,
                    "max_steps": max_steps,
                    "require_confirmation": require_confirmation,
                    "allowed_domains": allowed_domains or [],
                    "credential_ref": credential_ref or "",
                    "browser_trace": browser_trace,
                    "browser_video": browser_video,
                    "browser_site": browser_site or "",
                    "browser_channel": browser_channel or "",
                    "browser_slow_mo_ms": browser_slow_mo_ms,
                },
                config=self._graph_config(task_id, session_id),
            )
            runtime_result = self._runtime_state(result) if isinstance(result, dict) else result
            if isinstance(runtime_result, dict) and runtime_result.get("__interrupt__"):
                self._finalize_interrupted_state(runtime_result, progress_callback)
            return runtime_result
        finally:
            self._runtime_context.progress_callback = None
            self._runtime_context.trace_id = None

    def _graph_config(self, task_id: str, session_id: str | None = None) -> dict:
        configurable = {"thread_id": task_id, "task_id": task_id}
        if session_id:
            configurable["session_id"] = session_id
        return {"configurable": configurable}

    def get_state(self, task_id: str):
        return self.graph.get_state(self._graph_config(task_id))

    def get_state_history(self, task_id: str):
        return list(self.graph.get_state_history(self._graph_config(task_id)))

    def get_web_state(self, task_id: str):
        thread_id = self._web_thread_id_for_task(task_id)
        return self._browser_agent_tool().get_state(thread_id)

    def get_web_state_history(self, task_id: str):
        thread_id = self._web_thread_id_for_task(task_id)
        return self._browser_agent_tool().get_state_history(thread_id)

    def _web_thread_id_for_task(self, task_id: str) -> str:
        task = self.task_manager.load(task_id)
        data = (task.result or {}).get("data") if task is not None and isinstance(task.result, dict) else {}
        thread_id = (data or {}).get("web_thread_id")
        if not thread_id:
            raise ValueError(f"任务没有 Web 子图 thread_id: {task_id}")
        return str(thread_id)

    def _browser_agent_tool(self):
        registry = getattr(self.tool_executor, "registry", None)
        if registry is None:
            raise ValueError("当前控制器没有工具注册表")
        return registry.get("browser_agent").tool

    def confirm(
        self,
        task_id: str,
        *,
        decision: str = "approved",
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> Task:
        task = self.task_manager.load(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        if task.status != "awaiting_confirmation":
            raise ValueError(f"任务不是等待确认状态: {task.status}")
        result_data = (task.result or {}).get("data") or {}
        pending_action = result_data.get("pending_action_raw")
        confirmation_type = result_data.get("confirmation_type") or (result_data.get("confirmation") or {}).get("type")
        # Web 动作确认：优先走 LangGraph interrupt/resume；没有可恢复 interrupt 时，
        # 再退回旧逻辑，把 pending_action 注回 browser_agent 工具调用。
        if pending_action:
            if self._has_resumable_interrupt(task.id):
                return self._resume_browser_confirmation(task, result_data, decision, progress_callback)
            if decision != "approved":
                self._reject_confirmation_task(task, decision)
                return self._finalize_confirmed_task(task, progress_callback)
            return self._confirm_browser_action(task, result_data, progress_callback)

        # Plan 级确认没有具体 pending_action，恢复点在主图 policy_check。
        if decision != "approved":
            if confirmation_type == "plan":
                return self._resume_plan_confirmation(task, result_data, decision, progress_callback)
            self._reject_confirmation_task(task, decision)
            return self._finalize_confirmed_task(task, progress_callback)

        if confirmation_type == "plan":
            return self._resume_plan_confirmation(task, result_data, decision, progress_callback)

        raise ValueError("任务缺少待确认动作或计划确认上下文，无法恢复执行")

    def _resume_plan_confirmation(
        self,
        task: Task,
        result_data: dict,
        decision: str,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> Task:
        if not self._has_resumable_interrupt(task.id):
            # 兼容旧任务：没有 checkpoint interrupt 时，按 task.result 里的 pending_tool_calls 恢复。
            if decision != "approved":
                self._reject_confirmation_task(task, decision)
                return self._finalize_confirmed_task(task, progress_callback)
            return self._confirm_plan(task, result_data, progress_callback)

        set_trace_id(task.trace_id)
        self._runtime_context.progress_callback = progress_callback
        self._runtime_context.trace_id = task.trace_id
        try:
            result = self.graph.invoke(
                Command(resume={"decision": decision}),
                config=self._graph_config(task.id, task.session_id),
            )
        finally:
            self._runtime_context.progress_callback = None
            self._runtime_context.trace_id = None

        result = self._runtime_state(result) if isinstance(result, dict) else result
        if isinstance(result, dict) and result.get("__interrupt__"):
            self._finalize_interrupted_state(result, progress_callback)

        resumed_task = result.get("task") if isinstance(result, dict) else None
        if not isinstance(resumed_task, Task):
            resumed_task = self.task_manager.load(task.id)
        if resumed_task is None:
            raise RuntimeError(f"确认恢复后无法加载任务: {task.id}")
        return self.task_manager.load(resumed_task.id) or resumed_task

    def _has_resumable_interrupt(self, task_id: str) -> bool:
        try:
            snapshot = self.get_state(task_id)
        except Exception:
            return False
        # LangGraph checkpoint 中还有 interrupts，才说明可以用 Command(resume=...) 原地续跑。
        return bool(getattr(snapshot, "interrupts", None))

    def _reject_confirmation_task(self, task: Task, decision: str) -> None:
        self.task_manager.mark_blocked(
            task,
            {
                "success": False,
                "error": "用户拒绝确认，任务已阻塞。",
                "data": {"status": "blocked", "confirmation_decision": decision},
            },
        )

    def _resume_browser_confirmation(
        self,
        task: Task,
        result_data: dict,
        decision: str,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> Task:
        set_trace_id(task.trace_id)
        self._runtime_context.progress_callback = progress_callback
        self._runtime_context.trace_id = task.trace_id
        try:
            result = self.graph.invoke(
                # 先恢复主图 interrupt；主图 route_execution 再把确认传递给 Web 子图 risk_gate。
                Command(resume={"decision": decision}),
                config=self._graph_config(task.id, task.session_id),
            )
        finally:
            self._runtime_context.progress_callback = None
            self._runtime_context.trace_id = None

        result = self._runtime_state(result) if isinstance(result, dict) else result
        if isinstance(result, dict) and result.get("__interrupt__"):
            self._finalize_interrupted_state(result, progress_callback)

        resumed_task = result.get("task") if isinstance(result, dict) else None
        if not isinstance(resumed_task, Task):
            resumed_task = self.task_manager.load(task.id)
        if resumed_task is None:
            raise RuntimeError(f"浏览器确认恢复后无法加载任务: {task.id}")
        return self.task_manager.load(resumed_task.id) or resumed_task

    def _confirm_browser_action(
        self,
        task: Task,
        result_data: dict,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> Task:
        call_spec = self._prepare_confirmed_browser_call(task, result_data)
        self.task_manager.mark_running(task)
        self._emit(
            progress_callback,
            ProgressEvent(
                stage="tool.running",
                message="已确认，正在恢复执行工具。",
                task_id=task.id,
                session_id=task.session_id,
            ),
        )
        pending_action = result_data.get("pending_action_raw")
        self.audit_logger.record(
            AuditEvent(
                event_type="confirmation.confirmed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": task.session_id,
                    "node": "route_execution",
                    "action_type": pending_action.get("type") if pending_action else None,
                    "risk_level": pending_action.get("risk_level") if pending_action else None,
                },
            )
        )
        self._execute_confirmed_tool_call(task, call_spec, progress_callback)
        return self._finalize_confirmed_task(task, progress_callback)

    def _prepare_confirmed_browser_call(self, task: Task, result_data: dict) -> ToolCallSpec:
        pending_action = result_data.get("pending_action_raw")
        if not task.tool_calls:
            raise ValueError("任务缺少工具调用，无法恢复执行")

        call_spec = task.tool_calls[0]
        call_spec.params = dict(call_spec.params)
        # 把确认前保存的 payload 还原成浏览器工具参数：
        # confirmed_action 是本次真正要执行的危险动作；replay_actions 只用于重建安全页面上下文。
        call_spec.params["confirmed_action"] = pending_action
        call_spec.params["replay_actions"] = result_data.get("replay_actions") or []
        call_spec.params["prior_steps"] = result_data.get("steps") or []
        # 已完成的 mutation key 会传回 planner，避免确认后再次提出同一个提交动作。
        call_spec.params["completed_action_keys"] = result_data.get("completed_action_keys") or []
        # 确认后不再把任务整体当作“需要远端变更确认”，只执行这一条已确认 action。
        call_spec.params["requires_remote_mutation"] = False
        call_spec.params["start_url"] = result_data.get("resume_url") or call_spec.params.get("start_url")
        call_spec.params["session_state_path"] = result_data.get("session_state_path") or call_spec.params.get("session_state_path")
        if result_data.get("web_thread_id"):
            call_spec.params["web_thread_id"] = result_data.get("web_thread_id")
        if result_data.get("web_run_id"):
            call_spec.params["web_run_id"] = result_data.get("web_run_id")
        call_spec.params["confirmation_decision"] = result_data.get("confirmation_decision") or "approved"
        call_spec.params["trace_id"] = task.trace_id
        call_spec.params["task_id"] = task.id
        call_spec.params["session_id"] = task.session_id
        call_spec.params["max_steps"] = min(int(call_spec.params.get("max_steps", task.max_steps)), task.max_steps)
        task.tool_calls = [call_spec]
        return call_spec

    def _confirm_plan(
        self,
        task: Task,
        result_data: dict,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> Task:
        result_data["confirmed"] = True
        confirmation = dict(result_data.get("confirmation") or {})
        confirmation["type"] = confirmation.get("type") or "plan"
        confirmation["confirmed"] = True
        result_data["confirmation"] = confirmation
        if task.result is not None:
            task.result["data"] = result_data

        self.audit_logger.record(
            AuditEvent(
                event_type="confirmation.confirmed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": task.session_id,
                    "confirmation_type": "plan",
                    "resume_node": result_data.get("resume_node"),
                    "pending_tool_count": len(result_data.get("pending_tool_calls") or []),
                },
            )
        )
        pending_tool_calls = self._deserialize_tool_calls(result_data.get("pending_tool_calls") or [])
        task.tool_calls = pending_tool_calls
        if not pending_tool_calls:
            self.task_manager.mark_blocked(
                task,
                {
                    "success": False,
                    "error": "用户已确认，但当前任务没有可执行工具。",
                    "data": {
                        "status": "blocked",
                        "block_reason": "confirmed_without_executable_tool",
                        "intent": task.intent,
                        "entities": task.entities,
                        "plan_steps": task.plan.steps if task.plan else result_data.get("plan_steps", []),
                        "confirmation_type": "plan",
                        "confirmation_summary": result_data.get("confirmation_summary") or {},
                        "pending_tool_calls": [],
                        "confirmation": confirmation,
                    },
                },
            )
            return self._finalize_confirmed_task(task, progress_callback)

        call_spec = pending_tool_calls[0]
        call_spec.params = dict(call_spec.params)
        call_spec.params.setdefault("trace_id", task.trace_id)
        call_spec.params.setdefault("task_id", task.id)
        call_spec.params.setdefault("session_id", task.session_id)
        call_spec.params["max_steps"] = task.max_steps
        self.task_manager.mark_running(task)
        self._emit(
            progress_callback,
            ProgressEvent(
                stage="tool.running",
                message="已确认，正在执行工具。",
                task_id=task.id,
                session_id=task.session_id,
            ),
        )
        self._execute_confirmed_tool_call(task, call_spec, progress_callback)
        return self._finalize_confirmed_task(task, progress_callback)

    def _deserialize_tool_calls(self, raw_calls: list) -> list[ToolCallSpec]:
        calls: list[ToolCallSpec] = []
        for item in raw_calls:
            if isinstance(item, ToolCallSpec):
                calls.append(item)
            elif isinstance(item, dict):
                calls.append(ToolCallSpec(**item))
        return calls

    def _execute_confirmed_tool_call(
        self,
        task: Task,
        call_spec: ToolCallSpec,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> None:
        try:
            tool_result = self._execute_tool_call(task, call_spec)
        except ToolError as exc:
            self.task_manager.mark_failed(task, {"success": False, "error": str(exc), "data": {}})
            return

        task.artifacts.extend(tool_result.artifacts)
        self.audit_logger.record(
            AuditEvent(
                event_type="tool_called",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "tool_name": call_spec.tool_name,
                    "action": call_spec.action,
                    "risk_level": call_spec.risk_level,
                },
            )
        )
        fallback_result = self._try_skill_fallback(call_spec, tool_result, task, progress_callback)
        if fallback_result is not None:
            tool_result = fallback_result
            task.artifacts.extend(tool_result.artifacts)
        prior_step_count = len(call_spec.params.get("prior_steps") or [])
        self._emit_domain_tool_events(
            task,
            tool_result,
            progress_callback,
            web_step_offset=prior_step_count,
        )
        result_status = (tool_result.data or {}).get("status")
        if tool_result.success:
            self.task_manager.mark_success(task, tool_result.to_dict())
        elif result_status == "awaiting_confirmation":
            self.task_manager.mark_awaiting_confirmation(task, tool_result.to_dict())
        elif result_status == "blocked":
            self.task_manager.mark_blocked(task, tool_result.to_dict())
        else:
            self.task_manager.mark_failed(task, tool_result.to_dict())

    def _finalize_confirmed_task(
        self,
        task: Task,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> Task:
        task.report = self.summarizer.summarize(task, task.result or {})
        self._emit(
            progress_callback,
            ProgressEvent(stage="summary.ready", message="已生成执行摘要。", task_id=task.id, session_id=task.session_id),
        )
        session = self.session_store.load(task.session_id) if task.session_id else None
        if session is not None:
            session.last_task_id = task.id
            self._sync_legacy_session_and_store(session, task)
            self.session_store.save(session)
        self.task_manager.persist(task)
        completion_details = {
            "session_id": task.session_id,
            "result_success": bool(task.result and task.result.get("success")),
            "error": (task.result or {}).get("error"),
        }
        for event_type in ("task.completed", "task_completed"):
            self.audit_logger.record(
                AuditEvent(
                    event_type=event_type,
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details=completion_details,
                )
            )
        self._emit(
            progress_callback,
            ProgressEvent(
                stage="task.completed",
                message=f"任务已结束，状态：{task.status}。",
                task_id=task.id,
                session_id=task.session_id,
                details={"status": task.status},
            ),
        )
        return task

    def _finalize_interrupted_state(
        self,
        state: dict,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> None:
        task = state.get("task")
        session = state.get("session")
        if not isinstance(task, Task) or session is None:
            return
        persisted_task = self.task_manager.load(task.id)
        if persisted_task is not None:
            task = persisted_task
        interrupt_value = self._first_interrupt_value(state)
        interrupted_node = ((interrupt_value.get("langgraph") or {}).get("node") if isinstance(interrupt_value, dict) else None) or "unknown"
        if not task.report:
            task.report = self.summarizer.summarize(task, task.result or {})
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="summary.ready",
                    message="已生成执行摘要。",
                    task_id=task.id,
                    session_id=task.session_id,
                ),
            )
        session.last_task_id = task.id
        self._sync_legacy_session_and_store(session, task)
        self.task_manager.persist(task)
        self.session_store.save(session)
        self.audit_logger.record(
            AuditEvent(
                event_type="graph.interrupted",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={"session_id": task.session_id, "node": interrupted_node},
            )
        )
        self._emit(
            progress_callback,
            ProgressEvent(
                stage="graph.interrupted",
                message="LangGraph 已在人工确认点暂停。",
                task_id=task.id,
                session_id=task.session_id,
                details={"node": interrupted_node},
            ),
        )
        self.audit_logger.record(
            AuditEvent(
                event_type="task_completed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "result_success": bool(task.result and task.result.get("success")),
                    "error": (task.result or {}).get("error"),
                    "session_id": task.session_id,
                },
            )
        )
        self._emit(
            progress_callback,
            ProgressEvent(
                stage="task.completed",
                message=f"任务已结束，状态：{task.status}。",
                task_id=task.id,
                session_id=session.id,
                details={"status": task.status},
            ),
        )

    def _first_interrupt_value(self, state: dict):
        interrupts = state.get("__interrupt__") or []
        if not interrupts:
            return None
        first = interrupts[0]
        return getattr(first, "value", None)

    def save_web_skill(self, session_id: str | None, name: str | None = None) -> WebSkillSaveResult:
        if not session_id:
            raise ValueError("当前还没有 active session，无法保存 skill。")
        session = self.session_store.load(session_id)
        if session is None:
            raise ValueError(f"当前 session 尚未持久化: {session_id}")
        store_task_id, store_trace = self._web_skill_source_from_store(session_id)
        metadata = getattr(session, "metadata", {}) or {}
        browser_memory = getattr(session, "browser_memory", None)
        task_id = (
            store_task_id
            or getattr(browser_memory, "last_success_task_id", None)
            or metadata.get("browser_last_success_task_id")
        )
        if not task_id:
            raise ValueError("当前 session 没有最近一次成功的 web_action。")
        task = self.task_manager.load(task_id)
        if task is None:
            raise ValueError(f"最近成功 web_action 任务不存在: {task_id}")
        if store_trace:
            task.result = dict(task.result or {})
            data = dict(task.result.get("data") or {})
            data["canonical_action_trace"] = store_trace
            task.result["data"] = data
        generator = self.web_skill_generator or WebSkillGenerator()
        try:
            return generator.generate_from_task(task, name=name)
        except (WebSkillGenerationError, WebSkillValidationError) as exc:
            raise ValueError(str(exc)) from exc

    def list_web_skills(self) -> list[dict]:
        service = self._web_skill_invocation_service()
        if service is None:
            return []
        return service.list_skills()

    def delete_web_skill(self, name: str):
        generator = self.web_skill_generator or WebSkillGenerator()
        store = getattr(generator, "store", None)
        if store is None:
            raise ValueError("当前没有可用的 web skill store。")
        try:
            return store.delete(name)
        except WebSkillValidationError as exc:
            raise ValueError(str(exc)) from exc

    def rename_web_skill(self, old_name: str, new_name: str):
        generator = self.web_skill_generator or WebSkillGenerator()
        store = getattr(generator, "store", None)
        if store is None:
            raise ValueError("当前没有可用的 web skill store。")
        try:
            return store.rename(old_name, new_name)
        except WebSkillValidationError as exc:
            raise ValueError(str(exc)) from exc

    def run_web_skill(
        self,
        skill_name: str,
        parameters: dict[str, str],
        *,
        session_id: str | None = None,
        llm_profile: str | None = None,
        max_steps: int = 20,
        require_confirmation: bool = False,
        allowed_domains: list[str] | None = None,
        credential_ref: str | None = None,
        browser_trace: bool = False,
        browser_video: bool = False,
        browser_site: str | None = None,
        browser_channel: str | None = None,
        browser_slow_mo_ms: int = 0,
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> Task:
        service = self._web_skill_invocation_service()
        if service is None:
            raise ValueError("当前没有可用的 web skill matcher。")
        try:
            invocation = service.prepare_invocation(
                skill_name,
                parameters,
                max_steps=max_steps,
                allowed_domains=allowed_domains or [],
                credential_ref=credential_ref,
                browser_trace=browser_trace,
                browser_video=browser_video,
                browser_site=browser_site,
                browser_channel=browser_channel,
                browser_slow_mo_ms=browser_slow_mo_ms,
            )
        except WebSkillValidationError as exc:
            raise ValueError(str(exc)) from exc

        trace_id = get_trace_id()
        set_trace_id(trace_id)
        self._runtime_context.progress_callback = progress_callback
        self._runtime_context.trace_id = trace_id
        try:
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="graph.started",
                    message="LangGraph 主图已启动。",
                    details={"trace_id": trace_id, "graph": "main", "node": "intake", "status": "started"},
                ),
            )
            session = self.session_store.create_or_resume(session_id)
            session_event = "session.resumed" if session.task_ids else "session.created"
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage=session_event,
                    message="已恢复会话。" if session.task_ids else "已创建会话。",
                    session_id=session.id,
                    details={"trace_id": trace_id, "graph": "main", "node": "intake"},
                ),
            )
            task_input = invocation.task_input
            task = self.task_manager.create_task(
                task_input=task_input,
                trace_id=trace_id,
                session_id=session.id,
                llm_profile=llm_profile,
                max_steps=max_steps,
                requires_explicit_confirmation=require_confirmation,
            )
            session.task_ids.append(task.id)
            session.last_task_id = task.id
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="task.created",
                    message="已创建任务。",
                    task_id=task.id,
                    session_id=session.id,
                    details={"trace_id": trace_id, "graph": "main", "node": "intake"},
                ),
            )

            browser_memory = getattr(session, "browser_memory", None)
            risk_level = invocation.risk_level
            task.intent = "web_action"
            task.status = "planning"
            task.current_stage = "planning"
            task.entities = {
                **invocation.entities,
                "raw_text": task_input,
                "workflow_fields": invocation.skill_parameters,
                "skill_name": skill_name,
            }
            task.plan = invocation.plan
            call_spec = invocation.call_spec
            call_spec.params = dict(call_spec.params)
            call_spec.params["session_state_path"] = getattr(browser_memory, "state_path", None)
            task.tool_calls = [call_spec]
            task.selected_tools = ["browser_agent"]
            task.risk_level = risk_level
            task.confirmation_required = False
            self.task_manager.persist(task)
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="plan.generated",
                    message="已生成计划，工具：browser_agent。",
                    task_id=task.id,
                    session_id=session.id,
                    details={"risk_level": risk_level, "selected_tools": task.selected_tools},
                ),
            )
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="web.skill.matched",
                    message=f"命中 web skill：{skill_name}。",
                    task_id=task.id,
                    session_id=session.id,
                    details={"skill_name": skill_name, "score": 1.0, "parameters": invocation.skill_parameters},
                ),
            )
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="policy.checked",
                    message=f"策略检查通过，风险等级：{risk_level}。",
                    task_id=task.id,
                    session_id=session.id,
                    details={"risk_level": risk_level, "allowed": True},
                ),
            )

            state = self._route_execution_node({"task": task, "session": session, "progress_callback": progress_callback})
            state = self._summarize_node(state)
            state = self._persist_audit_node(state)
            completed_task = state.get("task")
            if isinstance(completed_task, Task):
                return self.task_manager.load(completed_task.id) or completed_task
            loaded = self.task_manager.load(task.id)
            if loaded is None:
                raise RuntimeError(f"skill 执行结束但无法加载任务: {task.id}")
            return loaded
        finally:
            self._runtime_context.progress_callback = None
            self._runtime_context.trace_id = None

    def _web_skill_source_from_store(self, session_id: str) -> tuple[str | None, dict | None]:
        namespace = ("sessions", session_id, "web")
        context_item = self.langgraph_runtime.store.get(namespace, "context")
        context = context_item.value if context_item is not None and isinstance(context_item.value, dict) else {}
        task_id = context.get("last_success_task_id") if context else None
        if not task_id:
            return None, None
        trace_item = self.langgraph_runtime.store.get(namespace, f"trace:{task_id}")
        trace_value = trace_item.value if trace_item is not None and isinstance(trace_item.value, dict) else {}
        trace = trace_value.get("canonical_action_trace") if isinstance(trace_value.get("canonical_action_trace"), dict) else None
        return str(task_id), trace

    def _web_skill_matcher(self):
        try:
            browser_tool = self._browser_agent_tool()
        except Exception:
            browser_tool = None
        matcher = getattr(browser_tool, "web_skill_matcher", None) if browser_tool is not None else None
        if matcher is not None:
            return matcher
        generator = self.web_skill_generator
        store = getattr(generator, "store", None)
        if store is None:
            return None
        from aiops_agent.browser.skills import WebSkillMatcher

        return WebSkillMatcher(store)

    def _web_skill_invocation_service(self) -> WebSkillInvocationService | None:
        matcher = self._web_skill_matcher()
        if matcher is None:
            return None
        return WebSkillInvocationService(
            matcher,
            browser_sites_config=self.browser_sites_config,
            credential_ref_resolver=self._default_credential_ref,
            credential_user_resolver=self._default_credential_user,
            credential_ref_for_site_user=self._credential_ref_for_site_user,
            credential_site_resolver=self._site_key_for_credential,
        )

    def _build_graph(self):
        graph = StateGraph(OrchestrationState)
        graph.add_node("intake", self._checkpointed_node(self._intake_node))
        graph.add_node("intent_parse", self._checkpointed_node(self._intent_parse_node))
        graph.add_node("task_plan", self._checkpointed_node(self._task_plan_node))
        graph.add_node("policy_check", self._checkpointed_node(self._policy_check_node))
        graph.add_node("route_execution", self._checkpointed_node(self._route_execution_node))
        graph.add_node("summarize", self._checkpointed_node(self._summarize_node))
        graph.add_node("persist_audit", self._checkpointed_node(self._persist_audit_node))
        graph.set_entry_point("intake")
        graph.add_edge("intake", "intent_parse")
        graph.add_edge("intent_parse", "task_plan")
        graph.add_edge("task_plan", "policy_check")
        graph.add_conditional_edges(
            "policy_check",
            self._route_after_policy,
            {"route_execution": "route_execution", "summarize": "summarize"},
        )
        graph.add_edge("route_execution", "summarize")
        graph.add_edge("summarize", "persist_audit")
        graph.add_edge("persist_audit", END)
        return graph.compile(
            checkpointer=self.langgraph_runtime.checkpointer,
            store=self.langgraph_runtime.store,
        )

    def _checkpointed_node(self, func):
        def wrapped(state: OrchestrationState) -> OrchestrationState:
            return self._checkpoint_state(func(self._runtime_state(state)))

        return wrapped

    def _runtime_state(self, state: dict) -> dict:
        runtime = dict(state)
        if "task" in runtime and isinstance(runtime["task"], dict):
            runtime["task"] = task_from_state(runtime["task"])
        if "session" in runtime and isinstance(runtime["session"], dict):
            runtime["session"] = session_from_state(runtime["session"])
        return runtime

    def _checkpoint_state(self, state: dict | None) -> dict:
        if not state:
            return {}
        checkpoint = dict(state)
        checkpoint.pop("progress_callback", None)
        if "task" in checkpoint:
            checkpoint["task"] = task_to_state(checkpoint["task"])
        if "session" in checkpoint:
            checkpoint["session"] = session_to_state(checkpoint["session"])
        return to_plain(checkpoint)

    def _intake_node(self, state: OrchestrationState) -> OrchestrationState:
        task_input = state["task_input"]
        trace_id = state["trace_id"]
        task_id = state["task_id"]
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="graph.started",
                message="LangGraph 主图已启动。",
                details={
                    "trace_id": trace_id,
                    "graph": "main",
                    "node": "intake",
                    "status": "started",
                },
            ),
        )
        session = self.session_store.create_or_resume(state.get("requested_session_id"))
        session_event = "session.resumed" if session.task_ids else "session.created"
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage=session_event,
                message="已恢复会话。" if session.task_ids else "已创建会话。",
                session_id=session.id,
                details={"trace_id": trace_id, "graph": "main", "node": "intake"},
            ),
        )
        task = self.task_manager.create_task(
            task_input=task_input,
            trace_id=trace_id,
            session_id=session.id,
            llm_profile=state.get("llm_profile"),
            max_steps=int(state.get("max_steps", 20)),
            requires_explicit_confirmation=bool(state.get("require_confirmation", False)),
            task_id=task_id,
        )
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="task.created",
                message="已创建任务。",
                task_id=task.id,
                session_id=session.id,
                details={"trace_id": trace_id, "graph": "main", "node": "intake"},
            ),
        )
        session.task_ids.append(task.id)
        session.last_task_id = task.id
        self.audit_logger.record(
            AuditEvent(
                event_type=session_event,
                trace_id=trace_id,
                task_id=task.id,
                status=session.status,
                details={"session_id": session.id, "last_task_id": session.last_task_id},
            )
        )
        self.audit_logger.record(
            AuditEvent(
                event_type="task_created",
                trace_id=trace_id,
                task_id=task.id,
                status=task.status,
                details={"input": self._audit_task_input(task_input), "session_id": session.id},
            )
        )
        return {
            "task": task,
            "session": session,
            "allowed_domains": state.get("allowed_domains", []),
            "credential_ref": state.get("credential_ref", ""),
            "browser_trace": bool(state.get("browser_trace", False)),
            "browser_video": bool(state.get("browser_video", False)),
            "browser_site": state.get("browser_site", ""),
            "browser_channel": state.get("browser_channel", ""),
            "browser_slow_mo_ms": int(state.get("browser_slow_mo_ms", 0)),
        }

    def _intent_parse_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        intent_result = self.parser.parse(task.input)
        task.intent = intent_result.intent
        task.entities = intent_result.entities
        if state.get("allowed_domains"):
            existing_domains = list(task.entities.get("allowed_domains") or [])
            task.entities["allowed_domains"] = sorted(set(existing_domains + state["allowed_domains"]))
        if state.get("credential_ref"):
            task.entities["credential_ref"] = state["credential_ref"]
        if not task.entities.get("credential_ref"):
            credential_ref = self._credential_ref_from_text(task.input)
            if credential_ref:
                task.entities["credential_ref"] = credential_ref
        if state.get("browser_trace"):
            task.entities["trace_enabled"] = True
        if state.get("browser_video"):
            task.entities["video_enabled"] = True
        if state.get("browser_channel"):
            task.entities["browser_channel"] = state["browser_channel"]
        if state.get("browser_slow_mo_ms"):
            task.entities["browser_slow_mo_ms"] = int(state["browser_slow_mo_ms"])
        credential_site_key = self._site_key_for_credential(task.entities.get("credential_ref"))
        apply_credential_site = bool(credential_site_key and (task.intent == "web_action" or self._has_web_navigation_cue(task.input)))
        site_key = str(state["browser_site"]) if state.get("browser_site") else (credential_site_key if apply_credential_site else None) or self._browser_site_key_from_text(task.input)
        if site_key and (state.get("browser_site") or apply_credential_site or self._should_apply_browser_site(task)):
            self._apply_browser_site(task, site_key)
        if task.intent == "web_action" and task.entities.get("site_key") and not task.entities.get("credential_ref"):
            credential_ref = self._default_credential_ref(str(task.entities["site_key"]))
            if credential_ref:
                task.entities["credential_ref"] = credential_ref
        if task.intent == "web_action":
            enrich = getattr(self.parser, "enrich_web_action_entities", None)
            if callable(enrich):
                task.entities = enrich(task.input, task.entities)
        task.current_stage = "planning"
        task.status = "planning"
        self.audit_logger.record(
            AuditEvent(
                event_type="intent_parsed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={"intent": task.intent, "entities": self._audit_entities(task.intent, task.entities)},
            )
        )
        log_kv(self.logger, logging.INFO, "Intent parsed", intent=task.intent, task_id=task.id)
        message = f"已识别意图：{task.intent}。"
        if task.entities.get("llm_fallback_used"):
            message = f"{message} LLM 识别失败，已使用规则 fallback。"
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="intent.parsed",
                message=message,
                task_id=task.id,
                session_id=task.session_id,
                details={"intent": task.intent},
            ),
        )
        return {
            "task": task,
            "session": state["session"],
            "allowed_domains": state.get("allowed_domains", []),
            "credential_ref": state.get("credential_ref", ""),
            "browser_trace": bool(state.get("browser_trace", False)),
            "browser_video": bool(state.get("browser_video", False)),
            "browser_site": state.get("browser_site", ""),
            "browser_channel": state.get("browser_channel", ""),
            "browser_slow_mo_ms": int(state.get("browser_slow_mo_ms", 0)),
            "progress_callback": state.get("progress_callback"),
        }

    def _apply_browser_site(self, task: Task, site_key: str) -> None:
        try:
            site = self.browser_sites_config.get(site_key)
        except BrowserSiteConfigError as exc:
            task.intent = "web_action"
            task.entities["browser_config_error"] = str(exc)
            task.entities["site_key"] = site_key
            return
        task.intent = "web_action"
        task.entities["site_key"] = site_key
        task.entities["site_config"] = site.to_runtime_dict()
        task.entities["start_url"] = task.entities.get("start_url") or site.login_url or site.base_url
        existing_domains = list(task.entities.get("allowed_domains") or [])
        task.entities["allowed_domains"] = sorted(set(existing_domains + site.allowed_domains))
        task.entities["requires_login"] = bool(task.entities.get("requires_login") or site.login_url or site.login_fields)

    def _browser_site_key_from_text(self, text: str) -> str | None:
        lowered = text.lower()
        candidates: list[tuple[str, str]] = []
        for site_key, site in self.browser_sites_config.sites.items():
            candidates.append((site_key, site_key))
            candidates.extend((alias, site_key) for alias in site.aliases)
        for alias, site_key in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
            normalized_alias = alias.strip().lower()
            if normalized_alias and normalized_alias in lowered:
                return site_key
        return None

    def _should_apply_browser_site(self, task: Task) -> bool:
        if task.intent == "web_action":
            return True
        if task.intent == "general_chat":
            return self._has_web_navigation_cue(task.input)
        if task.intent == "rpa_action":
            return self._has_web_navigation_cue(task.input) and not self._has_explicit_rpa_cue(task.input)
        return False

    def _has_web_navigation_cue(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in lowered for keyword in ("登录", "打开", "进入", "访问", "网页", "网站", "浏览器", "login", "open", "visit"))

    def _has_explicit_rpa_cue(self, text: str) -> bool:
        lowered = text.lower()
        if re.search(r"(?<![0-9.])\d{2,3}(?:\.\d{1,3}){1,3}(?![0-9.])", text):
            return True
        return any(keyword in lowered for keyword in ("ssh", "sftp", "数据库", "pl/sql", "plsql", "sql", "服务器"))

    def _default_credential_ref(self, site_key: str) -> str | None:
        if self.credential_ref_resolver is None:
            return None
        return self.credential_ref_resolver(site_key)

    def _default_credential_user(self, site_key: str | None) -> str | None:
        if self.credential_user_resolver is None:
            return None
        return self.credential_user_resolver(site_key)

    def _credential_ref_for_site_user(self, site_key: str | None, user: str | None) -> str | None:
        if self.credential_ref_for_site_user is None:
            return None
        return self.credential_ref_for_site_user(site_key, user)

    def _credential_ref_from_text(self, text: str) -> str | None:
        if self.credential_ref_detector is None:
            return None
        return self.credential_ref_detector(text)

    def _site_key_for_credential(self, credential_ref: str | None) -> str | None:
        if self.credential_site_resolver is None:
            return None
        return self.credential_site_resolver(credential_ref)

    def _task_plan_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        session = state["session"]

        session_memory = self.session_memory_manager.retrieve(
            session,
            task.intent,
            task.input,
            limit=5,
        )
        task.entities["session_memory"] = session_memory

        if task.intent in {"ops_qa", "knowledge_write"}:
            import json
            qa_memory = session_memory.get("qa_memory", [])
            if qa_memory:
                qa_turns = [
                    {"question": turn.get("question", ""), "answer": turn.get("answer", "")}
                    for turn in qa_memory[-5:]
                ]
            elif getattr(session, "qa_memory", None):
                qa_turns = [
                    {"question": turn.question, "answer": turn.answer}
                    for turn in (getattr(session, "qa_memory", []) or [])[-5:]
                ]
            else:
                metadata = getattr(session, "metadata", {}) or {}
                raw_turns = metadata.get("qa_turns", "")
                try:
                    qa_turns = json.loads(raw_turns) if raw_turns else []
                except (json.JSONDecodeError, ValueError, TypeError):
                    qa_turns = []
            task.entities["conversation_history"] = qa_turns[-5:]

        plan = self.planning_service.plan(task.input, task.intent, task.entities)
        task.plan = plan
        task.selected_tools = plan.selected_tools
        task.tool_calls = list(plan.tool_calls)
        task.risk_level = plan.risk_level
        task.confirmation_required = plan.confirmation_required
        skill_params = self._web_skill_params(task)
        self.audit_logger.record(
            AuditEvent(
                event_type="plan_generated",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "risk_level": task.risk_level,
                    "selected_tools": task.selected_tools,
                    "confirmation_required": task.confirmation_required,
                },
            )
        )
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="plan.generated",
                message=f"已生成计划，工具：{','.join(task.selected_tools) or '无'}。",
                task_id=task.id,
                session_id=task.session_id,
                details={"risk_level": task.risk_level, "selected_tools": task.selected_tools},
            ),
        )
        if skill_params:
            self._emit(
                state.get("progress_callback"),
                ProgressEvent(
                    stage="web.skill.matched",
                    message=f"命中 web skill：{skill_params.get('skill_name')}。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "skill_name": skill_params.get("skill_name"),
                        "score": skill_params.get("skill_score"),
                        "parameters": skill_params.get("skill_parameters") or {},
                    },
                ),
            )
        return {
            "task": task,
            "session": state["session"],
            "allowed_domains": state.get("allowed_domains", []),
            "browser_site": state.get("browser_site", ""),
            "progress_callback": state.get("progress_callback"),
        }

    def _policy_check_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        decision = self.policy_engine.evaluate(task, task.plan)
        task.risk_level = decision.risk_level
        task.confirmation_required = decision.requires_confirmation
        if decision.allowed:
            self.audit_logger.record(
                AuditEvent(
                    event_type="policy_approved",
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={"risk_level": decision.risk_level},
                )
            )
            self._emit(
                state.get("progress_callback"),
                ProgressEvent(
                    stage="policy.checked",
                    message=f"策略检查通过，风险等级：{decision.risk_level}。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={"risk_level": decision.risk_level, "allowed": True},
                ),
            )
            return {
                "task": task,
                "session": state["session"],
                "next_node": "route_execution",
                "allowed_domains": state.get("allowed_domains", []),
                "browser_site": state.get("browser_site", ""),
                "progress_callback": state.get("progress_callback"),
            }

        if decision.status == "awaiting_confirmation":
            return self._interrupt_for_plan_confirmation(state, task, decision)

        self.task_manager.mark_blocked(
            task,
            {
                "success": False,
                "error": decision.reason,
                "data": {
                    "intent": task.intent,
                    "entities": task.entities,
                },
            },
        )
        self.audit_logger.record(
            AuditEvent(
                event_type="policy_blocked",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={"reason": decision.reason, "risk_level": decision.risk_level},
            )
        )
        self._emit_policy_blocked(state, task, decision)
        return {
            "task": task,
            "session": state["session"],
            "next_node": "summarize",
            "allowed_domains": state.get("allowed_domains", []),
            "browser_site": state.get("browser_site", ""),
            "progress_callback": state.get("progress_callback"),
        }

    def _interrupt_for_plan_confirmation(self, state: OrchestrationState, task: Task, decision) -> OrchestrationState:
        persisted = self.task_manager.load(task.id)
        if persisted is not None and persisted.status == "awaiting_confirmation":
            task = persisted
        else:
            self.task_manager.mark_awaiting_confirmation(
                task,
                {
                    "success": False,
                    "error": decision.reason,
                    "data": self._build_plan_confirmation_payload(task, decision),
                },
            )
            self.audit_logger.record(
                AuditEvent(
                    event_type="confirmation_requested",
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={"reason": decision.reason, "risk_level": decision.risk_level},
                )
            )
            self._emit_policy_blocked(state, task, decision)

        payload = ((task.result or {}).get("data") or self._build_plan_confirmation_payload(task, decision))
        resume_value = interrupt(payload)
        confirmation_decision = self._normalize_confirmation_decision(resume_value)
        if confirmation_decision != "approved":
            self._reject_confirmation_task(task, confirmation_decision)
            self.audit_logger.record(
                AuditEvent(
                    event_type="confirmation.rejected",
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={"session_id": task.session_id, "confirmation_type": "plan"},
                )
            )
            self._emit(
                state.get("progress_callback"),
                ProgressEvent(
                    stage="confirmation.rejected",
                    message="用户拒绝确认，任务已阻塞。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={"confirmation_type": "plan", "decision": confirmation_decision},
                ),
            )
            return {
                "task": task,
                "session": state["session"],
                "next_node": "summarize",
                "allowed_domains": state.get("allowed_domains", []),
                "browser_site": state.get("browser_site", ""),
                "progress_callback": state.get("progress_callback"),
            }

        result_data = dict((task.result or {}).get("data") or payload)
        result_data["confirmed"] = True
        confirmation = dict(result_data.get("confirmation") or {})
        confirmation["type"] = confirmation.get("type") or "plan"
        confirmation["confirmed"] = True
        result_data["confirmation"] = confirmation
        if task.result is not None:
            task.result["data"] = result_data

        self.audit_logger.record(
            AuditEvent(
                event_type="confirmation.confirmed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": task.session_id,
                    "confirmation_type": "plan",
                    "resume_node": result_data.get("resume_node"),
                    "pending_tool_count": len(result_data.get("pending_tool_calls") or []),
                },
            )
        )
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="confirmation.confirmed",
                message="已确认计划，继续执行。",
                task_id=task.id,
                session_id=task.session_id,
                details={"confirmation_type": "plan"},
            ),
        )

        pending_tool_calls = self._deserialize_tool_calls(result_data.get("pending_tool_calls") or [])
        task.tool_calls = pending_tool_calls
        if not pending_tool_calls:
            self.task_manager.mark_blocked(
                task,
                {
                    "success": False,
                    "error": "用户已确认，但当前任务没有可执行工具。",
                    "data": {
                        "status": "blocked",
                        "block_reason": "confirmed_without_executable_tool",
                        "intent": task.intent,
                        "entities": task.entities,
                        "plan_steps": task.plan.steps if task.plan else result_data.get("plan_steps", []),
                        "confirmation_type": "plan",
                        "confirmation_summary": result_data.get("confirmation_summary") or {},
                        "pending_tool_calls": [],
                        "confirmation": confirmation,
                    },
                },
            )
            return {
                "task": task,
                "session": state["session"],
                "next_node": "summarize",
                "allowed_domains": state.get("allowed_domains", []),
                "browser_site": state.get("browser_site", ""),
                "progress_callback": state.get("progress_callback"),
            }

        return {
            "task": task,
            "session": state["session"],
            "next_node": "route_execution",
            "allowed_domains": state.get("allowed_domains", []),
            "browser_site": state.get("browser_site", ""),
            "progress_callback": state.get("progress_callback"),
        }

    def _emit_policy_blocked(self, state: OrchestrationState, task: Task, decision) -> None:
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="policy.checked",
                message=f"策略检查未直接放行，状态：{decision.status}。",
                task_id=task.id,
                session_id=task.session_id,
                details={"risk_level": decision.risk_level, "allowed": False, "status": decision.status},
            ),
        )

    def _normalize_confirmation_decision(self, resume_value) -> str:
        if isinstance(resume_value, dict):
            raw = resume_value.get("decision") or resume_value.get("status") or resume_value.get("value")
        else:
            raw = resume_value
        if raw is True:
            return "approved"
        if raw is False or raw is None:
            return "rejected"
        normalized = str(raw).strip().lower()
        if normalized in {"approve", "approved", "yes", "y", "true", "confirm", "confirmed"}:
            return "approved"
        return normalized or "rejected"

    def _route_after_policy(self, state: OrchestrationState) -> str:
        return state["next_node"]

    def _build_plan_confirmation_payload(self, task: Task, decision) -> dict:
        plan_steps = task.plan.steps if task.plan else []
        summary = {
            "current_page": "-",
            "current_url": "-",
            "prepared_action": task.plan.goal if task.plan else task.input,
            "target": task.entities.get("target_permission")
            or task.entities.get("system")
            or task.entities.get("raw_text")
            or task.input,
            "expected_outcome": "确认后继续执行计划工具。"
            if task.tool_calls
            else "确认后记录治理结果；当前没有接入实际执行工具。",
        }
        payload = {
            "status": "awaiting_confirmation",
            "intent": task.intent,
            "entities": task.entities,
            "plan_steps": plan_steps,
            "confirmation_summary": summary,
            "pending_tool_calls": [asdict(call) for call in task.tool_calls],
            "confirmation": {
                "type": "plan",
                "confirmed": False,
            },
        }
        payload.update(decision.data or {})
        payload["confirmation_type"] = "plan"
        payload["resume_node"] = "policy_check"
        payload["langgraph"] = {
            "graph": "main",
            "node": "policy_check",
            "thread_id": task.id,
            "resume": "Command(resume={'decision': 'approved'})",
        }
        return payload

    def _route_execution_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        persisted = self.task_manager.load(task.id)
        if self._has_pending_browser_confirmation(persisted):
            return self._interrupt_for_browser_confirmation(state, persisted)

        self.task_manager.mark_running(task)
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(stage="tool.running", message="正在执行工具。", task_id=task.id, session_id=task.session_id),
        )
        if not task.tool_calls:
            placeholder = self._build_placeholder_result(task)
            if placeholder["success"]:
                self.task_manager.mark_success(task, placeholder)
            else:
                self.task_manager.mark_failed(task, placeholder)
            return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}

        call_spec = task.tool_calls[0]
        call_spec.params.setdefault("trace_id", task.trace_id)
        call_spec.params.setdefault("task_id", task.id)
        call_spec.params.setdefault("session_id", task.session_id)
        call_spec.params["max_steps"] = task.max_steps
        try:
            tool_result = self._execute_tool_call(task, call_spec)
        except ToolError as exc:
            tool_result = None
            self.task_manager.mark_failed(task, {"success": False, "error": str(exc), "data": {}})
        else:
            task.artifacts.extend(tool_result.artifacts)
            self.audit_logger.record(
                AuditEvent(
                    event_type="tool_called",
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={
                        "tool_name": call_spec.tool_name,
                        "action": call_spec.action,
                        "risk_level": call_spec.risk_level,
                    },
                )
            )
            fallback_result = self._try_skill_fallback(call_spec, tool_result, task, state.get("progress_callback"))
            if fallback_result is not None:
                tool_result = fallback_result
                task.artifacts.extend(tool_result.artifacts)
            self._emit_domain_tool_events(task, tool_result, state.get("progress_callback"))
            result_status = (tool_result.data or {}).get("status")
            # ToolResult 是工具边界的统一协议；Controller 在这里把它映射成 task 生命周期状态。
            if tool_result.success:
                self.task_manager.mark_success(task, tool_result.to_dict())
            elif result_status == "awaiting_confirmation":
                self.task_manager.mark_awaiting_confirmation(task, tool_result.to_dict())
                if task.intent == "web_action" and (tool_result.data or {}).get("pending_action_raw"):
                    if (tool_result.data or {}).get("web_thread_id"):
                        self._emit_browser_interrupt_requested(state, task, tool_result.data or {})
                        return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}
                    return self._interrupt_for_browser_confirmation(state, task)
            elif result_status == "blocked":
                self.task_manager.mark_blocked(task, tool_result.to_dict())
            else:
                self.task_manager.mark_failed(task, tool_result.to_dict())
        return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}

    def _tool_execute_node(self, state: OrchestrationState) -> OrchestrationState:
        return self._route_execution_node(state)

    def _execute_tool_call(self, task: Task, call_spec: ToolCallSpec) -> ToolExecutionResult:
        if task.intent in {"ops_qa", "knowledge_write"} and call_spec.tool_name in {"knowledge", "knowledge_writer"}:
            return self.knowledge_subgraph.run(task, call_spec)
        return self.tool_executor.execute(call_spec)

    def _has_pending_browser_confirmation(self, task: Task | None) -> bool:
        if task is None or task.intent != "web_action" or task.status != "awaiting_confirmation":
            return False
        data = (task.result or {}).get("data") or {}
        return bool(data.get("pending_action_raw"))

    def _emit_browser_interrupt_requested(self, state: OrchestrationState, task: Task, payload: dict) -> None:
        self.audit_logger.record(
            AuditEvent(
                event_type="interrupt.requested",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": task.session_id,
                    "confirmation_type": "web_action",
                    "graph": "web_agent",
                    "node": "risk_gate",
                    "web_thread_id": payload.get("web_thread_id"),
                    "risk_level": (payload.get("pending_action") or {}).get("risk_level"),
                },
            )
        )
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="interrupt.requested",
                message="浏览器动作需要人工确认，Web 子图已暂停。",
                task_id=task.id,
                session_id=task.session_id,
                details={
                    "confirmation_type": "web_action",
                    "graph": "web_agent",
                    "node": "risk_gate",
                    "web_thread_id": payload.get("web_thread_id"),
                    "risk_level": (payload.get("pending_action") or {}).get("risk_level"),
                    "current_url": payload.get("resume_url"),
                },
            ),
        )

    def _interrupt_for_browser_confirmation(self, state: OrchestrationState, task: Task) -> OrchestrationState:
        result_data = dict((task.result or {}).get("data") or {})
        payload = self._build_browser_confirmation_payload(task, result_data)
        if task.result is not None:
            task.result["data"] = payload
            self.task_manager.persist(task)

        if not result_data.get("langgraph"):
            self.audit_logger.record(
                AuditEvent(
                    event_type="interrupt.requested",
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={
                        "session_id": task.session_id,
                        "confirmation_type": "web_action",
                        "risk_level": (payload.get("pending_action") or {}).get("risk_level"),
                    },
                )
            )
            self._emit(
                state.get("progress_callback"),
                ProgressEvent(
                    stage="interrupt.requested",
                    message="浏览器动作需要人工确认，LangGraph 已暂停。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "confirmation_type": "web_action",
                        "risk_level": (payload.get("pending_action") or {}).get("risk_level"),
                        "current_url": payload.get("resume_url"),
                    },
                ),
            )

        resume_value = interrupt(payload)
        confirmation_decision = self._normalize_confirmation_decision(resume_value)
        if confirmation_decision != "approved":
            self._reject_confirmation_task(task, confirmation_decision)
            self.audit_logger.record(
                AuditEvent(
                    event_type="confirmation.rejected",
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={"session_id": task.session_id, "confirmation_type": "web_action"},
                )
            )
            self._emit(
                state.get("progress_callback"),
                ProgressEvent(
                    stage="confirmation.rejected",
                    message="用户拒绝确认，任务已阻塞。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "confirmation_type": "web_action",
                        "decision": confirmation_decision,
                        "node": "route_execution",
                    },
                ),
            )
            return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}

        call_spec = self._prepare_confirmed_browser_call(task, payload)
        self.task_manager.mark_running(task)
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="tool.running",
                message="已确认，正在恢复执行工具。",
                task_id=task.id,
                session_id=task.session_id,
            ),
        )
        pending_action = payload.get("pending_action_raw") or {}
        self.audit_logger.record(
            AuditEvent(
                event_type="confirmation.confirmed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": task.session_id,
                    "node": "route_execution",
                    "confirmation_type": "web_action",
                    "action_type": pending_action.get("type"),
                    "risk_level": pending_action.get("risk_level"),
                },
            )
        )
        self._execute_confirmed_tool_call(task, call_spec, state.get("progress_callback"))
        return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}

    def _build_browser_confirmation_payload(self, task: Task, result_data: dict) -> dict:
        payload = dict(result_data)
        payload["status"] = "awaiting_confirmation"
        payload["confirmation_type"] = "web_action"
        payload["resume_node"] = "route_execution"
        payload["confirmation"] = {
            "type": "web_action",
            "confirmed": False,
        }
        payload["langgraph"] = {
            "graph": "main",
            "node": "route_execution",
            "thread_id": task.id,
            "resume": "Command(resume={'decision': 'approved'})",
        }
        return payload

    def _emit_domain_tool_events(
        self,
        task: Task,
        tool_result: ToolExecutionResult,
        progress_callback: Callable[[ProgressEvent], None] | None,
        *,
        web_step_offset: int = 0,
    ) -> None:
        data = tool_result.data or {}
        if task.intent == "web_action":
            self._emit_web_tool_events(
                task,
                tool_result,
                progress_callback,
                step_offset=web_step_offset,
            )
        elif task.intent == "ops_qa":
            answer = data.get("answer") if isinstance(data.get("answer"), dict) else {}
            sources = list(answer.get("sources") or []) if answer else []
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="knowledge.sources.ready",
                    message=f"知识来源已就绪，数量：{len(sources)}。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={"source_count": len(sources), "sources": sources},
                ),
            )
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="knowledge.answer.ready",
                    message="知识回答已生成。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "has_answer": bool(answer.get("answer")) if answer else False,
                        "confidence": answer.get("confidence") if answer else None,
                        "missing_info": answer.get("missing_info") if answer else [],
                    },
                ),
            )
        elif task.intent == "knowledge_write":
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="knowledge.write.completed",
                    message="知识写入工具已完成。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "success": bool(tool_result.success),
                        "title": data.get("title"),
                        "note_path": data.get("note_path"),
                        "type": data.get("type"),
                        "reindex_status": data.get("reindex_status"),
                    },
                ),
            )

    def _emit_web_tool_events(
        self,
        task: Task,
        tool_result: ToolExecutionResult,
        progress_callback: Callable[[ProgressEvent], None] | None,
        *,
        step_offset: int = 0,
    ) -> None:
        data = tool_result.data or {}
        skill_params = self._web_skill_params(task) or dict(data.get("skill_execution") or {})
        if skill_params:
            if not self._web_skill_params(task):
                self._emit(
                    progress_callback,
                    ProgressEvent(
                        stage="web.skill.matched",
                        message=f"命中 web skill：{skill_params.get('skill_name')}。",
                        task_id=task.id,
                        session_id=task.session_id,
                        details={
                            "skill_name": skill_params.get("skill_name"),
                            "score": skill_params.get("score"),
                            "parameters": skill_params.get("parameters") or {},
                            "matched_keywords": skill_params.get("matched_keywords") or [],
                        },
                    ),
                )
            result_status = str(data.get("status") or "")
            if tool_result.success:
                skill_message = f"web skill 执行完成：{skill_params.get('skill_name')}。"
            elif result_status == "awaiting_confirmation":
                skill_message = f"web skill 已暂停，等待确认：{skill_params.get('skill_name')}。"
            else:
                skill_message = f"web skill 执行结束：{skill_params.get('skill_name')}。"
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="web.skill.executing",
                    message=skill_message,
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "skill_name": skill_params.get("skill_name"),
                        "success": bool(tool_result.success),
                        "status": result_status,
                        "fallback_attempted": bool((data.get("skill_fallback") or {}).get("llm_fallback_used")),
                    },
                ),
            )
        if data.get("skill_fallback"):
            fallback = data.get("skill_fallback") or {}
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="web.skill.fallback",
                    message="web skill 执行未完成，已回退 LLM planner。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "skill_name": fallback.get("skill_name"),
                        "failure_category": fallback.get("failure_category"),
                        "llm_fallback_used": bool(fallback.get("llm_fallback_used")),
                    },
                ),
            )
        trace = data.get("canonical_action_trace") if isinstance(data.get("canonical_action_trace"), dict) else {}
        steps = list(trace.get("steps") or data.get("steps") or [])
        pending_action = data.get("pending_action") or trace.get("pending_action") or {}
        if pending_action:
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="web.action.proposed",
                    message="浏览器动作已规划，等待风险处理。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "action_type": pending_action.get("type"),
                        "risk_level": pending_action.get("risk_level"),
                        "requires_confirmation": bool(pending_action.get("requires_confirmation", False)),
                        "current_url": data.get("resume_url") or (data.get("confirmation_summary") or {}).get("current_url"),
                    },
                ),
            )
        for step in steps[step_offset:]:
            action = step.get("action") or {}
            observation = step.get("observation") or {}
            step_index = step.get("step_index")
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="web.action.executed",
                    message=f"浏览器动作已执行：{action.get('type') or '-'}。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "step_index": step_index,
                        "action_type": action.get("type"),
                        "risk_level": action.get("risk_level") or step.get("risk_level"),
                        "status": step.get("result"),
                    },
                ),
            )
            self._emit(
                progress_callback,
                ProgressEvent(
                    stage="web.page.observed",
                    message="浏览器页面状态已观察。",
                    task_id=task.id,
                    session_id=task.session_id,
                    details={
                        "step_index": step_index,
                        "current_url": observation.get("url"),
                        "page_type": observation.get("page_type"),
                        "artifact_paths": [
                            path
                            for path in (
                                observation.get("screenshot_path"),
                                observation.get("page_summary_path"),
                            )
                            if path
                        ],
                    },
                ),
            )

    def _try_skill_fallback(
        self,
        call_spec: ToolCallSpec,
        tool_result: ToolExecutionResult,
        task: Task,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> ToolExecutionResult | None:
        params = call_spec.params or {}
        if call_spec.tool_name == "browser_agent":
            return None
        if tool_result.success or not params.get("skill_name"):
            return None
        if not params.get("skill_fallback_to_llm_once", True) or params.get("skill_fallback_attempted"):
            return None
        result_status = (tool_result.data or {}).get("status")
        if result_status == "awaiting_confirmation":
            return None
        category = self._skill_failure_category(tool_result)
        if category in {"system_missing_information", "login_failure", "site_unavailable"}:
            return None
        fallback_params = dict(params)
        fallback_params["auto_plan"] = True
        fallback_params["actions"] = []
        fallback_params["skill_fallback_attempted"] = True
        fallback_params["skill_failed_reason"] = tool_result.error or category or "unknown"
        fallback_call = ToolCallSpec(
            tool_name=call_spec.tool_name,
            action=call_spec.action,
            params=fallback_params,
            idempotency_key=call_spec.idempotency_key,
            risk_level=call_spec.risk_level,
            timeout_seconds=call_spec.timeout_seconds,
        )
        self._emit(
            progress_callback,
            ProgressEvent(
                stage="skill.fallback",
                message="web skill 执行未完成，正在回退 LLM planner 一次。",
                task_id=task.id,
                session_id=task.session_id,
                details={"skill_name": params.get("skill_name"), "failure_category": category},
            ),
        )
        try:
            fallback_result = self.tool_executor.execute(fallback_call)
        except ToolError:
            return None
        fallback_result.data = dict(fallback_result.data or {})
        fallback_result.data["skill_fallback"] = {
            "skill_name": params.get("skill_name"),
            "original_error": tool_result.error,
            "failure_category": category,
            "llm_fallback_used": True,
        }
        return fallback_result

    def _skill_failure_category(self, tool_result: ToolExecutionResult) -> str:
        steps = (tool_result.data or {}).get("steps") or []
        for step in reversed(steps):
            reflection = step.get("reflection") or {}
            category = reflection.get("failure_category")
            if category and category != "none":
                return str(category)
        error = tool_result.error or ""
        if "无法打开目标网站" in error:
            return "site_unavailable"
        if "登录失败" in error:
            return "login_failure"
        if "系统中没有" in error:
            return "system_missing_information"
        return "execution_failure"

    def _web_skill_params(self, task: Task) -> dict:
        if task.intent != "web_action" or not task.tool_calls:
            return {}
        params = dict(task.tool_calls[0].params or {})
        if not params.get("skill_name"):
            return {}
        return params

    def _summarize_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        task.report = self.summarizer.summarize(task, task.result or {})
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(stage="summary.ready", message="已生成执行摘要。", task_id=task.id, session_id=task.session_id),
        )
        return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}

    def _persist_audit_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        session = state["session"]
        session.last_task_id = task.id

        if task.intent == "knowledge_write":
            data = (task.result or {}).get("data") or {}
            self.audit_logger.record(
                AuditEvent(
                    event_type="knowledge_write.completed",
                    trace_id=task.trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={
                        "session_id": task.session_id,
                        "title": data.get("title"),
                        "path": data.get("note_path"),
                        "type": data.get("type"),
                        "moc": data.get("moc_path"),
                        "reindex_status": data.get("reindex_status"),
                    },
                )
            )

        # 当前记忆压缩/同步不再走 ContextCompressor：
        # legacy session 字段和 LangGraph Store 都在这里同步。
        self._sync_legacy_session_and_store(session, task)
        self.task_manager.persist(task)
        self.session_store.save(session)
        self.audit_logger.record(
            AuditEvent(
                event_type="task_completed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "result_success": bool(task.result and task.result.get("success")),
                    "error": (task.result or {}).get("error"),
                    "session_id": task.session_id,
                },
            )
        )
        log_kv(self.logger, logging.INFO, "Task finished", task_id=task.id, status=task.status)
        self._emit(
            state.get("progress_callback"),
            ProgressEvent(
                stage="task.completed",
                message=f"任务已结束，状态：{task.status}。",
                task_id=task.id,
                session_id=session.id,
                details={"status": task.status},
            ),
        )
        return {"task": task, "session": session}

    def _sync_legacy_session_and_store(self, session, task: Task):
        session = self.legacy_session_memory_writer.sync(session, task)
        self._sync_session_memory_store(session, task)
        self._record_legacy_memory_synced(session, task)
        return session

    def _record_legacy_memory_synced(self, session, task: Task) -> None:
        self.audit_logger.record(
            AuditEvent(
                event_type="memory.legacy.synced",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={"session_id": session.id, "summary": session.rolling_summary},
            )
        )

    def _sync_session_memory_store(self, session, task: Task) -> None:
        try:
            result = self.session_memory_manager.sync(session, task)
        except Exception as exc:
            log_kv(self.logger, logging.WARNING, "LangGraph memory sync failed", task_id=task.id, error=str(exc))
            return
        namespaces = ["/".join(namespace) for namespace in result.get("namespaces", [])]
        self.audit_logger.record(
            AuditEvent(
                event_type="memory.store.synced",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": getattr(session, "id", task.session_id),
                    "namespaces": namespaces,
                    "migrated_legacy": bool(result.get("migrated_legacy", False)),
                },
            )
        )

    def _emit(self, callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
        callback = callback or getattr(self._runtime_context, "progress_callback", None)
        trace_id = getattr(self._runtime_context, "trace_id", None)
        event.with_details(
            trace_id=trace_id,
            task_id=event.task_id,
            session_id=event.session_id,
            graph="main",
            node=self._node_from_stage(event.stage),
            status=event.details.get("status") if event.details else None,
        )
        if callback is not None:
            callback(event)

    def _node_from_stage(self, stage: str) -> str | None:
        if stage in {"graph.started", "session.created", "session.resumed", "task.created"}:
            return "intake"
        if stage == "intent.parsed":
            return "intent_parse"
        if stage == "plan.generated":
            return "task_plan"
        if stage == "web.skill.matched":
            return "task_plan"
        if stage in {"policy.checked", "confirmation.confirmed", "confirmation.rejected"}:
            return "policy_check"
        if stage == "graph.interrupted":
            return None
        if stage in {
            "tool.running",
            "skill.fallback",
            "interrupt.requested",
            "web.action.proposed",
            "web.action.executed",
            "web.page.observed",
            "web.skill.executing",
            "web.skill.fallback",
            "knowledge.sources.ready",
            "knowledge.answer.ready",
            "knowledge.write.completed",
        }:
            return "route_execution"
        if stage == "summary.ready":
            return "summarize"
        if stage == "task.completed":
            return "persist_audit"
        return None

    def _audit_task_input(self, task_input: str) -> str:
        lowered = task_input.lower()
        write_keywords = (
            "记录到知识库",
            "保存到知识库",
            "添加入知识库",
            "添加到知识库",
            "加入知识库",
            "写入知识库",
            "录入知识库",
            "沉淀文档",
            "写入 vault",
            "写入vault",
            "/save-note",
            "save note",
            "write note",
            "save to knowledge",
            "record to knowledge",
            "生成知识库",
            "生成 knowledge",
            "生成knowledge",
            "整理成知识库",
            "整理成 knowledge",
            "整理成knowledge",
            "知识沉淀",
            "knowledge base",
            "vault",
        )
        if any(keyword in lowered for keyword in write_keywords):
            return "[knowledge_write redacted]"
        return task_input

    def _audit_entities(self, intent: str, entities: dict) -> dict:
        if intent != "knowledge_write":
            return entities
        return {
            "system": entities.get("system"),
            "env": entities.get("env"),
            "explicit_trigger": bool(entities.get("explicit_trigger")),
            "dry_run": bool(entities.get("dry_run", False)),
        }

    def _build_placeholder_result(self, task: Task) -> dict:
        if task.intent == "ops_qa":
            return {
                "success": True,
                "error": None,
                "data": {
                    "message": "知识检索工具尚未接入，当前已完成统一入口与编排预留。",
                    "entities": task.entities,
                },
            }
        return {
            "success": False,
            "error": f"暂不支持的任务类型: {task.intent}",
            "data": {"entities": task.entities},
        }
