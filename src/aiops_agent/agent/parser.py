from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiops_agent.config import RPAConfig
from aiops_agent.llm.base import BaseLLMProvider, LLMError


@dataclass(slots=True)
class IntentResult:
    intent: str
    entities: dict[str, Any]


class IntentParser:
    INSPECTION_KEYWORDS = ("巡检", "检查", "inspect", "inspection")
    PERMISSION_KEYWORDS = ("权限", "授权", "permission", "grant")
    QA_KEYWORDS = (
        "怎么", "如何", "why", "what", "知识库", "sop",
        "是什么", "什么意思", "步骤", "手册", "runbook",
        "排查", "troubleshoot", "处理", "解决", "原因",
        "告警", "故障", "incident", "最佳实践",
    )
    GENERAL_CHAT_KEYWORDS = ("hello", "hi", "你好", "您好", "hey")
    WEB_ACTION_KEYWORDS = (
        "网页",
        "浏览器",
        "页面自动化",
        "网站",
        "登录",
        "打开",
        "访问",
        "click",
        "form",
        "http://",
        "https://",
    )
    WEB_ACCOUNT_KEYWORDS = (
        "创建账号",
        "新建账号",
        "创建用户",
        "新建用户",
        "查询用户",
        "搜索用户",
        "查找用户",
        "分配角色",
        "分配权限",
        "只读权限",
        "授权",
        "create user",
        "search user",
        "find user",
        "assign role",
        "grant",
    )

    def __init__(
        self,
        rpa_config: RPAConfig | None = None,
        llm_provider: BaseLLMProvider | None = None,
    ):
        inspection_defaults = (rpa_config or RPAConfig()).inspection
        self.default_system = inspection_defaults.default_system
        self.default_env = inspection_defaults.default_env
        self.llm_provider = llm_provider
        self.last_llm_error: str | None = None

    def parse(self, text: str) -> IntentResult:
        normalized = text.strip()
        self.last_llm_error = None
        llm_result = self._parse_with_llm(normalized)
        if llm_result is not None:
            return llm_result

        rule_result = self._parse_with_rules(normalized)
        if self.last_llm_error:
            rule_result.entities["llm_fallback_used"] = True
            rule_result.entities["llm_fallback_error"] = self.last_llm_error
        return rule_result

    def _parse_with_llm(self, text: str) -> IntentResult | None:
        if self.llm_provider is None:
            return None

        try:
            parsed = self.llm_provider.classify_intent(
                text,
                defaults={"system": self.default_system, "env": self.default_env},
            )
        except LLMError as exc:
            self.last_llm_error = str(exc)
            return None

        entities = dict(parsed.entities)
        entities.setdefault("system", self.default_system)
        entities.setdefault("env", self.default_env)
        entities["raw_text"] = text
        entities["llm_provider"] = parsed.provider
        entities["llm_model"] = parsed.model
        if parsed.request_id:
            entities["llm_request_id"] = parsed.request_id
        return IntentResult(intent=parsed.intent, entities=entities)

    def _parse_with_rules(self, normalized: str) -> IntentResult:
        lowered = normalized.lower()

        if any(keyword in lowered for keyword in self.INSPECTION_KEYWORDS):
            return IntentResult(
                intent="inspection",
                entities={
                    "system": self._extract_system(normalized),
                    "env": self._extract_env(normalized),
                    "raw_text": normalized,
                },
            )

        if any(keyword in lowered for keyword in self.WEB_ACTION_KEYWORDS + self.WEB_ACCOUNT_KEYWORDS):
            start_url = self._extract_url(normalized)
            allowed_domains = []
            if start_url:
                host = urlparse(start_url).netloc
                if host:
                    allowed_domains.append(host)
            return IntentResult(
                intent="web_action",
                entities={
                    "raw_text": normalized,
                    "start_url": start_url,
                    "allowed_domains": allowed_domains,
                    "requires_login": any(keyword in lowered for keyword in ("登录", "login", "账号", "password", "密码")),
                    "has_side_effect": any(
                        keyword in lowered
                        for keyword in (
                            "提交",
                            "保存",
                            "删除",
                            "创建",
                            "开通",
                            "授权",
                            "submit",
                            "save",
                            "delete",
                            "create",
                            "grant",
                        )
                    ),
                    "workflow": self._extract_web_workflow(normalized),
                    "workflow_fields": self._extract_workflow_fields(normalized),
                },
            )

        if any(keyword in lowered for keyword in self.PERMISSION_KEYWORDS):
            return IntentResult(
                intent="permission_change",
                entities={"raw_text": normalized},
            )

        if (any(keyword in lowered for keyword in self.QA_KEYWORDS)
                and not any(keyword in lowered for keyword in self.WEB_ACTION_KEYWORDS)):
            return IntentResult(intent="ops_qa", entities={"raw_text": normalized})

        if any(keyword in lowered for keyword in self.GENERAL_CHAT_KEYWORDS):
            return IntentResult(intent="general_chat", entities={"raw_text": normalized})

        return IntentResult(intent="general_chat", entities={"raw_text": normalized})

    def _extract_url(self, text: str) -> str | None:
        match = re.search(r"https?://[^\s，。；,;]+", text, flags=re.IGNORECASE)
        if match:
            return match.group(0).rstrip("。,.，")
        return None

    def _extract_web_workflow(self, text: str) -> str | None:
        lowered = text.lower()
        search = any(keyword in lowered for keyword in ("查询用户", "搜索用户", "查找用户", "search user", "find user"))
        create = any(keyword in lowered for keyword in ("创建账号", "新建账号", "创建用户", "新建用户", "create user"))
        role = any(keyword in lowered for keyword in ("分配角色", "分配权限", "授权", "只读权限", "assign role", "grant"))
        if create and role:
            return "create_user_and_assign_role"
        if search:
            return "search_user"
        if create:
            return "create_user"
        if role:
            return "assign_role"
        return None

    def _extract_workflow_fields(self, text: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        username_patterns = (
            r"(?:账号|用户|用户名|user|username)\s*(?:为|叫|是|:|：)?\s*([A-Za-z0-9_.@-]{2,})",
            r"(?:创建账号|新建账号|创建用户|新建用户)\s*([A-Za-z0-9_.@-]{2,})",
            r"(?:查询用户|搜索用户|查找用户|search user|find user)\s*([A-Za-z0-9_.@-]{2,})",
        )
        for pattern in username_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                fields["username"] = match.group(1)
                break
        email = re.search(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
        if email:
            fields["email"] = email.group(0)
        department = re.search(r"(?:部门|department)\s*(?:为|是|:|：)?\s*([\w\u4e00-\u9fff-]{2,})", text, flags=re.IGNORECASE)
        if department:
            fields["department"] = department.group(1)
        display_name = re.search(r"(?:姓名|显示名|display_name|display name)\s*(?:为|是|:|：)?\s*([\w\u4e00-\u9fff.-]{2,})", text, flags=re.IGNORECASE)
        if display_name:
            fields["display_name"] = display_name.group(1)
        role_patterns = (
            r"(只读权限|管理员|普通用户|只读|readonly|read-only|admin|viewer)",
            r"(?:角色|权限|role|permission)\s*(?:为|是|:|：)?\s*([\w\u4e00-\u9fff.-]{2,})",
        )
        for pattern in role_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                fields["role"] = match.group(1)
                break
        return fields

    def _extract_system(self, text: str) -> str:
        known_systems = ("WebLogic", "Nginx", "Redis", "MySQL", "K8s", "Kafka")
        for system in known_systems:
            if system.lower() in text.lower():
                return system
        return self.default_system

    def _extract_env(self, text: str) -> str:
        env_patterns = {
            "prod": r"(生产|prod|production)",
            "test": r"(测试|test)",
            "dev": r"(开发|dev)",
            "staging": r"(预发|staging|stage)",
        }
        for env, pattern in env_patterns.items():
            if re.search(pattern, text, flags=re.IGNORECASE):
                return env
        return self.default_env
