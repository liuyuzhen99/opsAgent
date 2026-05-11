from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig

from aiops_agent.tools.knowledge import KnowledgeAnswer, KnowledgeSource


class KnowledgeRetriever:
    TOP_K = 5
    SYNTHESIS_MAX_TOKENS = 2048

    def __init__(self, config: KnowledgeConfig, llm_config: LLMProviderConfig):
        self.config = config
        self.llm_config = llm_config

    def retrieve_keyword(self, question: str, bm25, docs: list[Document]) -> list[Document]:
        if not docs:
            return []
        tokens = re.findall(r"\w+", question.lower())
        if not tokens:
            return docs[: self.TOP_K]
        scores = bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [docs[i] for i, score in ranked[: self.TOP_K] if score > 0] or docs[: self.TOP_K]

    def retrieve_vector(self, question: str, db) -> list[Document]:
        return db.similarity_search(question, k=self.TOP_K)

    def retrieve_hybrid(
        self, question: str, bm25, bm25_docs: list[Document], vector_db
    ) -> list[Document]:
        kw = self.retrieve_keyword(question, bm25, bm25_docs)
        vec = self.retrieve_vector(question, vector_db)
        return self._rrf_merge([kw, vec], k=60)

    def _rrf_merge(self, lists: list[list[Document]], k: int = 60) -> list[Document]:
        scores: dict[str, float] = {}
        identity: dict[str, Document] = {}
        for doc_list in lists:
            for rank, doc in enumerate(doc_list):
                key = "{}::{}".format(
                    doc.metadata.get("source", ""),
                    doc.metadata.get("chunk_index", 0),
                )
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
                identity[key] = doc
        ranked = sorted(scores, key=lambda x: scores[x], reverse=True)
        return [identity[key] for key in ranked[: self.TOP_K]]

    def rewrite_query(self, question: str, history: list[dict]) -> str:
        if not history:
            return question
        history_text = "\n".join(
            f"Q: {turn.get('question', '')}\nA: {turn.get('answer', '')}"
            for turn in history[-3:]
        )
        prompt = ChatPromptTemplate.from_messages([
            ("system", (
                "你是一个查询改写助手。根据以下对话历史，将用户的追问改写为一个完整、独立的问句，"
                "使其在没有历史上下文的情况下也能被理解。只输出改写后的问句，不要解释。"
            )),
            ("human", "对话历史：\n{history}\n\n追问：{question}\n\n改写后的独立问句："),
        ])
        model = self._build_synthesis_model()
        chain = prompt | model | StrOutputParser()
        try:
            rewritten = chain.invoke({"history": history_text, "question": question})
            return rewritten.strip() or question
        except Exception:
            return question

    def synthesize(self, question: str, docs: list[Document]) -> KnowledgeAnswer:
        return self.synthesize_with_history(question, docs, history=[])

    def synthesize_with_history(
        self, question: str, docs: list[Document], history: list[dict]
    ) -> KnowledgeAnswer:
        if not docs:
            return KnowledgeAnswer(
                answer="知识库中未找到相关文档，请确认 vault_path 配置并执行索引重建。",
                confidence=0.0,
            )

        context = "\n\n".join(
            f"[{i + 1}] {doc.metadata.get('title', doc.metadata.get('rel_path', ''))}:\n{doc.page_content}"
            for i, doc in enumerate(docs)
        )

        history_block = ""
        if history:
            lines = ["已有对话历史："]
            for turn in history[-5:]:
                lines.append(f"Q: {turn.get('question', '')}")
                lines.append(f"A: {turn.get('answer', '')}")
            history_block = "\n".join(lines) + "\n\n"

        model = self._build_synthesis_model()
        prompt = ChatPromptTemplate.from_messages([
            ("system", (
                "你是一名经验丰富的运维助手。根据以下知识库文档片段回答用户的运维问题。\n"
                "要求：\n"
                "1. 优先引用文档中的具体步骤和操作指令\n"
                "2. 如果文档内容不足以完整回答问题，明确说明\n"
                "3. 使用中文回答，格式清晰"
            )),
            ("human", "{history}知识库文档：\n{context}\n\n问题：{question}"),
        ])
        chain = prompt | model | StrOutputParser()

        try:
            answer_text = chain.invoke({
                "history": history_block,
                "context": context,
                "question": question,
            })
        except Exception as exc:
            return KnowledgeAnswer(
                answer=f"LLM 合成失败：{exc}",
                confidence=0.0,
            )

        sources = [
            KnowledgeSource(
                title=str(doc.metadata.get("title", doc.metadata.get("rel_path", ""))),
                path=str(doc.metadata.get("source", "")),
                section=str(doc.metadata.get("rel_path", "")),
                matched_text=doc.page_content[:200],
            )
            for doc in docs
        ]
        return KnowledgeAnswer(
            answer=answer_text,
            sources=sources,
            confidence=1.0 if docs else 0.0,
        )

    def _build_synthesis_model(self):
        config = self.llm_config
        model_name = config.role_models.get("knowledge", config.model)

        if config.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            kwargs: dict[str, Any] = {
                "model": model_name,
                "timeout": config.timeout_seconds,
                "max_retries": config.max_retries,
                "temperature": config.temperature,
                "max_tokens": self.SYNTHESIS_MAX_TOKENS,
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
                "temperature": config.temperature,
                "max_tokens": self.SYNTHESIS_MAX_TOKENS,
                "api_key": config.api_key,
            }
            if config.base_url:
                kwargs["base_url"] = config.base_url
            return ChatOpenAI(**kwargs)

        raise ValueError(f"Unsupported LLM provider for knowledge synthesis: {config.provider}")
