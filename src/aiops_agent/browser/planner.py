from __future__ import annotations

import re
from urllib.parse import urljoin

from aiops_agent.browser.models import BrowserAction, BrowserObservation, BrowserTaskSpec


class BrowserPlanner:
    def next_action(
        self,
        spec: BrowserTaskSpec,
        observation: BrowserObservation | None,
        steps: list[dict],
    ) -> BrowserAction:
        successful_actions = [
            step.get("action", {}).get("type")
            for step in steps
            if step.get("result") == "success"
        ]
        if spec.workflow and spec.site_config:
            workflow_action = self._workflow_action(spec, observation, steps, successful_actions)
            if workflow_action is not None:
                return workflow_action

        if spec.start_url and "open_url" not in successful_actions:
            return BrowserAction(
                type="open_url",
                value=spec.start_url,
                expected_outcome="页面完成初始加载",
                risk_level="safe_read",
            )

        if spec.requires_remote_mutation and not spec.start_url:
            return self._remote_mutation_action(spec)

        if observation is not None and observation.page_type == "verification":
            return BrowserAction(
                type="finish",
                expected_outcome="检测到验证码、MFA 或二次校验，等待执行器阻断",
                risk_level="safe_read",
            )

        if "observe_page" not in successful_actions:
            return BrowserAction(
                type="observe_page",
                expected_outcome="获取压缩页面状态，供下一步规划使用",
                risk_level="safe_read",
            )

        login_action = self._login_action(spec, observation, successful_actions)
        if login_action is not None:
            return login_action

        if spec.requires_remote_mutation:
            return self._remote_mutation_action(spec)

        draft_action = self._local_draft_action(spec, observation, successful_actions)
        if draft_action is not None:
            return draft_action

        if "extract_text" not in successful_actions:
            return BrowserAction(
                type="extract_text",
                target_hint="main content",
                expected_outcome="提取当前页面关键可见信息",
                risk_level="safe_read",
            )

        if "save_artifact" not in successful_actions:
            return BrowserAction(
                type="save_artifact",
                expected_outcome="保存最终截图和页面摘要",
                risk_level="safe_read",
            )

        return BrowserAction(
            type="finish",
            expected_outcome="完成网页任务并记录最终状态",
            risk_level="safe_read",
        )

    def _login_action(
        self,
        spec: BrowserTaskSpec,
        observation: BrowserObservation | None,
        successful_actions: list[str | None],
    ) -> BrowserAction | None:
        if not spec.requires_login:
            return None
        if observation is None or observation.page_type != "login":
            return None
        if not spec.credential_username or not spec.credential_password:
            return BrowserAction(
                type="finish",
                expected_outcome="登录任务缺少可用凭据，等待执行器失败收敛",
                risk_level="safe_read",
            )
        if "type_username" not in successful_actions:
            return BrowserAction(
                type="type_username",
                target_hint=self._username_field(observation),
                value=spec.credential_username,
                expected_outcome="填写登录用户名",
                risk_level="safe_local_edit",
            )
        if "type_password" not in successful_actions:
            return BrowserAction(
                type="type_password",
                target_hint=self._password_field(observation),
                value=spec.credential_password,
                expected_outcome="填写登录密码",
                risk_level="safe_local_edit",
            )
        if "login_submit" not in successful_actions:
            return BrowserAction(
                type="login_submit",
                target_hint=self._login_button(observation),
                expected_outcome="提交登录表单",
                risk_level="safe_local_edit",
            )
        return None

    def _remote_mutation_action(self, spec: BrowserTaskSpec) -> BrowserAction:
        return BrowserAction(
            type="click",
            target_hint="提交/保存/确认",
            expected_outcome=f"执行用户请求的可能远端写入动作: {spec.user_goal}",
            risk_level="unsafe_mutation",
            requires_confirmation=True,
        )

    def _local_draft_action(
        self,
        spec: BrowserTaskSpec,
        observation: BrowserObservation | None,
        successful_actions: list[str | None],
    ) -> BrowserAction | None:
        if "type" in successful_actions:
            return None
        if observation is None or observation.page_type not in {"form", "login"}:
            return None
        value = self._extract_fill_value(spec.user_goal)
        if value is None:
            return None
        target_hint = self._first_text_field(observation)
        if target_hint is None:
            return None
        return BrowserAction(
            type="type",
            target_hint=target_hint,
            value=value,
            expected_outcome=f"在页面本地草稿字段中填写 {target_hint}",
            risk_level="safe_local_edit",
        )

    def _extract_fill_value(self, goal: str) -> str | None:
        patterns = (
            r"(?:填写|输入|填入)(?:用户名|用户|字段|名称)?(?:为|成|:|：)?\s*([A-Za-z0-9_.@-]{2,})",
            r"(?:type|fill|enter)\s+([A-Za-z0-9_.@-]{2,})",
        )
        for pattern in patterns:
            match = re.search(pattern, goal, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _first_text_field(self, observation: BrowserObservation) -> str | None:
        for element in observation.interactive_elements:
            if (
                element.role in {"input", "textarea"}
                and element.input_type.lower() != "password"
                and element.is_enabled
                and element.is_visible
            ):
                return element.name or element.text or element.element_id
        return None

    def _username_field(self, observation: BrowserObservation) -> str:
        return self._first_text_field(observation) or "__username__"

    def _password_field(self, observation: BrowserObservation) -> str:
        for element in observation.interactive_elements:
            if element.role == "input" and (
                element.input_type.lower() == "password" or "password" in element.name.lower() or "密码" in element.name
            ):
                return element.name or "__password__"
        return "__password__"

    def _login_button(self, observation: BrowserObservation) -> str:
        for element in observation.interactive_elements:
            text = " ".join((element.name, element.text)).lower()
            if element.role in {"button", "input"} and any(keyword in text for keyword in ("login", "sign in", "登录", "登陆")):
                return element.name or element.text or "登录"
        return "登录"

    def _workflow_action(
        self,
        spec: BrowserTaskSpec,
        observation: BrowserObservation | None,
        steps: list[dict],
        successful_actions: list[str | None],
    ) -> BrowserAction | None:
        if spec.start_url and "open_url" not in successful_actions:
            return BrowserAction(
                type="open_url",
                value=spec.start_url,
                expected_outcome="打开站点入口页面",
                risk_level="safe_read",
                key="site.open",
            )
        if "observe_page" not in successful_actions:
            return BrowserAction(
                type="observe_page",
                expected_outcome="获取页面状态",
                risk_level="safe_read",
                key="site.observe",
            )
        login_action = self._login_action(spec, observation, successful_actions)
        if login_action is not None:
            return login_action

        completed_keys = self._completed_keys(spec, steps)
        for action in self._workflow_actions(spec):
            if action.key and action.key in completed_keys:
                continue
            return action
        if "save_artifact" not in successful_actions:
            return BrowserAction(
                type="save_artifact",
                expected_outcome="保存最终截图和页面摘要",
                risk_level="safe_read",
                key="site.artifact",
            )
        return BrowserAction(type="finish", expected_outcome="完成账号/权限网页工作流", risk_level="safe_read", key="site.finish")

    def _workflow_actions(self, spec: BrowserTaskSpec) -> list[BrowserAction]:
        if spec.workflow == "create_user":
            return self._create_user_actions(spec)
        if spec.workflow == "assign_role":
            return self._assign_role_actions(spec)
        if spec.workflow == "create_user_and_assign_role":
            return self._create_user_actions(spec) + self._assign_role_actions(spec)
        return []

    def _create_user_actions(self, spec: BrowserTaskSpec) -> list[BrowserAction]:
        workflow = self._workflow_config(spec, "create_user")
        actions: list[BrowserAction] = []
        entry_url = self._workflow_url(spec, workflow)
        if entry_url:
            actions.append(BrowserAction(type="open_url", value=entry_url, expected_outcome="进入用户管理页面", risk_level="safe_read", key="create_user.open"))
        open_button = workflow.get("open_button")
        if open_button:
            actions.append(BrowserAction(type="click", target_hint=str(open_button), expected_outcome="打开用户表单", risk_level="safe_local_edit", key="create_user.open_form"))
        for field_key, target_hint in (workflow.get("fields") or {}).items():
            value = spec.workflow_fields.get(field_key)
            if value is None:
                continue
            actions.append(
                BrowserAction(
                    type="type",
                    target_hint=str(target_hint),
                    value=str(value),
                    expected_outcome=f"填写用户字段 {field_key}",
                    risk_level="safe_local_edit",
                    key=f"create_user.field.{field_key}",
                )
            )
        actions.append(
            BrowserAction(
                type="click",
                target_hint=str(workflow.get("submit_button", "保存")),
                expected_outcome="提交创建用户表单",
                risk_level="unsafe_mutation",
                requires_confirmation=True,
                key="create_user.submit",
            )
        )
        return actions

    def _assign_role_actions(self, spec: BrowserTaskSpec) -> list[BrowserAction]:
        workflow = self._workflow_config(spec, "assign_role")
        actions: list[BrowserAction] = []
        entry_url = self._workflow_url(spec, workflow)
        if entry_url:
            actions.append(BrowserAction(type="open_url", value=entry_url, expected_outcome="进入用户权限页面", risk_level="safe_read", key="assign_role.open"))
        open_button = workflow.get("open_button")
        if open_button:
            actions.append(BrowserAction(type="click", target_hint=str(open_button), expected_outcome="打开角色分配表单", risk_level="safe_local_edit", key="assign_role.open_form"))
        for field_key, target_hint in (workflow.get("fields") or {}).items():
            value = spec.workflow_fields.get(field_key)
            if value is None:
                continue
            action_type = "select" if field_key == "role" else "type"
            actions.append(
                BrowserAction(
                    type=action_type,
                    target_hint=str(target_hint),
                    value=str(value),
                    expected_outcome=f"填写权限字段 {field_key}",
                    risk_level="safe_local_edit",
                    key=f"assign_role.field.{field_key}",
                )
            )
        actions.append(
            BrowserAction(
                type="click",
                target_hint=str(workflow.get("submit_button", "保存")),
                expected_outcome="提交角色/权限分配",
                risk_level="unsafe_mutation",
                requires_confirmation=True,
                key="assign_role.submit",
            )
        )
        return actions

    def _workflow_config(self, spec: BrowserTaskSpec, workflow: str) -> dict:
        workflows = spec.site_config.get("workflows") or {}
        return dict(workflows.get(workflow) or {})

    def _workflow_url(self, spec: BrowserTaskSpec, workflow: dict) -> str | None:
        url = workflow.get("entry_url")
        if not url:
            return None
        formatted = str(url).format(**spec.workflow_fields)
        base_url = str(spec.site_config.get("base_url") or "")
        return urljoin(base_url.rstrip("/") + "/", formatted)

    def _completed_keys(self, spec: BrowserTaskSpec, steps: list[dict]) -> set[str]:
        keys = set(spec.completed_action_keys)
        for step in steps:
            if step.get("result") != "success":
                continue
            action = step.get("action") or {}
            key = action.get("key")
            if key:
                keys.add(key)
        return keys
