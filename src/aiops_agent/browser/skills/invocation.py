from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from aiops_agent.browser.site_config import BrowserSiteConfigError, BrowserSitesConfig
from aiops_agent.browser.skills.matcher import WebSkillMatcher
from aiops_agent.browser.skills.models import WebSkillMatch, WebSkillValidationError
from aiops_agent.tasks.models import ExecutionPlan, ToolCallSpec


@dataclass(slots=True)
class WebSkillInvocation:
    skill_name: str
    task_input: str
    raw_parameters: dict[str, str]
    skill_parameters: dict[str, str]
    credential_ref: str | None
    site_key: str
    entities: dict[str, Any]
    match: WebSkillMatch
    plan: ExecutionPlan
    call_spec: ToolCallSpec
    risk_level: str


class WebSkillInvocationService:
    def __init__(
        self,
        matcher: WebSkillMatcher,
        *,
        browser_sites_config: BrowserSitesConfig | None = None,
        credential_ref_resolver: Callable[[str | None], str | None] | None = None,
        credential_user_resolver: Callable[[str | None], str | None] | None = None,
        credential_ref_for_site_user: Callable[[str | None, str | None], str | None] | None = None,
        credential_site_resolver: Callable[[str | None], str | None] | None = None,
    ):
        self.matcher = matcher
        self.browser_sites_config = browser_sites_config or BrowserSitesConfig()
        self.credential_ref_resolver = credential_ref_resolver
        self.credential_user_resolver = credential_user_resolver
        self.credential_ref_for_site_user = credential_ref_for_site_user
        self.credential_site_resolver = credential_site_resolver

    def list_skills(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for skill in self.matcher.store.list_skills():
            workflow = skill.workflow
            inputs = [
                {
                    "name": str(item.get("name") or ""),
                    "required": bool(item.get("required", True)),
                    "type": str(item.get("type") or "text"),
                    "examples": [str(example) for example in (item.get("examples") or [])],
                }
                for item in workflow.get("inputs") or []
                if isinstance(item, dict) and item.get("name")
            ]
            site_key = str(workflow.get("site_key") or "")
            execution = dict(workflow.get("execution") or {})
            requires_login = bool(execution.get("requires_login", False))
            credential_refs = self._credential_refs(execution.get("credential_refs"))
            runtime_inputs = self._runtime_inputs(
                site_key=site_key,
                requires_login=requires_login,
                credential_refs=credential_refs,
            )
            summaries.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "site_key": site_key,
                    "inputs": inputs,
                    "runtime_inputs": runtime_inputs,
                    "execution": execution,
                }
            )
        return summaries

    def prepare_invocation(
        self,
        skill_name: str,
        parameters: dict[str, str] | None,
        *,
        max_steps: int = 20,
        allowed_domains: list[str] | None = None,
        credential_ref: str | None = None,
        browser_trace: bool = False,
        browser_video: bool = False,
        browser_site: str | None = None,
        browser_channel: str | None = None,
        browser_slow_mo_ms: int = 0,
    ) -> WebSkillInvocation:
        skill = self.matcher.store.load(skill_name)
        raw_parameters = {str(key): str(value) for key, value in (parameters or {}).items()}
        special_keys = {"credential_ref", "credential_refs", "site_key", "user"}
        skill_parameters = {key: value for key, value in raw_parameters.items() if key not in special_keys}
        execution = dict(skill.workflow.get("execution") or {})
        workflow_credential_refs = self._credential_refs(execution.get("credential_refs"))
        runtime_credential_refs = (
            self._credential_refs(raw_parameters.get("credential_refs"))
            or list(workflow_credential_refs)
        )
        runtime_credential_ref = (
            raw_parameters.get("credential_ref")
            or credential_ref
            or (runtime_credential_refs[0] if runtime_credential_refs else None)
        )
        if runtime_credential_ref:
            if runtime_credential_refs:
                runtime_credential_refs[0] = runtime_credential_ref
            else:
                runtime_credential_refs = [runtime_credential_ref]
        runtime_user = raw_parameters.get("user")
        workflow_requires_login = bool(execution.get("requires_login", False))
        site_key = (
            raw_parameters.get("site_key")
            or browser_site
            or str(skill.workflow.get("site_key") or "")
            or self._site_key_for_credential(runtime_credential_ref)
            or ""
        )
        if not runtime_credential_ref and workflow_requires_login:
            runtime_user = runtime_user or self._default_credential_user(site_key)
            runtime_credential_ref = self._credential_ref_for_site_user(site_key, runtime_user)
            if not runtime_credential_ref and not runtime_user:
                runtime_credential_ref = self._default_credential_ref(site_key)
            if runtime_credential_ref and not runtime_credential_refs:
                runtime_credential_refs = [runtime_credential_ref]
        entities = self._build_entities(
            skill.workflow,
            skill_parameters,
            site_key=site_key,
            start_url=raw_parameters.get("start_url"),
            credential_ref=runtime_credential_ref,
            credential_refs=runtime_credential_refs,
            credential_user=runtime_user,
            allowed_domains=allowed_domains or [],
        )
        if entities.get("requires_login") and not runtime_credential_ref:
            if runtime_user:
                raise WebSkillValidationError(
                    f"credential user not found: site_key={site_key or '-'} user={runtime_user}. "
                    "请检查 configs/credentials.local.json 的 sites.<site_key>.users 配置。"
                )
            missing = "site_key, user" if not site_key else "user"
            raise WebSkillValidationError(
                f"missing required runtime parameters: {missing} "
                f"(skill {skill_name} requires login). "
                f"用法: /skill {skill_name} site_key={site_key or '<site-key>'} user=<user>"
            )
        match = self.matcher.match_by_name(skill_name, skill_parameters, entities)
        risk_level = self._risk_level(match)
        task_input = self._task_input(skill_name, raw_parameters)
        browser_goal = self.matcher.renderer.render_goal(skill.workflow, skill_parameters) or task_input
        plan = ExecutionPlan(
            goal=f"执行 web skill: {skill_name}",
            steps=[
                "加载指定 web skill",
                "渲染固定浏览器动作",
                "执行浏览器动作并保留确认门",
            ],
            selected_tools=["browser_agent"],
            risk_level=risk_level,
            confirmation_required=False,
            success_criteria=["按 skill workflow 完成浏览器动作", "保存关键 artifact 与审计记录"],
        )
        call_spec = ToolCallSpec(
            tool_name="browser_agent",
            action="run_browser_task",
            params={
                "start_url": entities.get("start_url"),
                "user_goal": browser_goal,
                "success_criteria": plan.success_criteria,
                "forbidden_actions": ["绕过验证码/MFA", "访问非允许域名", "未确认执行远端写入"],
                "allowed_domains": entities.get("allowed_domains") or [],
                "credential_ref": runtime_credential_ref,
                "credential_refs": runtime_credential_refs,
                "credential_user": runtime_user,
                "requires_login": bool(
                    entities.get("requires_login")
                    or (skill.workflow.get("execution") or {}).get("requires_login", False)
                ),
                "requires_remote_mutation": risk_level == "unsafe_mutation",
                "auto_plan": False,
                "session_state_path": None,
                "site_key": entities.get("site_key"),
                "workflow": None,
                "workflow_fields": skill_parameters,
                "site_config": entities.get("site_config") or {},
                "browser_channel": browser_channel,
                "browser_slow_mo_ms": int(browser_slow_mo_ms),
                "trace_enabled": bool(browser_trace),
                "video_enabled": bool(browser_video),
                "max_steps": int(max_steps),
                "actions": [asdict(action) for action in match.actions],
                "skill_name": skill_name,
                "skill_score": 1.0,
                "skill_parameters": skill_parameters,
                "skill_matched_keywords": match.matched_keywords,
                "skill_fallback_to_llm_once": bool(
                    (skill.workflow.get("execution") or {}).get("fallback_to_llm_once", True)
                ),
                "entities": entities,
            },
            risk_level=risk_level,
        )
        return WebSkillInvocation(
            skill_name=skill_name,
            task_input=task_input,
            raw_parameters=raw_parameters,
            skill_parameters=skill_parameters,
            credential_ref=runtime_credential_ref,
            site_key=site_key,
            entities=entities,
            match=match,
            plan=plan,
            call_spec=call_spec,
            risk_level=risk_level,
        )

    def _build_entities(
        self,
        workflow: dict[str, Any],
        skill_parameters: dict[str, str],
        *,
        site_key: str,
        start_url: str | None,
        credential_ref: str | None,
        credential_refs: list[str],
        credential_user: str | None,
        allowed_domains: list[str],
    ) -> dict[str, Any]:
        entities: dict[str, Any] = {
            "site_key": site_key,
            "workflow_fields": skill_parameters,
            "credential_ref": credential_ref,
            "credential_refs": list(credential_refs),
            "credential_user": credential_user,
            "allowed_domains": list(allowed_domains),
            "start_url": start_url,
            "requires_login": bool((workflow.get("execution") or {}).get("requires_login", False)),
        }
        if site_key:
            try:
                site = self.browser_sites_config.get(site_key)
            except BrowserSiteConfigError:
                site = None
            if site is not None:
                entities["site_config"] = site.to_runtime_dict()
                entities["start_url"] = start_url or site.login_url or site.base_url
                entities["allowed_domains"] = sorted(set(list(allowed_domains) + site.allowed_domains))
                entities["requires_login"] = bool(entities["requires_login"] or site.login_url or site.login_fields)
        return entities

    def _site_key_for_credential(self, credential_ref: str | None) -> str | None:
        if self.credential_site_resolver is None:
            return None
        return self.credential_site_resolver(credential_ref)

    def _default_credential_ref(self, site_key: str | None) -> str | None:
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

    def _runtime_inputs(
        self,
        *,
        site_key: str,
        requires_login: bool,
        credential_refs: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not requires_login:
            return []
        runtime_inputs: list[dict[str, Any]] = []
        if not site_key:
            runtime_inputs.append(
                {
                    "name": "site_key",
                    "required": True,
                    "type": "site_key",
                    "description": "站点标识，对应 credentials.local.json 的 sites.<site_key>。",
                    "examples": [],
                }
            )
        else:
            runtime_inputs.append(
                {
                    "name": "site_key",
                    "required": False,
                    "type": "site_key",
                    "description": "站点标识，对应 credentials.local.json 的 sites.<site_key>。",
                    "examples": [site_key],
                    "default": site_key,
                }
            )
        if credential_refs:
            joined_refs = ",".join(credential_refs)
            runtime_inputs.append(
                {
                    "name": "credential_refs",
                    "required": False,
                    "type": "credential_refs",
                    "description": "按登录阶段顺序使用的凭据引用，逗号分隔。",
                    "examples": [joined_refs],
                    "default": joined_refs,
                }
            )
            return runtime_inputs
        default_user = self._default_credential_user(site_key)
        user_input: dict[str, Any] = {
            "name": "user",
            "required": default_user is None,
            "type": "credential_user",
            "description": "站点下的登录用户，对应 credentials.local.json 的 sites.<site_key>.users.<user>。",
            "examples": [default_user] if default_user else [],
        }
        if default_user:
            user_input["default"] = default_user
        runtime_inputs.append(user_input)
        return runtime_inputs

    def _credential_refs(self, value: Any) -> list[str]:
        if isinstance(value, str):
            raw_values = value.split(",")
        elif isinstance(value, (list, tuple)):
            raw_values = value
        else:
            raw_values = []
        return [str(item).strip() for item in raw_values if str(item).strip()]

    def _risk_level(self, match: WebSkillMatch) -> str:
        if any(action.risk_level in {"unsafe_mutation", "unknown_risk"} for action in match.actions):
            return "unsafe_mutation"
        return "safe_read"

    def _task_input(self, skill_name: str, parameters: dict[str, str]) -> str:
        args = " ".join(f"{key}={value}" for key, value in sorted(parameters.items()))
        return f"/skill {skill_name}" + (f" {args}" if args else "")
