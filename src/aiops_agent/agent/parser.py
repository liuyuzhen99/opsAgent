from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aiops_agent.config import RPAConfig
from aiops_agent.llm.base import BaseLLMProvider, LLMError


@dataclass(slots=True)
class IntentResult:
    intent: str
    entities: dict[str, Any]


class IntentParser:
    INSPECTION_KEYWORDS = ("巡检", "检查", "inspect", "inspection")
    PERMISSION_KEYWORDS = ("权限", "授权", "permission", "grant")
    QA_KEYWORDS = ("怎么", "如何", "why", "what", "知识库", "sop")

    def __init__(
        self,
        rpa_config: RPAConfig | None = None,
        llm_provider: BaseLLMProvider | None = None,
    ):
        inspection_defaults = (rpa_config or RPAConfig()).inspection
        self.default_system = inspection_defaults.default_system
        self.default_env = inspection_defaults.default_env
        self.llm_provider = llm_provider

    def parse(self, text: str) -> IntentResult:
        normalized = text.strip()
        llm_result = self._parse_with_llm(normalized)
        if llm_result is not None:
            return llm_result

        return self._parse_with_rules(normalized)

    def _parse_with_llm(self, text: str) -> IntentResult | None:
        if self.llm_provider is None:
            return None

        try:
            parsed = self.llm_provider.classify_intent(
                text,
                defaults={"system": self.default_system, "env": self.default_env},
            )
        except LLMError:
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

        if any(keyword in lowered for keyword in self.PERMISSION_KEYWORDS):
            return IntentResult(
                intent="permission_change",
                entities={"raw_text": normalized},
            )

        return IntentResult(intent="ops_qa", entities={"raw_text": normalized})

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
