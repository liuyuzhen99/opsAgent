from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig
from aiops_agent.knowledge.tokenizer import tokenize_knowledge_text

from aiops_agent.tools.knowledge import KnowledgeAnswer, KnowledgeSource


class KnowledgeRetriever:
    TOP_K = 5
    RETRIEVAL_CANDIDATES = 15
    SYNTHESIS_SOURCE_LIMIT = 4
    SYNTHESIS_SOURCE_MAX_CHARS = 12000
    SYNTHESIS_MAX_TOKENS = 4096

    def __init__(self, config: KnowledgeConfig, llm_config: LLMProviderConfig):
        self.config = config
        self.llm_config = llm_config

    def retrieve_keyword(self, question: str, bm25, docs: list[Document]) -> list[Document]:
        if not docs:
            return []
        tokens = tokenize_knowledge_text(question)
        if not tokens:
            return self._prefer_concrete_docs(docs[: self.RETRIEVAL_CANDIDATES])[: self.TOP_K]
        scores = bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        candidates = [docs[i] for i, score in ranked[: self.RETRIEVAL_CANDIDATES] if score > 0]
        return self._prefer_concrete_docs(candidates or docs[: self.RETRIEVAL_CANDIDATES])[: self.TOP_K]

    def retrieve_vector(self, question: str, db) -> list[Document]:
        docs = db.similarity_search(question, k=self.RETRIEVAL_CANDIDATES)
        return self._prefer_concrete_docs(docs)[: self.TOP_K]

    def retrieve_hybrid(
        self, question: str, bm25, bm25_docs: list[Document], vector_db
    ) -> list[Document]:
        kw = self.retrieve_keyword(question, bm25, bm25_docs)
        vec = self.retrieve_vector(question, vector_db)
        return self._prefer_concrete_docs(self._rrf_merge([kw, vec], k=60, limit=self.RETRIEVAL_CANDIDATES))[: self.TOP_K]

    def _rrf_merge(self, lists: list[list[Document]], k: int = 60, limit: int | None = None) -> list[Document]:
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
        return [identity[key] for key in ranked[: limit or self.TOP_K]]

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

        synthesis_docs = self._prepare_synthesis_docs(docs)
        context = "\n\n".join(
            f"[{i + 1}] {doc.metadata.get('title', doc.metadata.get('rel_path', ''))}:\n{doc.page_content}"
            for i, doc in enumerate(synthesis_docs)
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
                "你是一名经验丰富的运维助手。根据以下知识库文档回答用户的运维问题。\n"
                "要求：\n"
                "1. 优先引用文档中的具体步骤和操作指令\n"
                "2. 文档包含 SQL、Shell、配置或代码块时，必须完整保留相关代码块，不要只摘要 SELECT 字段或 ORDER BY 片段\n"
                "3. 如果文档内容不足以完整回答问题，明确说明\n"
                "4. 使用中文回答，格式清晰"
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
            for doc in synthesis_docs
        ]
        return KnowledgeAnswer(
            answer=answer_text,
            sources=sources,
            confidence=1.0 if synthesis_docs else 0.0,
        )

    def _prefer_concrete_docs(self, docs: list[Document]) -> list[Document]:
        concrete = [doc for doc in docs if not self._is_reference_doc(doc)]
        return concrete or docs

    def _prepare_synthesis_docs(self, docs: list[Document]) -> list[Document]:
        selected = self._prefer_concrete_docs(docs)
        hydrated: list[Document] = []
        seen_sources: set[str] = set()
        for doc in selected:
            source = str(doc.metadata.get("source", ""))
            identity = source or str(doc.metadata.get("rel_path", "")) or str(id(doc))
            if identity in seen_sources:
                continue
            seen_sources.add(identity)
            hydrated.append(self._hydrate_source_doc(doc))
            if len(hydrated) >= self.SYNTHESIS_SOURCE_LIMIT:
                break
        return hydrated

    def _hydrate_source_doc(self, doc: Document) -> Document:
        source = str(doc.metadata.get("source", ""))
        if not source:
            return doc
        path = Path(source)
        if not path.exists() or not path.is_file():
            return doc
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return doc
        content = self._strip_frontmatter(raw).strip()
        if not content:
            return doc
        metadata = dict(doc.metadata)
        metadata["hydrated_source"] = True
        return Document(
            page_content=content[: self.SYNTHESIS_SOURCE_MAX_CHARS],
            metadata=metadata,
        )

    def _is_reference_doc(self, doc: Document) -> bool:
        rel_path = str(doc.metadata.get("rel_path", ""))
        name = Path(rel_path).name if rel_path else Path(str(doc.metadata.get("source", ""))).name
        if str(doc.metadata.get("is_moc", "")).lower() == "true":
            return True
        if name.lower() == "readme.md" or name.endswith("MOC.md"):
            return True
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in self.config.moc_patterns)

    @staticmethod
    def _strip_frontmatter(raw: str) -> str:
        if raw.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n?(.*)", raw, re.DOTALL)
            if match:
                return match.group(1)
        return raw

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
