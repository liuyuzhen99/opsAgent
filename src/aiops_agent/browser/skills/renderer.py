from __future__ import annotations

import re
from typing import Any

from aiops_agent.browser.models import BrowserAction
from aiops_agent.browser.skills.models import WebSkillValidationError


PARAM_ALIASES = {
    "username": ("用户名", "登录名", "登录名称", "账号", "用户", "user", "username"),
    "company_name": ("公司", "授权单位", "客户名称", "客户", "企业"),
    "role": ("角色", "权限", "岗位", "role", "permission"),
    "department": ("部门", "department"),
    "display_name": ("姓名", "显示名", "display name", "display_name"),
    "email": ("邮箱", "邮件", "email"),
}


class WebSkillRenderer:
    def infer_parameters(self, workflow: dict[str, Any], goal: str, entities: dict[str, Any]) -> dict[str, str]:
        inputs = workflow.get("inputs") or []
        workflow_fields = dict(entities.get("workflow_fields") or {})
        parameters: dict[str, str] = {}
        for item in inputs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            value = self._value_from_entities(name, workflow_fields)
            if value is None:
                value = self._value_from_goal(name, goal)
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
        for raw_action in workflow.get("actions") or []:
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
        return None

    def _value_from_goal(self, name: str, goal: str) -> str | None:
        aliases = PARAM_ALIASES.get(name, (name,))
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
