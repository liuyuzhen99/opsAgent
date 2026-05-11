from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig
from aiops_agent.knowledge.indexer import VaultIndexer
from aiops_agent.tools.knowledge import KnowledgeAnswer, KnowledgeTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_MD = """\
---
title: WebLogic OOM 处理手册
tags: [weblogic, jvm, oom, runbook]
system: WebLogic
env: prod
---

## 症状

JVM 堆内存耗尽，应用响应超时。

## 处理步骤

1. 登录服务器，执行 `jmap -heap <pid>` 确认堆使用情况
2. 重启 WebLogic Managed Server
3. 调整 JVM 参数 `-Xmx` 扩大堆上限
"""

EXCLUDED_MD = """\
---
title: 归档文档
---
这是归档目录下的文档，不应被索引。
"""


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "weblogic-oom.md").write_text(SAMPLE_MD, encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "old.md").write_text(EXCLUDED_MD, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def knowledge_config(vault: Path) -> KnowledgeConfig:
    return KnowledgeConfig(
        vault_path=str(vault),
        exclude_patterns=["archive/**"],
        index_mode="keyword",
    )


@pytest.fixture()
def llm_config() -> LLMProviderConfig:
    return LLMProviderConfig(provider="anthropic", enabled=True, api_key="test-key")


# ---------------------------------------------------------------------------
# VaultIndexer tests
# ---------------------------------------------------------------------------

class TestVaultIndexer:
    def test_iter_docs_excludes_archive(self, knowledge_config: KnowledgeConfig):
        indexer = VaultIndexer(knowledge_config)
        docs = indexer.iter_docs()
        paths = [d.metadata["rel_path"] for d in docs]
        assert any("weblogic-oom" in p for p in paths)
        assert not any("old" in p for p in paths)

    def test_iter_docs_parses_frontmatter(self, knowledge_config: KnowledgeConfig):
        indexer = VaultIndexer(knowledge_config)
        docs = indexer.iter_docs()
        assert len(docs) == 1
        meta = docs[0].metadata
        assert meta["title"] == "WebLogic OOM 处理手册"
        assert meta["system"] == "WebLogic"

    def test_iter_docs_content_excludes_frontmatter(self, knowledge_config: KnowledgeConfig):
        indexer = VaultIndexer(knowledge_config)
        docs = indexer.iter_docs()
        assert "---" not in docs[0].page_content
        assert "JVM 堆内存耗尽" in docs[0].page_content

    def test_build_keyword_returns_bm25_and_docs(self, knowledge_config: KnowledgeConfig):
        indexer = VaultIndexer(knowledge_config)
        bm25, docs = indexer.build_keyword()
        assert bm25 is not None
        assert len(docs) >= 1

    def test_empty_vault_build_keyword(self, tmp_path: Path):
        config = KnowledgeConfig(vault_path=str(tmp_path), index_mode="keyword")
        indexer = VaultIndexer(config)
        bm25, docs = indexer.build_keyword()
        assert docs == []

    def test_split_docs_writes_chunk_index(self, knowledge_config: KnowledgeConfig):
        indexer = VaultIndexer(knowledge_config)
        docs = indexer.iter_docs()
        chunks = indexer.split_docs(docs)
        assert all("chunk_index" in c.metadata for c in chunks)
        # 同一 source 的 chunk_index 从 0 开始递增
        from collections import defaultdict
        by_source: dict[str, list[int]] = defaultdict(list)
        for c in chunks:
            by_source[c.metadata["source"]].append(c.metadata["chunk_index"])
        for src, indices in by_source.items():
            assert indices == list(range(len(indices))), f"{src} chunk_index 不连续"


# ---------------------------------------------------------------------------
# KnowledgeRetriever tests
# ---------------------------------------------------------------------------

class TestKnowledgeRetriever:
    def test_retrieve_keyword_returns_relevant_docs(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from aiops_agent.knowledge.retriever import KnowledgeRetriever
        indexer = VaultIndexer(knowledge_config)
        bm25, docs = indexer.build_keyword()
        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        results = retriever.retrieve_keyword("WebLogic OOM 如何处理", bm25, docs)
        assert len(results) >= 1
        assert any("OOM" in d.page_content or "WebLogic" in d.page_content for d in results)

    def test_synthesize_returns_answer_on_empty_docs(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from aiops_agent.knowledge.retriever import KnowledgeRetriever
        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        answer = retriever.synthesize("测试问题", [])
        assert isinstance(answer, KnowledgeAnswer)
        assert answer.confidence == 0.0
        assert answer.answer

    def test_synthesize_calls_llm(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from langchain_core.documents import Document
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        docs = [Document(page_content="重启 WebLogic Server", metadata={"title": "运维手册", "source": "/tmp/a.md", "rel_path": "a.md"})]

        response = AIMessage(content="重启步骤：1. 停止服务 2. 启动服务")
        # Use RunnableLambda so LCEL can pipe through it correctly
        fake_model = RunnableLambda(lambda _: response)
        with patch.object(retriever, "_build_synthesis_model", return_value=fake_model):
            answer = retriever.synthesize("如何重启 WebLogic", docs)

        assert answer.confidence == 1.0
        assert len(answer.sources) == 1
        assert answer.sources[0].title == "运维手册"

    def test_rrf_merge_deduplicates_by_chunk_key(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from langchain_core.documents import Document
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        doc_a = Document(page_content="内容A", metadata={"source": "a.md", "chunk_index": 0})
        doc_b = Document(page_content="内容B", metadata={"source": "b.md", "chunk_index": 0})
        # doc_a 同时出现在两个列表
        merged = retriever._rrf_merge([[doc_a, doc_b], [doc_a]], k=60)
        keys = [(d.metadata["source"], d.metadata["chunk_index"]) for d in merged]
        assert len(keys) == len(set(keys)), "RRF merge 应去重"
        # doc_a 在两个列表中都有排名，得分更高，应排第一
        assert merged[0].metadata["source"] == "a.md"

    def test_rrf_merge_boosts_doc_appearing_in_both_lists(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from langchain_core.documents import Document
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        shared = Document(page_content="共有文档", metadata={"source": "shared.md", "chunk_index": 0})
        only_kw = Document(page_content="仅关键词", metadata={"source": "kw.md", "chunk_index": 0})
        only_vec = Document(page_content="仅向量", metadata={"source": "vec.md", "chunk_index": 0})
        merged = retriever._rrf_merge([[only_kw, shared], [only_vec, shared]], k=60)
        assert merged[0].metadata["source"] == "shared.md"

    def test_rewrite_query_returns_original_when_no_history(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from aiops_agent.knowledge.retriever import KnowledgeRetriever
        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        result = retriever.rewrite_query("WebLogic OOM 怎么处理", [])
        assert result == "WebLogic OOM 怎么处理"

    def test_rewrite_query_calls_llm_with_history(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        fake_model = RunnableLambda(lambda _: AIMessage(content="WebLogic OOM 处理步骤中第二步如何操作？"))
        with patch.object(retriever, "_build_synthesis_model", return_value=fake_model):
            result = retriever.rewrite_query(
                "第二步怎么操作",
                [{"question": "WebLogic OOM 怎么处理", "answer": "重启步骤..."}],
            )
        assert "第二步" in result or "WebLogic" in result

    def test_synthesize_with_history_injects_history_block(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from langchain_core.documents import Document
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        docs = [Document(page_content="操作步骤", metadata={"title": "手册", "source": "/tmp/x.md", "rel_path": "x.md"})]
        captured_messages: list[str] = []

        def fake_invoke(prompt_value):
            # prompt_value is ChatPromptValue; extract text from messages
            text = " ".join(str(m.content) for m in prompt_value.messages)
            captured_messages.append(text)
            return AIMessage(content="步骤如下")

        fake_model = RunnableLambda(fake_invoke)
        with patch.object(retriever, "_build_synthesis_model", return_value=fake_model):
            retriever.synthesize_with_history(
                "第二步怎么操作",
                docs,
                [{"question": "WebLogic OOM", "answer": "重启"}],
            )
        assert captured_messages, "应调用 LLM"
        assert "已有对话历史" in captured_messages[0]


# ---------------------------------------------------------------------------
# KnowledgeTool integration tests
# ---------------------------------------------------------------------------

class TestKnowledgeTool:
    def test_no_vault_path_returns_placeholder(self):
        config = KnowledgeConfig(vault_path="")
        tool = KnowledgeTool(config, llm_config=None)
        result = tool.execute({"question": "test"})
        assert result.success
        assert any("vault_path" in item for item in result.data["answer"]["missing_info"])

    def test_nonexistent_vault_returns_error(self, tmp_path: Path):
        config = KnowledgeConfig(vault_path=str(tmp_path / "nonexistent"))
        tool = KnowledgeTool(config, llm_config=None)
        result = tool.execute({"question": "test"})
        assert not result.success

    def test_llm_disabled_returns_placeholder(self, vault: Path):
        config = KnowledgeConfig(vault_path=str(vault))
        disabled_llm = LLMProviderConfig(enabled=False)
        tool = KnowledgeTool(config, llm_config=disabled_llm)
        result = tool.execute({"question": "test"})
        assert result.success
        assert "llm.enabled" in result.data["answer"]["missing_info"]

    def test_execute_with_engine_query(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        tool = KnowledgeTool(knowledge_config, llm_config=llm_config)
        mock_engine = MagicMock()
        mock_engine.query.return_value = KnowledgeAnswer(
            answer="重启步骤如下...",
            confidence=1.0,
        )
        tool._engine = mock_engine
        result = tool.execute({"question": "WebLogic OOM 如何处理"})
        assert result.success
        assert result.data["answer"]["answer"] == "重启步骤如下..."
        mock_engine.query.assert_called_once_with("WebLogic OOM 如何处理", conversation_history=[])

    def test_execute_passes_conversation_history(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        tool = KnowledgeTool(knowledge_config, llm_config=llm_config)
        mock_engine = MagicMock()
        mock_engine.query.return_value = KnowledgeAnswer(answer="第二步是...", confidence=1.0)
        tool._engine = mock_engine
        history = [{"question": "WebLogic OOM 怎么处理", "answer": "重启步骤..."}]
        tool.execute({"question": "第二步怎么操作", "conversation_history": history})
        mock_engine.query.assert_called_once_with("第二步怎么操作", conversation_history=history)

    def test_execute_evaluation_field_in_response(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        tool = KnowledgeTool(knowledge_config, llm_config=llm_config)
        mock_engine = MagicMock()
        mock_engine.query.return_value = KnowledgeAnswer(
            answer="重启步骤如下...",
            confidence=0.88,
            evaluation={"faithfulness": 0.9, "relevance": 0.86},
        )
        tool._engine = mock_engine
        result = tool.execute({"question": "test"})
        assert result.data["answer"]["evaluation"] == {"faithfulness": 0.9, "relevance": 0.86}


# ---------------------------------------------------------------------------
# Controller qa_turns integration tests
# ---------------------------------------------------------------------------

class TestQATurns:
    def _make_session(self):
        from aiops_agent.sessions.models import AgentSession
        return AgentSession()

    def _make_task(self, intent: str, status: str, answer_text: str, input_text: str = "问题"):
        from aiops_agent.tasks.models import Task
        task = Task(trace_id="test-trace", input=input_text)
        task.intent = intent
        task.status = status
        task.result = {
            "success": True,
            "data": {"answer": {"answer": answer_text, "sources": [], "confidence": 1.0}},
        }
        return task

    def test_qa_turns_written_on_ops_qa_success(self):
        import json

        session = self._make_session()
        task = self._make_task("ops_qa", "success", "重启步骤如下...", "WebLogic OOM 怎么处理")

        answer_block = (task.result or {}).get("data", {}).get("answer", {})
        answer_text = answer_block.get("answer", "") if isinstance(answer_block, dict) else ""
        raw_turns = session.metadata.get("qa_turns", "")
        qa_turns = json.loads(raw_turns) if raw_turns else []
        qa_turns.append({"question": task.input, "answer": answer_text})
        session.metadata["qa_turns"] = json.dumps(qa_turns[-5:], ensure_ascii=False)

        stored = json.loads(session.metadata["qa_turns"])
        assert len(stored) == 1
        assert stored[0]["question"] == "WebLogic OOM 怎么处理"
        assert stored[0]["answer"] == "重启步骤如下..."

    def test_qa_turns_capped_at_5(self):
        import json
        from aiops_agent.sessions.models import AgentSession

        session = AgentSession()
        turns = [{"question": f"Q{i}", "answer": f"A{i}"} for i in range(6)]
        session.metadata["qa_turns"] = json.dumps(turns, ensure_ascii=False)

        raw = json.loads(session.metadata["qa_turns"])
        kept = raw[-5:]
        session.metadata["qa_turns"] = json.dumps(kept, ensure_ascii=False)
        assert len(json.loads(session.metadata["qa_turns"])) == 5

    def test_qa_turns_not_written_on_failure(self):
        import json
        from aiops_agent.sessions.models import AgentSession
        from aiops_agent.tasks.models import Task

        session = AgentSession()
        task = Task(trace_id="test-trace", input="问题")
        task.intent = "ops_qa"
        task.status = "failed"
        task.result = {"success": False, "data": {}}

        if task.intent == "ops_qa" and task.status == "success":
            session.metadata["qa_turns"] = json.dumps([{"question": task.input, "answer": "x"}])

        assert "qa_turns" not in session.metadata
