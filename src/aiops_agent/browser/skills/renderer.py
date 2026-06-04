from __future__ import annotations

import re
from typing import Any

from aiops_agent.browser.models import BrowserAction
from aiops_agent.browser.skills.models import WebSkillValidationError


PARAM_ALIASES = {
    "start_date": ("开始日期", "起始日期", "开始时间", "起始时间", "from", "start", "start_date"),
    "end_date": ("结束日期", "截止日期", "结束时间", "截止时间", "to", "end", "end_date"),
    "username": ("用户名", "登录名", "登录名称", "账号", "用户", "user", "username"),
    "company_name": ("公司", "授权单位", "客户名称", "客户", "企业"),
    "role": ("角色", "权限", "岗位", "role", "permission"),
    "department": ("部门", "department"),
    "display_name": ("姓名", "显示名", "display name", "display_name"),
    "email": ("邮箱", "邮件", "email"),
    "amount": ("金额", "amount"),
    "batch_no": ("批次号", "网银批次号", "batch", "batch_no"),
    "account_no": ("账号", "账户号", "银行卡号", "account", "account_no"),
}

DATE_PATTERN = r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"


class WebSkillRenderer:
    def infer_parameters(self, workflow: dict[str, Any], goal: str, entities: dict[str, Any]) -> dict[str, str]:
        inputs = workflow.get("inputs") or []
        workflow_fields = dict(entities.get("workflow_fields") or {})
        parameters: dict[str, str] = {}
        date_range = self._date_range_from_goal(goal)
        for item in inputs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            param_type = str(item.get("type") or "").strip().lower()
            aliases = self._aliases_for_input(item, name)
            value = self._value_from_entities(name, workflow_fields)
            if value is None:
                value = self._value_from_typed_goal(name, param_type, aliases, goal, date_range)
            if value is None:
                value = self._value_from_goal(name, goal, aliases=aliases)
            if value is None:
                value = self._value_from_examples(item, goal)
            if value is not None:
                parameters[name] = value
        return parameters

    def render_actions(self, workflow: dict[str, Any], parameters: dict[str, str], entities: dict[str, Any]) -> list[BrowserAction]:
        missing = [
            str(item.get("name"))
            for item in workflow.get("inputs") or []
            if isinstance(item, dict) and item.get("required", True) and str(item.get("name")) not in parameters
        ]
        if missing:
            raise WebSkillValidationError("missing required skill parameters: " + ", ".join(missing))
        actions: list[BrowserAction] = []
        for raw_action in self._workflow_actions(workflow):
            if not isinstance(raw_action, dict):
                continue
            payload = {
                "type": str(raw_action.get("type") or ""),
                "target_hint": self._render_value(raw_action.get("target_hint", ""), parameters),
                "target_id": self._render_optional(raw_action.get("target_id"), parameters),
                "value": self._render_optional(raw_action.get("value"), parameters),
                "expected_outcome": self._render_value(raw_action.get("expected_outcome", ""), parameters),
                "risk_level": str(raw_action.get("risk_level") or "safe_read"),
                "requires_confirmation": bool(raw_action.get("requires_confirmation", False)),
                "timeout_ms": int(raw_action.get("timeout_ms") or 5000),
                "key": str(raw_action.get("key") or ""),
            }
            if payload["type"] == "open_url" and not payload["value"]:
                payload["value"] = str(entities.get("start_url") or "")
            actions.append(BrowserAction(**payload))
        return actions

    def _workflow_actions(self, workflow: dict[str, Any]) -> list:
        return list(workflow.get("actions") or workflow.get("steps") or [])

    def _value_from_entities(self, name: str, workflow_fields: dict[str, Any]) -> str | None:
        if name in workflow_fields and workflow_fields[name]:
            return str(workflow_fields[name])
        if name == "company_name":
            for key in ("company", "customer", "customer_name"):
                if workflow_fields.get(key):
                    return str(workflow_fields[key])
        if name == "role":
            for key in ("permission", "role_name"):
                if workflow_fields.get(key):
                    return str(workflow_fields[key])
        if name == "start_date":
            for key in ("date_start", "start", "from_date"):
                if workflow_fields.get(key):
                    return str(workflow_fields[key])
        if name == "end_date":
            for key in ("date_end", "end", "to_date"):
                if workflow_fields.get(key):
                    return str(workflow_fields[key])
        return None

    def _value_from_goal(self, name: str, goal: str, *, aliases: tuple[str, ...] | None = None) -> str | None:
        aliases = aliases or PARAM_ALIASES.get(name, (name,))
        for alias in aliases:
            patterns = (
                rf"{re.escape(alias)}\s*(?:为|是|叫|:|：)\s*([A-Za-z0-9_.@\-]+|[\u4e00-\u9fffA-Za-z0-9_.@\-]{{2,40}})",
                rf"(?:在|向)\s*{re.escape(alias)}(?:中|里)?\s*(?:输入|填写|填入|选择)\s*[\"“']?([^,，。；;\"”']+)",
            )
            for pattern in patterns:
                match = re.search(pattern, goal, flags=re.IGNORECASE)
                if match:
                    return match.group(1).strip("'\"“” ")
        if name == "email":
            match = re.search(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", goal)
            if match:
                return match.group(0)
        return None

    def _value_from_typed_goal(
        self,
        name: str,
        param_type: str,
        aliases: tuple[str, ...],
        goal: str,
        date_range: tuple[str, str] | None,
    ) -> str | None:
        if param_type != "date":
            return None
        if name == "start_date" and date_range:
            return date_range[0]
        if name == "end_date" and date_range:
            return date_range[1]
        for alias in aliases:
            match = re.search(
                rf"{re.escape(alias)}\s*(?:为|是|叫|:|：|=)?\s*({DATE_PATTERN})",
                goal,
                flags=re.IGNORECASE,
            )
            if match:
                return self._normalize_date(match.group(1))
        return None

    def _aliases_for_input(self, item: dict[str, Any], name: str) -> tuple[str, ...]:
        aliases = [str(alias) for alias in item.get("aliases") or [] if str(alias).strip()]
        if aliases:
            return tuple(dict.fromkeys([*aliases, name]))
        return PARAM_ALIASES.get(name, (name,))

    def _date_range_from_goal(self, goal: str) -> tuple[str, str] | None:
        match = re.search(
            rf"({DATE_PATTERN})\s*(?:到|至|~|－|—|--|-)\s*({DATE_PATTERN})",
            goal,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return self._normalize_date(match.group(1)), self._normalize_date(match.group(2))

    def _normalize_date(self, value: str) -> str:
        parts = re.split(r"[-/]", value.strip())
        if len(parts) != 3:
            return value.strip()
        year, month, day = parts
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    def _value_from_examples(self, item: dict[str, Any], goal: str) -> str | None:
        for example in item.get("examples") or []:
            value = str(example)
            if value and value in goal:
                return value
        return None

    def _render_optional(self, value: Any, parameters: dict[str, str]) -> str | None:
        if value is None:
            return None
        rendered = self._render_value(value, parameters)
        return rendered if rendered != "" else None

    def _render_value(self, value: Any, parameters: dict[str, str]) -> str:
        text = str(value or "")
        for name, replacement in parameters.items():
            text = text.replace("{{" + name + "}}", replacement)
        return text
