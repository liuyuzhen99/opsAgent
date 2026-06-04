from __future__ import annotations

import re
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from aiops_agent.browser.action_trace import legacy_steps_from_canonical_trace
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
DATE_VALUE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")

PARAMETER_ALIASES = {
    "start_date": ["开始日期", "起始日期", "开始时间", "起始时间", "from", "start", "start_date"],
    "end_date": ["结束日期", "截止日期", "结束时间", "截止时间", "to", "end", "end_date"],
    "username": ["用户名", "登录名", "登录名称", "账号", "用户", "user", "username"],
    "company_name": ["公司", "授权单位", "客户名称", "客户", "企业"],
    "role": ["角色", "权限", "岗位", "role", "permission"],
    "department": ["部门", "department"],
    "display_name": ["姓名", "显示名", "display name", "display_name"],
    "email": ["邮箱", "邮件", "email"],
    "amount": ["金额", "amount"],
    "batch_no": ["批次号", "网银批次号", "batch", "batch_no"],
    "account_no": ["账号", "账户号", "银行卡号", "account", "account_no"],
}


class WebSkillGenerator:
    def __init__(self, store: WebSkillStore | None = None):
        self.store = store or WebSkillStore()

    def generate_from_task(self, task: Task, name: str | None = None) -> WebSkillSaveResult:
        data = (task.result or {}).get("data") or {}
        steps = self._source_steps(data)
        if task.intent != "web_action":
            raise WebSkillGenerationError("最近一次成功任务不是 web_action。")
        if task.status != "success" or data.get("status") != "completed":
            raise WebSkillGenerationError("最近一次 web_action 未成功完成，不能沉淀 skill。")
        if not isinstance(steps, list) or not steps:
            raise WebSkillGenerationError("成功任务缺少 canonical_action_trace 或 result.data.steps，不能沉淀 skill。")
        self._validate_reflections(steps)
        self._validate_answer_contract(task, data)

        params = self._tool_params(task)
        site_key = str(params.get("site_key") or task.entities.get("site_key") or "")
        skill_name = validate_skill_name(name.strip() if name else self._generate_name(task, steps, site_key))
        workflow_actions, inputs, parameterization_decisions = self._workflow_actions(task, steps)
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
                dict(input_spec)
                for input_spec in inputs.values()
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
            "parameterization_decisions": parameterization_decisions,
            "validation": {
                "success_signals": ["task completed"],
                "business_stop_rules": ["missing_menu", "missing_option", "empty_result"],
                "requires_answer": self._requires_answer(task.input),
            },
        }
        body = self._skill_body(inputs)
        notes = self._notes(task, site_key, workflow_actions, inputs, parameterization_decisions)
        path = self.store.write(name=skill_name, frontmatter=frontmatter, body=body, workflow=workflow, notes=notes)
        return WebSkillSaveResult(
            name=skill_name,
            path=path,
            inputs=list(inputs.keys()),
            action_count=len(workflow_actions),
            matched_keywords=keywords,
            parameterization_decisions=parameterization_decisions,
        )

    def _source_steps(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        canonical = data.get("canonical_action_trace") or {}
        steps = legacy_steps_from_canonical_trace(canonical)
        if steps:
            return steps
        raw_steps = data.get("steps") or []
        return raw_steps if isinstance(raw_steps, list) else []

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

    def _workflow_actions(
        self,
        task: Task,
        steps: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], OrderedDict[str, dict[str, Any]], list[dict[str, Any]]]:
        actions: list[dict[str, Any]] = []
        inputs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        decisions: list[dict[str, Any]] = []
        for step in steps:
            if step.get("result") != "success":
                continue
            raw_action = dict(step.get("action") or {})
            action_type = str(raw_action.get("type") or "")
            if action_type in SECRET_ACTION_TYPES or action_type not in SUPPORTED_ACTION_TYPES:
                continue
            action = {key: raw_action[key] for key in ACTION_FIELDS if key in raw_action and raw_action[key] not in (None, "")}
            if action_type == "finish" and action.get("value"):
                decisions.append(
                    self._decision(
                        action,
                        original_value="[dynamic result omitted]",
                        decision="dynamic_result/excluded",
                        confidence=1.0,
                        reason="final answer/result text is regenerated from the next run",
                    )
                )
                action.pop("value", None)
            if action_type in {"type", "select", "press"} and action.get("value"):
                value = str(action["value"])
                decision = self._parameter_decision_for_action(action, value, task.input, inputs)
                decisions.append(decision)
                if decision["decision"] == "variable":
                    param_name = str(decision["param_name"])
                    self._record_input(inputs, param_name, str(decision["param_type"]), value)
                    action["value"] = "{{" + param_name + "}}"
            if action_type == "open_url" and action.get("value"):
                action["value"] = str(action["value"])
            if action:
                action.setdefault("key", f"skill.step.{len(actions) + 1}")
                actions.append(action)
        if not actions:
            raise WebSkillGenerationError("成功任务中没有可复用的非登录浏览器动作。")
        return actions, inputs, decisions

    def _parameter_decision_for_action(
        self,
        action: dict[str, Any],
        value: str,
        goal: str,
        existing: OrderedDict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        target = self._action_context(action)
        lowered = target.lower()
        if DATE_VALUE_RE.fullmatch(value.strip()):
            base_name = self._date_param_name(action, existing)
            param_name = self._unique_param_name(base_name, value, existing)
            return self._decision(
                action,
                original_value=value,
                decision="variable",
                param_name=param_name,
                param_type="date",
                confidence=0.95,
                reason="date-like value in date-like field",
            )
        if "@" in value:
            return self._variable_decision(action, value, "email", "email", 0.95, "email value")
        candidates = (
            ("username", "text", PARAMETER_ALIASES["username"]),
            ("company_name", "text", PARAMETER_ALIASES["company_name"]),
            ("role", "text", PARAMETER_ALIASES["role"]),
            ("department", "text", PARAMETER_ALIASES["department"]),
            ("display_name", "text", PARAMETER_ALIASES["display_name"]),
            ("amount", "amount", PARAMETER_ALIASES["amount"]),
            ("batch_no", "text", PARAMETER_ALIASES["batch_no"]),
            ("account_no", "text", PARAMETER_ALIASES["account_no"]),
        )
        for param_name, param_type, markers in candidates:
            if any(marker.lower() in lowered for marker in markers):
                return self._variable_decision(
                    action,
                    value,
                    param_name,
                    param_type,
                    0.85,
                    f"field context matched {param_name}",
                    existing,
                )
        if value and value in goal:
            param_name = self._unique_param_name("input_value", value, existing)
            return self._decision(
                action,
                original_value=value,
                decision="variable",
                param_name=param_name,
                param_type="text",
                confidence=0.7,
                reason="value appears in original user goal",
            )
        return self._decision(
            action,
            original_value=value,
            decision="constant",
            param_type="text",
            confidence=0.35,
            reason="value was not found in user goal and field type was not recognized",
        )

    def _variable_decision(
        self,
        action: dict[str, Any],
        value: str,
        base_name: str,
        param_type: str,
        confidence: float,
        reason: str,
        existing: OrderedDict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        param_name = self._unique_param_name(base_name, value, existing or OrderedDict())
        return self._decision(
            action,
            original_value=value,
            decision="variable",
            param_name=param_name,
            param_type=param_type,
            confidence=confidence,
            reason=reason,
        )

    def _decision(
        self,
        action: dict[str, Any],
        *,
        original_value: str,
        decision: str,
        confidence: float,
        reason: str,
        param_name: str | None = None,
        param_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "action_key": str(action.get("key") or ""),
            "field_hint": str(action.get("target_hint") or action.get("target_id") or ""),
            "original_value": original_value,
            "decision": decision,
            "param_name": param_name,
            "param_type": param_type,
            "confidence": round(confidence, 2),
            "reason": reason,
        }

    def _record_input(
        self,
        inputs: OrderedDict[str, dict[str, Any]],
        name: str,
        param_type: str,
        value: str,
    ) -> None:
        if name not in inputs:
            inputs[name] = {
                "name": name,
                "type": param_type,
                "required": True,
                "source": "user_goal",
                "aliases": PARAMETER_ALIASES.get(name, [name]),
                "examples": [value],
                "original_value": value,
            }
            return
        examples = inputs[name].setdefault("examples", [])
        if value not in examples:
            examples.append(value)

    def _unique_param_name(
        self,
        base_name: str,
        value: str,
        existing: OrderedDict[str, dict[str, Any]],
    ) -> str:
        if base_name not in existing:
            return base_name
        if value in existing[base_name].get("examples", []):
            return base_name
        index = 2
        while f"{base_name}_{index}" in existing:
            index += 1
        return f"{base_name}_{index}"

    def _action_context(self, action: dict[str, Any]) -> str:
        return " ".join(str(action.get(key) or "") for key in ("target_hint", "target_id", "expected_outcome"))

    def _date_param_name(self, action: dict[str, Any], existing: OrderedDict[str, dict[str, Any]]) -> str:
        target_id = str(action.get("target_id") or "").lower()
        hint = str(action.get("target_hint") or "")
        expected = str(action.get("expected_outcome") or "").lower()
        if any(marker in target_id for marker in ("end", "to")) or hint.strip() in {"至", "至："}:
            return "end_date"
        if any(marker in target_id for marker in ("start", "begin", "from")):
            return "start_date"
        if any(marker in hint for marker in ("结束", "截止")) or any(marker in expected for marker in ("end", "to")):
            return "end_date"
        if any(marker in hint for marker in ("开始", "起始")) or any(marker in expected for marker in ("start", "begin", "from")):
            return "start_date"
        return "start_date" if "start_date" not in existing else "end_date"

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

    def _skill_body(self, inputs: OrderedDict[str, dict[str, Any]]) -> str:
        input_lines = "\n".join(
            f"- `{name}` ({spec.get('type') or 'text'}): required task parameter."
            for name, spec in inputs.items()
        ) or "- No user-provided inputs."
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
        inputs: OrderedDict[str, dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> str:
        action_lines = "\n".join(
            f"- {action.get('key', '-')}: {action.get('type')} -> {action.get('target_hint') or action.get('value') or '-'}"
            for action in actions
        )
        input_lines = "\n".join(
            (
                f"- `{name}` ({spec.get('type') or 'text'}) replaces source value "
                f"`{spec.get('original_value') or '-'}`."
            )
            for name, spec in inputs.items()
        )
        fixed_lines = "\n".join(
            (
                f"- {item.get('action_key') or '-'}: {item.get('field_hint') or '-'}="
                f"`{item.get('original_value') or '-'}` confidence={item.get('confidence')}"
            )
            for item in decisions
            if item.get("decision") == "constant"
        )
        excluded_lines = "\n".join(
            f"- {item.get('action_key') or '-'}: {item.get('decision')} ({item.get('reason')})"
            for item in decisions
            if str(item.get("decision") or "").endswith("/excluded")
        )
        return f"""# Web Skill Notes

## Source
- Source task ID: `{task.id}`
- Site key: `{site_key or '-'}`

## Parameters
{input_lines or "- No parameterized user input was captured."}

## Fixed Values
{fixed_lines or "- No fixed typed/select/press values were captured."}

## Excluded Dynamic Values
{excluded_lines or "- No dynamic result values were excluded."}

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
