from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from aiops_agent.agent.state_codec import (
    action_result_from_state,
    action_result_to_state,
    artifact_from_state,
    artifact_to_state,
    browser_action_from_state,
    browser_action_to_state,
    browser_observation_from_state,
    browser_observation_to_state,
    browser_task_spec_from_state,
    browser_task_spec_to_state,
    tool_result_from_state,
    tool_result_to_state,
    to_plain,
    web_step_result_from_state,
    web_step_result_to_state,
)
from aiops_agent.browser.action_trace import build_canonical_action_trace
from aiops_agent.browser.credentials import CredentialError
from aiops_agent.browser.models import ActionResult, BrowserAction, BrowserObservation, BrowserTaskSpec
from aiops_agent.tasks.models import TaskArtifact, ToolExecutionResult


class WebAgentSubgraphState(TypedDict, total=False):
    run_id: str
    params: dict[str, Any]
    spec: BrowserTaskSpec
    session_id: str
    task_id: str
    trace_id: str
    active_key: str
    live_resume: bool
    keep_browser_open: bool
    steps: list[dict]
    artifacts: list[TaskArtifact]
    consecutive_failures: int
    step_index: int
    action: BrowserAction
    runtime_action: BrowserAction
    step_result: ToolExecutionResult | tuple[ActionResult, BrowserObservation]
    action_result: ActionResult
    observation: BrowserObservation
    result: ToolExecutionResult
    route: str
    web_memory_context: dict[str, Any]
    running_summary: str
    skill_execution: dict[str, Any]


class WebAgentSubgraph:
    def __init__(self, host, *, checkpointer=None, store=None):
        # host 是外层 BrowserAgentTool。子图只负责 LangGraph 编排；
        # 创建浏览器、风险判断、审计、artifact 等领域能力都回调给 host。
        self.host = host
        self.checkpointer = checkpointer
        self.store = store
        self.graph = self._build_graph()
        # 进程内运行态：保存 Playwright tool、steps、artifacts 等不适合进 checkpoint 的对象。
        # 进程重启后这里会丢失，所以 resume 时还要能从 checkpoint + storage_state 重建。
        self._contexts: dict[str, dict[str, Any]] = {}

    def run(self, params: dict) -> ToolExecutionResult:
        # 每次 Web 执行都有一个 run_id；后续 interrupt payload、内存上下文和恢复逻辑都靠它串起来。
        run_id = str(params.get("web_run_id") or uuid4())
        params = dict(params)
        params["web_run_id"] = run_id
        self._contexts[run_id] = {"params": params, "steps": [], "artifacts": []}
        # interrupted=True 表示图停在人工确认点，此时不能清理 _contexts，
        # 因为 live resume 还要复用原浏览器页面。
        interrupted = False
        try:
            state = self.graph.invoke(
                {"run_id": run_id, "params": params},
                config=self._graph_config(self._thread_id(params, run_id)),
            )
            state = self._runtime_state(state) if isinstance(state, dict) else state
            if state.get("__interrupt__"):
                interrupted = True
                # 把 LangGraph interrupt 转成 ToolExecutionResult(status=awaiting_confirmation)。
                return self._interrupted_result(state)
            return state["result"]
        except Exception as exc:
            return self._failed_result(run_id, exc)
        finally:
            # 正常完成或失败都释放进程内上下文；只有等待人工确认时保留。
            if not interrupted:
                self._contexts.pop(run_id, None)

    def resume(self, params: dict) -> ToolExecutionResult:
        thread_id = str(params.get("web_thread_id") or "")
        if not thread_id:
            return ToolExecutionResult(
                success=False,
                error="浏览器恢复缺少 web_thread_id。",
                retryable=False,
                data={"status": "failed"},
            )
        run_id = str(params.get("web_run_id") or thread_id.rsplit(":", 1)[-1])
        # resume 前先恢复运行上下文：优先复用活浏览器；没有就用 checkpoint/spec 和 storage_state 新建。
        self._restore_context_for_resume(run_id, thread_id, params)
        interrupted = False
        try:
            state = self.graph.invoke(
                # LangGraph 会把这个 resume 值交还给之前 interrupt(payload) 的位置。
                Command(resume={"decision": params.get("confirmation_decision") or "approved"}),
                config=self._graph_config(thread_id),
            )
            state = self._runtime_state(state) if isinstance(state, dict) else state
            if state.get("__interrupt__"):
                interrupted = True
                return self._interrupted_result(state)
            return state["result"]
        except Exception as exc:
            return self._failed_result(run_id, exc)
        finally:
            if not interrupted:
                self._contexts.pop(run_id, None)

    def get_state(self, thread_id: str):
        return self.graph.get_state(self._graph_config(thread_id))

    def get_state_history(self, thread_id: str):
        return list(self.graph.get_state_history(self._graph_config(thread_id)))

    def _build_graph(self):
        graph = StateGraph(WebAgentSubgraphState)
        graph.add_node("prepare_spec", self._checkpointed_node(self._prepare_spec_node))
        graph.add_node("load_web_memory", self._checkpointed_node(self._load_web_memory_node))
        graph.add_node("restore_browser_context", self._checkpointed_node(self._restore_browser_context_node))
        graph.add_node("plan_action", self._checkpointed_node(self._plan_action_node))
        graph.add_node("stabilize_action", self._checkpointed_node(self._stabilize_action_node))
        graph.add_node("risk_gate", self._checkpointed_node(self._risk_gate_node))
        graph.add_node("execute_action", self._checkpointed_node(self._execute_action_node))
        graph.add_node("observe_page", self._checkpointed_node(self._observe_page_node))
        graph.add_node("reflect", self._checkpointed_node(self._reflect_node))
        graph.add_node("route_next", self._checkpointed_node(self._route_next_node))
        graph.add_node("skill_fallback", self._checkpointed_node(self._skill_fallback_node))
        graph.add_node("finalize", self._checkpointed_node(self._finalize_node))
        graph.set_entry_point("prepare_spec")
        graph.add_conditional_edges(
            "prepare_spec",
            self._route_after_prepare,
            {"load_web_memory": "load_web_memory", "finalize": "finalize"},
        )
        graph.add_edge("load_web_memory", "restore_browser_context")
        graph.add_edge("restore_browser_context", "plan_action")
        graph.add_conditional_edges(
            "plan_action",
            self._route_after_plan_action,
            {"stabilize_action": "stabilize_action", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "stabilize_action",
            self._route_after_stabilize_action,
            {"risk_gate": "risk_gate", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "risk_gate",
            self._route_after_risk_gate,
            {"execute_action": "execute_action", "finalize": "finalize"},
        )
        graph.add_edge("execute_action", "observe_page")
        graph.add_conditional_edges(
            "observe_page",
            self._route_after_observe_page,
            {"reflect": "reflect", "finalize": "finalize"},
        )
        graph.add_edge("reflect", "route_next")
        graph.add_conditional_edges(
            "route_next",
            self._route_after_route_next,
            {"plan_action": "plan_action", "skill_fallback": "skill_fallback", "finalize": "finalize"},
        )
        graph.add_edge("skill_fallback", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self.checkpointer if self.checkpointer is not None else False, store=self.store, name="web_agent")

    def _checkpointed_node(self, func):
        def wrapped(state: WebAgentSubgraphState) -> WebAgentSubgraphState:
            return self._checkpoint_state(func(self._runtime_state(state)))

        return wrapped

    def _runtime_state(self, state: dict) -> dict:
        runtime = dict(state)
        if "spec" in runtime and isinstance(runtime["spec"], dict):
            runtime["spec"] = browser_task_spec_from_state(runtime["spec"])
        for key in ("action", "runtime_action"):
            if key in runtime and isinstance(runtime[key], dict):
                runtime[key] = browser_action_from_state(runtime[key])
        if "artifacts" in runtime:
            runtime["artifacts"] = [artifact_from_state(item) for item in runtime.get("artifacts") or []]
        if "step_result" in runtime:
            runtime["step_result"] = web_step_result_from_state(runtime["step_result"])
        if "action_result" in runtime and isinstance(runtime["action_result"], dict):
            runtime["action_result"] = action_result_from_state(runtime["action_result"])
        if "observation" in runtime and isinstance(runtime["observation"], dict):
            runtime["observation"] = browser_observation_from_state(runtime["observation"])
        if "result" in runtime and isinstance(runtime["result"], dict) and "success" in runtime["result"]:
            runtime["result"] = tool_result_from_state(runtime["result"])
        return runtime

    def _checkpoint_state(self, state: dict | None) -> dict:
        if not state:
            return {}
        checkpoint = dict(state)
        if "spec" in checkpoint:
            checkpoint["spec"] = browser_task_spec_to_state(checkpoint["spec"])
        for key in ("action", "runtime_action"):
            if key in checkpoint:
                checkpoint[key] = browser_action_to_state(checkpoint[key])
        if "artifacts" in checkpoint:
            checkpoint["artifacts"] = [artifact_to_state(item) for item in checkpoint.get("artifacts") or []]
        if "step_result" in checkpoint:
            checkpoint["step_result"] = web_step_result_to_state(checkpoint["step_result"])
        if "action_result" in checkpoint:
            checkpoint["action_result"] = action_result_to_state(checkpoint["action_result"])
        if "observation" in checkpoint:
            checkpoint["observation"] = browser_observation_to_state(checkpoint["observation"])
        if "result" in checkpoint:
            checkpoint["result"] = tool_result_to_state(checkpoint["result"])
        return to_plain(checkpoint)

    def _prepare_spec_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        params = self._apply_skill_match(dict(state["params"]))
        spec = self.host._spec_from_params(params)
        session_id = str(params.get("session_id", "default"))
        task_id = str(params.get("task_id", ""))
        trace_id = str(params.get("trace_id", ""))
        validation_error = self.host._validate_spec(spec, params)
        if validation_error:
            return {
                "spec": spec,
                "session_id": session_id,
                "task_id": task_id,
                "trace_id": trace_id,
                "result": ToolExecutionResult(
                    success=False,
                    error=validation_error,
                    retryable=False,
                    data={"status": "blocked", "goal": spec.user_goal, "site_key": spec.site_key, "workflow": spec.workflow},
                ),
                "route": "finalize",
            }
        spec.session_state_path = spec.session_state_path or self.host._default_session_state_path(session_id)
        try:
            self.host._attach_credentials(spec)
        except CredentialError as exc:
            return {
                "spec": spec,
                "session_id": session_id,
                "task_id": task_id,
                "trace_id": trace_id,
                "result": ToolExecutionResult(
                    success=False,
                    error=str(exc),
                    retryable=False,
                    data={"status": "failed", "goal": spec.user_goal, "credential_ref": spec.credential_ref},
                ),
                "route": "finalize",
            }
        spec = self._checkpoint_safe_spec(spec)
        return {
            "params": params,
            "spec": spec,
            "session_id": session_id,
            "task_id": task_id,
            "trace_id": trace_id,
            "skill_execution": dict(params.get("skill_execution") or {}),
            "route": "load_web_memory",
        }

    def _load_web_memory_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        session_memory = dict((state["params"].get("session_memory") or {}))
        web_memory = dict(session_memory.get("browser_memory") or {})
        return {
            "web_memory_context": web_memory,
            "running_summary": str(session_memory.get("summary") or session_memory.get("rolling_summary") or ""),
        }

    def _restore_browser_context_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        spec = state["spec"]
        params = state["params"]
        session_id = state["session_id"]
        task_id = state["task_id"]
        active_key = self.host._active_tool_key(session_id, task_id)
        # confirmed_action 说明这是确认后的恢复路径。若 _active_tools 还能找到 tool，
        # 就是同进程 live resume；否则说明进程/对象已丢失，需要重新创建浏览器上下文。
        tool = self.host._active_tools.get(active_key) if spec.confirmed_action else None
        live_resume = tool is not None
        if tool is None:
            tool = self.host._create_browser_tool(
                session_id=session_id,
                task_id=task_id,
                headless=bool(params.get("headless", self.host.headless)),
                allowed_domains=spec.allowed_domains,
                session_state_path=spec.session_state_path,
                trace_enabled=spec.trace_enabled,
                video_enabled=spec.video_enabled,
                browser_channel=spec.browser_channel,
                slow_mo_ms=spec.browser_slow_mo_ms,
            )
        # live resume 保留确认前已经执行过的 steps；crash resume 依赖 checkpoint/params 重新推进。
        steps = list(params.get("prior_steps") or []) if live_resume else []
        artifacts: list[TaskArtifact] = []
        self.host._record("browser.started", state["trace_id"], task_id, session_id, 0, {"start_url": spec.start_url})
        self._update_context(
            state["run_id"],
            spec=spec,
            session_id=session_id,
            task_id=task_id,
            trace_id=state["trace_id"],
            active_key=active_key,
            tool=tool,
            steps=steps,
            artifacts=artifacts,
            keep_browser_open=False,
        )
        return {
            "active_key": active_key,
            "live_resume": live_resume,
            "steps": steps,
            "artifacts": artifacts,
            "consecutive_failures": 0,
            "keep_browser_open": False,
        }

    def _plan_action_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        try:
            spec = self._runtime_spec(state["spec"])
        except CredentialError as exc:
            result = ToolExecutionResult(
                success=False,
                error=str(exc),
                retryable=False,
                data={"status": "failed", "goal": state["spec"].user_goal, "credential_ref": state["spec"].credential_ref},
            )
            return {"result": result, "route": "finalize"}
        tool = self._tool(state)
        artifacts = list(state.get("artifacts") or [])
        steps = list(state.get("steps") or [])
        step_index = int(state.get("step_index", 0)) + 1
        if step_index > spec.max_steps:
            last_observation = tool.observe(last_action_result="step budget exceeded", force_artifact=True)
            artifacts.extend(self.host._artifacts_from_observation(last_observation))
            result = self.host._blocked_result("达到最大浏览器步骤预算。", steps, last_observation, artifacts)
            self._update_context(state["run_id"], steps=steps, artifacts=artifacts)
            return {"step_index": step_index, "artifacts": artifacts, "result": result, "route": "finalize"}
        action = self.host._next_action(spec, steps)
        return {"step_index": step_index, "action": action, "route": "stabilize_action"}

    def _stabilize_action_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        spec = state["spec"]
        tool = self._tool(state)
        action = state["action"]
        steps = state["steps"]
        artifacts = state["artifacts"]

        if self.host._is_repeated_action(action, steps, spec.repeated_action_threshold):
            observation = tool.observe(last_action_result="repeated action blocked", force_artifact=True)
            artifacts.extend(self.host._artifacts_from_observation(observation))
            result = self.host._blocked_result("检测到同一页面重复动作超过阈值，已停止执行。", steps, observation, artifacts)
            self._update_context(state["run_id"], steps=steps, artifacts=artifacts)
            return {"artifacts": artifacts, "result": result, "route": "finalize"}

        proposed_action = self.host._stabilize_action(spec, action, steps)
        intent_aligned, intent_reason = self.host._action_intent_alignment(spec, proposed_action, steps)
        if not intent_aligned:
            observation = tool.observe(last_action_result="intent mismatch blocked", force_artifact=True)
            artifacts.extend(self.host._artifacts_from_observation(observation))
            result = self.host._blocked_result(f"规划动作与用户意图不一致，已停止执行：{intent_reason}", steps, observation, artifacts)
            self._update_context(state["run_id"], steps=steps, artifacts=artifacts)
            return {"action": proposed_action, "artifacts": artifacts, "result": result, "route": "finalize"}
        return {"action": proposed_action, "route": "risk_gate"}

    def _risk_gate_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        spec = state["spec"]
        tool = self._tool(state)
        proposed_action = state["action"]
        runtime_action = self.host._runtime_action(proposed_action)
        # 每个动作执行前都重新分类风险；unsafe/unknown 会被标记 requires_confirmation。
        self.host._prepare_runtime_action_for_risk(spec, proposed_action, runtime_action)
        self.host._record_action_proposed(
            action=proposed_action,
            trace_id=state["trace_id"],
            task_id=state["task_id"],
            session_id=state["session_id"],
            step_index=state["step_index"],
        )
        if proposed_action.requires_confirmation:
            artifacts = state["artifacts"]
            steps = state["steps"]
            result = self.host._awaiting_confirmation_result(
                tool=tool,
                action=proposed_action,
                trace_id=state["trace_id"],
                task_id=state["task_id"],
                session_id=state["session_id"],
                step_index=state["step_index"],
                steps=steps,
                artifacts=artifacts,
                spec=spec,
            )
            self.host._active_tools[state["active_key"]] = tool
            payload = self._interrupt_payload(state, proposed_action, runtime_action, result)
            self._update_context(
                state["run_id"],
                active_key=state["active_key"],
                steps=steps,
                artifacts=artifacts,
                keep_browser_open=True,
                interrupt_payload=payload,
            )
            # 这里真正暂停 LangGraph。payload 同时给人看确认摘要，也给 resume 用来恢复上下文。
            resume_value = interrupt(payload)
            if self._confirmation_decision(resume_value) != "approved":
                blocked = ToolExecutionResult(
                    success=False,
                    error="用户拒绝确认，浏览器动作未执行。",
                    retryable=False,
                    data={
                        "status": "blocked",
                        "confirmation_decision": self._confirmation_decision(resume_value),
                        "steps": steps,
                        "canonical_action_trace": build_canonical_action_trace(
                            steps,
                            status="blocked",
                            task_id=state["task_id"],
                            session_id=state["session_id"],
                        ),
                    },
                    artifacts=artifacts,
                )
                return {"result": blocked, "keep_browser_open": False, "route": "finalize"}
            # 确认后只放行当前 pending action，一次性清掉确认标记，避免同一动作反复卡确认。
            proposed_action.requires_confirmation = False
            runtime_action.requires_confirmation = False
            return {
                "action": proposed_action,
                "runtime_action": runtime_action,
                "artifacts": artifacts,
                "keep_browser_open": False,
                "route": "execute_action",
            }
        return {"action": proposed_action, "runtime_action": runtime_action, "route": "execute_action"}

    def _execute_action_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        step_result = self.host._execute_runtime_action(
            tool=self._tool(state),
            proposed_action=state["action"],
            runtime_action=state["runtime_action"],
            trace_id=state["trace_id"],
            task_id=state["task_id"],
            session_id=state["session_id"],
            step_index=state["step_index"],
            steps=state["steps"],
            artifacts=state["artifacts"],
            spec=state["spec"],
        )
        keep_browser_open = bool(state.get("keep_browser_open", False))
        if isinstance(step_result, ToolExecutionResult):
            if (step_result.data or {}).get("status") == "awaiting_confirmation":
                self.host._active_tools[state["active_key"]] = self._tool(state)
                keep_browser_open = True
        self._update_context(
            state["run_id"],
            active_key=state["active_key"],
            steps=state["steps"],
            artifacts=state["artifacts"],
            keep_browser_open=keep_browser_open,
        )
        return {
            "step_result": step_result,
            "steps": state["steps"],
            "artifacts": state["artifacts"],
            "keep_browser_open": keep_browser_open,
        }

    def _observe_page_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        step_result = state["step_result"]
        if isinstance(step_result, ToolExecutionResult):
            return {"result": step_result, "route": "finalize"}
        result, observation = step_result
        return {"action_result": result, "observation": observation, "route": "reflect"}

    def _reflect_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        spec = state["spec"]
        tool = self._tool(state)
        action = state["action"]
        result = state["action_result"]
        observation = state["observation"]
        steps = list(state.get("steps") or [])
        artifacts = list(state.get("artifacts") or [])

        if observation.page_type == "verification":
            artifacts.extend(self.host._artifacts_from_observation(tool.observe(last_action_result="verification blocked", force_artifact=True)))
            blocked = self.host._blocked_result("遇到验证码、MFA 或二次校验，需要人工接手。", steps, observation, artifacts)
            return {"steps": steps, "artifacts": artifacts, "result": blocked, "route": "finalize"}
        if spec.requires_login and action.type == "login_submit" and observation.page_type == "login":
            if not self.host._login_has_failure_signal(observation) and self.host._login_still_pending(steps):
                return {"steps": steps, "artifacts": artifacts, "route": "plan_action"}
            failed_observation = tool.observe(last_action_result="login failed", force_artifact=True)
            artifacts.extend(self.host._artifacts_from_observation(failed_observation))
            blocked = self.host._blocked_result(self.host._login_failure_reason(failed_observation), steps, failed_observation, artifacts)
            return {"steps": steps, "artifacts": artifacts, "result": blocked, "route": "finalize"}
        early_stop_reason = self.host._early_stop_reason(spec, result, observation, steps)
        if early_stop_reason:
            stopped_observation = tool.observe(last_action_result="early stop", force_artifact=True)
            artifacts.extend(self.host._artifacts_from_observation(stopped_observation))
            blocked = self.host._blocked_result(early_stop_reason, steps, stopped_observation, artifacts)
            return {"steps": steps, "artifacts": artifacts, "result": blocked, "route": "finalize"}
        if action.type == "finish" and result.status == "success":
            completed = self._completed_result(state, artifacts)
            return {"steps": steps, "artifacts": artifacts, "result": completed, "route": "finalize"}
        if result.status == "success":
            self._update_context(state["run_id"], steps=steps, artifacts=artifacts)
            return {"steps": steps, "artifacts": artifacts, "consecutive_failures": 0, "route": "plan_action"}

        consecutive_failures = int(state.get("consecutive_failures", 0)) + 1
        if consecutive_failures >= spec.max_consecutive_failures or result.status == "terminal_failure":
            blocked = self.host._blocked_result(result.error or "浏览器动作连续失败。", steps, observation, artifacts)
            return {"steps": steps, "artifacts": artifacts, "consecutive_failures": consecutive_failures, "result": blocked, "route": "finalize"}
        return {"steps": steps, "artifacts": artifacts, "consecutive_failures": consecutive_failures, "route": "plan_action"}

    def _route_next_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        if state.get("route") == "finalize" and self._should_fallback_from_skill(state):
            return {"route": "skill_fallback"}
        return {"route": state.get("route", "finalize")}

    def _skill_fallback_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        original_result = state.get("result")
        params = dict(state.get("params") or {})
        tool = self._contexts.get(state["run_id"], {}).get("tool")
        active_key = state.get("active_key")
        if active_key:
            self.host._active_tools.pop(active_key, None)
        if tool is not None:
            tool.close()

        fallback_params = dict(params)
        fallback_params["auto_plan"] = True
        fallback_params["actions"] = []
        fallback_params["skill_fallback_attempted"] = True
        fallback_params["skill_failed_reason"] = (
            (original_result.error if isinstance(original_result, ToolExecutionResult) else None)
            or self._skill_failure_category(original_result)
            or "unknown"
        )
        fallback_result = self.run(fallback_params)
        fallback_result.data = dict(fallback_result.data or {})
        fallback_result.data["skill_fallback"] = {
            "skill_name": params.get("skill_name"),
            "original_error": original_result.error if isinstance(original_result, ToolExecutionResult) else None,
            "failure_category": self._skill_failure_category(original_result),
            "llm_fallback_used": True,
        }
        if isinstance(original_result, ToolExecutionResult):
            fallback_result.artifacts = list(original_result.artifacts) + list(fallback_result.artifacts)
        return {"result": fallback_result, "keep_browser_open": False}

    def _finalize_node(self, state: WebAgentSubgraphState) -> WebAgentSubgraphState:
        result = state.get("result") or ToolExecutionResult(
            success=False,
            error="浏览器子图未产生执行结果。",
            retryable=False,
            data={"status": "failed", "steps": state.get("steps") or []},
            artifacts=state.get("artifacts") or [],
        )
        tool = self._contexts.get(state["run_id"], {}).get("tool")
        if not state.get("keep_browser_open") and tool is not None:
            active_key = state.get("active_key")
            if active_key:
                self.host._active_tools.pop(active_key, None)
            tool.close()
        self._attach_skill_execution(result, state)
        return {"result": result}

    def _completed_result(self, state: WebAgentSubgraphState, artifacts: list[TaskArtifact]) -> ToolExecutionResult:
        spec = state["spec"]
        tool = self._tool(state)
        steps = state["steps"]
        task_id = state["task_id"]
        session_id = state["session_id"]
        trace_id = state["trace_id"]
        final_observation = tool.observe(last_action_result="task completed", force_artifact=True)
        artifacts.extend(self.host._artifacts_from_observation(final_observation))
        report_path = self.host._write_execution_report(spec, steps, final_observation, "completed", None)
        artifacts.append(TaskArtifact(kind="execution_report", path=report_path))
        answer = self.host._answer_from_observation(spec, final_observation, steps)
        if self.host._requires_answer(spec.user_goal) and not answer.get("answer"):
            return self.host._blocked_result("任务要求返回答案，但未能从当前页面提取到明确结果。", steps, final_observation, artifacts)
        canonical_action_trace = build_canonical_action_trace(
            steps,
            status="completed",
            task_id=task_id,
            session_id=session_id,
        )
        self.host._record(
            "task.completed",
            trace_id,
            task_id,
            session_id,
            len(steps),
            {"current_url": final_observation.url, "result": "success"},
        )
        self.host._record(
            "web.skill.trace.ready",
            trace_id,
            task_id,
            session_id,
            len(steps),
            {
                "result": "success",
                "schema_version": canonical_action_trace["schema_version"],
                "step_count": canonical_action_trace["step_count"],
            },
        )
        self.host._active_tools.pop(state["active_key"], None)
        return ToolExecutionResult(
            success=True,
            data={
                "status": "completed",
                "goal": spec.user_goal,
                "web_run_id": state["run_id"],
                "web_thread_id": self._thread_id(state.get("params") or {}, state["run_id"]),
                "answer": answer,
                "last_observation": asdict(final_observation),
                "steps": steps,
                "canonical_action_trace": canonical_action_trace,
                "summary": self.host._execution_summary(spec, steps),
                "session_state_path": self.host._save_session_state(tool),
                "execution_report_path": report_path,
            },
            artifacts=artifacts,
        )

    def _failed_result(self, run_id: str, exc: Exception) -> ToolExecutionResult:
        context = self._contexts.get(run_id, {})
        tool = context.get("tool")
        active_key = context.get("active_key")
        steps = list(context.get("steps") or [])
        artifacts = list(context.get("artifacts") or [])
        if active_key:
            self.host._active_tools.pop(active_key, None)
        if tool is not None and not context.get("keep_browser_open"):
            tool.close()
        return ToolExecutionResult(
            success=False,
            error=str(exc),
            retryable=False,
            data={
                "status": "failed",
                "goal": getattr(context.get("spec"), "user_goal", ""),
                "steps": steps,
                "canonical_action_trace": build_canonical_action_trace(
                    steps,
                    status="failed",
                    task_id=str(context.get("task_id") or ""),
                    session_id=str(context.get("session_id") or ""),
                ),
            },
            artifacts=artifacts,
        )

    def _interrupted_result(self, state: WebAgentSubgraphState) -> ToolExecutionResult:
        payload = self._first_interrupt_payload(state)
        run_id = str(payload.get("web_run_id") or state.get("run_id") or "")
        context = self._contexts.get(run_id, {})
        artifacts = list(context.get("artifacts") or state.get("artifacts") or [])
        data = dict(payload)
        data.setdefault("status", "awaiting_confirmation")
        data.setdefault(
            "canonical_action_trace",
            build_canonical_action_trace(
                list(data.get("steps") or []),
                status="awaiting_confirmation",
                task_id=str(data.get("task_id") or ""),
                session_id=str(data.get("session_id") or ""),
                pending_action=data.get("pending_action_raw") if isinstance(data.get("pending_action_raw"), dict) else None,
            ),
        )
        return ToolExecutionResult(
            success=False,
            error="浏览器动作需要人工确认，未执行可能产生远端副作用的操作。",
            retryable=False,
            data=data,
            artifacts=artifacts,
        )

    def _first_interrupt_payload(self, state: WebAgentSubgraphState) -> dict[str, Any]:
        interrupts = state.get("__interrupt__") or []
        if not interrupts:
            return {}
        value = getattr(interrupts[0], "value", None)
        return dict(value) if isinstance(value, dict) else {}

    def _interrupt_payload(
        self,
        state: WebAgentSubgraphState,
        proposed_action: BrowserAction,
        runtime_action: BrowserAction,
        result: ToolExecutionResult,
    ) -> dict[str, Any]:
        run_id = state["run_id"]
        thread_id = self._thread_id(state.get("params") or {}, run_id)
        data = dict(result.data or {})
        data.update(
            {
                "status": "awaiting_confirmation",
                "confirmation_type": "web_action",
                "resume_node": "risk_gate",
                "task_id": state["task_id"],
                "session_id": state["session_id"],
                "web_run_id": run_id,
                "web_thread_id": thread_id,
                "resume_context": {
                    "graph": "web_agent",
                    "node": "risk_gate",
                    "thread_id": thread_id,
                    "active_key": state.get("active_key"),
                    "step_index": state.get("step_index"),
                },
                "langgraph": {
                    "graph": "web_agent",
                    "node": "risk_gate",
                    "thread_id": thread_id,
                    "resume": "Command(resume={'decision': 'approved'})",
                },
                "risk_gate": {
                    "action_type": proposed_action.type,
                    "runtime_action_type": runtime_action.type,
                    "target": proposed_action.target_hint or proposed_action.target_id,
                    "risk_level": proposed_action.risk_level,
                    "requires_confirmation": proposed_action.requires_confirmation,
                },
            }
        )
        return data

    def _confirmation_decision(self, resume_value) -> str:
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

    def _restore_context_for_resume(self, run_id: str, thread_id: str, params: dict) -> None:
        # 如果同进程上下文还在，说明浏览器对象仍可直接使用，不需要从 checkpoint 重建。
        if run_id in self._contexts and self._contexts[run_id].get("tool") is not None:
            return
        values: dict[str, Any] = {}
        try:
            # 进程重启后只能从 LangGraph checkpoint 读取可序列化 state。
            snapshot = self.graph.get_state(self._graph_config(thread_id))
            values = dict(getattr(snapshot, "values", {}) or {})
        except Exception:
            values = {}
        spec = values.get("spec")
        if isinstance(spec, dict):
            spec = browser_task_spec_from_state(spec)
        if not isinstance(spec, BrowserTaskSpec):
            spec = self.host._spec_from_params(params)
            spec.session_state_path = spec.session_state_path or self.host._default_session_state_path(str(params.get("session_id", "default")))
            try:
                self.host._attach_credentials(spec)
            except CredentialError:
                pass
        session_id = str(values.get("session_id") or params.get("session_id") or "default")
        task_id = str(values.get("task_id") or params.get("task_id") or "")
        active_key = str(values.get("active_key") or self.host._active_tool_key(session_id, task_id))
        tool = self.host._active_tools.get(active_key)
        if tool is None:
            # crash resume: 重新创建 Playwright tool，并用 session_state_path 恢复 cookies/localStorage 等登录态。
            tool = self.host._create_browser_tool(
                session_id=session_id,
                task_id=task_id,
                headless=bool(params.get("headless", self.host.headless)),
                allowed_domains=spec.allowed_domains,
                session_state_path=params.get("session_state_path") or spec.session_state_path,
                trace_enabled=spec.trace_enabled,
                video_enabled=spec.video_enabled,
                browser_channel=spec.browser_channel,
                slow_mo_ms=spec.browser_slow_mo_ms,
            )
        self._contexts[run_id] = {
            "params": dict(values.get("params") or params),
            "spec": spec,
            "session_id": session_id,
            "task_id": task_id,
            "trace_id": str(values.get("trace_id") or params.get("trace_id") or ""),
            "active_key": active_key,
            "tool": tool,
            "steps": list(values.get("steps") or params.get("prior_steps") or []),
            "artifacts": [artifact_from_state(item) for item in values.get("artifacts") or []],
            "keep_browser_open": False,
        }

    def _thread_id(self, params: dict, run_id: str) -> str:
        if params.get("web_thread_id"):
            return str(params["web_thread_id"])
        task_id = str(params.get("task_id") or "")
        session_id = str(params.get("session_id") or "default")
        owner = task_id or session_id
        return f"web:{owner}:{run_id}"

    def _graph_config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id, "subgraph": "web_agent"}}

    def _checkpoint_safe_spec(self, spec: BrowserTaskSpec) -> BrowserTaskSpec:
        return replace(spec, credential_username=None, credential_password=None)

    def _runtime_spec(self, spec: BrowserTaskSpec) -> BrowserTaskSpec:
        runtime_spec = replace(spec)
        if runtime_spec.requires_login:
            self.host._attach_credentials(runtime_spec)
        return runtime_spec

    def _route_after_prepare(self, state: WebAgentSubgraphState) -> str:
        return state.get("route", "load_web_memory")

    def _route_after_plan_action(self, state: WebAgentSubgraphState) -> str:
        return state.get("route", "stabilize_action")

    def _route_after_stabilize_action(self, state: WebAgentSubgraphState) -> str:
        return state.get("route", "risk_gate")

    def _route_after_risk_gate(self, state: WebAgentSubgraphState) -> str:
        return state.get("route", "execute_action")

    def _route_after_observe_page(self, state: WebAgentSubgraphState) -> str:
        return state.get("route", "reflect")

    def _route_after_route_next(self, state: WebAgentSubgraphState) -> str:
        return state.get("route", "finalize")

    def _update_context(self, run_id: str, **updates: Any) -> None:
        context = self._contexts.setdefault(run_id, {})
        context.update(updates)

    def _tool(self, state: WebAgentSubgraphState):
        return self._contexts[state["run_id"]]["tool"]

    def _should_fallback_from_skill(self, state: WebAgentSubgraphState) -> bool:
        params = state.get("params") or {}
        result = state.get("result")
        if not isinstance(result, ToolExecutionResult):
            return False
        if result.success or not params.get("skill_name"):
            return False
        if not params.get("skill_fallback_to_llm_once", True) or params.get("skill_fallback_attempted"):
            return False
        if (result.data or {}).get("status") == "awaiting_confirmation":
            return False
        return self._skill_failure_category(result) not in {"system_missing_information", "login_failure", "site_unavailable"}

    def _skill_failure_category(self, result: ToolExecutionResult | None) -> str:
        if result is None:
            return "execution_failure"
        steps = (result.data or {}).get("steps") or []
        for step in reversed(steps):
            reflection = step.get("reflection") or {}
            category = reflection.get("failure_category")
            if category and category != "none":
                return str(category)
        error = result.error or ""
        if "无法打开目标网站" in error:
            return "site_unavailable"
        if "登录失败" in error:
            return "login_failure"
        if "系统中没有" in error:
            return "system_missing_information"
        return "execution_failure"

    def _apply_skill_match(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("skill_name") or self.host.web_skill_matcher is None:
            if params.get("skill_name"):
                params["skill_execution"] = {
                    "skill_name": params.get("skill_name"),
                    "score": params.get("skill_score"),
                    "parameters": params.get("skill_parameters") or {},
                    "matched_keywords": params.get("skill_matched_keywords") or [],
                }
            return params
        goal = str(params.get("user_goal") or "")
        entities = self._skill_match_entities(params)
        match = self.host.web_skill_matcher.match(goal, entities)
        if match is None:
            return params
        params["auto_plan"] = False
        params["actions"] = [asdict(action) for action in match.actions]
        params["skill_name"] = match.skill.name
        params["skill_score"] = round(match.score, 4)
        params["skill_parameters"] = match.parameters
        params["skill_matched_keywords"] = match.matched_keywords
        params["skill_fallback_to_llm_once"] = bool(
            (match.skill.workflow.get("execution") or {}).get("fallback_to_llm_once", True)
        )
        params["requires_login"] = bool(
            params.get("requires_login")
            or (match.skill.workflow.get("execution") or {}).get("requires_login", False)
        )
        params["skill_execution"] = {
            "skill_name": match.skill.name,
            "score": round(match.score, 4),
            "parameters": match.parameters,
            "matched_keywords": match.matched_keywords,
        }
        return params

    def _skill_match_entities(self, params: dict[str, Any]) -> dict[str, Any]:
        entities = dict(params.get("entities") or {})
        for key in (
            "site_key",
            "workflow",
            "workflow_fields",
            "site_config",
            "start_url",
            "allowed_domains",
            "requires_login",
            "credential_ref",
        ):
            if params.get(key) is not None and key not in entities:
                entities[key] = params.get(key)
        return entities

    def _attach_skill_execution(self, result: ToolExecutionResult, state: WebAgentSubgraphState) -> None:
        skill_execution = dict(state.get("skill_execution") or {})
        if not skill_execution:
            return
        result.data = dict(result.data or {})
        result.data.setdefault("skill_execution", skill_execution)
