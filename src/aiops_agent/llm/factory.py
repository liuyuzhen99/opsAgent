from __future__ import annotations

from aiops_agent.config import AnthropicConfig
from aiops_agent.llm.anthropic_provider import AnthropicLLMProvider
from aiops_agent.llm.base import BaseLLMProvider


def create_llm_provider(config: AnthropicConfig) -> BaseLLMProvider:
    if config.provider != "anthropic":
        raise ValueError(f"Unsupported LLM provider: {config.provider}")
    return AnthropicLLMProvider(config)
