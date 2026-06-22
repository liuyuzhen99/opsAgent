from __future__ import annotations

import re
import time
from dataclasses import asdict
from pathlib import Path

from aiops_agent.audit.models import AuditEvent
from aiops_agent.browser.action_trace import build_canonical_action_trace
from aiops_agent.browser.credentials import CredentialError, CredentialStore
from aiops_agent.browser.models import BrowserAction, BrowserObservation, BrowserTaskSpec, InteractiveElement
from aiops_agent.browser.playwright_tool import PlaywrightBrowserTool
from aiops_agent.browser.planner import BrowserPlanner
from aiops_agent.browser.risk import RiskEvaluator
from aiops_agent.browser.skills.matcher import WebSkillMatcher
from aiops_agent.browser.table_extractor import TextTableExtractor
from aiops_agent.tasks.models import TaskArtifact, ToolExecutionResult
from aiops_agent.tools.base import BaseTool


MAX_RUNTIME_RETRIES = 2


class BrowserAgentTool(BaseTool):
    def __init__(
        self,
        *,
        audit_logger,
        artifact_root: str | Path = "storage/artifacts",
        headless: bool = True,
        credential_store: CredentialStore | None = None,
        planner: BrowserPlanner | None = None,
        risk_evaluator: RiskEvaluator | None = None,
        web_skill_matcher: WebSkillMatcher | None = None,
        table_extractor: TextTableExtractor | None = None,
        langgraph_checkpointer=None,
        langgraph_store=None,
    ):
        self.audit_logger = audit_logger
        self.artifact_root = Path(artifact_root)
        self.headless = headless
        self.credential_store = credential_store or CredentialStore()
        self.planner = planner or BrowserPlanner()
        self.risk_evaluator = risk_evaluator or RiskEvaluator()
        self.web_skill_matcher = web_skill_matcher
        self.table_extractor = table_extractor or TextTableExtractor()
        self._active_tools: dict[str, PlaywrightBrowserTool] = {}
        from aiops_agent.browser.subgraph import WebAgentSubgraph
        self.subgraph = WebAgentSubgraph(self, checkpointer=langgraph_checkpointer, store=langgraph_store)

    def execute(self, params: dict) -> ToolExecutionResult:
        # web_thread_id 表示这是确认后的恢复调用；否则是一次新的 Web 任务。
        if params.get("web_thread_id"):
            return self.subgraph.resume(params)
        return self.subgraph.run(params)

    def configure_langgraph_runtime(self, *, checkpointer=None, store=None) -> None:
        from aiops_agent.browser.subgraph import WebAgentSubgraph
        self.subgraph = WebAgentSubgraph(self, checkpointer=checkpointer, store=store)

    def get_state(self, thread_id: str):
        return self.subgraph.get_state(thread_id)

    def get_state_history(self, thread_id: str):
        return self.subgraph.get_state_history(thread_id)

    def _create_browser_tool(
        self,
        *,
        session_id: str,
        task_id: str,
        headless: bool,
        allowed_domains: list[str],
        session_state_path: str | None,
        trace_enabled: bool,
        video_enabled: bool,
        browser_channel: str | None,
        slow_mo_ms: int,
    ) -> PlaywrightBrowserTool:
        return PlaywrightBrowserTool(
            session_id=session_id,
            task_id=task_id,
            artifact_root=self.artifact_root,
            headless=headless,
            allowed_domains=allowed_domains,
            session_state_path=session_state_path,
            trace_enabled=trace_enabled,
            video_enabled=video_enabled,
            browser_channel=browser_channel,
            slow_mo_ms=slow_mo_ms,
        )

    def _spec_from_params(self, params: dict) -> BrowserTaskSpec:
        # ToolCallSpec.params 是松散 dict，这里收束成 BrowserTaskSpec：
        # 后续子图、planner、risk gate 都围绕这个结构化规格工作。
        actions = [BrowserAction(**item) for item in params.get("actions", [])]
        return BrowserTaskSpec(
            start_url=params.get("start_url"),
            user_goal=str(params.get("user_goal", "")),
            success_criteria=list(params.get("success_criteria", [])),
            forbidden_actions=list(params.get("forbidden_actions", [])),
            allowed_domains=list(params.get("allowed_domains", [])),
            credential_ref=params.get("credential_ref"),
            credential_refs=list(params.get("credential_refs") or []),
            requires_login=bool(params.get("requires_login", False)),
            max_steps=int(params.get("max_steps", 20)),
            max_consecutive_failures=int(params.get("max_consecutive_failures", 3)),
            repeated_action_threshold=int(params.get("repeated_action_threshold", 3)),
            actions=actions,
            requires_remote_mutation=bool(params.get("requires_remote_mutation", False)),
            auto_plan=bool(params.get("auto_plan", True)),
            session_state_path=params.get("session_state_path"),
            confirmed_action=BrowserAction(**params["confirmed_action"]) if params.get("confirmed_action") else None,
            replay_actions=[BrowserAction(**item) for item in params.get("replay_actions", [])],
            site_key=params.get("site_key"),
            workflow=params.get("workflow"),
            workflow_fields=dict(params.get("workflow_fields") or {}),
            site_config=dict(params.get("site_config") or {}),
            completed_action_keys=list(params.get("completed_action_keys") or []),
            trace_enabled=bool(params.get("trace_enabled", False)),
            video_enabled=bool(params.get("video_enabled", False)),
            browser_channel=params.get("browser_channel"),
            browser_slow_mo_ms=int(params.get("browser_slow_mo_ms", 0)),
        )

    def _validate_spec(self, spec: BrowserTaskSpec, params: dict) -> str | None:
        if params.get("browser_config_error"):
            return str(params["browser_config_error"])
        if not spec.workflow:
            return None
        if not spec.site_key:
            return "账号/权限网页工作流缺少 browser site_key。"
        if not spec.site_config:
            return f"账号/权限网页工作流缺少站点配置: {spec.site_key}"
        missing = []
        if spec.workflow in {"search_user", "create_user", "create_user_and_assign_role"} and not spec.workflow_fields.get("username"):
            missing.append("username")
        if spec.workflow in {"assign_role", "create_user_and_assign_role"}:
            if not spec.workflow_fields.get("username"):
                missing.append("username")
            if not spec.workflow_fields.get("role"):
                missing.append("role")
        workflows = spec.site_config.get("workflows") or {}
        workflow_names = ["create_user", "assign_role"] if spec.workflow == "create_user_and_assign_role" else [spec.workflow]
        for workflow_name in workflow_names:
            if workflow_name not in workflows:
                missing.append(f"workflow_config.{workflow_name}")
        if missing:
            return "账号/权限网页工作流缺少必要信息: " + ", ".join(sorted(set(missing)))
        return None

    def _attach_credentials(self, spec: BrowserTaskSpec) -> None:
        if not spec.requires_login:
            return
        refs = spec.credential_refs or ([spec.credential_ref] if spec.credential_ref else [])
        if not refs:
            raise CredentialError("登录任务缺少 credential_ref 或凭据配置")
        pairs: list[tuple[str, str]] = []
        for ref in refs:
            credential = self.credential_store.get(ref)
            if credential is None:
                raise CredentialError(f"登录任务缺少凭据配置: {ref}")
            pairs.append((credential.username, credential.password))
        spec.credential_ref = spec.credential_ref or refs[0]
        spec.credential_refs = refs
        spec.credential_pairs = pairs
        spec.credential_username, spec.credential_password = pairs[0]

    def _next_action(self, spec: BrowserTaskSpec, steps: list[dict]) -> BrowserAction:
        # 确认恢复时先重放安全动作，再执行唯一的 confirmed_action；
        # confirmed_action 已成功出现在 steps 里时，不再重复提交。
        if spec.confirmed_action and len(steps) < len(spec.replay_actions):
            return spec.replay_actions[len(steps)]
        if spec.confirmed_action and not self._action_already_executed(spec.confirmed_action, steps):
            return spec.confirmed_action
        observation = None
        if steps:
            observation_raw = steps[-1].get("observation") or {}
            if isinstance(observation_raw, dict):
                observation = self._observation_from_dict(observation_raw)
        if not spec.auto_plan and spec.actions:
            login_action = self._fixed_workflow_login_action(spec, observation, steps)
            if login_action is not None:
                return login_action
            fixed_action = self._next_fixed_action(spec.actions, steps)
            if fixed_action is not None:
                return fixed_action
        return self.planner.next_action(spec, observation, steps)

    def _fixed_workflow_login_action(
        self,
        spec: BrowserTaskSpec,
        observation: BrowserObservation | None,
        steps: list[dict],
    ) -> BrowserAction | None:
        if not spec.requires_login or observation is None or observation.page_type != "login":
            return None
        action = self.planner.next_action(spec, observation, steps)
        if action.type in {"observe_page", "type_username", "type_password", "login_submit", "wait_for", "finish"}:
            return action
        return None

    def _next_fixed_action(self, actions: list[BrowserAction], steps: list[dict]) -> BrowserAction | None:
        search_from = 0
        for action in actions:
            matched_index = self._matching_successful_step_index(action, steps, search_from)
            if matched_index is None:
                return action
            search_from = matched_index + 1
        return None

    def _matching_successful_step_index(self, action: BrowserAction, steps: list[dict], start: int) -> int | None:
        for index in range(start, len(steps)):
            step = steps[index]
            if step.get("result") != "success":
                continue
            if self._action_signature_matches(action, step.get("action") or {}):
                return index
        return None

    def _action_signature_matches(self, action: BrowserAction, previous: dict) -> bool:
        exact_match = (
            previous.get("type"),
            previous.get("target_hint"),
            previous.get("target_id"),
            previous.get("value"),
            previous.get("key", ""),
        ) == (
            action.type,
            action.target_hint,
            action.target_id,
            action.value,
            action.key,
        )
        if exact_match:
            return True
        if previous.get("type") != action.type or previous.get("value") != action.value:
            return False

        expected_target = str(action.target_hint or action.target_id or "")
        completed_target = str(previous.get("target_hint") or previous.get("target_id") or "")
        if not expected_target or not completed_target:
            return False
        if action.type == "click":
            return self._click_target_matches_label(completed_target, expected_target) or self._click_target_matches_label(
                expected_target,
                completed_target,
            )
        if action.type in {"type", "select"}:
            return self._field_label_matches_target(expected_target, completed_target) or self._field_label_matches_target(
                completed_target,
                expected_target,
            )
        return False

    def _execute_action(
        self,
        *,
        tool: PlaywrightBrowserTool,
        action: BrowserAction,
        trace_id: str,
        task_id: str,
        session_id: str,
        step_index: int,
        steps: list[dict],
        artifacts: list[TaskArtifact],
        spec: BrowserTaskSpec,
    ):
        proposed_action = self._stabilize_action(spec, action, steps)
        if self._is_repeated_action(proposed_action, steps, spec.repeated_action_threshold):
            observation = tool.observe(last_action_result="repeated action blocked", force_artifact=True)
            artifacts.extend(self._artifacts_from_observation(observation))
            return self._blocked_result("检测到同一页面重复动作超过阈值，已停止执行。", steps, observation, artifacts)
        intent_aligned, intent_reason = self._action_intent_alignment(spec, proposed_action, steps)
        if not intent_aligned:
            observation = tool.observe(last_action_result="intent mismatch blocked", force_artifact=True)
            artifacts.extend(self._artifacts_from_observation(observation))
            return self._blocked_result(f"规划动作与用户意图不一致，已停止执行：{intent_reason}", steps, observation, artifacts)
        runtime_action = self._runtime_action(proposed_action)
        self._prepare_runtime_action_for_risk(spec, proposed_action, runtime_action)
        self._record_action_proposed(
            action=proposed_action,
            trace_id=trace_id,
            task_id=task_id,
            session_id=session_id,
            step_index=step_index,
        )
        if proposed_action.requires_confirmation:
            return self._awaiting_confirmation_result(
                tool=tool,
                action=proposed_action,
                trace_id=trace_id,
                task_id=task_id,
                session_id=session_id,
                step_index=step_index,
                steps=steps,
                artifacts=artifacts,
                spec=spec,
            )
        return self._execute_runtime_action(
            tool=tool,
            proposed_action=proposed_action,
            runtime_action=runtime_action,
            trace_id=trace_id,
            task_id=task_id,
            session_id=session_id,
            step_index=step_index,
            steps=steps,
            artifacts=artifacts,
            spec=spec,
        )

    def _prepare_runtime_action_for_risk(
        self,
        spec: BrowserTaskSpec,
        proposed_action: BrowserAction,
        runtime_action: BrowserAction,
    ) -> str:
        risk_level = self.risk_evaluator.classify(runtime_action)
        proposed_action.risk_level = risk_level
        runtime_action.risk_level = risk_level
        confirmed_execution = spec.confirmed_action is not None and proposed_action == spec.confirmed_action
        login_local_action = proposed_action.type in {"type_username", "type_password", "login_submit"}
        confirmation_ui_opener = self.risk_evaluator.opens_confirmation_ui(runtime_action)
        proposed_action.requires_confirmation = (
            False
            if confirmed_execution or login_local_action or confirmation_ui_opener
            else proposed_action.requires_confirmation or self.risk_evaluator.requires_confirmation(runtime_action)
        )
        runtime_action.requires_confirmation = proposed_action.requires_confirmation
        return risk_level

    def _record_action_proposed(
        self,
        *,
        action: BrowserAction,
        trace_id: str,
        task_id: str,
        session_id: str,
        step_index: int,
    ) -> None:
        self._record(
            "action.proposed",
            trace_id,
            task_id,
            session_id,
            step_index,
            {"action": self._safe_action_dict(action), "risk_level": action.risk_level},
        )

    def _awaiting_confirmation_result(
        self,
        *,
        tool: PlaywrightBrowserTool,
        action: BrowserAction,
        trace_id: str,
        task_id: str,
        session_id: str,
        step_index: int,
        steps: list[dict],
        artifacts: list[TaskArtifact],
        spec: BrowserTaskSpec,
    ) -> ToolExecutionResult:
        if step_index == 1 and not spec.start_url:
            observation = BrowserObservation(
                title="未打开页面",
                last_action_result="blocked for confirmation",
                blocking_reason="缺少站点入口且动作可能产生远端副作用",
            )
        else:
            observation = tool.observe(last_action_result="blocked for confirmation", force_artifact=True)
        artifacts.extend(self._artifacts_from_observation(observation))
        event_type = "action.blocked_for_unknown_risk" if action.risk_level == "unknown_risk" else "action.blocked_for_confirmation"
        self._record(
            event_type,
            trace_id,
            task_id,
            session_id,
            step_index,
            {
                "current_url": observation.url,
                "action_type": action.type,
                "risk_level": action.risk_level,
                "summary": self._confirmation_summary(action, observation),
            },
        )
        state_path = self._save_session_state(tool)
        return ToolExecutionResult(
            success=False,
            error="浏览器动作需要人工确认，未执行可能产生远端副作用的操作。",
            retryable=False,
            data={
                "status": "awaiting_confirmation",
                "confirmation_summary": self._confirmation_summary(action, observation),
                "pending_action": self._safe_action_dict(action),
                # raw action 给 resume 执行用；safe action 给人和审计看。
                "pending_action_raw": asdict(action),
                # crash resume 时只重放安全动作，远端 mutation 不进入 replay。
                "replay_actions": [asdict(action) for action in self._replay_actions(steps)],
                # 已成功 mutation 的 key 用来防止后续 planner 再提出同一提交。
                "completed_action_keys": self._completed_action_keys(steps),
                "resume_url": observation.url,
                "session_state_path": state_path,
                "last_observation": asdict(observation),
                "steps": steps,
                "canonical_action_trace": build_canonical_action_trace(
                    steps,
                    status="awaiting_confirmation",
                    task_id=task_id,
                    session_id=session_id,
                    pending_action=asdict(action),
                ),
            },
            artifacts=artifacts,
        )

    def _execute_runtime_action(
        self,
        *,
        tool: PlaywrightBrowserTool,
        proposed_action: BrowserAction,
        runtime_action: BrowserAction,
        trace_id: str,
        task_id: str,
        session_id: str,
        step_index: int,
        steps: list[dict],
        artifacts: list[TaskArtifact],
        spec: BrowserTaskSpec,
    ) -> tuple[ActionResult, BrowserObservation]:
        retry_attempts = 0
        while True:
            result = tool.execute(runtime_action)
            if not self._should_retry_runtime_action(spec, proposed_action, result, retry_attempts):
                break
            retry_attempts += 1
            self._record_action_retrying(
                action=proposed_action,
                result=result,
                trace_id=trace_id,
                task_id=task_id,
                session_id=session_id,
                step_index=step_index,
                retry_attempt=retry_attempts,
            )
            time.sleep(self._retry_backoff_seconds(retry_attempts))
        observation = result.observation
        self._record(
            "action.executed",
            trace_id,
            task_id,
            session_id,
            step_index,
            {
                "current_url": observation.url,
                "action_type": proposed_action.type,
                "risk_level": proposed_action.risk_level,
                "result": result.status,
                "error": result.error,
                "retry_attempts": retry_attempts,
            },
        )
        self._record(
            "page.observed",
            trace_id,
            task_id,
            session_id,
            step_index,
            {
                "current_url": observation.url,
                "title": observation.title,
                "page_type": observation.page_type,
                "element_count": len(observation.interactive_elements),
                "blocking_reason": observation.blocking_reason,
            },
        )
        reflection = self._reflect_after_action(spec, proposed_action, result, observation, steps)
        reflection["retry_attempts"] = retry_attempts
        self._record(
            "action.reflected",
            trace_id,
            task_id,
            session_id,
            step_index,
            reflection,
        )
        steps.append(
            {
                "step_index": step_index,
                "action": self._safe_action_dict(proposed_action),
                "result": result.status,
                "observation": asdict(observation),
                "error": result.error,
                "reflection": reflection,
            }
        )
        artifacts.extend(self._artifacts_from_observation(observation))
        return result, observation

    def _should_retry_runtime_action(self, spec: BrowserTaskSpec, action: BrowserAction, result, retry_attempts: int) -> bool:
        if retry_attempts >= MAX_RUNTIME_RETRIES:
            return False
        if result.status != "retryable_failure":
            return False
        if action.requires_confirmation or action.risk_level in {"unsafe_mutation", "unknown_risk"}:
            return False
        if action.type == "login_submit":
            return False
        if action.type in {"open_url", "observe_page", "extract_text"}:
            return True
        terminal_reason = self._failed_action_terminal_reason(spec, self._safe_action_dict(action), result.error or "")
        if terminal_reason:
            return False
        return self._looks_like_transient_browser_failure(result.error or "")

    def _record_action_retrying(
        self,
        *,
        action: BrowserAction,
        result,
        trace_id: str,
        task_id: str,
        session_id: str,
        step_index: int,
        retry_attempt: int,
    ) -> None:
        self._record(
            "action.retrying",
            trace_id,
            task_id,
            session_id,
            step_index,
            {
                "current_url": result.observation.url,
                "action_type": action.type,
                "risk_level": action.risk_level,
                "result": result.status,
                "error": result.error,
                "retry_attempt": retry_attempt,
            },
        )

    def _retry_backoff_seconds(self, retry_attempt: int) -> float:
        return min(0.02 * (2 ** max(retry_attempt - 1, 0)), 0.1)

    def _looks_like_transient_browser_failure(self, error: str) -> bool:
        lowered = error.lower()
        return any(
            marker in lowered
            for marker in (
                "timeout",
                "waiting for",
                "navigation",
                "load state",
                "not visible",
                "not enabled",
                "intercepts pointer events",
                "detached",
                "closed",
                "net::",
                "err_",
                "temporarily",
                "暂时",
                "超时",
            )
        )

    def _runtime_action(self, action: BrowserAction) -> BrowserAction:
        if action.type == "type_username":
            return BrowserAction(
                type="type",
                target_hint=action.target_hint or "__username__",
                target_id=action.target_id,
                value=action.value,
                expected_outcome=action.expected_outcome,
                risk_level="safe_local_edit",
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key=action.key,
            )
        if action.type == "type_password":
            return BrowserAction(
                type="type",
                target_hint=action.target_hint or "__password__",
                target_id=action.target_id,
                value=action.value,
                expected_outcome=action.expected_outcome,
                risk_level="safe_local_edit",
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key=action.key,
            )
        return action

    def _stabilize_action(self, spec: BrowserTaskSpec, action: BrowserAction, steps: list[dict]) -> BrowserAction:
        if not steps:
            return action
        last_observation = self._observation_from_dict(steps[-1].get("observation") or {})
        if last_observation.page_type == "login":
            return action
        review_confirmation = self._review_confirmation_action(action, steps)
        if review_confirmation is not None:
            return review_confirmation
        business_center_action = self._pending_business_center_action(action, steps, last_observation)
        if business_center_action is not None:
            return business_center_action
        post_login_wait = self._wait_for_post_login_target(action, steps, last_observation)
        if post_login_wait is not None:
            return post_login_wait
        navigation_action = self._pending_explicit_navigation_action(spec, action, steps)
        if navigation_action is not None:
            return navigation_action
        post_navigation_action = self._pending_click_after_explicit_navigation(spec, action, steps)
        if post_navigation_action is not None:
            return post_navigation_action
        result_sequence_action = self._pending_explicit_result_sequence_action(spec, action, steps)
        if result_sequence_action is not None:
            return result_sequence_action
        review_selection = self._pending_review_select_all_action(spec, action, steps, last_observation)
        if review_selection is not None:
            return review_selection
        membership_answer = self._membership_answer(spec.user_goal, steps[-1].get("observation") or {}, steps)
        if membership_answer is not None:
            return BrowserAction(
                type="finish",
                value=membership_answer["answer"],
                expected_outcome="已从用户指定列表中判断目标值是否存在",
                risk_level="safe_read",
                requires_confirmation=False,
                timeout_ms=action.timeout_ms,
                key="stabilized.membership_answer",
            )
        if action.type == "type":
            pending_input = self._pending_explicit_input_request(spec, steps)
            if pending_input is not None:
                field, value = pending_input
                observation = self._observation_from_dict(steps[-1].get("observation") or {})
                current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
                expected = self._find_exact_field_element(observation, field)
                if expected is not None and (
                    str(action.value or "") != value or not self._field_label_matches_target(field, current_target)
                ):
                    return BrowserAction(
                        type="type",
                        target_hint=expected.name or expected.placeholder or expected.title or expected.text or field,
                        target_id=expected.element_id,
                        value=value,
                        expected_outcome=f"按用户指令先在字段 {field} 中输入 {value}",
                        risk_level="safe_local_edit",
                        requires_confirmation=False,
                        timeout_ms=action.timeout_ms,
                        key="stabilized.pending_explicit_input",
                    )
                if self._dropdown_appears_open_for_field(field, value, observation):
                    return BrowserAction(
                        type="type",
                        target_hint=f"{field}搜索输入框",
                        target_id=None,
                        value=value,
                        expected_outcome=f"按用户指令先在 {field} 下拉搜索框中输入 {value}",
                        risk_level="safe_local_edit",
                        requires_confirmation=False,
                        timeout_ms=action.timeout_ms,
                        key="stabilized.pending_dropdown_input",
                    )
                if str(action.value or "") != value or not self._field_label_matches_target(field, current_target):
                    return BrowserAction(
                        type="type",
                        target_hint=field,
                        target_id=None,
                        value=value,
                        expected_outcome=f"按用户指令先在字段 {field} 中输入 {value}",
                        risk_level="safe_local_edit",
                        requires_confirmation=False,
                        timeout_ms=action.timeout_ms,
                        key="stabilized.pending_explicit_input",
                    )
            return self._stabilize_type_action(spec, action, steps)
        if self._should_select_first_result_row_before_assignment(spec, action, steps):
            return BrowserAction(
                type="click",
                target_hint="第一条数据",
                expected_outcome="按用户指令先选中查询结果中的第一条数据",
                risk_level=action.risk_level,
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key="stabilized.first_table_row",
            )
        pending_after_first_result = self._pending_click_after_first_search_result(spec, action, steps)
        if pending_after_first_result is not None:
            return pending_after_first_result
        redundant_dropdown_click = self._redundant_selected_dropdown_value_click(spec, action, steps)
        if redundant_dropdown_click is not None:
            return redundant_dropdown_click
        pending_input = self._pending_explicit_input_request(spec, steps)
        if pending_input is not None and action.type in {
            "click",
            "wait_for",
            "observe_page",
            "extract_text",
            "finish",
            "save_artifact",
            "press",
            "select",
        }:
            field, value = pending_input
            observation = self._observation_from_dict(steps[-1].get("observation") or {})
            expected = self._find_exact_field_element(observation, field)
            if expected is not None:
                return BrowserAction(
                    type="type",
                    target_hint=expected.name or expected.placeholder or expected.title or expected.text or field,
                    target_id=expected.element_id,
                    value=value,
                    expected_outcome=f"按用户指令先在字段 {field} 中输入 {value}",
                    risk_level="safe_local_edit",
                    requires_confirmation=False,
                    timeout_ms=action.timeout_ms,
                    key="stabilized.pending_explicit_input",
                )
            if self._should_type_into_open_dropdown_search(field, value, action, observation):
                return BrowserAction(
                    type="type",
                    target_hint=f"{field}搜索输入框",
                    target_id=None,
                    value=value,
                    expected_outcome=f"按用户指令先在 {field} 下拉搜索框中输入 {value}",
                    risk_level="safe_local_edit",
                    requires_confirmation=False,
                    timeout_ms=action.timeout_ms,
                    key="stabilized.pending_dropdown_input",
                )
        pending_press = self._pending_press_after_explicit_type(spec, steps)
        if pending_press is not None and action.type in {
            "click",
            "wait_for",
            "observe_page",
            "extract_text",
            "finish",
            "save_artifact",
            "press",
            "select",
        }:
            if action.type != "press" or str(action.value or action.target_hint or "").lower() != "enter":
                return pending_press
        expected_click = self._pending_click_after_explicit_type(spec, steps)
        if expected_click and action.type in {
            "wait_for",
            "observe_page",
            "extract_text",
            "finish",
            "save_artifact",
        }:
            observation = self._observation_from_dict(steps[-1].get("observation") or {})
            expected = self._find_command_element(observation, expected_click)
            if expected is not None:
                return BrowserAction(
                    type="click",
                    target_hint=expected.text or expected.name or expected_click,
                    target_id=expected.element_id,
                    expected_outcome=f"按用户指令先点击后续按钮: {expected_click}",
                    risk_level="safe_local_edit",
                    requires_confirmation=False,
                    timeout_ms=action.timeout_ms,
                    key="stabilized.pending_expected_click",
                )
        if action.type != "click":
            return action
        expected_click = expected_click or self._expected_click_after_last_type(spec.user_goal, steps[-1].get("action") or {})
        if not expected_click:
            return self._correct_command_click_from_expected_outcome(action, steps) or action
        if self._means_first_search_result(expected_click):
            previous_value = self._latest_dropdown_typed_value(steps) or str((steps[-1].get("action") or {}).get("value") or "")
            return BrowserAction(
                type="click",
                target_hint=previous_value or "第一个",
                expected_outcome=f"按用户指令点击 {expected_click}",
                risk_level=action.risk_level,
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key="stabilized.first_search_result",
            )
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        expected = self._find_command_element(observation, expected_click)
        if expected is None:
            current_target = f"{action.target_hint or ''} {action.target_id or ''}"
            if self._click_target_matches_label(current_target, expected_click):
                return action
            return self._correct_command_click_from_expected_outcome(action, steps) or action
        if action.target_id == expected.element_id:
            return action
        return BrowserAction(
            type="click",
            target_hint=expected.text or expected.name or expected_click,
            target_id=expected.element_id,
            expected_outcome=f"点击用户指定的后续按钮: {expected_click}",
            risk_level=action.risk_level,
            requires_confirmation=action.requires_confirmation,
            timeout_ms=action.timeout_ms,
            key="stabilized.expected_click",
        )

    def _wait_for_post_login_target(
        self,
        action: BrowserAction,
        steps: list[dict],
        observation: BrowserObservation,
    ) -> BrowserAction | None:
        if action.type != "click":
            return None
        login_index = next(
            (
                index
                for index in range(len(steps) - 1, -1, -1)
                if steps[index].get("result") == "success"
                and (steps[index].get("action") or {}).get("type") == "login_submit"
            ),
            None,
        )
        if login_index is None:
            return None
        if any(
            step.get("result") == "success" and (step.get("action") or {}).get("type") == "click"
            for step in steps[login_index + 1 :]
        ):
            return None
        target = action.target_hint or action.target_id or ""
        if not target or self._find_element(observation, set(self._click_label_aliases(target))) is not None:
            return None
        return BrowserAction(
            type="wait_for",
            expected_outcome=f"等待登录跳转完成并出现首个业务目标: {target}",
            risk_level="safe_read",
            requires_confirmation=False,
            timeout_ms=min(max(action.timeout_ms, 500), 2000),
            key="stabilized.wait_after_login",
        )

    def _pending_business_center_action(
        self,
        action: BrowserAction,
        steps: list[dict],
        observation: BrowserObservation,
    ) -> BrowserAction | None:
        if action.type not in {"click", "wait_for", "observe_page", "extract_text", "finish"}:
            return None
        login_index = next(
            (
                index
                for index in range(len(steps) - 1, -1, -1)
                if steps[index].get("result") == "success"
                and (steps[index].get("action") or {}).get("type") == "login_submit"
            ),
            None,
        )
        if login_index is None or any(
            step.get("result") == "success"
            and (step.get("action") or {}).get("type") == "click"
            and self._click_target_matches_label(self._display_target(step.get("action") or {}), "财司系统")
            for step in steps[login_index + 1 :]
        ):
            return None
        element = self._find_element(observation, {"财司系统", "Business Center"})
        if element is None:
            return None
        current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
        if action.type == "click" and self._click_target_matches_label(current_target, "财司系统"):
            return None
        return BrowserAction(
            type="click",
            target_hint=element.text or element.name or "财司系统",
            target_id=element.element_id,
            expected_outcome="登录后先进入财司系统主界面",
            risk_level="safe_local_edit",
            requires_confirmation=False,
            timeout_ms=action.timeout_ms,
            key="stabilized.business_center_after_login",
        )

    def _review_confirmation_action(self, action: BrowserAction, steps: list[dict]) -> BrowserAction | None:
        if action.type != "click":
            return None
        target = self._clean_click_label(action.target_hint or action.target_id or "") or ""
        if target not in {"确定", "确认", "是"}:
            return None
        review_index = next(
            (
                index
                for index in range(len(steps) - 1, -1, -1)
                if steps[index].get("result") == "success"
                and (steps[index].get("action") or {}).get("type") == "click"
                and self._click_target_matches_label(self._display_target(steps[index].get("action") or {}), "复核")
            ),
            None,
        )
        if review_index is None:
            return None
        return BrowserAction(
            type="click",
            target_hint=action.target_hint or target,
            target_id=action.target_id,
            expected_outcome="确认提交复核操作",
            risk_level="unsafe_mutation",
            requires_confirmation=action.requires_confirmation,
            timeout_ms=action.timeout_ms,
            key=action.key or "stabilized.confirm_review",
        )

    def _pending_review_select_all_action(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
        observation: BrowserObservation,
    ) -> BrowserAction | None:
        if action.type not in {"click", "wait_for", "observe_page", "extract_text", "finish"}:
            return None
        if not re.search(r"(?:选中|选择|勾选)所有.{0,20}(?:复核|数据|记录)", spec.user_goal):
            return None
        review_index = next(
            (
                index
                for index in range(len(steps) - 1, -1, -1)
                if steps[index].get("result") == "success"
                and (steps[index].get("action") or {}).get("type") == "click"
                and self._click_target_matches_label(
                    self._display_target(steps[index].get("action") or {}),
                    "网银用户复核",
                )
            ),
            None,
        )
        if review_index is None or self._has_empty_result_signal(observation):
            return None
        if any(
            step.get("result") == "success"
            and (step.get("action") or {}).get("type") == "click"
            and (
                (step.get("action") or {}).get("key") == "stabilized.review_select_all"
                or "全选" in self._display_target(step.get("action") or {})
            )
            for step in steps[review_index + 1 :]
        ):
            return None
        checkbox = next(
            (
                element
                for element in observation.interactive_elements
                if element.is_enabled
                and element.is_visible
                and (element.input_type or "").lower() == "checkbox"
            ),
            None,
        )
        if checkbox is None:
            return None
        current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
        if action.type == "click" and action.target_id == checkbox.element_id and "全选" in current_target:
            return None
        return BrowserAction(
            type="click",
            target_hint="全选复选框",
            target_id=checkbox.element_id,
            expected_outcome="按用户指令选中当前列表中所有需要复核的数据",
            risk_level="safe_local_edit",
            requires_confirmation=False,
            timeout_ms=action.timeout_ms,
            key="stabilized.review_select_all",
        )

    def _stabilize_type_action(self, spec: BrowserTaskSpec, action: BrowserAction, steps: list[dict]) -> BrowserAction:
        if action.value is None:
            return action
        pending_input = self._pending_explicit_input_request(spec, steps)
        if pending_input is not None and pending_input[1] == action.value:
            expected_field = pending_input[0]
        else:
            expected_field = self._explicit_input_field_for_value(spec.user_goal, action.value)
        if not expected_field:
            return action
        current_target = f"{action.target_hint or ''} {action.target_id or ''}"
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        if self._dropdown_appears_open_for_field(expected_field, action.value or "", observation):
            return BrowserAction(
                type=action.type,
                target_hint=f"{expected_field}搜索输入框",
                target_id=None,
                value=action.value,
                expected_outcome=f"按用户指令在 {expected_field} 下拉搜索框中输入 {action.value}",
                risk_level=action.risk_level,
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key="stabilized.pending_dropdown_input",
            )
        expected = self._find_exact_field_element(observation, expected_field)
        if expected is not None:
            if action.target_id == expected.element_id:
                return action
            return BrowserAction(
                type=action.type,
                target_hint=expected.name or expected.placeholder or expected.title or expected.text or expected_field,
                target_id=expected.element_id,
                value=action.value,
                expected_outcome=f"按用户指令在字段 {expected_field} 中输入 {action.value}",
                risk_level=action.risk_level,
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key=action.key or "stabilized.explicit_input",
            )
        if self._should_open_dropdown_before_type(expected_field, action.value, observation):
            return BrowserAction(
                type="click",
                target_hint=f"{expected_field}下拉列表",
                target_id=None,
                expected_outcome=f"先展开 {expected_field} 下拉列表，再输入 {action.value}",
                risk_level="safe_local_edit",
                requires_confirmation=False,
                timeout_ms=action.timeout_ms,
                key=action.key or "stabilized.open_dropdown_before_type",
            )
        if action.target_id and expected_field in current_target:
            return action
        if (action.target_hint or "").strip() == expected_field:
            return action
        return BrowserAction(
            type=action.type,
            target_hint=expected_field,
            target_id=None,
            value=action.value,
            expected_outcome=f"按用户指令在字段 {expected_field} 中输入 {action.value}",
            risk_level=action.risk_level,
            requires_confirmation=action.requires_confirmation,
            timeout_ms=action.timeout_ms,
            key=action.key or "stabilized.explicit_input",
        )

    def _pending_explicit_navigation_action(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
    ) -> BrowserAction | None:
        labels = self._explicit_navigation_labels(spec.user_goal)
        if not labels or action.type not in {"click", "wait_for", "observe_page", "extract_text", "finish"}:
            return None
        next_label = next(
            (
                label
                for label in labels
                if not any(
                    step.get("result") == "success"
                    and (step.get("action") or {}).get("type") == "click"
                    and self._click_target_matches_label(self._display_target(step.get("action") or {}), label)
                    for step in steps
                )
            ),
            None,
        )
        if next_label is None:
            return None
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        element = self._find_element(observation, {next_label})
        if element is None:
            return None
        current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
        if action.type == "click" and self._click_target_matches_label(current_target, next_label):
            return None
        return BrowserAction(
            type="click",
            target_hint=element.text or element.name or next_label,
            target_id=element.element_id,
            expected_outcome=f"按用户指定顺序点击菜单: {next_label}",
            risk_level="safe_local_edit",
            requires_confirmation=False,
            timeout_ms=action.timeout_ms,
            key="stabilized.explicit_navigation",
        )

    def _explicit_navigation_labels(self, goal: str) -> list[str]:
        match = re.search(r"依次点击(.+?)(?:进入对应菜单|进入[^,，。；;]{0,20}|[,，]\s*等待)", goal)
        if not match:
            return []
        labels = [item.strip(" '\"“”") for item in re.split(r"[,，、]", match.group(1))]
        return [label for label in labels if label]

    def _pending_click_after_explicit_navigation(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
    ) -> BrowserAction | None:
        labels = self._explicit_navigation_labels(spec.user_goal)
        if not labels or action.type not in {"click", "wait_for", "observe_page", "extract_text", "finish"}:
            return None
        navigation_indexes = [
            index
            for index, step in enumerate(steps)
            if step.get("result") == "success"
            and (step.get("action") or {}).get("type") == "click"
            and any(
                self._click_target_matches_label(self._display_target(step.get("action") or {}), label)
                for label in labels
            )
        ]
        if len(navigation_indexes) < len(labels):
            return None
        match = re.search(r"等待[^,，。；;]{0,20}[,，]?\s*然后点击\s*([^,，。；;]+)", spec.user_goal)
        if not match:
            return None
        target = self._clean_click_label(match.group(1))
        if not target or self._click_done_after_index(target, steps, max(navigation_indexes)):
            return None
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        element = self._find_command_element(observation, target)
        if element is None:
            return BrowserAction(
                type="wait_for",
                expected_outcome=f"等待目标菜单加载并出现按钮: {target}",
                risk_level="safe_read",
                requires_confirmation=False,
                timeout_ms=min(max(action.timeout_ms, 500), 2000),
                key="stabilized.wait_after_explicit_navigation",
            )
        current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
        if action.type == "click" and self._click_target_matches_label(current_target, target):
            return None
        return BrowserAction(
            type="click",
            target_hint=element.text or element.name or target,
            target_id=element.element_id,
            expected_outcome=f"完成菜单导航后点击用户指定按钮: {target}",
            risk_level="safe_local_edit",
            requires_confirmation=False,
            timeout_ms=action.timeout_ms,
            key="stabilized.after_explicit_navigation",
        )

    def _pending_explicit_result_sequence_action(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
    ) -> BrowserAction | None:
        if action.type not in {
            "click",
            "wait_for",
            "observe_page",
            "extract_text",
            "finish",
            "type",
            "select",
            "press",
            "save_artifact",
        }:
            return None
        labels = self._explicit_result_click_labels(spec.user_goal)
        if not labels:
            return None
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        label = self._first_unmatched_click_label(labels, steps)
        if label is None:
            return None
        element = self._find_command_element(observation, label)
        if element is None:
            return None
        current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
        if action.type == "click" and self._click_target_matches_label(current_target, label):
            return None
        return BrowserAction(
            type="click",
            target_hint=element.text or element.name or label,
            target_id=element.element_id,
            expected_outcome=f"按用户指定结果顺序点击: {label}",
            risk_level="safe_local_edit",
            requires_confirmation=False,
            timeout_ms=action.timeout_ms,
            key="stabilized.explicit_result_sequence",
        )

    def _first_unmatched_click_label(self, labels: list[str], steps: list[dict]) -> str | None:
        search_from = 0
        for label in labels:
            matched_index = next(
                (
                    index
                    for index in range(search_from, len(steps))
                    if steps[index].get("result") == "success"
                    and (steps[index].get("action") or {}).get("type") == "click"
                    and self._click_target_matches_label(
                        self._display_target(steps[index].get("action") or {}),
                        label,
                    )
                ),
                None,
            )
            if matched_index is None:
                return label
            search_from = matched_index + 1
        return None

    def _explicit_result_click_labels(self, goal: str) -> list[str]:
        match = re.search(r"点击查询结果中的\s*([^,，。；;]+)", goal)
        if not match:
            return []
        labels = [match.group(1).strip(" '\"“”")]
        input_starts = [start for _, _, start, _ in self._explicit_input_requests_with_spans(goal) if start >= match.end()]
        tail_end = min(input_starts, default=len(goal))
        tail = goal[match.end():tail_end]
        labels.extend(
            item.strip(" '\"“”")
            for item in re.findall(r"(?:之后|然后|随后|接着|再)?点击\s*([^,，。；;]+)", tail)
        )
        return [self._clean_click_label(label) or label for label in labels if label]

    def _explicit_input_field_for_value(self, goal: str, value: str) -> str | None:
        normalized_value = value.strip("'\"“”")
        if not normalized_value or normalized_value not in goal:
            return None
        for field, matched_value in self._explicit_input_requests(goal):
            if matched_value == normalized_value:
                return field
        return None

    def _explicit_input_requests(self, goal: str) -> list[tuple[str, str]]:
        return [(field, value) for field, value, _, _ in self._explicit_input_requests_with_spans(goal)]

    def _explicit_input_requests_with_spans(self, goal: str) -> list[tuple[str, str, int, int]]:
        requests: list[tuple[str, str, int, int]] = []
        for match in self._explicit_input_matches(goal):
            matched_value = match.group(2).strip("'\"“” ")
            field = self._normalize_explicit_input_field(match.group(1), matched_value)
            if field and matched_value:
                requests.append((field, matched_value, match.start(), match.end()))

        value_first_pattern = (
            r"(?:将|把)\s*[\"“”']?(?P<value>[^,，。；;\"“”']+?)[\"“”']?\s*"
            r"(?:输入|填写|填入)(?:到|至|进)?\s*(?P<fields>.+?)(?:中|里)"
            r"(?=$|[,，。；;]|然后|之后|随后|接着|再)"
        )
        for match in re.finditer(value_first_pattern, goal):
            value = match.group("value").strip("'\"“” ")
            fields_text = match.group("fields")
            fields = [item.strip("'\"“” ") for item in re.split(r"\s*(?:和|及|与|、|,|，)\s*", fields_text)]
            for raw_field in fields:
                field = self._normalize_explicit_input_field(raw_field, value)
                if field and value:
                    requests.append((field, value, match.start(), match.end()))

        requests.sort(key=lambda item: (item[2], item[3]))
        deduplicated: list[tuple[str, str, int, int]] = []
        for item in requests:
            if not any(existing[:2] == item[:2] for existing in deduplicated):
                deduplicated.append(item)
        return deduplicated

    def _explicit_input_matches(self, goal: str):
        patterns = (
            r"(?:在|向)\s*([^,，。；;\s]{2,30}?)(?:展开|打开|选择|点击)"
            r"[^,，。；;]{0,60}(?:下拉|列表|搜索|框)[^,，。；;]{0,20}"
            r"[,，]?\s*(?:输入|填写|填入)\s*[\"“']?([^,，。；;\"”']+)",
            r"(?:在|向)\s*([^,，。；;\s]{2,30}?)(?:中|里)?\s*(?:输入|填写|填入)\s*[\"“']?([^,，。；;\"”']+)",
            r"(用户名称|登录名称|所属单位编号|所属单位|单位编号)(?:字段)?(?:中|里)\s*"
            r"(?:输入|填写|填入)\s*[\"“']?([^,，。；;\"”']+)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, goal):
                yield match

    def _normalize_explicit_input_field(self, field: str, value: str) -> str:
        cleaned = field.strip("'\"“” ")
        if "授权单位" in cleaned:
            return "授权单位"
        if "登录名称" in cleaned:
            return "登录名称"
        if "用户名称" in cleaned:
            return "用户名称"
        if "用户名" in cleaned:
            return "用户名"
        if any(token in cleaned for token in ("下拉", "搜索框", "候选")) and self._looks_like_org_unit(value):
            return "授权单位"
        for suffix in ("下拉搜索框", "搜索输入框", "下拉输入框", "下拉列表", "输入框", "文本框", "字段"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
                break
        return cleaned.strip("'\"“” ")

    def _pending_explicit_input_request(self, spec: BrowserTaskSpec, steps: list[dict]) -> tuple[str, str] | None:
        for field, value in self._explicit_input_requests(spec.user_goal):
            if not self._explicit_input_already_done(field, value, steps):
                return field, value
        return None

    def _explicit_input_already_done(self, field: str, value: str, steps: list[dict]) -> bool:
        for step in steps:
            if step.get("result") != "success":
                continue
            observation = step.get("observation") or {}
            if isinstance(observation, dict) and observation.get("page_type") == "login":
                continue
            action = step.get("action") or {}
            if action.get("type") != "type" or str(action.get("value") or "") != value:
                continue
            target = " ".join(
                str(part)
                for part in (
                    action.get("target_hint"),
                    action.get("target_id"),
                    action.get("expected_outcome"),
                    action.get("key"),
                )
                if part
            )
            if self._field_label_matches_target(field, target):
                return True
        return False

    def _should_type_into_open_dropdown_search(
        self,
        field: str,
        value: str,
        action: BrowserAction,
        observation: BrowserObservation,
    ) -> bool:
        if "授权单位" not in field and not self._looks_like_org_unit(value):
            return False
        action_text = " ".join(part for part in (action.target_hint, action.expected_outcome) if part)
        page_text = observation.page_text or ""
        field_aliases = self._field_aliases(field)
        if not any(self._text_contains(page_text, alias) or self._text_contains(action_text, alias) for alias in field_aliases):
            return False
        action_points_to_open_search = any(
            self._text_contains(action_text, token)
            for token in ("当前选中", "搜索框", "高亮", "第一个内容", "下拉", "dropdown")
        )
        return action_points_to_open_search and self._dropdown_appears_open_for_field(field, value, observation)

    def _dropdown_appears_open_for_field(self, field: str, value: str, observation: BrowserObservation) -> bool:
        if "授权单位" not in field and not self._looks_like_org_unit(value):
            return False
        page_text = observation.page_text or ""
        field_aliases = self._field_aliases(field)
        if not any(self._text_contains(page_text, alias) for alias in field_aliases):
            return False
        return any(
            token in page_text
            for token in (
                "results are available",
                "result is available",
                "press enter to select it",
                "加载结果中",
                "搜索中",
                "use up and down arrow keys",
                "Searching",
            )
        )

    def _should_open_dropdown_before_type(self, field: str, value: str | None, observation: BrowserObservation) -> bool:
        if "授权单位" not in field and not self._looks_like_org_unit(value or ""):
            return False
        page_text = observation.page_text or ""
        if not any(token in page_text for token in ("授权单位", "Authorized Agency")):
            return False
        return not any(token in page_text for token in ("results are available", "加载结果中", "use up and down arrow keys"))

    def _pending_click_after_explicit_type(self, spec: BrowserTaskSpec, steps: list[dict]) -> str | None:
        last_type_index: int | None = None
        for index in range(len(steps) - 1, -1, -1):
            action = steps[index].get("action") or {}
            if action.get("type") == "type" and action.get("value"):
                last_type_index = index
                break
        if last_type_index is None:
            return None
        typed_action = steps[last_type_index].get("action") or {}
        expected_click = self._expected_click_after_last_type(spec.user_goal, typed_action)
        if not expected_click:
            return None
        for step_index in range(last_type_index + 1, len(steps)):
            step = steps[step_index]
            if step.get("result") != "success":
                continue
            action = step.get("action") or {}
            target = self._display_target(action)
            if action.get("type") == "press" and self._press_satisfies_first_search_result(
                expected_click,
                action,
                step.get("observation") or {},
                steps[: step_index + 1],
            ):
                return None
            if action.get("type") == "click" and (
                self._click_target_matches_label(target, expected_click)
                or self._click_satisfies_first_search_result(
                    expected_click,
                    target,
                    steps[: last_type_index + 1],
                )
            ):
                return None
        return expected_click

    def _pending_press_after_explicit_type(
        self,
        spec: BrowserTaskSpec,
        steps: list[dict],
    ) -> BrowserAction | None:
        for type_index in range(len(steps) - 1, -1, -1):
            step = steps[type_index]
            action = step.get("action") or {}
            if step.get("result") != "success" or action.get("type") != "type" or not action.get("value"):
                continue
            match_end = self._explicit_input_match_end_for_value(spec.user_goal, str(action["value"]))
            if match_end is None:
                continue
            tail = spec.user_goal[match_end : match_end + 40]
            if not re.search(r"(?:然后|随后|并|再|后)?\s*(?:按下?|敲击)?\s*(?:回车|enter)", tail, re.I):
                continue
            if any(
                later.get("result") == "success"
                and (later.get("action") or {}).get("type") == "press"
                and str(
                    (later.get("action") or {}).get("value")
                    or (later.get("action") or {}).get("target_hint")
                    or ""
                ).strip().lower()
                == "enter"
                for later in steps[type_index + 1 :]
            ):
                return None
            return BrowserAction(
                type="press",
                target_hint=str(action.get("target_hint") or "当前输入字段"),
                value="Enter",
                expected_outcome="按用户指令在输入后按回车，触发字段校验或候选项选择",
                risk_level="safe_local_edit",
                requires_confirmation=False,
                timeout_ms=3000,
                key="stabilized.pending_explicit_press",
            )
        return None

    def _press_satisfies_first_search_result(
        self,
        expected_click: str,
        action: dict,
        observation: dict,
        steps: list[dict],
    ) -> bool:
        key = str(action.get("value") or action.get("target_hint") or "")
        if key.lower() != "enter" or not self._means_first_search_result(expected_click):
            return False
        value = self._latest_dropdown_typed_value(steps)
        if not value:
            return False
        page_text = str((observation or {}).get("page_text") or "")
        return value in page_text

    def _redundant_selected_dropdown_value_click(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
    ) -> BrowserAction | None:
        if action.type != "click":
            return None
        value = self._latest_dropdown_typed_value(steps)
        if not value or not self._click_text_matches(action.target_hint or action.target_id or "", value):
            return None
        if not self._dropdown_value_selected(value, steps):
            return None
        expected_click = self._expected_click_after_first_search_result(spec.user_goal)
        if not expected_click:
            return None
        return BrowserAction(
            type="click",
            target_hint=expected_click,
            expected_outcome=f"授权单位已选择 {value}，继续点击后续按钮: {expected_click}",
            risk_level=action.risk_level,
            requires_confirmation=action.requires_confirmation,
            timeout_ms=action.timeout_ms,
            key="stabilized.after_dropdown_selection",
        )

    def _dropdown_value_selected(self, value: str, steps: list[dict]) -> bool:
        for step in reversed(steps):
            observation = step.get("observation") or {}
            page_text = str(observation.get("page_text") or "")
            dropdown_open_markers = (
                "results are available",
                "result is available",
                "press enter to select it",
                "use up and down arrow keys",
                "加载结果中",
                "Searching",
            )
            if value in page_text:
                return not any(marker in page_text for marker in dropdown_open_markers)
        return False

    def _expected_click_after_first_search_result(self, goal: str) -> str | None:
        match = re.search(
            r"(?:第一个内容|第一个公司|第一个候选项|第一个匹配候选项|高亮[^,，。；;]*内容)"
            r"[^,，。；;]*[,，]\s*(?:之后|然后)?点击\s*([^,，。；;]+)",
            goal,
        )
        if not match:
            return None
        label = self._clean_click_label(match.group(1))
        return label or None

    def _pending_click_after_first_search_result(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
    ) -> BrowserAction | None:
        if action.type not in {"click", "extract_text", "finish", "save_artifact"}:
            return None
        expected_click = self._expected_click_after_first_search_result(spec.user_goal)
        if not expected_click:
            return None
        selection_index = self._first_search_result_selection_index(steps)
        if selection_index is None or self._click_done_after_index(expected_click, steps, selection_index):
            return None
        current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
        if self._click_target_matches_label(current_target, expected_click):
            return None
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        expected = self._find_command_element(observation, expected_click)
        return BrowserAction(
            type="click",
            target_hint=expected.text or expected.name or expected_click if expected is not None else expected_click,
            target_id=expected.element_id if expected is not None else None,
            expected_outcome=f"下拉第一项已选中，继续点击后续按钮: {expected_click}",
            risk_level=action.risk_level,
            requires_confirmation=action.requires_confirmation,
            timeout_ms=action.timeout_ms,
            key="stabilized.after_first_search_result",
        )

    def _first_search_result_selection_index(self, steps: list[dict]) -> int | None:
        for index, step in enumerate(steps):
            if step.get("result") != "success":
                continue
            action = step.get("action") or {}
            if action.get("key") == "stabilized.first_search_result":
                return index
            key = str(action.get("value") or action.get("target_hint") or "")
            if action.get("type") == "press" and key.lower() == "enter" and self._latest_dropdown_typed_value(steps[: index + 1]):
                return index
        return None

    def _click_done_after_index(self, expected_click: str, steps: list[dict], index: int) -> bool:
        for step in steps[index + 1 :]:
            if step.get("result") != "success":
                continue
            action = step.get("action") or {}
            if action.get("type") != "click":
                continue
            if self._click_target_matches_label(self._display_target(action), expected_click):
                return True
        return False

    def _find_exact_field_element(self, observation: BrowserObservation, label: str) -> InteractiveElement | None:
        candidates = [
            element for element in observation.interactive_elements
            if element.role in {"input", "select", "textarea"} and element.is_enabled and element.is_visible
        ]
        labels = self._field_aliases(label)
        exact_attrs = ("name", "placeholder", "title", "text")
        for label_item in labels:
            for element in candidates:
                if any(self._text_equal(getattr(element, attr) or "", label_item) for attr in exact_attrs):
                    return element
        for label_item in labels:
            for element in candidates:
                haystack = " ".join(
                    str(part)
                    for part in (
                        element.name,
                        element.placeholder,
                        element.title,
                        element.text,
                        element.context,
                    )
                    if part
                )
                if self._text_contains(haystack, label_item):
                    return element
        return None

    def _field_aliases(self, label: str) -> list[str]:
        aliases = [label]
        if label in {"用户名称", "用户姓名", "用户名"}:
            aliases.extend(["用户名称", "用户姓名", "用户名", "User Name", "UserName", "userName", "username"])
        elif label == "登录名称":
            aliases.extend(["登录名称", "Login Name", "loginName", "login_name", "login"])
        elif label == "授权单位":
            aliases.extend(["授权单位", "Authorized Agency", "authorization unit", "orgUnit"])
        elif label in {"账户编号由", "账户号由", "账号由", "账户编号从"}:
            aliases.extend(
                [
                    "账户编号由",
                    "账户号由",
                    "账号由",
                    "Account No From",
                    "Account Number From",
                    "startAccountNo",
                    "accountNoFrom",
                    "accountFrom",
                ]
            )
        elif label in {"账户编号至", "账户号至", "账号至", "账户编号到"}:
            aliases.extend(
                [
                    "账户编号至",
                    "账户号至",
                    "账号至",
                    "Account No To",
                    "Account Number To",
                    "endAccountNo",
                    "accountNoTo",
                    "accountTo",
                ]
            )
        return list(dict.fromkeys(item for item in aliases if item))

    def _field_label_matches_target(self, label: str, target: str) -> bool:
        return any(self._text_contains(target, alias) for alias in self._field_aliases(label))

    def _click_target_matches_label(self, target: str, label: str) -> bool:
        return any(self._click_text_matches(target, alias) for alias in self._click_label_aliases(label))

    def _click_label_aliases(self, label: str) -> list[str]:
        aliases = [label]
        if label in {"财司系统", "Business Center"}:
            aliases.extend(["财司系统", "Business Center"])
        elif label in {"确定", "确认"}:
            aliases.extend(["确定", "确认", "OK", "Ok", "ok", "Search"])
        elif label in {"查询", "Query", "Search"}:
            aliases.extend(["查询", "Search", "Query"])
        elif label == "分配岗位":
            aliases.extend(["分配岗位", "Assign Job"])
        elif label == "已分配岗位":
            aliases.extend(["已分配岗位", "Assigned Job", "Assigned Jobs", "Assigned position"])
        elif label in {"已分配账户", "Assigned Account"}:
            aliases.extend(["已分配账户", "Assigned Account"])
        elif label == "取消":
            aliases.extend(["取消", "Cancel"])
        return list(dict.fromkeys(item for item in aliases if item))

    def _find_command_element(self, observation: BrowserObservation, label: str) -> InteractiveElement | None:
        if label == "查询":
            query = self._find_query_button(observation)
            if query is not None:
                return query
        return self._find_element(observation, set(self._click_label_aliases(label)))

    def _find_query_button(self, observation: BrowserObservation) -> InteractiveElement | None:
        labels = set(self._click_label_aliases("查询"))
        direct = [
            element
            for element in observation.interactive_elements
            if self._is_clickable_element(element)
            and any(
                self._click_text_matches(part, label)
                for label in labels
                for part in (element.name, element.text, element.title)
                if part
            )
        ]
        candidates = direct or [
            element
            for element in observation.interactive_elements
            if self._is_clickable_element(element)
            and any(self._click_text_matches(element.context or "", label) for label in labels)
        ]
        if not candidates:
            return None
        return max(candidates, key=self._query_button_score)

    def _query_button_score(self, element: InteractiveElement) -> int:
        text = " ".join(part for part in (element.name, element.text, element.title) if part)
        context = element.context or ""
        compact_context = " ".join(context.split())
        score = 0
        if any(self._text_equal(text, label) for label in self._click_label_aliases("查询")):
            score += 100
        if compact_context in set(self._click_label_aliases("查询")):
            score += 60
        if any(
            token in context
            for token in ("Cancel", "取消", "Login Name", "User Name", "User Flag", "用户名", "登录名称", "授权单位")
        ):
            score += 30
        if any(token in context for token in ("Assign Job", "Run", "分配岗位", "启用/停用", "Duty Assign")):
            score -= 50
        if (element.role or "").lower() == "button" or (element.input_type or "").lower() in {"button", "submit"}:
            score += 5
        score -= min(len(compact_context), 80) // 10
        return score

    def _correct_command_click_from_expected_outcome(
        self,
        action: BrowserAction,
        steps: list[dict],
    ) -> BrowserAction | None:
        if action.type != "click":
            return None
        expected_outcome = action.expected_outcome or ""
        command_labels = ("已分配岗位", "分配岗位", "查询", "确定", "取消", "关闭")
        command = next(
            (
                label
                for label in command_labels
                if label in expected_outcome
                and re.search(
                    rf"(?:点击|按下|触发|提交|执行|click|press|submit)"
                    rf"[^,，。；;]{{0,48}}{re.escape(label)}"
                    + (r"(?!结果|后|到|出的)" if label == "查询" else ""),
                    expected_outcome,
                    re.I,
                )
            ),
            None,
        )
        if command is None:
            return None
        current_target = " ".join(part for part in (action.target_hint, action.target_id) if part)
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        expected = self._find_command_element(observation, command)
        if self._click_target_matches_label(current_target, command) and expected is None:
            return None
        target_hint = command
        target_id = None
        if expected is not None:
            target_hint = expected.text or expected.name or expected.title or command
            target_id = expected.element_id
        return BrowserAction(
            type="click",
            target_hint=target_hint,
            target_id=target_id,
            expected_outcome=f"按动作意图点击命令按钮: {command}",
            risk_level=action.risk_level,
            requires_confirmation=action.requires_confirmation,
            timeout_ms=action.timeout_ms,
            key="stabilized.expected_command_click",
        )

    def _text_equal(self, value: str, expected: str) -> bool:
        if value == expected:
            return True
        return value.lower() == expected.lower()

    def _text_contains(self, value: str, expected: str) -> bool:
        if expected in value:
            return True
        return expected.lower() in value.lower()

    def _click_text_matches(self, value: str, expected: str) -> bool:
        if not value or not expected:
            return False
        compact_value = re.sub(r"\s+", "", value)
        compact_expected = re.sub(r"\s+", "", expected)
        if re.search(r"[A-Za-z0-9_]", compact_expected):
            return (
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(compact_expected)}(?![A-Za-z0-9_])",
                    compact_value,
                    re.I,
                )
                is not None
            )
        return self._text_contains(compact_value, compact_expected)

    def _should_select_first_result_row_before_assignment(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
    ) -> bool:
        if action.type != "click":
            return False
        if self._pending_click_after_explicit_type(spec, steps):
            return False
        if not re.search(r"选中.{0,30}(?:第一条|第一个|首个)", spec.user_goal):
            return False
        current_target = " ".join(
            part for part in (action.target_hint, action.target_id, action.expected_outcome) if part
        )
        if self._first_result_row_selection_already_done(steps):
            return False
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        if not self._observation_has_query_result_row(observation):
            return False
        if "分配岗位" in current_target:
            return True
        if re.search(r"(?:选中|选择|勾选|高亮).{0,30}(?:第一条|第一个|首个|数据行|记录)", current_target):
            return True
        return re.search(r"\bU\d{4,}\b", current_target) is not None

    def _first_result_row_selection_already_done(self, steps: list[dict]) -> bool:
        for step in reversed(steps):
            action = step.get("action") or {}
            if action.get("key") == "stabilized.first_table_row" and step.get("result") == "success":
                return True
        return False

    def _observation_has_query_result_row(self, observation: BrowserObservation) -> bool:
        text = observation.page_text or ""
        if "已分配岗位" in text and "岗位名称" in text:
            return False
        has_result_count = any(
            marker in text
            for marker in ("显示1到1", "共1记录", "共 1 记录", "Displaying 1 to 1", "of 1 items")
        )
        has_user_table = (
            "用户编号" in text and ("用户名" in text or "用户名称" in text or "登录名称" in text)
        ) or (
            "User No" in text and ("User Name" in text or "Login Name" in text)
        )
        has_data_row = re.search(r"U\d{4,}\s+\S+\s+\S+", text) is not None
        return has_user_table and (has_result_count or has_data_row)

    def _expected_click_after_last_type(self, goal: str, previous_action: dict) -> str | None:
        if previous_action.get("type") != "type":
            return None
        field = str(previous_action.get("target_hint") or "")
        value = str(previous_action.get("value") or "")
        if not field and not value:
            return None
        start = self._explicit_input_match_end_for_value(goal, value)
        if start is None:
            anchors = [anchor for anchor in (value, field) if anchor and anchor in goal]
            if not anchors:
                return None
            start = max(goal.rfind(anchor) + len(anchor) for anchor in anchors)
        tail = goal[start:]
        match = re.search(r"点击\s*([^,，。；;]+?)(?=$|[,，。；;])", tail)
        if not match:
            return None
        label = self._clean_click_label(match.group(1))
        return label or None

    def _explicit_input_match_end_for_value(self, goal: str, value: str) -> int | None:
        normalized_value = value.strip("'\"“”")
        if not normalized_value:
            return None
        best_end: int | None = None
        for _, matched_value, _, match_end in self._explicit_input_requests_with_spans(goal):
            if matched_value == normalized_value:
                best_end = match_end
        return best_end

    def _clean_click_label(self, label: str) -> str:
        cleaned = label.strip("'\"“” ")
        cleaned = re.sub(
            r"^(?:下方的|上方的|弹出内容中的|弹窗中的|页面中的|列表中的|"
            r"弹层中的?|弹层中|下拉列表中的?)",
            "",
            cleaned,
        )
        cleaned = re.sub(r"(?:按钮|链接|标签|菜单项).*$", "", cleaned)
        if "候选项" not in cleaned:
            cleaned = re.sub(r"选项.*$", "", cleaned)
        return cleaned.strip("'\"“” ")

    def _means_first_search_result(self, label: str) -> bool:
        first_markers = ("第一个", "第一条", "首个", "第一项", "第1个", "第1条")
        result_markers = ("搜索", "结果", "公司", "数据", "匹配", "候选", "选项", "内容", "弹层", "下拉")
        return (
            any(token in label for token in first_markers)
            and any(token in label for token in result_markers)
        ) or (
            "高亮" in label and any(token in label for token in ("项", "内容", "候选", "选项"))
        )

    def _click_satisfies_first_search_result(
        self,
        expected_click: str,
        target: str,
        prior_steps: list[dict],
    ) -> bool:
        if not self._means_first_search_result(expected_click):
            return False
        last_value = self._latest_typed_value(prior_steps)
        if last_value and self._click_text_matches(target, last_value):
            return True
        return (
            self._looks_like_company_name(target)
            or self._looks_like_org_unit(target)
            or self._means_first_search_result(target)
        )

    def _latest_typed_value(self, steps: list[dict]) -> str:
        for step in reversed(steps):
            action = step.get("action") or {}
            if action.get("type") == "type" and action.get("value"):
                return str(action.get("value") or "")
        return ""

    def _latest_dropdown_typed_value(self, steps: list[dict]) -> str:
        for step in reversed(steps):
            action = step.get("action") or {}
            value = str(action.get("value") or "")
            if action.get("type") != "type" or not value:
                continue
            target = " ".join(
                str(part)
                for part in (
                    action.get("target_hint"),
                    action.get("target_id"),
                    action.get("expected_outcome"),
                    action.get("key"),
                )
                if part
            )
            if "授权单位" in target or "下拉" in target or "搜索输入框" in target or self._looks_like_org_unit(value):
                return value
        return ""

    def _find_element(self, observation: BrowserObservation, labels: set[str]) -> InteractiveElement | None:
        for element in observation.interactive_elements:
            if not self._is_clickable_element(element):
                continue
            text = " ".join(part for part in (element.name, element.text, element.title) if part)
            if any(self._click_text_matches(text, label) for label in labels) and element.is_enabled and element.is_visible:
                return element
        for element in observation.interactive_elements:
            if not self._is_clickable_element(element):
                continue
            if any(self._click_text_matches(element.context or "", label) for label in labels) and element.is_enabled and element.is_visible:
                return element
        return None

    def _is_clickable_element(self, element: InteractiveElement) -> bool:
        if not element.is_enabled or not element.is_visible:
            return False
        role = (element.role or "").lower()
        input_type = (element.input_type or "").lower()
        if role in {"a", "button", "link", "menuitem"}:
            return True
        return role == "input" and input_type in {"button", "submit", "reset", "image"}

    def _is_repeated_action(self, action: BrowserAction, steps: list[dict], threshold: int) -> bool:
        if threshold <= 0:
            return False
        count = 0
        for step in reversed(steps):
            previous = step.get("action") or {}
            if not self._action_signature_matches(action, previous):
                break
            count += 1
        return count >= threshold

    def _action_already_executed(self, action: BrowserAction, steps: list[dict]) -> bool:
        signature = (action.type, action.target_hint, action.target_id, action.value, action.key)
        for step in steps:
            if step.get("result") != "success":
                continue
            previous = step.get("action") or {}
            previous_signature = (
                previous.get("type"),
                previous.get("target_hint"),
                previous.get("target_id"),
                previous.get("value"),
                previous.get("key", ""),
            )
            if previous_signature == signature:
                return True
        return False

    def _early_stop_reason(
        self,
        spec: BrowserTaskSpec,
        result,
        observation: BrowserObservation,
        steps: list[dict],
    ) -> str | None:
        if not steps:
            return None
        reflection = steps[-1].get("reflection") or {}
        terminal_reason = reflection.get("terminal_reason")
        if terminal_reason:
            return str(terminal_reason)
        action = steps[-1].get("action") or {}
        if result.status != "success":
            return self._failed_action_terminal_reason(spec, action, result.error or "")
        page_missing_reason = self._page_missing_information_reason(spec, action, observation, steps)
        if page_missing_reason:
            return page_missing_reason
        return None

    def _reflect_after_action(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        result,
        observation: BrowserObservation,
        prior_steps: list[dict],
    ) -> dict:
        action_dict = self._safe_action_dict(action)
        candidate_steps = [
            *prior_steps,
            {
                "action": action_dict,
                "result": result.status,
                "observation": asdict(observation),
                "error": result.error,
            },
        ]
        intent_aligned, intent_reason = self._action_intent_alignment(spec, action, prior_steps)
        terminal_reason: str | None = None
        failure_category = "none"
        failure_reason = ""
        if result.status != "success":
            failure_reason = result.error or "动作执行失败。"
            terminal_reason = self._failed_action_terminal_reason(spec, action_dict, failure_reason)
            failure_category = self._failure_category(action_dict, terminal_reason, failure_reason)
        else:
            validation_reason = self._page_validation_error_reason(action_dict, observation)
            if validation_reason:
                failure_reason = validation_reason
                terminal_reason = validation_reason
                failure_category = "terminal_failure"
            else:
                page_missing_reason = self._page_missing_information_reason(spec, action_dict, observation, candidate_steps)
                if page_missing_reason:
                    failure_reason = page_missing_reason
                    terminal_reason = page_missing_reason
                    failure_category = "system_missing_information"
        return {
            "intent_aligned": intent_aligned,
            "intent_reason": intent_reason,
            "failure_category": failure_category,
            "failure_reason": failure_reason,
            "terminal": terminal_reason is not None,
            "terminal_reason": terminal_reason,
            "next_decision": "stop" if terminal_reason else "continue",
        }

    def _action_intent_alignment(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        prior_steps: list[dict],
    ) -> tuple[bool, str]:
        target = self._display_target(self._safe_action_dict(action))
        if action.key.startswith("stabilized."):
            return True, "动作已根据用户原始指令和当前页面状态完成稳定化修正。"
        if action.type == "type" and action.value:
            pending_input = self._pending_explicit_input_request(spec, prior_steps)
            if pending_input is not None and pending_input[1] == action.value:
                expected_field = pending_input[0]
            else:
                expected_field = self._explicit_input_field_for_value(spec.user_goal, action.value)
            if expected_field and not self._field_label_matches_target(expected_field, target):
                return False, f"用户要求在 {expected_field} 输入，但当前动作目标是 {target or '未知字段'}。"
            if expected_field:
                return True, f"输入动作目标与用户要求的字段 {expected_field} 一致。"
        if action.type == "click" and prior_steps:
            expected_click = self._expected_click_after_last_type(spec.user_goal, prior_steps[-1].get("action") or {})
            if expected_click:
                if self._click_target_matches_label(target, expected_click) or self._click_satisfies_first_search_result(
                    expected_click,
                    target,
                    prior_steps,
                ):
                    return True, f"点击动作符合用户要求的后续动作：{expected_click}。"
                return False, f"用户要求下一步点击 {expected_click}，但当前动作目标是 {target or '未知目标'}。"
        return True, "动作与当前规划和页面状态一致。"

    def _failure_category(self, action: dict, terminal_reason: str | None, raw_error: str) -> str:
        action_type = str(action.get("type") or "")
        if action_type in {"type_username", "type_password", "login_submit"} or (terminal_reason or "").startswith("登录失败"):
            return "login_failure"
        if terminal_reason:
            if terminal_reason.startswith("无法打开目标网站"):
                return "site_unavailable"
            if "系统中没有" in terminal_reason:
                return "system_missing_information"
            return "terminal_failure"
        if self._looks_like_locator_failure(raw_error):
            return "locator_failure"
        return "execution_failure"

    def _failed_action_terminal_reason(self, spec: BrowserTaskSpec, action: dict, error: str) -> str | None:
        action_type = str(action.get("type") or "")
        target = self._display_target(action)
        if action_type == "open_url":
            return "无法打开目标网站，请检查登录网站地址或网络连通性。"
        if action_type in {"type_username", "type_password", "login_submit"}:
            return self._login_action_failure_reason(action_type)
        if not self._looks_like_locator_failure(error):
            return None
        if action_type == "click":
            if self._is_navigation_target(spec.user_goal, target):
                return f"系统中没有找到对应菜单：{target}。"
            if self._looks_like_company_name(target):
                return f"系统中没有找到对应公司：{target}。"
            if self._means_first_search_result(target):
                return "系统中没有找到可选择的查询结果。"
            if target:
                return f"系统中没有找到可点击项：{target}。"
        if action_type in {"type", "select"} and target:
            return f"系统中没有找到输入字段：{target}。"
        return None

    def _page_missing_information_reason(
        self,
        spec: BrowserTaskSpec,
        action: dict,
        observation: BrowserObservation,
        steps: list[dict],
    ) -> str | None:
        page_text = observation.page_text or ""
        if self._is_assigned_role_result(spec.user_goal, page_text):
            return None
        if not self._has_empty_result_signal(observation):
            return None
        action_type = str(action.get("type") or "")
        target = self._display_target(action)
        if action_type == "click" and "查询" in target:
            query = self._latest_business_query_summary(spec, steps)
            if query:
                return f"系统中没有找到符合条件的信息：{query}。"
            if "选中" in spec.user_goal and "第一" in spec.user_goal:
                return "系统中没有找到查询后的第一条数据。"
        return None

    def _page_validation_error_reason(self, action: dict, observation: BrowserObservation) -> str | None:
        if action.get("type") != "click" or action.get("risk_level") != "unsafe_mutation":
            return None
        text = " ".join(part for part in (observation.page_text, self._visible_message_text(observation)) if part)
        compact = re.sub(r"\s+", "", text)
        signals = (
            "该输入项为必输项",
            "此项为必填项",
            "请输入必填项",
            "必填项不能为空",
            "requiredfield",
            "thisfieldisrequired",
        )
        if any(signal in compact.lower() for signal in signals):
            return "表单提交失败：页面仍存在未填写或未完成联动的必填项。"
        return None

    def _login_action_failure_reason(self, action_type: str) -> str:
        if action_type == "type_username":
            return "登录失败：系统中没有找到用户名输入框。"
        if action_type == "type_password":
            return "登录失败：系统中没有找到密码输入框。"
        return "登录失败：系统中没有找到登录按钮或登录提交失败。"

    def _login_failure_reason(self, observation: BrowserObservation) -> str:
        messages = self._visible_message_text(observation)
        if messages:
            return f"登录失败：{messages}"
        return "登录失败或仍停留在登录页，请检查账号、密码或登录页面状态。"

    def _display_target(self, action: dict) -> str:
        return str(action.get("target_hint") or action.get("target_id") or action.get("expected_outcome") or "").strip()

    def _looks_like_locator_failure(self, error: str) -> bool:
        lowered = error.lower()
        return any(
            marker in lowered
            for marker in (
                "timeout",
                "waiting for",
                "strict mode",
                "not visible",
                "not enabled",
                "intercepts pointer events",
                "无法定位",
                "找不到",
            )
        )

    def _looks_like_company_name(self, text: str) -> bool:
        return bool(text) and any(
            marker in text
            for marker in ("公司", "有限责任", "集团", "银行", "支行", "分行", "厂", "中心")
        )

    def _looks_like_org_unit(self, text: str) -> bool:
        return bool(text) and (
            re.search(r"\b\d{2,}-\d{5,}[_\-\s]?.+", text) is not None
            or "内部客户" in text
        )

    def _is_navigation_target(self, goal: str, target: str) -> bool:
        if not target:
            return False
        for item in self._navigation_targets_from_goal(goal):
            if target == item or target in item or item in target:
                return True
        return False

    def _navigation_targets_from_goal(self, goal: str) -> list[str]:
        match = re.search(r"(?:侧边栏|菜单|导航).{0,20}?依次点击(.{2,120}?)(?:进入|等待|然后|之后)", goal)
        if not match:
            match = re.search(r"依次点击(.{2,120}?)(?:进入|等待|然后|之后)", goal)
        if not match:
            return []
        raw = match.group(1)
        items = re.split(r"[,，、/>\s]+", raw)
        return [item.strip("'\"“” ") for item in items if item.strip("'\"“” ")]

    def _has_empty_result_signal(self, observation: BrowserObservation) -> bool:
        text = " ".join(part for part in (observation.page_text, self._visible_message_text(observation)) if part)
        compact = re.sub(r"\s+", "", text)
        return bool(
            re.search(r"显示\s*0\s*到\s*0\s*,?\s*共\s*0\s*记录", text)
            or any(
                marker in compact
                for marker in ("暂无数据", "没有数据", "无数据", "无记录", "未找到", "不存在", "查询无结果", "没有符合条件", "无匹配数据")
            )
        )

    def _latest_business_query_summary(self, spec: BrowserTaskSpec, steps: list[dict]) -> str | None:
        for step in reversed(steps[:-1]):
            if step.get("result") != "success":
                continue
            action = step.get("action") or {}
            if action.get("type") == "login_submit":
                break
            if action.get("type") == "click" and self._is_navigation_target(
                spec.user_goal,
                self._display_target(action),
            ):
                break
            if action.get("type") != "type":
                continue
            field = str(action.get("target_hint") or "").strip()
            value = str(action.get("value") or "").strip()
            if not value:
                continue
            if "授权单位" in field or self._looks_like_company_name(value):
                continue
            return f"{field or '查询条件'}={value}"
        return None

    def _visible_message_text(self, observation: BrowserObservation) -> str:
        return "；".join(message.strip() for message in observation.visible_messages if message.strip())

    def _is_assigned_role_result(self, goal: str, page_text: str) -> bool:
        if "已分配岗位" not in goal:
            return False
        has_assigned_section = "已分配岗位" in page_text or "Assigned position" in page_text
        has_role_name_column = "岗位名称" in page_text or re.search(r"\b(?:Assign duty Name|Duty Name)\b", page_text, re.I)
        return has_assigned_section and bool(has_role_name_column)

    def _login_still_pending(self, steps: list[dict]) -> bool:
        login_submits_on_login = 0
        wait_or_observe_after_submit = 0
        seen_submit = False
        for step in steps:
            action = step.get("action") or {}
            observation = step.get("observation") or {}
            action_type = action.get("type")
            if action_type == "login_submit" and observation.get("page_type") == "login":
                login_submits_on_login += 1
                seen_submit = True
                continue
            if seen_submit and action_type in {"wait_for", "observe_page"}:
                wait_or_observe_after_submit += 1
        return login_submits_on_login <= 1 and wait_or_observe_after_submit < 2

    def _login_has_failure_signal(self, observation: BrowserObservation) -> bool:
        text = " ".join(observation.visible_messages).lower()
        return any(
            keyword in text
            for keyword in (
                "错误",
                "失败",
                "无效",
                "不正确",
                "密码错误",
                "账号不存在",
                "incorrect",
                "invalid",
                "failed",
                "error",
            )
        )

    def _safe_action_dict(self, action: BrowserAction) -> dict:
        payload = asdict(action)
        if self._is_secret_action(action):
            payload["value"] = "***"
        return payload

    def _replay_actions(self, steps: list[dict]) -> list[BrowserAction]:
        # 只保留可安全重建页面上下文的动作；登录 secret 和远端副作用动作都不能重放。
        replay: list[BrowserAction] = []
        for step in steps:
            if step.get("result") != "success":
                continue
            action_raw = step.get("action") or {}
            try:
                action = BrowserAction(**action_raw)
            except TypeError:
                continue
            if self._is_secret_action(action):
                continue
            if action.risk_level in {"unsafe_mutation", "unknown_risk"} or action.requires_confirmation:
                continue
            if action.type in {"open_url", "click", "hover", "type", "select", "wait_for"}:
                replay.append(action)
        return replay

    def _completed_action_keys(self, steps: list[dict]) -> list[str]:
        # 只记录已经成功的 unsafe mutation key。恢复后 planner 用这些 key 跳过重复提交。
        keys: list[str] = []
        for step in steps:
            if step.get("result") != "success":
                continue
            action = step.get("action") or {}
            if action.get("risk_level") == "unsafe_mutation" and action.get("key"):
                keys.append(str(action["key"]))
        return keys

    def _is_secret_action(self, action: BrowserAction) -> bool:
        target = action.target_hint or ""
        return (
            action.type in {"type_username", "type_password"}
            or "password" in target.lower()
            or "密码" in target
        )

    def _observation_from_dict(self, raw: dict) -> BrowserObservation:
        elements = raw.get("interactive_elements") or []
        raw = dict(raw)
        raw["interactive_elements"] = [
            item if hasattr(item, "role") else InteractiveElement(**item)
            for item in elements
        ]
        return BrowserObservation(**raw)

    def _record(self, event_type: str, trace_id: str, task_id: str, session_id: str, step_index: int, details: dict) -> None:
        payload = {"session_id": session_id, "step_index": step_index, **details}
        self.audit_logger.record(
            AuditEvent(event_type=event_type, trace_id=trace_id, task_id=task_id, status=details.get("result"), details=payload)
        )

    def _blocked_result(self, reason: str, steps: list[dict], observation, artifacts: list[TaskArtifact]) -> ToolExecutionResult:
        report_path = self._write_execution_report(BrowserTaskSpec(start_url=None, user_goal=""), steps, observation, "blocked", reason)
        if report_path:
            artifacts.append(TaskArtifact(kind="execution_report", path=report_path))
        return ToolExecutionResult(
            success=False,
            error=reason,
            retryable=False,
            data={
                "status": "blocked",
                "last_observation": asdict(observation),
                "steps": steps,
                "canonical_action_trace": build_canonical_action_trace(steps, status="blocked"),
            },
            artifacts=artifacts,
        )

    def _confirmation_summary(self, action: BrowserAction, observation) -> dict:
        return {
            "current_page": observation.title or observation.url,
            "current_url": observation.url,
            "prepared_action": action.type,
            "target": action.target_hint or action.target_id,
            "value": "***" if self._is_secret_action(action) else action.value,
            "expected_outcome": action.expected_outcome,
            "risk_level": action.risk_level,
        }

    def _execution_summary(self, spec: BrowserTaskSpec, steps: list[dict]) -> str:
        return f"目标={spec.user_goal}; steps={len(steps)}; success_criteria={' | '.join(spec.success_criteria)}"

    def _compound_workflow_completion_error(
        self,
        spec: BrowserTaskSpec,
        observation: BrowserObservation,
        steps: list[dict],
    ) -> str | None:
        login_stages = re.findall(
            r"(?:使用|用)\s*[A-Za-z0-9][A-Za-z0-9_.@:-]*\s*(?:登录|登陆)",
            spec.user_goal,
            flags=re.IGNORECASE,
        )
        required_logins = max(len(spec.credential_refs), len(login_stages))
        successful_steps = [step for step in steps if step.get("result") == "success"]
        if required_logins >= 2:
            completed_logins = sum(
                1 for step in successful_steps if (step.get("action") or {}).get("type") == "login_submit"
            )
            if completed_logins < required_logins:
                return f"复合网页任务未完成：需要切换登录 {required_logins} 次，实际完成 {completed_logins} 次。"
            missing_inputs = [
                field
                for field, value in self._explicit_input_requests(spec.user_goal)
                if not self._explicit_input_already_done(field, value, steps)
            ]
            if missing_inputs:
                return "复合网页任务未完成：尚未填写字段 " + "、".join(dict.fromkeys(missing_inputs)) + "。"
        required_mutations = [label for label in ("保存", "复核") if label in spec.user_goal]
        completed_targets = [
            self._display_target(step.get("action") or {})
            for step in successful_steps
            if (step.get("action") or {}).get("type") == "click"
        ]
        missing_mutations = [
            label
            for label in required_mutations
            if not any(self._click_target_matches_label(target, label) for target in completed_targets)
        ]
        if missing_mutations:
            return "复合网页任务未完成：尚未执行 " + "、".join(missing_mutations) + "。"
        if "确认复核" in spec.user_goal:
            review_index = next(
                (
                    index
                    for index, step in enumerate(successful_steps)
                    if (step.get("action") or {}).get("type") == "click"
                    and self._click_target_matches_label(self._display_target(step.get("action") or {}), "复核")
                ),
                None,
            )
            confirmed = review_index is not None and any(
                (step.get("action") or {}).get("type") == "click"
                and (self._clean_click_label(self._display_target(step.get("action") or {})) or "") in {"确定", "确认", "是"}
                for step in successful_steps[review_index + 1 :]
            )
            if not confirmed:
                return "复合网页任务未完成：尚未确认复核。"
        login_url = str(spec.site_config.get("login_url") or "").rstrip("/")
        if required_logins >= 2 and login_url and observation.url.rstrip("/") == login_url:
            return "复合网页任务未完成：任务仍停留在登录页。"
        return None

    def _answer_from_observation(self, spec: BrowserTaskSpec, observation: BrowserObservation, steps: list[dict] | None = None) -> dict:
        membership_answer = self._membership_answer(spec.user_goal, asdict(observation), steps or [])
        if membership_answer:
            return membership_answer
        if self._membership_request(spec.user_goal) is not None:
            return {}
        assigned_role_answer = self._assigned_role_name_answer(spec.user_goal, observation.page_text)
        if assigned_role_answer:
            return assigned_role_answer
        contract = self._answer_contract(spec.user_goal)
        detail_answer = self.table_extractor.detail_answer(spec.user_goal, observation.page_text, contract=contract)
        if detail_answer:
            return detail_answer
        if contract and not self.table_extractor.is_broad_output_field(contract["output_field"]):
            matches = self._extract_column_matches(
                observation.page_text,
                query_field=contract["query_field"],
                query_value=contract["query_value"],
                output_field=contract["output_field"],
            )
            if matches:
                exact = [item for item in matches if item["query_value"] == contract["query_value"]]
                selected = exact or matches
                if len(selected) == 1:
                    item = selected[0]
                    answer = f"{item['query_value']} 对应的{contract['output_field']}是 {item['output_value']}。"
                else:
                    answer = "；".join(
                        f"{item['query_value']} 对应的{contract['output_field']}是 {item['output_value']}" for item in selected
                    ) + "。"
                if exact and len(matches) > len(exact):
                    fuzzy = [item for item in matches if item["query_value"] != contract["query_value"]]
                    answer += " 另外还匹配到：" + "；".join(
                        f"{item['query_value']} -> {item['output_value']}" for item in fuzzy
                    ) + "。"
                return {"answer": answer, "matches": matches, **contract}
        finish_answer = self._finish_action_answer(steps or [])
        if finish_answer:
            return {"answer": finish_answer}
        return {}

    def _membership_answer(self, goal: str, observation: dict, steps: list[dict]) -> dict | None:
        match = self._membership_request(goal)
        if not match:
            return None
        if not self._explicit_membership_requirements_satisfied(goal, steps):
            return None
        label = match.group("label")
        if not any(
            step.get("result") == "success"
            and (step.get("action") or {}).get("type") == "click"
            and self._click_target_matches_label(self._display_target(step.get("action") or {}), label)
            for step in steps
        ):
            return None
        page_text = str(observation.get("page_text") or "")
        aliases = self._click_label_aliases(label)
        section_starts = [page_text.lower().rfind(alias.lower()) for alias in aliases]
        section_start = max(section_starts, default=-1)
        if section_start < 0:
            return None
        section = page_text[section_start:]
        account_headers = list(re.finditer(r"(?:Account\s+No|账户(?:编号|号)|账号)", section, re.I))
        has_account_table = bool(account_headers)
        has_loaded_result = bool(
            re.search(
                r"(?:Displaying\s+\d+\s+to\s+\d+\s+of\s+\d+\s+items|"
                r"显示\s*\d+\s*到\s*\d+.*?\d+\s*记录|Page\s+of\s+\d+)",
                section,
                re.I,
            )
        )
        if not has_account_table or not has_loaded_result:
            return None
        value = match.group("value")
        table_section = section[account_headers[-1].start():]
        present = re.search(rf"(?<![0-9A-Za-z_.:-]){re.escape(value)}(?![0-9A-Za-z_.:-])", table_section) is not None
        answer = f"{value}{'在' if present else '不在'}该用户的已分配账户中。"
        return {"answer": answer, "target": value, "present": present, "list": "已分配账户"}

    def _membership_request(self, goal: str):
        return re.search(
            r"(?P<value>[A-Za-z0-9][A-Za-z0-9_.:-]*)\s*是否在(?:该用户的)?\s*"
            r"(?P<label>已分配账户|Assigned Account)(?:列表)?中",
            goal,
            re.I,
        )

    def _explicit_membership_requirements_satisfied(self, goal: str, steps: list[dict]) -> bool:
        labels = self._explicit_result_click_labels(goal)
        if labels and self._first_unmatched_click_label(labels, steps) is not None:
            return False
        requests = self._explicit_input_requests(goal)
        if any(not self._explicit_input_already_done(field, value, steps) for field, value in requests):
            return False
        if not requests:
            return True
        last_input_index = max(
            (
                index
                for index, step in enumerate(steps)
                if step.get("result") == "success"
                and (step.get("action") or {}).get("type") == "type"
                and any(
                    str((step.get("action") or {}).get("value") or "") == value
                    and self._field_label_matches_target(field, self._display_target(step.get("action") or {}))
                    for field, value in requests
                )
            ),
            default=-1,
        )
        if last_input_index < 0:
            return False
        expected_click = self._expected_click_after_last_type(goal, steps[last_input_index].get("action") or {})
        return not expected_click or self._click_done_after_index(expected_click, steps, last_input_index)

    def _assigned_role_name_answer(self, goal: str, page_text: str) -> dict | None:
        if "岗位名称" not in goal:
            return None
        assigned_match = re.search(r"(?:已分配岗位|Assigned position)", page_text, re.I)
        if not assigned_match:
            return None
        assigned_section = page_text[assigned_match.end() :]
        if re.search(r"显示\s*0\s*到\s*0\s*,?\s*共\s*0\s*记录|Displaying\s+0\s+to\s+0\s+of\s+0\s+items", assigned_section, re.I):
            return {"answer": "当前已分配岗位中没有岗位名称。", "role_names": []}
        role_header = re.search(r"(?:岗位名称|Assign duty Name|Duty Name)", assigned_section, re.I)
        if not role_header:
            return None
        role_section = assigned_section[role_header.end() :]
        role_section = re.split(
            r"(?:\b10\s+20\s+50\s+100\s+200\b|第\s*共\d+页|显示\d+到\d+|Page\s+of\s+\d+|Displaying\s+\d+\s+to\s+\d+)",
            role_section,
            flags=re.I,
        )[0]
        role_names = [
            token.strip()
            for token in re.split(r"\s+", role_section)
            if token.strip()
            and token.strip()
            not in {"岗位列表", "取消分配", "查询", "查询条件", "Duty", "List", "CancelAssign", "Assign", "duty", "Name"}
        ]
        if not role_names:
            return None
        return {"answer": "当前已分配岗位中的岗位名称：" + "、".join(role_names) + "。", "role_names": role_names}

    def _finish_action_answer(self, steps: list[dict]) -> str | None:
        for step in reversed(steps):
            action = step.get("action") or {}
            if action.get("type") == "finish" and action.get("value"):
                return str(action["value"])
        return None

    def _answer_contract(self, goal: str) -> dict[str, str] | None:
        fill_matches = list(re.finditer(r"(?:在|向)?\s*([^,，。；;\s]{2,20}?)(?:中|里)?(?:输入|填写|填入)\s*([^,，。；;\s]+)", goal))
        output_field = self._requested_output_field(goal)
        if not fill_matches or not output_field:
            return None
        fill = fill_matches[-1]
        query_field = fill.group(1).strip("'\"“”")
        query_value = fill.group(2).strip("'\"“”")
        return {"query_field": query_field, "query_value": query_value, "output_field": output_field}

    def _requested_output_field(self, goal: str) -> str | None:
        match = re.search(r"对应的\s*([^,，。；;\s]{2,20})", goal)
        if match:
            return match.group(1).strip("'\"“”")
        match = re.search(r"(?:告诉我|返回|输出)\s*([^,，。；;\s]{2,20})(?:$|[，。；,;])", goal)
        if match:
            return match.group(1).strip("'\"“”")
        return None

    def _requires_answer(self, goal: str) -> bool:
        return any(keyword in goal for keyword in ("告诉我", "返回", "输出", "当前", "是什么", "岗位名称"))

    def _extract_column_matches(self, page_text: str, *, query_field: str, query_value: str, output_field: str) -> list[dict]:
        return self.table_extractor.column_matches(
            page_text,
            query_field=query_field,
            query_value=query_value,
            output_field=output_field,
        )

    def _artifacts_from_observation(self, observation) -> list[TaskArtifact]:
        artifacts = []
        if observation.screenshot_path:
            artifacts.append(TaskArtifact(kind="screenshot", path=observation.screenshot_path))
        if observation.page_summary_path:
            artifacts.append(TaskArtifact(kind="page_summary", path=observation.page_summary_path))
        return artifacts

    def _save_session_state(self, tool: PlaywrightBrowserTool) -> str | None:
        save = getattr(tool, "save_session_state", None)
        if save is None:
            return None
        return save()

    def _default_session_state_path(self, session_id: str) -> str:
        return str(self.artifact_root / session_id / "browser-state.json")

    def _active_tool_key(self, session_id: str, task_id: str) -> str:
        return f"{session_id}:{task_id}"

    def _write_execution_report(
        self,
        spec: BrowserTaskSpec,
        steps: list[dict],
        observation: BrowserObservation,
        status: str,
        error: str | None,
    ) -> str | None:
        session_id = "default"
        task_id = "report"
        if observation.screenshot_path:
            path = Path(observation.screenshot_path)
            if len(path.parts) >= 3:
                task_id = path.parent.name
                session_id = path.parent.parent.name
        report_dir = self.artifact_root / session_id / task_id
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "execution-report.json"
        payload = {
            "status": status,
            "goal": spec.user_goal,
            "error": error,
            "step_count": len(steps),
            "last_observation": asdict(observation),
            "actions": [step.get("action", {}) for step in steps],
        }
        report_path.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(report_path)
