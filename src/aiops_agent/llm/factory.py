from __future__ import annotations

from aiops_agent.config import LLMProviderConfig
from aiops_agent.llm.base import BaseLLMProvider
from aiops_agent.llm.langchain_provider import LangChainLLMProvider


def create_llm_provider(config: LLMProviderConfig) -> BaseLLMProvider:
    if config.provider not in {"anthropic", "openai"}:
        raise ValueError(f"Unsupported LLM provider: {config.provider}")
    return LangChainLLMProvider(config)
