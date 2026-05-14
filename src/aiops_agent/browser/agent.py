from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

from aiops_agent.audit.models import AuditEvent
from aiops_agent.browser.credentials import CredentialError, CredentialStore
from aiops_agent.browser.models import BrowserAction, BrowserObservation, BrowserTaskSpec, InteractiveElement
from aiops_agent.browser.playwright_tool import PlaywrightBrowserTool
from aiops_agent.browser.planner import BrowserPlanner
from aiops_agent.browser.risk import RiskEvaluator
from aiops_agent.tasks.models import TaskArtifact, ToolExecutionResult
from aiops_agent.tools.base import BaseTool


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
    ):
        self.audit_logger = audit_logger
        self.artifact_root = Path(artifact_root)
        self.headless = headless
        self.credential_store = credential_store or CredentialStore()
        self.planner = planner or BrowserPlanner()
        self.risk_evaluator = risk_evaluator or RiskEvaluator()
        self._active_tools: dict[str, PlaywrightBrowserTool] = {}

    def execute(self, params: dict) -> ToolExecutionResult:
        spec = self._spec_from_params(params)
        session_id = str(params.get("session_id", "default"))
        task_id = str(params.get("task_id", ""))
        validation_error = self._validate_spec(spec, params)
        if validation_error:
            return ToolExecutionResult(
                success=False,
                error=validation_error,
                retryable=False,
                data={"status": "blocked", "goal": spec.user_goal, "site_key": spec.site_key, "workflow": spec.workflow},
            )
        spec.session_state_path = spec.session_state_path or self._default_session_state_path(session_id)
        try:
            self._attach_credentials(spec)
        except CredentialError as exc:
            return ToolExecutionResult(
                success=False,
                error=str(exc),
                retryable=False,
                data={"status": "failed", "goal": spec.user_goal, "credential_ref": spec.credential_ref},
            )
        trace_id = str(params.get("trace_id", ""))
        active_key = self._active_tool_key(session_id, task_id)
        tool = self._active_tools.get(active_key) if spec.confirmed_action else None
        live_resume = tool is not None
        if tool is None:
            tool = PlaywrightBrowserTool(
                session_id=session_id,
                task_id=task_id,
                artifact_root=self.artifact_root,
                headless=bool(params.get("headless", self.headless)),
                allowed_domains=spec.allowed_domains,
                session_state_path=spec.session_state_path,
                trace_enabled=spec.trace_enabled,
                video_enabled=spec.video_enabled,
                browser_channel=spec.browser_channel,
                slow_mo_ms=spec.browser_slow_mo_ms,
            )
        steps: list[dict] = list(params.get("prior_steps") or []) if live_resume else []
        artifacts: list[TaskArtifact] = []
        consecutive_failures = 0
        keep_browser_open = False

        self._record("browser.started", trace_id, task_id, session_id, 0, {"start_url": spec.start_url})
        try:
            for index in range(1, spec.max_steps + 1):
                action = self._next_action(spec, steps)
                step_result = self._execute_action(
                    tool=tool,
                    action=action,
                    trace_id=trace_id,
                    task_id=task_id,
                    session_id=session_id,
                    step_index=index,
                    steps=steps,
                    artifacts=artifacts,
                    spec=spec,
                )
                if isinstance(step_result, ToolExecutionResult):
                    if (step_result.data or {}).get("status") == "awaiting_confirmation":
                        self._active_tools[active_key] = tool
                        keep_browser_open = True
                    return step_result
                result, observation = step_result

                if observation.page_type == "verification":
                    artifacts.extend(self._artifacts_from_observation(tool.observe(last_action_result="verification blocked", force_artifact=True)))
                    return self._blocked_result("遇到验证码、MFA 或二次校验，需要人工接手。", steps, observation, artifacts)
                if spec.requires_login and action.type == "login_submit" and observation.page_type == "login":
                    if not self._login_has_failure_signal(observation) and self._login_still_pending(steps):
                        continue
                    failed_observation = tool.observe(last_action_result="login failed", force_artifact=True)
                    artifacts.extend(self._artifacts_from_observation(failed_observation))
                    return self._blocked_result(self._login_failure_reason(failed_observation), steps, failed_observation, artifacts)
                early_stop_reason = self._early_stop_reason(spec, result, observation, steps)
                if early_stop_reason:
                    stopped_observation = tool.observe(last_action_result="early stop", force_artifact=True)
                    artifacts.extend(self._artifacts_from_observation(stopped_observation))
                    return self._blocked_result(early_stop_reason, steps, stopped_observation, artifacts)
                if action.type == "finish" and result.status == "success":
                    break
                if result.status == "success":
                    consecutive_failures = 0
                    continue
                consecutive_failures += 1
                if consecutive_failures >= spec.max_consecutive_failures or result.status == "terminal_failure":
                    return self._blocked_result(result.error or "浏览器动作连续失败。", steps, observation, artifacts)

            else:
                last_observation = tool.observe(last_action_result="step budget exceeded", force_artifact=True)
                artifacts.extend(self._artifacts_from_observation(last_observation))
                return self._blocked_result("达到最大浏览器步骤预算。", steps, last_observation, artifacts)
            final_observation = tool.observe(last_action_result="task completed", force_artifact=True)
            artifacts.extend(self._artifacts_from_observation(final_observation))
            report_path = self._write_execution_report(spec, steps, final_observation, "completed", None)
            artifacts.append(TaskArtifact(kind="execution_report", path=report_path))
            answer = self._answer_from_observation(spec, final_observation, steps)
            if self._requires_answer(spec.user_goal) and not answer.get("answer"):
                return self._blocked_result("任务要求返回答案，但未能从当前页面提取到明确结果。", steps, final_observation, artifacts)
            self._record(
                "task.completed",
                trace_id,
                task_id,
                session_id,
                len(steps),
                {"current_url": final_observation.url, "result": "success"},
            )
            self._active_tools.pop(active_key, None)
            return ToolExecutionResult(
                success=True,
                data={
                    "status": "completed",
                    "goal": spec.user_goal,
                    "answer": answer,
                    "last_observation": asdict(final_observation),
                    "steps": steps,
                    "summary": self._execution_summary(spec, steps),
                    "session_state_path": self._save_session_state(tool),
                    "execution_report_path": report_path,
                },
                artifacts=artifacts,
            )
        except Exception as exc:
            self._active_tools.pop(active_key, None)
            return ToolExecutionResult(
                success=False,
                error=str(exc),
                retryable=False,
                data={"status": "failed", "goal": spec.user_goal, "steps": steps},
                artifacts=artifacts,
            )
        finally:
            if not keep_browser_open:
                tool.close()

    def _spec_from_params(self, params: dict) -> BrowserTaskSpec:
        actions = [BrowserAction(**item) for item in params.get("actions", [])]
        return BrowserTaskSpec(
            start_url=params.get("start_url"),
            user_goal=str(params.get("user_goal", "")),
            success_criteria=list(params.get("success_criteria", [])),
            forbidden_actions=list(params.get("forbidden_actions", [])),
            allowed_domains=list(params.get("allowed_domains", [])),
            credential_ref=params.get("credential_ref"),
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
        credential = self.credential_store.get(spec.credential_ref)
        if credential is None:
            raise CredentialError("登录任务缺少 credential_ref 或凭据配置")
        spec.credential_username = credential.username
        spec.credential_password = credential.password

    def _next_action(self, spec: BrowserTaskSpec, steps: list[dict]) -> BrowserAction:
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
        return (
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
        if self._is_repeated_action(action, steps, spec.repeated_action_threshold):
            observation = tool.observe(last_action_result="repeated action blocked", force_artifact=True)
            artifacts.extend(self._artifacts_from_observation(observation))
            return self._blocked_result("检测到同一页面重复动作超过阈值，已停止执行。", steps, observation, artifacts)
        proposed_action = self._stabilize_action(spec, action, steps)
        intent_aligned, intent_reason = self._action_intent_alignment(spec, proposed_action, steps)
        if not intent_aligned:
            observation = tool.observe(last_action_result="intent mismatch blocked", force_artifact=True)
            artifacts.extend(self._artifacts_from_observation(observation))
            return self._blocked_result(f"规划动作与用户意图不一致，已停止执行：{intent_reason}", steps, observation, artifacts)
        runtime_action = self._runtime_action(proposed_action)
        risk_level = self.risk_evaluator.classify(runtime_action)
        proposed_action.risk_level = risk_level
        runtime_action.risk_level = risk_level
        confirmed_execution = spec.confirmed_action is not None and proposed_action == spec.confirmed_action
        proposed_action.requires_confirmation = (
            False if confirmed_execution else proposed_action.requires_confirmation or self.risk_evaluator.requires_confirmation(runtime_action)
        )
        runtime_action.requires_confirmation = proposed_action.requires_confirmation
        self._record(
            "action.proposed",
            trace_id,
            task_id,
            session_id,
            step_index,
            {"action": self._safe_action_dict(proposed_action), "risk_level": risk_level},
        )
        if proposed_action.requires_confirmation:
            if step_index == 1 and not spec.start_url:
                observation = BrowserObservation(
                    title="未打开页面",
                    last_action_result="blocked for confirmation",
                    blocking_reason="缺少站点入口且动作可能产生远端副作用",
                )
            else:
                observation = tool.observe(last_action_result="blocked for confirmation", force_artifact=True)
            artifacts.extend(self._artifacts_from_observation(observation))
            event_type = "action.blocked_for_unknown_risk" if risk_level == "unknown_risk" else "action.blocked_for_confirmation"
            self._record(
                event_type,
                trace_id,
                task_id,
                session_id,
                step_index,
                {
                    "current_url": observation.url,
                    "action_type": proposed_action.type,
                    "risk_level": risk_level,
                    "summary": self._confirmation_summary(proposed_action, observation),
                },
            )
            state_path = self._save_session_state(tool)
            return ToolExecutionResult(
                success=False,
                error="浏览器动作需要人工确认，未执行可能产生远端副作用的操作。",
                retryable=False,
                data={
                    "status": "awaiting_confirmation",
                    "confirmation_summary": self._confirmation_summary(proposed_action, observation),
                    "pending_action": self._safe_action_dict(proposed_action),
                    "pending_action_raw": asdict(proposed_action),
                    "replay_actions": [asdict(action) for action in self._replay_actions(steps)],
                    "completed_action_keys": self._completed_action_keys(steps),
                    "resume_url": observation.url,
                    "session_state_path": state_path,
                    "last_observation": asdict(observation),
                    "steps": steps,
                },
                artifacts=artifacts,
            )

        result = tool.execute(runtime_action)
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
                "risk_level": risk_level,
                "result": result.status,
                "error": result.error,
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

    def _runtime_action(self, action: BrowserAction) -> BrowserAction:
        if action.type == "type_username":
            return BrowserAction(
                type="type",
                target_hint=action.target_hint or "__username__",
                target_id=action.target_id,
                value=action.value,
                expected_outcome=action.expected_outcome,
                risk_level=action.risk_level,
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
                risk_level=action.risk_level,
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key=action.key,
            )
        return action

    def _stabilize_action(self, spec: BrowserTaskSpec, action: BrowserAction, steps: list[dict]) -> BrowserAction:
        if not steps:
            return action
        if action.type == "type":
            return self._stabilize_type_action(spec, action, steps)
        if action.type != "click":
            return action
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
        expected_click = self._expected_click_after_last_type(spec.user_goal, steps[-1].get("action") or {})
        if not expected_click:
            return action
        if self._means_first_search_result(expected_click):
            previous_value = str((steps[-1].get("action") or {}).get("value") or "")
            return BrowserAction(
                type="click",
                target_hint=previous_value or "第一个",
                expected_outcome=f"按用户指令点击 {expected_click}",
                risk_level=action.risk_level,
                requires_confirmation=action.requires_confirmation,
                timeout_ms=action.timeout_ms,
                key=action.key or "stabilized.first_search_result",
            )
        current_target = f"{action.target_hint or ''} {action.target_id or ''}"
        if expected_click in current_target:
            return action
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        expected = self._find_element(observation, {expected_click})
        if expected is None:
            return action
        return BrowserAction(
            type="click",
            target_hint=expected.text or expected.name or expected_click,
            target_id=expected.element_id,
            expected_outcome=f"点击用户指定的后续按钮: {expected_click}",
            risk_level=action.risk_level,
            requires_confirmation=action.requires_confirmation,
            timeout_ms=action.timeout_ms,
            key=action.key or "stabilized.expected_click",
        )

    def _stabilize_type_action(self, spec: BrowserTaskSpec, action: BrowserAction, steps: list[dict]) -> BrowserAction:
        if action.value is None:
            return action
        expected_field = self._explicit_input_field_for_value(spec.user_goal, action.value)
        if not expected_field:
            return action
        current_target = f"{action.target_hint or ''} {action.target_id or ''}"
        if action.target_id and expected_field in current_target:
            return action
        if (action.target_hint or "").strip() == expected_field:
            return action
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        expected = self._find_exact_field_element(observation, expected_field)
        if expected is None:
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

    def _explicit_input_field_for_value(self, goal: str, value: str) -> str | None:
        normalized_value = value.strip("'\"“”")
        if not normalized_value or normalized_value not in goal:
            return None
        patterns = (
            r"(?:在|向)\s*([^,，。；;\s]{2,30}?)(?:中|里)?\s*(?:输入|填写|填入)\s*[\"“']?([^,，。；;\"”']+)",
            r"(?:在|向)\s*([^,，。；;\s]{2,30}?)(?:展开|打开|选择|点击).{0,80}?(?:输入|填写|填入)\s*[\"“']?([^,，。；;\"”']+)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, goal):
                field = match.group(1).strip("'\"“”")
                matched_value = match.group(2).strip("'\"“”")
                if matched_value == normalized_value:
                    return field
        return None

    def _find_exact_field_element(self, observation: BrowserObservation, label: str) -> InteractiveElement | None:
        candidates = [
            element for element in observation.interactive_elements
            if element.role in {"input", "select", "textarea"} and element.is_enabled and element.is_visible
        ]
        exact_attrs = ("name", "placeholder", "title", "text")
        for element in candidates:
            if any((getattr(element, attr) or "").strip() == label for attr in exact_attrs):
                return element
        for element in candidates:
            if any(label in (getattr(element, attr) or "") for attr in exact_attrs):
                return element
        return None

    def _should_select_first_result_row_before_assignment(
        self,
        spec: BrowserTaskSpec,
        action: BrowserAction,
        steps: list[dict],
    ) -> bool:
        if not re.search(r"选中.{0,30}(?:第一条|第一个|首个)", spec.user_goal):
            return False
        current_target = " ".join(
            part for part in (action.target_hint, action.target_id, action.expected_outcome) if part
        )
        if "分配岗位" not in current_target:
            return False
        if self._first_result_row_selection_already_done(steps):
            return False
        observation = self._observation_from_dict(steps[-1].get("observation") or {})
        return self._observation_has_query_result_row(observation)

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
        has_result_count = any(marker in text for marker in ("显示1到1", "共1记录", "共 1 记录"))
        has_user_table = "用户编号" in text and ("用户名" in text or "用户名称" in text or "登录名称" in text)
        has_data_row = re.search(r"U\d{4,}\s+\S+\s+\S+", text) is not None
        return has_user_table and (has_result_count or has_data_row)

    def _expected_click_after_last_type(self, goal: str, previous_action: dict) -> str | None:
        if previous_action.get("type") != "type":
            return None
        field = str(previous_action.get("target_hint") or "")
        value = str(previous_action.get("value") or "")
        if not field and not value:
            return None
        anchors = [anchor for anchor in (value, field) if anchor and anchor in goal]
        if not anchors:
            return None
        start = max(goal.find(anchor) + len(anchor) for anchor in anchors)
        tail = goal[start:]
        match = re.search(r"点击\s*([^,，。；;]+?)(?=$|[,，。；;])", tail)
        if not match:
            return None
        label = self._clean_click_label(match.group(1))
        return label or None

    def _clean_click_label(self, label: str) -> str:
        cleaned = label.strip("'\"“” ")
        cleaned = re.sub(r"^(?:下方的|上方的|弹出内容中的|弹窗中的|页面中的|列表中的)", "", cleaned)
        cleaned = re.sub(r"(?:按钮|链接|选项|标签|菜单项).*$", "", cleaned)
        return cleaned.strip("'\"“” ")

    def _means_first_search_result(self, label: str) -> bool:
        return any(token in label for token in ("第一个", "第一条", "首个")) and any(
            token in label for token in ("搜索", "结果", "公司", "数据")
        )

    def _find_element(self, observation: BrowserObservation, labels: set[str]) -> InteractiveElement | None:
        for element in observation.interactive_elements:
            text = " ".join(part for part in (element.name, element.text, element.title, element.context) if part)
            if any(label in text for label in labels) and element.is_enabled and element.is_visible:
                return element
        return None

    def _is_repeated_action(self, action: BrowserAction, steps: list[dict], threshold: int) -> bool:
        if threshold <= 0:
            return False
        signature = (action.type, action.target_hint, action.target_id, action.value)
        count = 0
        for step in reversed(steps):
            previous = step.get("action") or {}
            previous_signature = (
                previous.get("type"),
                previous.get("target_hint"),
                previous.get("target_id"),
                previous.get("value"),
            )
            if previous_signature != signature:
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
            expected_field = self._explicit_input_field_for_value(spec.user_goal, action.value)
            if expected_field and expected_field not in target:
                return False, f"用户要求在 {expected_field} 输入，但当前动作目标是 {target or '未知字段'}。"
            if expected_field:
                return True, f"输入动作目标与用户要求的字段 {expected_field} 一致。"
        if action.type == "click" and prior_steps:
            expected_click = self._expected_click_after_last_type(spec.user_goal, prior_steps[-1].get("action") or {})
            if expected_click:
                if expected_click in target or (
                    self._means_first_search_result(expected_click)
                    and (self._looks_like_company_name(target) or self._means_first_search_result(target))
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
            if self._looks_like_company_name(target):
                return f"系统中没有找到对应公司：{target}。"
            if self._is_navigation_target(spec.user_goal, target):
                return f"系统中没有找到对应菜单：{target}。"
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
            query = self._latest_business_query_summary(steps)
            if query:
                return f"系统中没有找到符合条件的信息：{query}。"
            if "选中" in spec.user_goal and "第一" in spec.user_goal:
                return "系统中没有找到查询后的第一条数据。"
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

    def _latest_business_query_summary(self, steps: list[dict]) -> str | None:
        for step in reversed(steps[:-1]):
            if step.get("result") != "success":
                continue
            action = step.get("action") or {}
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
        return "已分配岗位" in goal and "已分配岗位" in page_text and "岗位名称" in page_text

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
            if action.type in {"open_url", "click", "type", "select", "wait_for"}:
                replay.append(action)
        return replay

    def _completed_action_keys(self, steps: list[dict]) -> list[str]:
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
            data={"status": "blocked", "last_observation": asdict(observation), "steps": steps},
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

    def _answer_from_observation(self, spec: BrowserTaskSpec, observation: BrowserObservation, steps: list[dict] | None = None) -> dict:
        assigned_role_answer = self._assigned_role_name_answer(spec.user_goal, observation.page_text)
        if assigned_role_answer:
            return assigned_role_answer
        contract = self._answer_contract(spec.user_goal)
        if contract:
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

    def _assigned_role_name_answer(self, goal: str, page_text: str) -> dict | None:
        if "岗位名称" not in goal or "已分配岗位" not in page_text or "岗位名称" not in page_text:
            return None
        assigned_section = page_text.split("已分配岗位", 1)[-1]
        if re.search(r"显示\s*0\s*到\s*0\s*,?\s*共\s*0\s*记录", assigned_section):
            return {"answer": "当前已分配岗位中没有岗位名称。", "role_names": []}
        if "岗位名称" not in assigned_section:
            return None
        role_section = assigned_section.split("岗位名称", 1)[-1]
        role_section = re.split(r"(?:\b10\s+20\s+50\s+100\s+200\b|第\s*共\d+页|显示\d+到\d+)", role_section)[0]
        role_names = [
            token.strip()
            for token in re.split(r"\s+", role_section)
            if token.strip() and token.strip() not in {"岗位列表", "取消分配", "查询", "查询条件"}
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
        if query_field not in page_text or output_field not in page_text:
            return []
        matches: list[dict] = []
        pattern = re.compile(r"(U\d{5,})\s+([^\s]+)\s+([A-Za-z][A-Za-z0-9_.-]*)")
        for match in pattern.finditer(page_text):
            row_id, candidate_query, candidate_output = match.groups()
            if query_value in candidate_query:
                matches.append(
                    {
                        "row_id": row_id,
                        "query_field": query_field,
                        "query_value": candidate_query,
                        "output_field": output_field,
                        "output_value": candidate_output,
                    }
                )
        return matches

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
