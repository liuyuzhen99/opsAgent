from __future__ import annotations

import re
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from aiops_agent.browser.action_trace import legacy_steps_from_canonical_trace
from aiops_agent.browser.models import BrowserAction
from aiops_agent.browser.risk import RiskEvaluator
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

GOAL_INPUT_FIELDS = (
    "账户编号由",
    "账户编号至",
    "账户号由",
    "账户号至",
    "账号由",
    "账号至",
    "授权单位",
    "所属单位",
    "用户名称",
    "用户名",
    "登录名称",
    "岗位名称",
    "角色名称",
    "账户名称",
    "客户名称",
    "公司名称",
    "邮箱",
    "金额",
    "批次号",
    "账号",
    "账户号",
)

PARAMETER_ALIASES = {
    "start_date": ["开始日期", "起始日期", "开始时间", "起始时间", "from", "start", "start_date"],
    "end_date": ["结束日期", "截止日期", "结束时间", "截止时间", "to", "end", "end_date"],
    "user_name": ["用户名称", "用户姓名", "user_name"],
    "login_name": ["登录名称", "登录名", "登陆名称", "登陆名", "login_name"],
    "username": ["用户名", "登录名", "登录名称", "账号", "用户", "user", "username"],
    "company_name": ["公司", "授权单位", "客户名称", "客户", "企业"],
    "role": ["角色", "权限", "岗位", "role", "permission"],
    "department": ["部门", "department"],
    "display_name": ["姓名", "显示名", "display name", "display_name"],
    "email": ["邮箱", "邮件", "email"],
    "amount": ["金额", "amount"],
    "batch_no": ["批次号", "网银批次号", "batch", "batch_no"],
    "account_no": [
        "账户编号由",
        "账户编号至",
        "账户编号",
        "账户号由",
        "账户号至",
        "账号由",
        "账号至",
        "账号",
        "账户号",
        "银行卡号",
        "account no from",
        "account no to",
        "account",
        "account_no",
    ],
}


class WebSkillGenerator:
    def __init__(self, store: WebSkillStore | None = None, *, risk_evaluator: RiskEvaluator | None = None):
        self.store = store or WebSkillStore()
        self.risk_evaluator = risk_evaluator or RiskEvaluator()

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
        credential_refs = [
            str(ref)
            for ref in (params.get("credential_refs") or [])
            if str(ref).strip()
        ]
        if not credential_refs and params.get("credential_ref"):
            credential_refs = [str(params["credential_ref"])]

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
            "goal_template": self._goal_template(task.input, workflow_actions, inputs),
            "inputs": [
                dict(input_spec)
                for input_spec in inputs.values()
            ],
            "match": {
                "keywords": keywords,
                "fields": self._fields_from_actions(workflow_actions),
                "answer_types": self._answer_types(task.input),
                "navigation": self._explicit_navigation_labels(task.input),
            },
            "execution": {
                "auto_plan": False,
                "requires_login": requires_login,
                "credential_refs": credential_refs,
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

    def _explicit_navigation_labels(self, goal: str) -> list[str]:
        return [label for path in self._explicit_navigation_paths(goal) for label in path]

    def _explicit_navigation_paths(self, goal: str) -> list[list[str]]:
        paths: list[list[str]] = []
        pattern = re.compile(
            r"依次点击\s*(.+?)(?=(?:进入对应菜单|进入[^,，。；;]{0,20}(?:[,，]|然后|之后|$)|"
            r"[,，]\s*(?:点击|等待|然后|之后|选中|选择|勾选|输入|填入|填写|告诉))|[。；;]|$)"
        )
        for match in pattern.finditer(goal):
            labels = [
                label
                for item in re.split(r"[,，、]", match.group(1))
                if (label := item.strip(" '\"“”"))
            ]
            if labels:
                paths.append(labels)
        return paths

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
        goal_inputs = self._goal_input_specs(task.input)
        for name, spec in self._goal_output_specs(task.input).items():
            goal_inputs.setdefault(name, spec)
        for step in steps:
            if step.get("result") != "success":
                continue
            raw_action = dict(step.get("action") or {})
            action_type = str(raw_action.get("type") or "")
            if action_type in SECRET_ACTION_TYPES or action_type not in SUPPORTED_ACTION_TYPES:
                continue
            action = {key: raw_action[key] for key in ACTION_FIELDS if key in raw_action and raw_action[key] not in (None, "")}
            target_id = str(action.get("target_id") or "")
            if target_id.startswith("aiops-"):
                action.pop("target_id", None)
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
            if action_type in {"extract_text", "save_artifact"} and action.get("value"):
                decisions.append(
                    self._decision(
                        action,
                        original_value="[dynamic result omitted]",
                        decision="dynamic_result/excluded",
                        confidence=1.0,
                        reason=f"{action_type} result text is regenerated from the next run",
                    )
                )
                action.pop("value", None)
            if action_type in {"type", "select"} and action.get("value"):
                value = str(action["value"])
                decision = self._parameter_decision_for_action(action, value, task.input, inputs, goal_inputs)
                decisions.append(decision)
                if decision["decision"] == "variable":
                    param_name = str(decision["param_name"])
                    goal_spec = goal_inputs.get(param_name)
                    if goal_spec and self._looks_like_technical_field_hint(action.get("target_hint")):
                        semantic_hint = self._semantic_field_hint(action, goal_spec)
                        if semantic_hint:
                            action["target_hint"] = semantic_hint
                    self._record_input(
                        inputs,
                        param_name,
                        str(decision["param_type"]),
                        value,
                        aliases=goal_spec.get("aliases") if goal_spec else None,
                    )
                    action["value"] = "{{" + param_name + "}}"
            if action_type == "open_url" and action.get("value"):
                action["value"] = str(action["value"])
            if action:
                self._normalize_action_risk(action)
                action.setdefault("key", f"skill.step.{len(actions) + 1}")
                actions.append(action)
        if not actions:
            raise WebSkillGenerationError("成功任务中没有可复用的非登录浏览器动作。")
        actions = self._prune_clicks_during_explicit_navigation(actions, task.input)
        for name, spec in goal_inputs.items():
            if not spec.get("output_only") or name in inputs:
                continue
            value = str(spec.get("value") or "")
            self._record_input(
                inputs,
                name,
                str(spec.get("type") or "text"),
                value,
                aliases=list(spec.get("aliases") or [name]),
            )
            decisions.append(
                {
                    "action_key": f"skill.output.{name}",
                    "field_hint": str(spec.get("field_hint") or ""),
                    "original_value": value,
                    "decision": "variable",
                    "param_name": name,
                    "param_type": str(spec.get("type") or "text"),
                    "confidence": 0.95,
                    "reason": "value is part of the requested answer contract",
                }
            )
        self._normalize_goal_field_click_targets(actions, goal_inputs)
        actions = self._insert_missing_goal_input_actions(actions, inputs, goal_inputs, decisions, task.input)
        self._render_parameter_references(actions, inputs)
        return actions, inputs, decisions

    def _normalize_action_risk(self, action: dict[str, Any]) -> None:
        try:
            browser_action = BrowserAction(**action)
        except TypeError:
            return
        risk_level = self.risk_evaluator.classify(browser_action)
        action["risk_level"] = risk_level
        action["requires_confirmation"] = risk_level in {"unsafe_mutation", "unknown_risk"}

    def _parameter_decision_for_action(
        self,
        action: dict[str, Any],
        value: str,
        goal: str,
        existing: OrderedDict[str, dict[str, Any]],
        goal_inputs: OrderedDict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        matched_goal_input = self._goal_input_for_value(value, goal_inputs or OrderedDict())
        if matched_goal_input:
            return self._decision(
                action,
                original_value=value,
                decision="variable",
                param_name=str(matched_goal_input["name"]),
                param_type=str(matched_goal_input.get("type") or "text"),
                confidence=0.95,
                reason="value matches explicit user input in original goal",
            )
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
            ("user_name", "text", PARAMETER_ALIASES["user_name"]),
            ("login_name", "text", PARAMETER_ALIASES["login_name"]),
            ("username", "text", PARAMETER_ALIASES["username"]),
            ("company_name", "text", PARAMETER_ALIASES["company_name"]),
            ("role", "text", PARAMETER_ALIASES["role"]),
            ("department", "text", PARAMETER_ALIASES["department"]),
            ("display_name", "text", PARAMETER_ALIASES["display_name"]),
            ("amount", "amount", PARAMETER_ALIASES["amount"]),
            ("batch_no", "text", PARAMETER_ALIASES["batch_no"]),
            ("account_no", "text", PARAMETER_ALIASES["account_no"]),
        )
        for context in self._action_contexts(action):
            lowered = context.lower()
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
        aliases: list[str] | None = None,
    ) -> None:
        if name not in inputs:
            inputs[name] = {
                "name": name,
                "type": param_type,
                "required": True,
                "source": "user_goal",
                "aliases": aliases or PARAMETER_ALIASES.get(name, [name]),
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

    def _action_contexts(self, action: dict[str, Any]) -> tuple[str, ...]:
        field_context = " ".join(str(action.get(key) or "") for key in ("target_hint", "target_id")).strip()
        outcome_context = str(action.get("expected_outcome") or "").strip()
        outcome_context = re.sub(r"按用户(?:原始)?指令", "", outcome_context).strip()
        return tuple(context for context in (field_context, outcome_context) if context)

    def _looks_like_technical_field_hint(self, value: Any) -> bool:
        hint = str(value or "").strip()
        return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", hint) is not None

    def _semantic_field_hint(self, action: dict[str, Any], goal_spec: dict[str, Any]) -> str | None:
        context = self._action_context(action)
        field_hints = [
            str(item).strip()
            for item in goal_spec.get("field_hints") or [goal_spec.get("field_hint")]
            if str(item or "").strip()
        ]
        for field_hint in field_hints:
            if field_hint in context:
                return field_hint

        outcome = str(action.get("expected_outcome") or "")
        chinese_match = re.search(
            r"(?:字段|输入框)\s*[\"“”']?([^,，。；;\"“”']{1,30}?)[\"“”']?\s*(?:中|里)\s*"
            r"(?:输入|填写|填入)",
            outcome,
        )
        if chinese_match:
            return chinese_match.group(1).strip()
        english_match = re.search(r"(?:into|in)\s+(?:the\s+)?(.+?)\s+field\b", outcome, re.I)
        if english_match:
            return english_match.group(1).strip(" '\"“”")
        if not goal_spec.get("output_only") and field_hints:
            return field_hints[0]
        return None

    def _normalize_goal_field_click_targets(
        self,
        actions: list[dict[str, Any]],
        goal_inputs: OrderedDict[str, dict[str, Any]],
    ) -> None:
        for action in actions:
            if action.get("type") != "click":
                continue
            expected_outcome = str(action.get("expected_outcome") or "")
            if not any(marker in expected_outcome for marker in ("下拉", "输入框", "输入区域")):
                continue
            for spec in goal_inputs.values():
                field_hint = str(spec.get("field_hint") or "")
                if field_hint and field_hint in expected_outcome:
                    action["target_hint"] = field_hint
                    break

    def _goal_input_specs(self, goal: str) -> OrderedDict[str, dict[str, Any]]:
        specs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        named_fields = "|".join(re.escape(field) for field in GOAL_INPUT_FIELDS)
        value_first_pattern = re.compile(
            r"(?:将|把)\s*[\"“”']?(?P<value>[^,，。；;\"“”']+?)[\"“”']?\s*"
            r"(?:输入|填写|填入)(?:到|至|进)?\s*(?P<fields>.+?)(?:中|里)"
            r"(?=$|[,，。；;]|然后|之后|随后|接着|再)"
        )
        for match in value_first_pattern.finditer(goal):
            value = match.group("value").strip("'\"“” ")
            raw_fields = [
                item.strip("'\"“” ")
                for item in re.split(r"\s*(?:和|及|与|、|,|，)\s*", match.group("fields"))
            ]
            for raw_field in raw_fields:
                field_match = re.search(named_fields, raw_field)
                field = field_match.group(0) if field_match else raw_field
                self._record_goal_input_spec(specs, field, value, position=match.start())
        expanded_input_pattern = re.compile(
            rf"(?:在|向)\s*({named_fields})(?:中|里)?"
            r"(?:\s*(?:展开|打开)[^,，。；;]{0,40}[,，]\s*)"
            r"(?:输入|填写|填入|选择)\s*[\"“']?([^,，。；;\"”']+)"
        )
        for match in expanded_input_pattern.finditer(goal):
            field = match.group(1).strip("'\"“” ")
            value = match.group(2).strip("'\"“” ")
            self._record_goal_input_spec(specs, field, value, position=match.start())
        typed_pattern = re.compile(
            r"(?:在|向)\s*([^,，。；;\s]{2,30}?)(?:中|里)?"
            r"(?:输入|填写|填入|选择)\s*[\"“']?([^,，。；;\"”']+)"
        )
        for match in typed_pattern.finditer(goal):
            field = match.group(1).strip("'\"“” ")
            value = match.group(2).strip("'\"“” ")
            self._record_goal_input_spec(specs, field, value, position=match.start())
        field_pattern = re.compile(rf"({named_fields})\s*(?:为|是|叫|:|：)\s*([^,，。；;\s]+)")
        for match in field_pattern.finditer(goal):
            field = match.group(1).strip()
            value = match.group(2).strip("'\"“” ")
            self._record_goal_input_spec(specs, field, value, position=match.start())
        return specs

    def _goal_output_specs(self, goal: str) -> OrderedDict[str, dict[str, Any]]:
        specs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for match in re.finditer(
            r"(?P<value>[A-Za-z0-9][A-Za-z0-9_.:-]*)\s*是否在(?:该用户的)?\s*"
            r"(?P<label>已分配账户|Assigned Account)(?:列表)?中",
            goal,
            re.I,
        ):
            value = match.group("value")
            specs["account_no"] = {
                "name": "account_no",
                "type": "text",
                "field_hint": match.group("label"),
                "value": value,
                "aliases": PARAMETER_ALIASES["account_no"],
                "examples": [value],
                "position": match.start(),
                "output_only": True,
            }
        return specs

    def _record_goal_input_spec(
        self,
        specs: OrderedDict[str, dict[str, Any]],
        field: str,
        value: str,
        *,
        position: int,
    ) -> None:
        field = re.sub(r"^(?:(?:之后|然后|随后|接着)再?|再)?(?:在|向)", "", field).strip()
        if not field or not value:
            return
        param_name = self._param_name_for_field(field)
        param_type = "date" if DATE_VALUE_RE.fullmatch(value) else "text"
        if param_name in specs:
            examples = specs[param_name].setdefault("examples", [])
            if value not in examples:
                examples.append(value)
            field_hints = specs[param_name].setdefault("field_hints", [str(specs[param_name].get("field_hint") or "")])
            if field not in field_hints:
                field_hints.append(field)
            return
        specs[param_name] = {
            "name": param_name,
            "type": param_type,
            "field_hint": field,
            "field_hints": [field],
            "value": value,
            "aliases": PARAMETER_ALIASES.get(param_name, [field, param_name]),
            "examples": [value],
            "position": position,
        }

    def _param_name_for_field(self, field: str) -> str:
        normalized = field.lower()
        if "用户名称" in field or "用户姓名" in field:
            return "user_name"
        if "登录" in field or "登陆" in field:
            return "login_name"
        for name, aliases in PARAMETER_ALIASES.items():
            if any(alias.lower() in normalized or alias in field for alias in aliases):
                return name
        return self._slug(field.replace("名称", "_name").replace("编号", "_no")) or "input_value"

    def _goal_input_for_value(
        self,
        value: str,
        goal_inputs: OrderedDict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        for spec in goal_inputs.values():
            if value in spec.get("examples", []):
                return spec
        return None

    def _insert_missing_goal_input_actions(
        self,
        actions: list[dict[str, Any]],
        inputs: OrderedDict[str, dict[str, Any]],
        goal_inputs: OrderedDict[str, dict[str, Any]],
        decisions: list[dict[str, Any]],
        goal: str,
    ) -> list[dict[str, Any]]:
        missing_specs = [
            spec for name, spec in goal_inputs.items()
            if name not in inputs and not spec.get("output_only") and str(spec.get("value") or "")
        ]
        if not missing_specs:
            return actions
        insert_index = self._first_result_action_index(actions)
        synthesized: list[dict[str, Any]] = []
        for spec in missing_specs:
            name = str(spec["name"])
            value = str(spec["value"])
            field_hint = str(spec.get("field_hint") or name)
            pre_click = self._click_target_before_goal_input(goal, spec)
            if pre_click and not self._has_click_action(actions + synthesized, pre_click):
                synthesized.append(
                    {
                        "type": "click",
                        "target_hint": pre_click,
                        "expected_outcome": f"打开{field_hint}输入区域",
                        "risk_level": "safe_read",
                        "key": f"skill.synthetic.{name}.open",
                    }
                )
            self._record_input(
                inputs,
                name,
                str(spec.get("type") or "text"),
                value,
                aliases=list(spec.get("aliases") or [field_hint, name]),
            )
            type_action = {
                "type": "type",
                "target_hint": field_hint,
                "value": "{{" + name + "}}",
                "expected_outcome": f"填写{field_hint}",
                "risk_level": "safe_local_edit",
                "key": f"skill.synthetic.{name}.type",
            }
            synthesized.append(type_action)
            decisions.append(
                self._decision(
                    type_action,
                    original_value=value,
                    decision="variable",
                    param_name=name,
                    param_type=str(spec.get("type") or "text"),
                    confidence=0.9,
                    reason="synthesized from explicit user input in original goal",
                )
            )
            post_click = self._click_target_after_goal_input(goal, spec)
            if post_click and not self._has_click_action(actions + synthesized, post_click):
                synthesized.append(
                    {
                        "type": "click",
                        "target_hint": post_click,
                        "expected_outcome": f"提交{field_hint}",
                        "risk_level": "safe_read",
                        "key": f"skill.synthetic.{name}.submit",
                    }
                )
        if not synthesized:
            return actions
        return [*actions[:insert_index], *synthesized, *actions[insert_index:]]

    def _first_result_action_index(self, actions: list[dict[str, Any]]) -> int:
        for index, action in enumerate(actions):
            if action.get("type") in {"extract_text", "save_artifact", "finish"}:
                return index
        return len(actions)

    def _click_target_before_goal_input(self, goal: str, spec: dict[str, Any]) -> str | None:
        position = int(spec.get("position") or 0)
        before = goal[:position]
        matches = re.findall(r"点击\s*([^,，。；;\s]{1,20}?)(?:按钮)?(?:后|，|,|$)", before)
        if not matches:
            return None
        return self._clean_click_target(matches[-1])

    def _click_target_after_goal_input(self, goal: str, spec: dict[str, Any]) -> str | None:
        value = str(spec.get("value") or "")
        position = goal.find(value, int(spec.get("position") or 0))
        if position < 0:
            return None
        after = goal[position + len(value):position + len(value) + 80]
        match = re.search(r"点击\s*([^,，。；;\s]{1,20}?)(?:按钮)?(?:后|，|,|$)", after)
        if not match:
            return None
        return self._clean_click_target(match.group(1))

    def _clean_click_target(self, value: str) -> str | None:
        cleaned = value.strip("'\"“” ")
        cleaned = re.sub(r"(按钮|后)$", "", cleaned).strip()
        return cleaned or None

    def _has_click_action(self, actions: list[dict[str, Any]], target_hint: str) -> bool:
        return any(
            action.get("type") == "click" and str(action.get("target_hint") or "") == target_hint
            for action in actions
        )

    def _prune_clicks_during_explicit_navigation(
        self,
        actions: list[dict[str, Any]],
        goal: str,
    ) -> list[dict[str, Any]]:
        paths = self._explicit_navigation_paths(goal)
        if not paths:
            return actions
        route_ranges: list[tuple[int, int, set[int]]] = []
        search_from = 0
        for labels in paths:
            expected = [self._normalize_navigation_label(label) for label in labels]
            positions: list[int] = []
            cursor = 0
            for index in range(search_from, len(actions)):
                action = actions[index]
                if action.get("type") != "click" or cursor >= len(expected):
                    continue
                target = self._normalize_navigation_label(str(action.get("target_hint") or ""))
                if target == expected[cursor]:
                    positions.append(index)
                    cursor += 1
            if cursor != len(expected):
                return actions
            route_ranges.append((positions[0], positions[-1], set(positions)))
            search_from = positions[-1] + 1
        return [
            action
            for index, action in enumerate(actions)
            if not any(
                first_route <= index <= last_route
                and action.get("type") == "click"
                and index not in route_positions
                for first_route, last_route, route_positions in route_ranges
            )
        ]

    def _normalize_navigation_label(self, value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    def _render_parameter_references(
        self,
        actions: list[dict[str, Any]],
        inputs: OrderedDict[str, dict[str, Any]],
    ) -> None:
        replacements = [
            (str(spec.get("original_value") or ""), "{{" + name + "}}")
            for name, spec in inputs.items()
            if spec.get("original_value")
        ]
        replacements.sort(key=lambda item: len(item[0]), reverse=True)
        for action in actions:
            for key in ("target_hint", "expected_outcome", "value"):
                if key not in action or not isinstance(action[key], str):
                    continue
                parts = re.split(r"(\{\{[^{}]+\}\})", action[key])
                for index in range(0, len(parts), 2):
                    for original, replacement in replacements:
                        parts[index] = parts[index].replace(original, replacement)
                action[key] = "".join(parts)

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
        if self._goal_output_specs(goal):
            return ["membership"]
        if "岗位名称" in goal or "角色" in goal:
            return ["role_names"]
        if self._requires_answer(goal):
            return ["text_answer"]
        return []

    def _goal_template(
        self,
        goal: str,
        actions: list[dict[str, Any]],
        inputs: OrderedDict[str, dict[str, Any]],
    ) -> str:
        template = goal.strip()
        replacements = sorted(
            (
                (str(spec.get("original_value") or ""), "{{" + name + "}}")
                for name, spec in inputs.items()
                if spec.get("original_value")
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for original, replacement in replacements:
            template = template.replace(original, replacement)
        if template:
            return template
        outcomes = list(
            dict.fromkeys(
                str(action.get("expected_outcome") or "").strip()
                for action in actions
                if action.get("type") != "open_url" and str(action.get("expected_outcome") or "").strip()
            )
        )
        return "按顺序完成以下网页任务：" + "；".join(outcomes)

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
