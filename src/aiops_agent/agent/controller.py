from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from aiops_agent.audit.models import AuditEvent
from aiops_agent.support.logging import log_kv
from aiops_agent.support.trace import get_trace_id
from aiops_agent.tasks.models import Task
from aiops_agent.tasks.manager import TaskManager
from aiops_agent.tools.base import ToolError
from aiops_agent.tools.executor import ToolExecutor
from aiops_agent.policy import PolicyEngine
from aiops_agent.planning import PlanningService


class OrchestrationState(TypedDict, total=False):
    task: Task
    session: object
    next_node: str


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
    ) -> Task:
        trace_id = get_trace_id()
        log_kv(self.logger, logging.INFO, "Received task input", trace_id=trace_id)
        session = self.session_store.create_or_resume(session_id)
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
        self.audit_logger.record(
            AuditEvent(
                event_type="task_created",
                trace_id=trace_id,
                task_id=task.id,
                status=task.status,
                details={"input": task_input, "session_id": session.id},
            )
        )
        final_state = self.graph.invoke({"task": task, "session": session})
        return final_state["task"]

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
        return {"task": task, "session": state["session"]}

    def _task_plan_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
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
        return {"task": task, "session": state["session"]}

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
            return {"task": task, "session": state["session"], "next_node": "tool_execute"}

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
        return {"task": task, "session": state["session"], "next_node": "summarize"}

    def _route_after_policy(self, state: OrchestrationState) -> str:
        return state["next_node"]

    def _tool_execute_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        self.task_manager.mark_running(task)
        if not task.tool_calls:
            placeholder = self._build_placeholder_result(task)
            if placeholder["success"]:
                self.task_manager.mark_success(task, placeholder)
            else:
                self.task_manager.mark_failed(task, placeholder)
            return {"task": task, "session": state["session"]}

        call_spec = task.tool_calls[0]
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
            if tool_result.success:
                self.task_manager.mark_success(task, tool_result.to_dict())
            else:
                self.task_manager.mark_failed(task, tool_result.to_dict())
        return {"task": task, "session": state["session"]}

    def _summarize_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        task.report = self.summarizer.summarize(task, task.result or {})
        return {"task": task, "session": state["session"]}

    def _persist_audit_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        session = state["session"]
        session.last_task_id = task.id
        session.summary = f"last_intent={task.intent}; last_status={task.status}"
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
        return {"task": task, "session": session}

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
