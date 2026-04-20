from __future__ import annotations

import json
import logging
from typing import Any

from aiops_agent.config import AnthropicConfig
from aiops_agent.llm.base import BaseLLMProvider, IntentClassification, LLMError
from aiops_agent.support.logging import log_kv


class AnthropicLLMProvider(BaseLLMProvider):
    def __init__(self, config: AnthropicConfig):
        self.config = config
        self._client = None
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def classify_intent(
        self, text: str, defaults: dict[str, str]
    ) -> IntentClassification:
        if not self.enabled:
            raise LLMError("Anthropic parsing disabled")
        if not self.config.api_key:
            raise LLMError("Anthropic API key missing")
        if not self.config.model:
            raise LLMError("Anthropic model missing")

        prompt = self._build_prompt(text, defaults)
        client = self._get_client()
        try:
            message = client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=(
                    "You are an intent parser for an enterprise AIOps agent. "
                    "Return only valid JSON with keys intent and entities. "
                    "intent must be one of inspection, permission_change, ops_qa. "
                    "entities must be an object. "
                    "When system or env is missing, fill them with the defaults provided by the user prompt."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # pragma: no cover - SDK exception mapping varies by version.
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        log_kv(self.logger, logging.INFO, "Anthropic intent classification succeeded", model=self.config.model)

        raw_text = self._extract_text_content(message)
        if not raw_text:
            raise LLMError("Anthropic returned empty content")

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LLMError("Anthropic returned invalid JSON") from exc

        intent = parsed.get("intent")
        entities = parsed.get("entities")
        if intent not in {"inspection", "permission_change", "ops_qa"}:
            raise LLMError("Anthropic returned unsupported intent")
        if not isinstance(entities, dict):
            raise LLMError("Anthropic returned invalid entities")

        return IntentClassification(
            intent=intent,
            entities=entities,
            provider="anthropic",
            model=self.config.model,
            request_id=getattr(message, "id", None),
        )

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on local environment.
            raise LLMError(
                "Anthropic SDK 未安装，请先安装 `anthropic` 依赖。"
            ) from exc

        client_kwargs: dict[str, Any] = {
            "api_key": self.config.api_key,
            "max_retries": self.config.max_retries,
            "timeout": self.config.timeout_seconds,
        }
        if self.config.base_url:
            client_kwargs["base_url"] = self.config.base_url
        if self.config.default_headers:
            client_kwargs["default_headers"] = self.config.default_headers

        self._client = anthropic.Anthropic(**client_kwargs)
        return self._client

    def _build_prompt(self, text: str, defaults: dict[str, str]) -> str:
        return (
            "Parse the following enterprise operations request into JSON.\n"
            "Schema: {\"intent\": \"inspection|permission_change|ops_qa\", \"entities\": {...}}\n"
            f"text: {text}\n"
            f"default_system: {defaults['system']}\n"
            f"default_env: {defaults['env']}\n"
            "Return JSON only."
        )

    def _extract_text_content(self, message: Any) -> str:
        content_blocks = getattr(message, "content", [])
        fragments: list[str] = []
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text":
                    fragments.append(block.get("text", ""))
                continue
            if block_type == "text":
                fragments.append(getattr(block, "text", ""))
        return "".join(fragment for fragment in fragments if fragment).strip()
