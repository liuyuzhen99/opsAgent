from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, StateGraph

from aiops_agent.audit.models import AuditEvent
from aiops_agent.agent.context import ContextCompressor
from aiops_agent.agent.progress import ProgressEvent
from aiops_agent.browser.skills import WebSkillGenerationError, WebSkillGenerator, WebSkillSaveResult
from aiops_agent.support.logging import log_kv
from aiops_agent.support.trace import get_trace_id
from aiops_agent.tasks.models import Task, ToolCallSpec, ToolExecutionResult
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
        web_skill_generator: WebSkillGenerator | None = None,
        credential_ref_resolver: Callable[[str], str | None] | None = None,
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
        self.web_skill_generator = web_skill_generator
        self.credential_ref_resolver = credential_ref_resolver
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
                details={"input": self._audit_task_input(task_input), "session_id": session.id},
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
        call_spec.params["prior_steps"] = result_data.get("steps") or []
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

    def save_web_skill(self, session_id: str | None, name: str | None = None) -> WebSkillSaveResult:
        if not session_id:
            raise ValueError("当前还没有 active session，无法保存 skill。")
        session = self.session_store.load(session_id)
        if session is None:
            raise ValueError(f"当前 session 尚未持久化: {session_id}")
        task_id = session.metadata.get("browser_last_success_task_id")
        if not task_id:
            raise ValueError("当前 session 没有最近一次成功的 web_action。")
        task = self.task_manager.load(task_id)
        if task is None:
            raise ValueError(f"最近成功 web_action 任务不存在: {task_id}")
        generator = self.web_skill_generator or WebSkillGenerator()
        try:
            return generator.generate_from_task(task, name=name)
        except WebSkillGenerationError as exc:
            raise ValueError(str(exc)) from exc

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
            self._apply_browser_site(task, site_key)
        elif task.intent == "web_action" and not task.entities.get("site_key"):
            site_key = self._browser_site_key_from_text(task.input)
            if site_key:
                self._apply_browser_site(task, site_key)
        if task.intent == "web_action" and task.entities.get("site_key") and not task.entities.get("credential_ref"):
            credential_ref = self._default_credential_ref(str(task.entities["site_key"]))
            if credential_ref:
                task.entities["credential_ref"] = credential_ref
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
        for site_key in sorted(self.browser_sites_config.sites):
            if site_key.lower() in lowered:
                return site_key
        return None

    def _default_credential_ref(self, site_key: str) -> str | None:
        if self.credential_ref_resolver is None:
            return None
        return self.credential_ref_resolver(site_key)

    def _task_plan_node(self, state: OrchestrationState) -> OrchestrationState:
        task = state["task"]
        session = state["session"]

        if task.intent in {"ops_qa", "knowledge_write"}:
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
            fallback_result = self._try_skill_fallback(call_spec, tool_result, task, state.get("progress_callback"))
            if fallback_result is not None:
                tool_result = fallback_result
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
        return {"task": task, "session": state["session"], "progress_callback": state.get("progress_callback")}

    def _try_skill_fallback(
        self,
        call_spec: ToolCallSpec,
        tool_result: ToolExecutionResult,
        task: Task,
        progress_callback: Callable[[ProgressEvent], None] | None,
    ) -> ToolExecutionResult | None:
        params = call_spec.params or {}
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
