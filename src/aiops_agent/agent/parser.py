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
    RPA_ACTION_KEYWORDS = ("ssh", "sftp", "数据库", "db", "pl/sql", "plsql", "服务器")
    RPA_LOGIN_VERBS = ("登录", "打开", "连接", "进入", "login", "open", "connect")
    PERMISSION_KEYWORDS = ("权限", "授权", "permission", "grant")
    KNOWLEDGE_WRITE_KEYWORDS = (
        "记录到知识库",
        "保存到知识库",
        "添加入知识库",
        "添加到知识库",
        "加入知识库",
        "沉淀文档",
        "写入知识库",
        "录入知识库",
        "写入 vault",
        "写入vault",
        "整理成知识库",
        "整理到知识库",
        "生成知识库",
        "生成 knowledge",
        "生成knowledge",
        "整理成 knowledge",
        "整理成knowledge",
        "知识沉淀",
    )
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
        explicit_write = self._parse_explicit_knowledge_write(normalized)
        if explicit_write is not None:
            return explicit_write
        explicit_rpa_action = self._parse_explicit_rpa_action(normalized)
        if explicit_rpa_action is not None:
            return explicit_rpa_action
        explicit_web_action = self._parse_explicit_web_action(normalized)
        if explicit_web_action is not None:
            return explicit_web_action

        llm_result = self._parse_with_llm(normalized)
        if llm_result is not None:
            return llm_result

        rule_result = self._parse_with_rules(normalized)
        if self.last_llm_error:
            rule_result.entities["llm_fallback_used"] = True
            rule_result.entities["llm_fallback_error"] = self.last_llm_error
        return rule_result

    def _parse_explicit_knowledge_write(self, normalized: str) -> IntentResult | None:
        lowered = normalized.lower()
        if not any(keyword in lowered for keyword in self.KNOWLEDGE_WRITE_KEYWORDS):
            return None
        return IntentResult(
            intent="knowledge_write",
            entities={
                "system": self._extract_system(normalized, use_default=False),
                "env": self._extract_env(normalized),
                "raw_text": normalized,
                "instruction": normalized,
                "explicit_trigger": True,
            },
        )

    def _parse_explicit_rpa_action(self, normalized: str) -> IntentResult | None:
        lowered = normalized.lower()
        has_rpa_keyword = any(keyword in lowered for keyword in self.RPA_ACTION_KEYWORDS)
        has_login_verb = any(keyword in lowered for keyword in self.RPA_LOGIN_VERBS)
        has_explicit_login_command = any(keyword in lowered for keyword in ("登录", "打开", "进入", "login", "open"))
        target = self._extract_rpa_target(normalized)
        capability = self._extract_rpa_capability(normalized)
        if self._extract_url(normalized) and not (capability or "服务器" in lowered):
            return None
        if not (
            (capability and (has_explicit_login_command or target))
            or ("服务器" in lowered and has_explicit_login_command)
            or (target and has_login_verb)
        ):
            return None
        if not has_rpa_keyword and not target:
            return None
        return IntentResult(
            intent="rpa_action",
            entities={
                "target": target,
                "capability": capability or "ssh",
                "operation": "login",
                "raw_text": normalized,
            },
        )

    def _parse_explicit_web_action(self, normalized: str) -> IntentResult | None:
        lowered = normalized.lower()
        if not any(keyword in lowered for keyword in ("网页", "浏览器", "网站", "页面自动化", "http://", "https://")):
            return None
        return self._web_action_result(normalized, lowered)

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
        parsed_intent = parsed.intent
        if parsed_intent == "rpa_action" and self._looks_like_browser_workflow(text):
            parsed_intent = "web_action"
        if parsed_intent == "knowledge_write":
            entities.setdefault("system", None)
        elif parsed_intent == "rpa_action":
            entities.setdefault("target", self._extract_rpa_target(text))
            entities.setdefault("capability", self._extract_rpa_capability(text) or "ssh")
            entities.setdefault("operation", "login")
        elif parsed_intent == "web_action":
            entities = self.enrich_web_action_entities(text, entities)
        else:
            entities.setdefault("system", self.default_system)
        entities.setdefault("env", self.default_env)
        entities["raw_text"] = text
        entities["llm_provider"] = parsed.provider
        entities["llm_model"] = parsed.model
        if parsed.request_id:
            entities["llm_request_id"] = parsed.request_id
        return IntentResult(intent=parsed_intent, entities=entities)

    def enrich_web_action_entities(self, text: str, entities: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(entities)
        rule_entities = self._web_action_result(text, text.lower()).entities
        credential_refs = self._extract_credential_refs(text)
        enriched["credential_ref"] = enriched.get("credential_ref") or (credential_refs[0] if credential_refs else None)
        enriched["credential_refs"] = credential_refs or (
            [str(enriched["credential_ref"])] if enriched.get("credential_ref") else []
        )
        enriched["requires_login"] = bool(enriched.get("requires_login") or rule_entities["requires_login"])
        enriched["has_side_effect"] = bool(enriched.get("has_side_effect") or rule_entities["has_side_effect"])
        enriched["start_url"] = enriched.get("start_url") or rule_entities["start_url"]
        enriched["allowed_domains"] = list(enriched.get("allowed_domains") or rule_entities["allowed_domains"])
        enriched["workflow"] = enriched.get("workflow") or rule_entities["workflow"]
        workflow_fields = dict(rule_entities["workflow_fields"])
        workflow_fields.update(enriched.get("workflow_fields") or {})
        enriched["workflow_fields"] = workflow_fields
        return enriched

    def _looks_like_browser_workflow(self, text: str) -> bool:
        lowered = text.lower()
        if self._extract_url(text):
            return True
        return any(cue in lowered for cue in ("侧边栏", "依次点击", "点击按钮", "网页", "网站", "浏览器"))

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
            return self._web_action_result(normalized, lowered)

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

    def _web_action_result(self, normalized: str, lowered: str) -> IntentResult:
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
                "credential_ref": self._extract_credential_ref(normalized),
                "credential_refs": self._extract_credential_refs(normalized),
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
                        "复核",
                        "审核",
                        "submit",
                        "save",
                        "delete",
                        "create",
                        "grant",
                        "approve",
                    )
                ),
                "workflow": self._extract_web_workflow(normalized),
                "workflow_fields": self._extract_workflow_fields(normalized),
            },
        )

    def _extract_url(self, text: str) -> str | None:
        match = re.search(r"https?://[^\s，。；,;]+", text, flags=re.IGNORECASE)
        if match:
            return match.group(0).rstrip("。,.，")
        return None

    def _extract_credential_ref(self, text: str) -> str | None:
        refs = self._extract_credential_refs(text)
        return refs[0] if refs else None

    def _extract_credential_refs(self, text: str) -> list[str]:
        explicit = re.search(
            r"(?:credential[_ -]?ref|凭据(?:引用)?|credential)\s*[:：=]\s*([A-Za-z0-9][A-Za-z0-9_.@:-]*)",
            text,
            flags=re.IGNORECASE,
        )
        if explicit:
            return [explicit.group(1)]
        return [
            match.group(1)
            for match in re.finditer(
                r"(?:使用|用)\s*([A-Za-z0-9][A-Za-z0-9_.@:-]*)\s*(?:登录|登陆|访问|打开|进入|login|visit|open)",
                text,
                flags=re.IGNORECASE,
            )
        ]

    def _extract_rpa_target(self, text: str) -> str | None:
        match = re.search(r"(?<![0-9.])\d{2,3}(?:\.\d{1,3}){1,3}(?![0-9.])", text)
        if match:
            return match.group(0)
        return None

    def _extract_rpa_capability(self, text: str) -> str | None:
        lowered = text.lower()
        if "sftp" in lowered:
            return "sftp"
        if "ssh" in lowered:
            return "ssh"
        if (
            any(keyword in lowered for keyword in ("数据库", "pl/sql", "plsql", "sql"))
            or re.search(r"(?<![a-z0-9])db(?![a-z0-9])", lowered)
        ):
            return "db"
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
            r"(?:登录名称|登录名|登陆名称|登陆名|login name|login_name|login)\s*(?:字段)?(?:中)?\s*(?:填入|输入|填写|为|叫|是|:|：)?\s*[\"'“”]?([A-Za-z0-9_.@-]{2,})",
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
        department = re.search(
            r"(?:所属单位编号|所属单位|单位编号|单位|部门|组织|机构|department)\s*"
            r"(?:字段)?(?:中)?\s*(?:填入|输入|填写|选择|为|是|:|：)?\s*[\"'“”]?"
            r"([A-Za-z0-9_.\u4e00-\u9fff-]{2,})",
            text,
            flags=re.IGNORECASE,
        )
        if department:
            fields["department"] = department.group(1)
        display_name = re.search(
            r"(?:用户名称|用户姓名|姓名|显示名|display_name|display name)\s*"
            r"(?:字段)?(?:中)?\s*(?:填入|输入|填写|为|是|:|：)?\s*[\"'“”]?"
            r"([A-Za-z0-9_.\u4e00-\u9fff-]{2,})",
            text,
            flags=re.IGNORECASE,
        )
        if display_name:
            fields["display_name"] = display_name.group(1)
        role_patterns = (
            r"(?<![A-Za-z0-9_.@-])(只读权限|管理员|普通用户|只读|readonly|read-only|admin|viewer)(?![A-Za-z0-9_.@-])",
            r"(?:角色|权限|role|permission)\s*(?:为|是|:|：)?\s*([\w\u4e00-\u9fff.-]{2,})",
        )
        for pattern in role_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                fields["role"] = match.group(1)
                break
        return fields

    def _extract_system(self, text: str, use_default: bool = True) -> str | None:
        known_systems = ("WebLogic", "Nginx", "Redis", "MySQL", "K8s", "Kafka", "财司系统")
        for system in known_systems:
            if system.lower() in text.lower():
                return system
        if "财司" in text:
            return "财司系统"
        return self.default_system if use_default else None

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
