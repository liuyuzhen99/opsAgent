from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from aiops_agent.config import LLMProviderConfig
from aiops_agent.llm.base import BaseLLMProvider, IntentClassification, LLMError, PlannedTask
from aiops_agent.support.logging import log_kv


class LangChainLLMProvider(BaseLLMProvider):
    SUPPORTED_INTENTS = {"inspection", "permission_change", "ops_qa", "web_action"}

    def __init__(self, config: LLMProviderConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def classify_intent(
        self, text: str, defaults: dict[str, str]
    ) -> IntentClassification:
        if not self.enabled:
            raise LLMError("LLM parsing disabled")

        prompt = (
            "Parse the enterprise AIOps request into JSON.\n"
            "Schema: {\"intent\": \"inspection|permission_change|ops_qa|web_action\", \"entities\": {...}}\n"
            f"text: {text}\n"
            f"default_system: {defaults['system']}\n"
            f"default_env: {defaults['env']}\n"
            "Return JSON only."
        )
        raw_text = self._invoke_json("intent", prompt)
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned invalid JSON for intent classification") from exc

        intent = parsed.get("intent")
        entities = parsed.get("entities")
        if intent not in self.SUPPORTED_INTENTS:
            raise LLMError("LLM returned unsupported intent")
        if not isinstance(entities, dict):
            raise LLMError("LLM returned invalid entities")

        return IntentClassification(
            intent=intent,
            entities=entities,
            provider=self.config.provider,
            model=self._resolve_model("intent"),
            request_id=None,
        )

    def plan_task(
        self, text: str, intent: str, entities: dict[str, Any]
    ) -> PlannedTask:
        if not self.enabled:
            raise LLMError("LLM planning disabled")

        prompt = (
            "Plan the enterprise AIOps task into JSON.\n"
            "Schema: {\"goal\": str, \"steps\": [str], \"risk_level\": \"read_only|controlled_change|high_risk_change\", "
            "\"confirmation_required\": bool}\n"
            f"text: {text}\n"
            f"intent: {intent}\n"
            f"entities: {json.dumps(entities, ensure_ascii=False)}\n"
            "Return JSON only."
        )
        raw_text = self._invoke_json("planning", prompt)
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned invalid JSON for planning") from exc

        return PlannedTask(
            goal=str(parsed.get("goal") or ""),
            steps=[str(step) for step in parsed.get("steps", []) if str(step).strip()],
            risk_level=str(parsed.get("risk_level") or "read_only"),
            confirmation_required=bool(parsed.get("confirmation_required")),
        )

    def _invoke_json(self, role: str, prompt: str) -> str:
        model = self._build_model(role)
        try:
            response = model.invoke(
                [
                    SystemMessage(content="You are a structured enterprise AIOps assistant. Return JSON only."),
                    HumanMessage(content=prompt),
                ]
            )
        except Exception as exc:  # pragma: no cover - network and SDK errors vary.
            raise LLMError(f"LLM request failed: {exc}") from exc

        raw_text = getattr(response, "content", "")
        if isinstance(raw_text, list):
            fragments: list[str] = []
            for item in raw_text:
                if isinstance(item, dict) and item.get("type") == "text":
                    fragments.append(str(item.get("text", "")))
                elif hasattr(item, "text"):
                    fragments.append(str(getattr(item, "text")))
            raw_text = "".join(fragments)

        raw_text = str(raw_text).strip()
        if not raw_text:
            raise LLMError("LLM returned empty content")

        log_kv(self.logger, logging.INFO, "LangChain model invocation succeeded", role=role, model=self._resolve_model(role))
        return raw_text

    def _build_model(self, role: str):
        model_name = self._resolve_model(role)
        if self.config.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            kwargs: dict[str, Any] = {
                "model": model_name,
                "timeout": self.config.timeout_seconds,
                "max_retries": self.config.max_retries,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "anthropic_api_key": self.config.api_key,
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            if self.config.api_version:
                kwargs["default_headers"] = {"anthropic-version": self.config.api_version}
            return ChatAnthropic(**kwargs)

        if self.config.provider == "openai":
            from langchain_openai import ChatOpenAI

            kwargs = {
                "model": model_name,
                "timeout": self.config.timeout_seconds,
                "max_retries": self.config.max_retries,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "api_key": self.config.api_key,
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            return ChatOpenAI(**kwargs)

        raise LLMError(f"Unsupported LLM provider: {self.config.provider}")

    def _resolve_model(self, role: str) -> str:
        return self.config.role_models.get(role, self.config.model)
