from __future__ import annotations

import contextlib
import gc
import logging
import time
from pathlib import Path

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig
from aiops_agent.knowledge.indexer import VaultIndexer
from aiops_agent.knowledge.retriever import KnowledgeRetriever
from aiops_agent.support.logging import log_kv
from aiops_agent.tools.knowledge import KnowledgeAnswer

logger = logging.getLogger(__name__)


class KnowledgeEngine:
    VECTOR_RETRY_ATTEMPTS = 3
    VECTOR_RETRY_DELAY_SECONDS = 0.4

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

        rewritten = self.rewrite_query(question, history)
        docs = self.retrieve_documents(rewritten)
        answer = self.synthesize_answer(rewritten, docs, history)
        self.evaluate_answer(rewritten, answer, docs)

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

    def rewrite_query(self, question: str, conversation_history: list[dict] | None = None) -> str:
        return self._retriever.rewrite_query(question, conversation_history or [])

    def retrieve_documents(self, question: str):
        if self.config.index_mode == "hybrid":
            bm25, bm25_docs = self._get_bm25()
            docs = self._retrieve_hybrid_with_retry(question, bm25, bm25_docs)
        elif self.config.index_mode == "vector":
            docs = self._retrieve_vector_with_retry(question)
        else:
            bm25, bm25_docs = self._get_bm25()
            docs = self._retriever.retrieve_keyword(question, bm25, bm25_docs)
        return self._expand_graph_context(question, docs)

    def synthesize_answer(self, question: str, docs, conversation_history: list[dict] | None = None) -> KnowledgeAnswer:
        return self._retriever.synthesize_with_history(question, docs, conversation_history or [])

    def evaluate_answer(self, question: str, answer: KnowledgeAnswer, docs) -> KnowledgeAnswer:
        if self.config.enable_eval and docs:
            from aiops_agent.knowledge.evaluator import RAGEvaluator
            evaluator = RAGEvaluator(self.llm_config)
            eval_result = evaluator.evaluate(question, answer.answer, docs)
            answer.confidence = eval_result.confidence
            answer.evaluation = {
                "faithfulness": eval_result.faithfulness,
                "relevance": eval_result.relevance,
            }
        return answer

    def rebuild_index(self, force: bool = False) -> None:
        if self.config.index_mode == "vector":
            if force or self._indexer.is_vector_stale():
                self._close_vector_db()
                self._vector_db = self._build_vector_with_retries()
            else:
                self._vector_db = self._load_vector_with_retries()
        elif self.config.index_mode == "hybrid":
            self._bm25, self._bm25_docs = self._indexer.build_keyword()
            if force or self._indexer.is_vector_stale():
                self._close_vector_db()
                self._vector_db = self._build_vector_with_retries()
            else:
                self._vector_db = self._load_vector_with_retries()
        else:
            self._bm25, self._bm25_docs = self._indexer.build_keyword()

    def invalidate_cache(self) -> None:
        self._close_vector_db()
        self._bm25 = None
        self._bm25_docs = None

    def reindex_after_write(self) -> str:
        self.invalidate_cache()
        if not self.config.auto_reindex_after_write:
            return "skipped"
        if self.config.index_mode in {"vector", "hybrid"}:
            self.rebuild_index(force=True)
            # Keep the persisted hybrid index, but release the SQLite handle in
            # the long-lived chat process. The next query will load it lazily.
            self._close_vector_db()
            return "rebuilt"
        self.rebuild_index(force=True)
        return "cache_refreshed"

    # ------------------------------------------------------------------

    def _get_bm25(self):
        if self._bm25 is None:
            self._bm25, self._bm25_docs = self._indexer.build_keyword()
        return self._bm25, self._bm25_docs

    def _get_vector_db(self):
        if self._vector_db is None:
            chroma_dir = Path(self.config.vault_path) / ".chroma"
            if chroma_dir.exists() and not self._indexer.is_vector_stale():
                self._vector_db = self._load_vector_with_retries()
            else:
                self._close_vector_db()
                self._vector_db = self._build_vector_with_retries()
        return self._vector_db

    def _get_hybrid(self):
        bm25, bm25_docs = self._get_bm25()
        vector_db = self._get_vector_db()
        return bm25, bm25_docs, vector_db

    def _expand_graph_context(self, question: str, docs):
        linked_docs = self._indexer.expand_outlinks(docs)
        if not linked_docs:
            return docs
        return self._retriever.merge_graph_results(question, docs, linked_docs)

    def _retrieve_hybrid_with_retry(self, question: str, bm25, bm25_docs):
        return self._run_vector_operation_with_retries(
            "retrieve_hybrid",
            lambda: self._retriever.retrieve_hybrid(question, bm25, bm25_docs, self._get_vector_db()),
        )

    def _retrieve_vector_with_retry(self, question: str):
        return self._run_vector_operation_with_retries(
            "retrieve_vector",
            lambda: self._retriever.retrieve_vector(question, self._get_vector_db()),
        )

    def _build_vector_with_retries(self):
        return self._run_vector_operation_with_retries("build_vector", self._indexer.build_vector)

    def _load_vector_with_retries(self):
        return self._run_vector_operation_with_retries("load_vector", self._indexer.load_vector)

    def _run_vector_operation_with_retries(self, operation: str, func):
        last_error: Exception | None = None
        for attempt in range(1, self.VECTOR_RETRY_ATTEMPTS + 1):
            try:
                return func()
            except Exception as exc:
                last_error = exc
                if not self._is_vector_lock_error(exc) or attempt >= self.VECTOR_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "Chroma vector operation failed with a retryable SQLite error; retrying",
                    extra={
                        "operation": operation,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
                self._close_vector_db()
                time.sleep(self.VECTOR_RETRY_DELAY_SECONDS * attempt)
        assert last_error is not None
        raise last_error

    def _close_vector_db(self) -> None:
        vector_db = self._vector_db
        self._vector_db = None
        if vector_db is None:
            return
        with contextlib.suppress(Exception):
            vector_db.persist()
        client = getattr(vector_db, "_client", None)
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        del vector_db
        gc.collect()

    @staticmethod
    def _is_vector_lock_error(exc: Exception) -> bool:
        text = str(exc).lower()
        retryable_fragments = (
            "readonly database",
            "attempt to write a readonly database",
            "unable to open database file",
            "database is locked",
            "database table is locked",
            "sqlite",
            "query error: database error",
        )
        return any(fragment in text for fragment in retryable_fragments)
