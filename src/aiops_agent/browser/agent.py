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
        steps: list[dict] = []
        artifacts: list[TaskArtifact] = []
        consecutive_failures = 0

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
                    return self._blocked_result("登录失败或仍停留在登录页。", steps, failed_observation, artifacts)
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
            self._record(
                "task.completed",
                trace_id,
                task_id,
                session_id,
                len(steps),
                {"current_url": final_observation.url, "result": "success"},
            )
            return ToolExecutionResult(
                success=True,
                data={
                    "status": "completed",
                    "goal": spec.user_goal,
                    "answer": self._answer_from_observation(spec, final_observation, steps),
                    "last_observation": asdict(final_observation),
                    "steps": steps,
                    "summary": self._execution_summary(spec, steps),
                    "session_state_path": self._save_session_state(tool),
                    "execution_report_path": report_path,
                },
                artifacts=artifacts,
            )
        except Exception as exc:
            return ToolExecutionResult(
                success=False,
                error=str(exc),
                retryable=False,
                data={"status": "failed", "goal": spec.user_goal, "steps": steps},
                artifacts=artifacts,
            )
        finally:
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
        if spec.confirmed_action and len(steps) == len(spec.replay_actions):
            return spec.confirmed_action
        if not spec.auto_plan and len(steps) < len(spec.actions):
            return spec.actions[len(steps)]
        observation = None
        if steps:
            observation_raw = steps[-1].get("observation") or {}
            if isinstance(observation_raw, dict):
                observation = self._observation_from_dict(observation_raw)
        return self.planner.next_action(spec, observation, steps)

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
        steps.append(
            {
                "step_index": step_index,
                "action": self._safe_action_dict(proposed_action),
                "result": result.status,
                "observation": asdict(observation),
                "error": result.error,
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
        if not steps or action.type != "click":
            return action
        expected_click = self._expected_click_after_last_type(spec.user_goal, steps[-1].get("action") or {})
        if not expected_click:
            return action
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
        match = re.search(r"(?:然后|再|并|之后|随后)?[^,，。；;]{0,12}?点击\s*([^,，。；;\s]+?)(?:按钮|$)", tail)
        if not match:
            return None
        label = match.group(1).strip("'\"“”")
        return label or None

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
