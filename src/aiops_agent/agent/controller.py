from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, StateGraph

from aiops_agent.audit.models import AuditEvent
from aiops_agent.agent.context import ContextCompressor
from aiops_agent.agent.progress import ProgressEvent
from aiops_agent.support.logging import log_kv
from aiops_agent.support.trace import get_trace_id
from aiops_agent.tasks.models import Task
from aiops_agent.tasks.manager import TaskManager
from aiops_agent.tools.base import ToolError
from aiops_agent.tools.executor import ToolExecutor
from aiops_agent.policy import PolicyEngine
from aiops_agent.planning import PlanningService
from aiops_agent.browser.site_config import BrowserSitesConfig, BrowserSiteConfigError


class OrchestrationState(TypedDict, total=False):
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
        context_compressor: ContextCompressor | None = None,
        browser_sites_config: BrowserSitesConfig | None = None,
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
        self.context_compressor = context_compressor or ContextCompressor()
        self.browser_sites_config = browser_sites_config or BrowserSitesConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.graph = self._build_graph()

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
        trace_id = get_trace_id()
        log_kv(self.logger, logging.INFO, "Received task input", trace_id=trace_id)
        session = self.session_store.create_or_resume(session_id)
        session_event = "session.resumed" if session.task_ids else "session.created"
        self._emit(
            progress_callback,
            ProgressEvent(
                stage=session_event,
                message="已恢复会话。" if session.task_ids else "已创建会话。",
                session_id=session.id,
            ),
        )
        task = self.task_manager.create_task(
            task_input=task_input,
            trace_id=trace_id,
            session_id=session.id,
            llm_profile=llm_profile,
            max_steps=max_steps,
            requires_explicit_confirmation=require_confirmation,
        )
        self._emit(
            progress_callback,
            ProgressEvent(stage="task.created", message="已创建任务。", task_id=task.id, session_id=session.id),
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
                details={"input": task_input, "session_id": session.id},
            )
        )
        final_state = self.graph.invoke(
            {
                "task": task,
                "session": session,
                "allowed_domains": allowed_domains or [],
                "credential_ref": credential_ref or "",
                "browser_trace": browser_trace,
                "browser_video": browser_video,
                "browser_site": browser_site or "",
                "browser_channel": browser_channel or "",
                "browser_slow_mo_ms": browser_slow_mo_ms,
                "progress_callback": progress_callback,
            }
        )
        return final_state["task"]

    def confirm(
        self,
        task_id: str,
        *,
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> Task:
        task = self.task_manager.load(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        if task.status != "awaiting_confirmation":
            raise ValueError(f"任务不是等待确认状态: {task.status}")
        result_data = (task.result or {}).get("data") or {}
        pending_action = result_data.get("pending_action_raw")
        if not pending_action:
            raise ValueError("任务缺少待确认动作，无法恢复执行")
        if not task.tool_calls:
            raise ValueError("任务缺少工具调用，无法恢复执行")

        call_spec = task.tool_calls[0]
        call_spec.params = dict(call_spec.params)
        call_spec.params["confirmed_action"] = pending_action
        call_spec.params["replay_actions"] = result_data.get("replay_actions") or []
        call_spec.params["completed_action_keys"] = result_data.get("completed_action_keys") or []
        call_spec.params["requires_remote_mutation"] = False
        call_spec.params["start_url"] = result_data.get("resume_url") or call_spec.params.get("start_url")
        call_spec.params["session_state_path"] = result_data.get("session_state_path") or call_spec.params.get("session_state_path")
        call_spec.params["trace_id"] = task.trace_id
        call_spec.params["task_id"] = task.id
        call_spec.params["session_id"] = task.session_id
        call_spec.params["max_steps"] = min(int(call_spec.params.get("max_steps", task.max_steps)), task.max_steps)
        task.tool_calls = [call_spec]
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
        self.audit_logger.record(
            AuditEvent(
                event_type="confirmation.confirmed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": task.session_id,
                    "action_type": pending_action.get("type"),
                    "risk_level": pending_action.get("risk_level"),
                },
            )
        )
        try:
            tool_result = self.tool_executor.execute(call_spec)
        except ToolError as exc:
            self.task_manager.mark_failed(task, {"success": False, "error": str(exc), "data": {}})
        else:
            task.artifacts.extend(tool_result.artifacts)
            result_status = (tool_result.data or {}).get("status")
            if tool_result.success:
                self.task_manager.mark_success(task, tool_result.to_dict())
            elif result_status == "awaiting_confirmation":
                self.task_manager.mark_awaiting_confirmation(task, tool_result.to_dict())
            elif result_status == "blocked":
                self.task_manager.mark_blocked(task, tool_result.to_dict())
            else:
                self.task_manager.mark_failed(task, tool_result.to_dict())
        task.report = self.summarizer.summarize(task, task.result or {})
        self._emit(
            progress_callback,
            ProgressEvent(stage="summary.ready", message="已生成执行摘要。", task_id=task.id, session_id=task.session_id),
        )
        self.task_manager.persist(task)
        self.audit_logger.record(
            AuditEvent(
                event_type="task.completed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "session_id": task.session_id,
                    "result_success": bool(task.result and task.result.get("success")),
                    "error": (task.result or {}).get("error"),
                },
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

    def _build_graph(self):
        graph = StateGraph(OrchestrationState)
        graph.add_node("intent_parse", self._intent_parse_node)
        graph.add_node("task_plan", self._task_plan_node)
        graph.add_node("policy_check", self._policy_check_node)
        graph.add_node("tool_execute", self._tool_execute_node)
        graph.add_node("summarize", self._summarize_node)
        graph.add_node("persist_audit", self._persist_audit_node)
        graph.set_entry_point("intent_parse")
        graph.add_edge("intent_parse", "task_plan")
        graph.add_edge("task_plan", "policy_check")
        graph.add_conditional_edges(
            "policy_check",
            self._route_after_policy,
            {"tool_execute": "tool_execute", "summarize": "summarize"},
        )
        graph.add_edge("tool_execute", "summarize")
        graph.add_edge("summarize", "persist_audit")
        graph.add_edge("persist_audit", END)
        return graph.compile()

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
        if state.get("browser_trace"):
            task.entities["trace_enabled"] = True
        if state.get("browser_video"):
            task.entities["video_enabled"] = True
        if state.get("browser_channel"):
            task.entities["browser_channel"] = state["browser_channel"]
        if state.get("browser_slow_mo_ms"):
            task.entities["browser_slow_mo_ms"] = int(state["browser_slow_mo_ms"])
        if state.get("browser_site"):
            site_key = str(state["browser_site"])
            try:
                site = self.browser_sites_config.get(site_key)
            except BrowserSiteConfigError as exc:
                task.intent = "web_action"
                task.entities["browser_config_error"] = str(exc)
                task.entities["site_key"] = site_key
            else:
                task.intent = "web_action"
                task.entities["site_key"] = site_key
                task.entities["site_config"] = site.to_runtime_dict()
                task.entities["start_url"] = task.entities.get("start_url") or site.login_url or site.base_url
                existing_domains = list(task.entities.get("allowed_domains") or [])
                task.entities["allowed_domains"] = sorted(set(existing_domains + site.allowed_domains))
                task.entities["requires_login"] = bool(site.login_url or site.login_fields)
        task.current_stage = "planning"
        task.status = "planning"
        self.audit_logger.record(
            AuditEvent(
                event_type="intent_parsed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={"intent": task.intent, "entities": task.entities},
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

    def _task_plan_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        session = state["session"]

        if task.intent == "ops_qa":
            import json
            raw_turns = session.metadata.get("qa_turns", "")
            try:
                qa_turns = json.loads(raw_turns) if raw_turns else []
            except (json.JSONDecodeError, ValueError):
                qa_turns = []
            task.entities["conversation_history"] = qa_turns[-5:]

        plan = self.planning_service.plan(task.input, task.intent, task.entities)
        task.plan = plan
        task.selected_tools = plan.selected_tools
        task.tool_calls = list(plan.tool_calls)
        task.risk_level = plan.risk_level
        task.confirmation_required = plan.confirmation_required
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
                "next_node": "tool_execute",
                "allowed_domains": state.get("allowed_domains", []),
                "browser_site": state.get("browser_site", ""),
                "progress_callback": state.get("progress_callback"),
            }

        if decision.status == "awaiting_confirmation":
            self.task_manager.mark_awaiting_confirmation(
                task,
                {
                    "success": False,
                    "error": decision.reason,
                    "data": {
                        "intent": task.intent,
                        "entities": task.entities,
                        "plan_steps": task.plan.steps if task.plan else [],
                    },
                },
            )
            event_type = "confirmation_requested"
        else:
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
            event_type = "policy_blocked"

        self.audit_logger.record(
            AuditEvent(
                event_type=event_type,
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={"reason": decision.reason, "risk_level": decision.risk_level},
            )
        )
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
        return {
            "task": task,
            "session": state["session"],
            "next_node": "summarize",
            "allowed_domains": state.get("allowed_domains", []),
            "browser_site": state.get("browser_site", ""),
            "progress_callback": state.get("progress_callback"),
        }

    def _route_after_policy(self, state: OrchestrationState) -> str:
        return state["next_node"]

    def _tool_execute_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
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
            tool_result = self.tool_executor.execute(call_spec)
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
            result_status = (tool_result.data or {}).get("status")
            if tool_result.success:
                self.task_manager.mark_success(task, tool_result.to_dict())
            elif result_status == "awaiting_confirmation":
                self.task_manager.mark_awaiting_confirmation(task, tool_result.to_dict())
            elif result_status == "blocked":
                self.task_manager.mark_blocked(task, tool_result.to_dict())
            else:
                self.task_manager.mark_failed(task, tool_result.to_dict())
        return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}

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

        if task.intent == "ops_qa" and task.status == "success":
            import json
            answer_block = (task.result or {}).get("data", {}).get("answer", {})
            answer_text = answer_block.get("answer", "") if isinstance(answer_block, dict) else ""
            if answer_text:
                raw_turns = session.metadata.get("qa_turns", "")
                try:
                    qa_turns = json.loads(raw_turns) if raw_turns else []
                except (json.JSONDecodeError, ValueError):
                    qa_turns = []
                qa_turns.append({"question": task.input, "answer": answer_text})
                session.metadata["qa_turns"] = json.dumps(qa_turns[-5:], ensure_ascii=False)

        session = self.context_compressor.compress(session, task)
        self.task_manager.persist(task)
        self.session_store.save(session)
        self.audit_logger.record(
            AuditEvent(
                event_type="memory.compressed",
                trace_id=task.trace_id,
                task_id=task.id,
                status=task.status,
                details={"session_id": session.id, "summary": session.rolling_summary},
            )
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

    def _emit(self, callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
        if callback is not None:
            callback(event)

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
