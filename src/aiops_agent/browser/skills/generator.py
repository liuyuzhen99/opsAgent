from __future__ import annotations

import re
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from aiops_agent.browser.skills.models import WebSkillGenerationError, WebSkillSaveResult
from aiops_agent.browser.skills.store import WebSkillStore
from aiops_agent.browser.skills.validator import validate_description, validate_skill_name
from aiops_agent.tasks.models import Task


ACTION_FIELDS = {
    "type",
    "target_hint",
    "target_id",
    "value",
    "expected_outcome",
    "risk_level",
    "requires_confirmation",
    "timeout_ms",
    "key",
}
SECRET_ACTION_TYPES = {"type_username", "type_password", "login_submit"}
SUPPORTED_ACTION_TYPES = {"open_url", "click", "type", "select", "press", "wait_for", "extract_text", "save_artifact", "finish"}


class WebSkillGenerator:
    def __init__(self, store: WebSkillStore | None = None):
        self.store = store or WebSkillStore()

    def generate_from_task(self, task: Task, name: str | None = None) -> WebSkillSaveResult:
        data = (task.result or {}).get("data") or {}
        steps = data.get("steps") or []
        if task.intent != "web_action":
            raise WebSkillGenerationError("最近一次成功任务不是 web_action。")
        if task.status != "success" or data.get("status") != "completed":
            raise WebSkillGenerationError("最近一次 web_action 未成功完成，不能沉淀 skill。")
        if not isinstance(steps, list) or not steps:
            raise WebSkillGenerationError("成功任务缺少 result.data.steps，不能沉淀 skill。")
        self._validate_reflections(steps)
        self._validate_answer_contract(task, data)

        params = self._tool_params(task)
        site_key = str(params.get("site_key") or task.entities.get("site_key") or "")
        skill_name = validate_skill_name(name.strip() if name else self._generate_name(task, steps, site_key))
        workflow_actions, inputs = self._workflow_actions(task, steps)
        if not any(action.get("type") == "finish" for action in workflow_actions):
            workflow_actions.append(
                {
                    "type": "finish",
                    "expected_outcome": "完成 skill 固定动作流",
                    "risk_level": "safe_read",
                    "key": "skill.finish",
                }
            )
        keywords = self._keywords(task, workflow_actions)
        description = validate_description(self._description(task, site_key, keywords))
        requires_login = bool(params.get("requires_login"))

        frontmatter = {
            "name": skill_name,
            "description": description,
            "compatibility": ["opsAgent web_action", "Playwright browser_agent"],
            "metadata": {
                "opsagent_site_key": site_key,
                "opsagent_source_task_id": str(task.id),
                "opsagent_skill_version": "1",
                "opsagent_created_by": "opsAgent",
                "opsagent_created_at": datetime.now(UTC).isoformat(),
            },
        }
        workflow = {
            "schema_version": "opsagent.web_skill.workflow.v1",
            "skill_name": skill_name,
            "site_key": site_key,
            "source_task_id": str(task.id),
            "inputs": [
                {
                    "name": input_name,
                    "required": True,
                    "source": "user_goal",
                    "examples": [],
                }
                for input_name in inputs
            ],
            "match": {
                "keywords": keywords,
                "fields": self._fields_from_actions(workflow_actions),
                "answer_types": self._answer_types(task.input),
            },
            "execution": {
                "auto_plan": False,
                "requires_login": requires_login,
                "fallback_to_llm_once": True,
            },
            "actions": workflow_actions,
            "validation": {
                "success_signals": ["task completed"],
                "business_stop_rules": ["missing_menu", "missing_option", "empty_result"],
                "requires_answer": self._requires_answer(task.input),
            },
        }
        body = self._skill_body(inputs)
        notes = self._notes(task, site_key, workflow_actions, inputs)
        path = self.store.write(name=skill_name, frontmatter=frontmatter, body=body, workflow=workflow, notes=notes)
        return WebSkillSaveResult(
            name=skill_name,
            path=path,
            inputs=list(inputs.keys()),
            action_count=len(workflow_actions),
            matched_keywords=keywords,
        )

    def _validate_reflections(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            reflection = step.get("reflection") or {}
            if reflection.get("terminal") or reflection.get("next_decision") == "stop":
                raise WebSkillGenerationError("成功路径中包含终止型 reflection，拒绝沉淀为 skill。")

    def _validate_answer_contract(self, task: Task, data: dict[str, Any]) -> None:
        if not self._requires_answer(task.input):
            return
        answer = data.get("answer") or {}
        if not isinstance(answer, dict) or not answer.get("answer"):
            raise WebSkillGenerationError("该任务要求返回答案，但成功结果中没有明确 answer，拒绝沉淀。")

    def _tool_params(self, task: Task) -> dict[str, Any]:
        if not task.tool_calls:
            return {}
        return dict(task.tool_calls[0].params or {})

    def _workflow_actions(self, task: Task, steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], OrderedDict[str, OrderedDict[str, None]]]:
        actions: list[dict[str, Any]] = []
        inputs: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()
        for step in steps:
            if step.get("result") != "success":
                continue
            raw_action = dict(step.get("action") or {})
            action_type = str(raw_action.get("type") or "")
            if action_type in SECRET_ACTION_TYPES or action_type not in SUPPORTED_ACTION_TYPES:
                continue
            action = {key: raw_action[key] for key in ACTION_FIELDS if key in raw_action and raw_action[key] not in (None, "")}
            if action_type in {"type", "select", "press"} and action.get("value"):
                param_name = self._param_name_for_action(action, task.input, inputs)
                value = str(action["value"])
                action["value"] = "{{" + param_name + "}}"
                inputs.setdefault(param_name, OrderedDict())[value] = None
            if action_type == "open_url" and action.get("value"):
                action["value"] = str(action["value"])
            if action:
                action.setdefault("key", f"skill.step.{len(actions) + 1}")
                actions.append(action)
        if not actions:
            raise WebSkillGenerationError("成功任务中没有可复用的非登录浏览器动作。")
        return actions, inputs

    def _param_name_for_action(
        self,
        action: dict[str, Any],
        goal: str,
        existing: OrderedDict[str, OrderedDict[str, None]],
    ) -> str:
        value = str(action.get("value") or "")
        target = " ".join(str(action.get(key) or "") for key in ("target_hint", "target_id", "expected_outcome"))
        if "@" in value:
            return "email"
        candidates = (
            ("username", ("用户名", "登录名", "登录名称", "账号", "用户", "user", "username")),
            ("company_name", ("公司", "授权单位", "客户名称", "客户", "企业")),
            ("role", ("角色", "权限", "岗位", "role", "permission")),
            ("department", ("部门", "department")),
            ("display_name", ("姓名", "显示名", "display")),
        )
        lowered = target.lower()
        for param_name, markers in candidates:
            if any(marker.lower() in lowered for marker in markers):
                return param_name
        if value and value in goal:
            return "input_value" if "input_value" not in existing else f"input_value_{len(existing) + 1}"
        return f"input_value_{len(existing) + 1}"

    def _generate_name(self, task: Task, steps: list[dict[str, Any]], site_key: str) -> str:
        key_text = " ".join(
            str((step.get("action") or {}).get("key") or (step.get("action") or {}).get("target_hint") or "")
            for step in steps
        ).lower()
        parts = [site_key or "web"]
        if "search_user" in key_text or "查询用户" in task.input:
            parts.extend(["search", "user"])
        elif "create_user" in key_text or "创建用户" in task.input or "创建账号" in task.input:
            parts.extend(["create", "user"])
        elif "assign_role" in key_text or "分配" in task.input:
            parts.extend(["assign", "role"])
        else:
            parts.append("skill")
        return self._slug("-".join(parts))

    def _slug(self, text: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", text.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
        return (slug or "web-skill")[:64].strip("-") or "web-skill"

    def _description(self, task: Task, site_key: str, keywords: list[str]) -> str:
        keyword_text = ", ".join(keywords[:6]) or "web actions"
        site_text = f" on site {site_key}" if site_key else ""
        return (
            f"Execute a reusable opsAgent web_action workflow{site_text} when the user asks for similar browser "
            f"operations involving {keyword_text}."
        )

    def _keywords(self, task: Task, actions: list[dict[str, Any]]) -> list[str]:
        keywords: OrderedDict[str, None] = OrderedDict()
        for action in actions:
            for key in ("target_hint", "expected_outcome"):
                value = str(action.get(key) or "").strip()
                if value and not value.startswith("{{") and len(value) <= 40:
                    keywords[value] = None
        for marker in ("查询用户", "创建用户", "分配角色", "分配权限", "岗位名称", "用户管理"):
            if marker in task.input:
                keywords[marker] = None
        return list(keywords.keys())[:16]

    def _fields_from_actions(self, actions: list[dict[str, Any]]) -> list[str]:
        fields: OrderedDict[str, None] = OrderedDict()
        for action in actions:
            if action.get("type") in {"type", "select"}:
                field = str(action.get("target_hint") or "").strip()
                if field:
                    fields[field] = None
        return list(fields.keys())

    def _answer_types(self, goal: str) -> list[str]:
        if "岗位名称" in goal or "角色" in goal or "权限" in goal:
            return ["role_names"]
        if self._requires_answer(goal):
            return ["text_answer"]
        return []

    def _requires_answer(self, goal: str) -> bool:
        return any(keyword in goal for keyword in ("告诉我", "返回", "输出", "当前", "是什么", "岗位名称"))

    def _skill_body(self, inputs: OrderedDict[str, OrderedDict[str, None]]) -> str:
        input_lines = "\n".join(f"- `{name}`: required task parameter." for name in inputs) or "- No user-provided inputs."
        return f"""## When to use this skill
Use this skill for browser automation tasks that match the site, menu, fields, and answer shape captured in the workflow.

## Inputs
{input_lines}

## Workflow
Machine-executable workflow: [assets/workflow.json](assets/workflow.json)

Implementation notes: [references/notes.md](references/notes.md)

## Validation and early stop rules
Stop early when required menus, fields, options, or query results are missing. Fall back to the LLM planner once only for non-business locator failures.

## Expected output
Return the extracted answer for read-only query tasks, or the final browser task status for write tasks.
"""

    def _notes(
        self,
        task: Task,
        site_key: str,
        actions: list[dict[str, Any]],
        inputs: OrderedDict[str, OrderedDict[str, None]],
    ) -> str:
        action_lines = "\n".join(
            f"- {action.get('key', '-')}: {action.get('type')} -> {action.get('target_hint') or action.get('value') or '-'}"
            for action in actions
        )
        input_lines = "\n".join(f"- `{name}` replaces user-provided values captured from the source task." for name in inputs)
        return f"""# Web Skill Notes

## Source
- Source task ID: `{task.id}`
- Site key: `{site_key or '-'}`

## Parameters
{input_lines or "- No parameterized user input was captured."}

## Page Structure
The workflow is based on stable menus, buttons, fields, tabs, and extraction steps observed in the successful source run. Real page text, screenshots, cookies, tokens, and credentials are intentionally omitted.

## Actions
{action_lines}

## Failure Handling
- Missing menu, missing field, missing option, and empty query result are business stop conditions.
- Locator or transient execution failures may fall back to the existing LLM planner once.

## Answer Extraction
Use the existing browser agent answer extraction from the final observation. Query-like tasks require a non-empty answer; write-like tasks require a verifiable final browser status.
"""
