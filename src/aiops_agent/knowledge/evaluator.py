from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from aiops_agent.config import LLMProviderConfig


@dataclass
class EvalResult:
    faithfulness: float
    relevance: float

    @property
    def confidence(self) -> float:
        return round((self.faithfulness + self.relevance) / 2, 4)


class RAGEvaluator:
    MAX_TOKENS = 256

    def __init__(self, llm_config: LLMProviderConfig):
        self.llm_config = llm_config

    def evaluate(self, question: str, answer: str, docs: list[Document]) -> EvalResult:
        context = "\n\n".join(
            doc.page_content[:600] for doc in docs[:5]
        )
        faithfulness = self._check_faithfulness(answer, context)
        relevance = self._check_relevance(question, answer)
        return EvalResult(faithfulness=faithfulness, relevance=relevance)

    def _check_faithfulness(self, answer: str, context: str) -> float:
        prompt = ChatPromptTemplate.from_messages([
            ("system", (
                "你是一个 RAG 评估助手。判断以下回答中的主要陈述是否都能在参考文档中找到依据。\n"
                "只输出 0.0 到 1.0 之间的数字，不要输出其他内容。\n"
                "1.0 = 所有陈述均有文档支撑；0.0 = 主要陈述无文档支撑。"
            )),
            ("human", "参考文档：\n{context}\n\n回答：\n{answer}\n\n忠实度评分（0.0~1.0）："),
        ])
        return self._score(prompt, {"context": context, "answer": answer})

    def _check_relevance(self, question: str, answer: str) -> float:
        prompt = ChatPromptTemplate.from_messages([
            ("system", (
                "你是一个 RAG 评估助手。判断以下回答是否直接回答了给定问题。\n"
                "只输出 0.0 到 1.0 之间的数字，不要输出其他内容。\n"
                "1.0 = 完全回答了问题；0.0 = 完全没有回答问题。"
            )),
            ("human", "问题：{question}\n\n回答：\n{answer}\n\n相关性评分（0.0~1.0）："),
        ])
        return self._score(prompt, {"question": question, "answer": answer})

    def _score(self, prompt: ChatPromptTemplate, inputs: dict) -> float:
        model = self._build_model()
        chain = prompt | model | StrOutputParser()
        try:
            raw = chain.invoke(inputs).strip()
            return max(0.0, min(1.0, float(raw)))
        except Exception:
            return 0.5

    def _build_model(self):
        config = self.llm_config
        model_name = config.role_models.get("knowledge", config.model)

        if config.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            kwargs: dict[str, Any] = {
                "model": model_name,
                "timeout": config.timeout_seconds,
                "max_retries": config.max_retries,
                "temperature": 0.0,
                "max_tokens": self.MAX_TOKENS,
                "anthropic_api_key": config.api_key,
            }
            if config.base_url:
                kwargs["base_url"] = config.base_url
            if config.api_version:
                kwargs["default_headers"] = {"anthropic-version": config.api_version}
            return ChatAnthropic(**kwargs)

        if config.provider == "openai":
            from langchain_openai import ChatOpenAI
            kwargs = {
                "model": model_name,
                "timeout": config.timeout_seconds,
                "max_retries": config.max_retries,
                "temperature": 0.0,
                "max_tokens": self.MAX_TOKENS,
                "api_key": config.api_key,
            }
            if config.base_url:
                kwargs["base_url"] = config.base_url
            return ChatOpenAI(**kwargs)

        raise ValueError(f"Unsupported LLM provider for evaluation: {config.provider}")
