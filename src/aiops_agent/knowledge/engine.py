from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig
from aiops_agent.knowledge.indexer import VaultIndexer
from aiops_agent.knowledge.retriever import KnowledgeRetriever
from aiops_agent.support.logging import log_kv
from aiops_agent.tools.knowledge import KnowledgeAnswer

logger = logging.getLogger(__name__)


class KnowledgeEngine:
    def __init__(self, config: KnowledgeConfig, llm_config: LLMProviderConfig):
        self.config = config
        self.llm_config = llm_config
        self._indexer = VaultIndexer(config)
        self._retriever = KnowledgeRetriever(config, llm_config)
        self._bm25 = None
        self._bm25_docs = None
        self._vector_db = None

    def query(self, question: str, conversation_history: list[dict] | None = None) -> KnowledgeAnswer:
        history = conversation_history or []
        t0 = time.monotonic()

        rewritten = self._retriever.rewrite_query(question, history)

        if self.config.index_mode == "hybrid":
            bm25, bm25_docs, vector_db = self._get_hybrid()
            docs = self._retriever.retrieve_hybrid(rewritten, bm25, bm25_docs, vector_db)
        elif self.config.index_mode == "vector":
            db = self._get_vector_db()
            docs = self._retriever.retrieve_vector(rewritten, db)
        else:
            bm25, bm25_docs = self._get_bm25()
            docs = self._retriever.retrieve_keyword(rewritten, bm25, bm25_docs)

        answer = self._retriever.synthesize_with_history(rewritten, docs, history)

        if self.config.enable_eval and docs:
            from aiops_agent.knowledge.evaluator import RAGEvaluator
            evaluator = RAGEvaluator(self.llm_config)
            eval_result = evaluator.evaluate(rewritten, answer.answer, docs)
            answer.confidence = eval_result.confidence
            answer.evaluation = {
                "faithfulness": eval_result.faithfulness,
                "relevance": eval_result.relevance,
            }

        latency_ms = int((time.monotonic() - t0) * 1000)
        log_kv(
            logger,
            logging.INFO,
            "knowledge query",
            query_original=question,
            query_rewritten=rewritten,
            retrieval_mode=self.config.index_mode,
            top_k_sources=[d.metadata.get("rel_path", "") for d in docs],
            faithfulness=answer.evaluation.get("faithfulness") if answer.evaluation else None,
            relevance=answer.evaluation.get("relevance") if answer.evaluation else None,
            latency_ms=latency_ms,
        )

        return answer

    def rebuild_index(self, force: bool = False) -> None:
        if self.config.index_mode == "vector":
            if force or self._indexer.is_vector_stale():
                self._vector_db = self._indexer.build_vector()
            else:
                self._vector_db = self._indexer.load_vector()
        elif self.config.index_mode == "hybrid":
            self._bm25, self._bm25_docs = self._indexer.build_keyword()
            if force or self._indexer.is_vector_stale():
                self._vector_db = self._indexer.build_vector()
            else:
                self._vector_db = self._indexer.load_vector()
        else:
            self._bm25, self._bm25_docs = self._indexer.build_keyword()

    # ------------------------------------------------------------------

    def _get_bm25(self):
        if self._bm25 is None:
            self._bm25, self._bm25_docs = self._indexer.build_keyword()
        return self._bm25, self._bm25_docs

    def _get_vector_db(self):
        if self._vector_db is None:
            chroma_dir = Path(self.config.vault_path) / ".chroma"
            if chroma_dir.exists() and not self._indexer.is_vector_stale():
                self._vector_db = self._indexer.load_vector()
            else:
                self._vector_db = self._indexer.build_vector()
        return self._vector_db

    def _get_hybrid(self):
        bm25, bm25_docs = self._get_bm25()
        vector_db = self._get_vector_db()
        return bm25, bm25_docs, vector_db
