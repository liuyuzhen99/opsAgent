from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig
from aiops_agent.knowledge.indexer import VaultIndexer
from aiops_agent.knowledge.writer import KnowledgeNoteWriter
from aiops_agent.tools.knowledge import KnowledgeAnswer, KnowledgeTool, KnowledgeWriteTool


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


def test_load_rpa_config_reads_obsidian_compatibility_options(tmp_path: Path):
    from aiops_agent.config import load_rpa_config

    config_path = tmp_path / "rpa.json"
    config_path.write_text(json.dumps({
        "knowledge": {
            "vault_path": str(tmp_path),
            "obsidian_graph_enabled": False,
            "link_context_enabled": False,
            "graph_expand_depth": 0,
            "graph_boost": 0.2,
            "moc_patterns": ["**/MOC.md"],
            "write_enabled": False,
            "auto_reindex_after_write": False,
            "note_type_dirs": {"runbooks": "playbooks"},
        }
    }), encoding="utf-8")

    config = load_rpa_config(str(config_path))

    assert config.knowledge.obsidian_graph_enabled is False
    assert config.knowledge.link_context_enabled is False
    assert config.knowledge.graph_expand_depth == 0
    assert config.knowledge.graph_boost == 0.2
    assert config.knowledge.moc_patterns == ["**/MOC.md"]
    assert config.knowledge.write_enabled is False
    assert config.knowledge.auto_reindex_after_write is False
    assert config.knowledge.note_type_dirs["runbooks"] == "playbooks"


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

    def test_iter_docs_respects_include_patterns(self, tmp_path: Path):
        (tmp_path / "runbooks").mkdir()
        (tmp_path / "notes").mkdir()
        (tmp_path / "runbooks" / "keep.md").write_text("# keep", encoding="utf-8")
        (tmp_path / "notes" / "skip.md").write_text("# skip", encoding="utf-8")
        config = KnowledgeConfig(vault_path=str(tmp_path), include_patterns=["runbooks/**"])

        docs = VaultIndexer(config).iter_docs()

        assert [doc.metadata["rel_path"] for doc in docs] == ["runbooks/keep.md"]

    def test_iter_docs_parses_obsidian_aliases_cn_properties_and_links(self, tmp_path: Path):
        note = """\
---
title: 财司系统 - 支付指令状态未知
aliases:
  - 支付指令状态未知
  - 付款状态未知
tags:
  - system/财司系统
  - type/incident
类型: incident
系统: 财司系统
环境: prod
严重度: P2
组件: icip
last_updated: 2026-05-12
---

## 处理步骤
检查 ICIP 日志。

## 相关知识
- [[财司系统 - 系统信息]]
- [[财司系统 - 服务的启停|服务启停]]
- ![[支付截图.png]]
"""
        (tmp_path / "incident.md").write_text(note, encoding="utf-8")
        config = KnowledgeConfig(vault_path=str(tmp_path))

        doc = VaultIndexer(config).iter_docs()[0]

        assert doc.metadata["aliases_text"] == "支付指令状态未知 付款状态未知"
        assert doc.metadata["type"] == "incident"
        assert doc.metadata["system"] == "财司系统"
        assert doc.metadata["env"] == "prod"
        assert doc.metadata["severity"] == "P2"
        assert doc.metadata["component"] == "icip"
        assert doc.metadata["outlinks_text"] == "财司系统 - 系统信息 财司系统 - 服务的启停 服务启停"
        assert "相关标题：财司系统 - 支付指令状态未知" in doc.page_content
        assert "相关路径：incident.md incident" in doc.page_content
        assert "相关别名：支付指令状态未知 付款状态未知" in doc.page_content
        assert "相关链接：财司系统 - 系统信息 财司系统 - 服务的启停 服务启停" in doc.page_content
        assert "支付截图.png" not in doc.metadata["outlinks_text"]

    def test_fenced_frontmatter_is_marked_but_not_parsed_as_contract(self, tmp_path: Path):
        note = """\
```yaml
---
title: 错误格式标题
---
```

正文内容
"""
        (tmp_path / "bad.md").write_text(note, encoding="utf-8")
        config = KnowledgeConfig(vault_path=str(tmp_path))

        doc = VaultIndexer(config).iter_docs()[0]

        assert doc.metadata["title"] == "bad"
        assert doc.metadata["has_fenced_frontmatter"] is True

    def test_vector_manifest_uses_schema_and_marks_old_format_stale(self, knowledge_config: KnowledgeConfig):
        indexer = VaultIndexer(knowledge_config)
        manifest_dir = Path(knowledge_config.vault_path) / ".chroma"
        manifest_dir.mkdir()
        old_manifest = {str(Path(knowledge_config.vault_path) / "runbooks" / "weblogic-oom.md"): 1.0}
        (manifest_dir / "index_manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")

        assert indexer.is_vector_stale() is True

        indexer._write_manifest()
        manifest = json.loads((manifest_dir / "index_manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema_version"] >= 2
        assert "files" in manifest
        assert manifest["index_options"]["link_context_enabled"] is True


# ---------------------------------------------------------------------------
# KnowledgeRetriever tests
# ---------------------------------------------------------------------------

class TestKnowledgeRetriever:
    def test_tokenize_chinese_query_uses_searchable_ngrams(self):
        from aiops_agent.knowledge.tokenizer import tokenize_knowledge_text

        tokens = tokenize_knowledge_text("财司系统怎么发版")

        assert "财司" in tokens
        assert "系统" in tokens
        assert "发版" in tokens

    def test_retrieve_keyword_returns_relevant_docs(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from aiops_agent.knowledge.retriever import KnowledgeRetriever
        indexer = VaultIndexer(knowledge_config)
        bm25, docs = indexer.build_keyword()
        retriever = KnowledgeRetriever(knowledge_config, llm_config)
        results = retriever.retrieve_keyword("WebLogic OOM 如何处理", bm25, docs)
        assert len(results) >= 1
        assert any("OOM" in d.page_content or "WebLogic" in d.page_content for d in results)

    def test_retrieve_keyword_matches_chinese_question_against_title_context(
        self,
        tmp_path: Path,
        llm_config: LLMProviderConfig,
    ):
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        for index in range(20):
            (tmp_path / f"a{index:02d}.md").write_text(
                f"# 无关文档 {index}\n\n这是普通巡检记录。",
                encoding="utf-8",
            )
        runbooks = tmp_path / "runbooks"
        runbooks.mkdir()
        (runbooks / "财司系统 - 生产环境发版.md").write_text("""\
---
title: 财司系统生产环境发版
aliases:
  - 财司发版
system: 财司系统
type: runbooks
env: prod
---
# 步骤

## 步骤1：接收补丁
收到发版补丁包。
""", encoding="utf-8")
        config = KnowledgeConfig(vault_path=str(tmp_path), index_mode="keyword")
        indexer = VaultIndexer(config)
        bm25, docs = indexer.build_keyword()
        retriever = KnowledgeRetriever(config, llm_config)

        results = retriever.retrieve_keyword("财司系统怎么发版", bm25, docs)

        assert results
        assert results[0].metadata["rel_path"] == "runbooks/财司系统 - 生产环境发版.md"

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
        assert answer.sources[0].relation == "direct"
        assert answer.sources[0].related_to == ""

    def test_prepare_synthesis_docs_hydrates_full_note_and_filters_reference_docs(
        self,
        tmp_path: Path,
        llm_config: LLMProviderConfig,
    ):
        from langchain_core.documents import Document
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        note = tmp_path / "runbooks" / "财司系统 - 对公付款表周报导出.md"
        note.parent.mkdir()
        note.write_text("""\
---
title: 财司系统 - 对公付款表周报导出
---

# 财司系统 - 对公付款表周报导出

```sql
SELECT B.PAYACCOUNTNO
  FROM BS_BANKINSTRUCTIONINFO B
 WHERE B.SENDTIME >= TO_DATE('2026-05-04', 'YYYY-MM-DD')
   AND B.SENDTIME < TO_DATE('2026-05-11', 'YYYY-MM-DD')
 ORDER BY B.SENDTIME, B.AMOUNT DESC;
```
""", encoding="utf-8")
        readme = tmp_path / "README.md"
        readme.write_text("# README", encoding="utf-8")
        moc = tmp_path / "runbooks" / "Runbooks MOC.md"
        moc.write_text("# MOC", encoding="utf-8")

        config = KnowledgeConfig(vault_path=str(tmp_path))
        retriever = KnowledgeRetriever(config, llm_config)
        docs = [
            Document(page_content="目录", metadata={"title": "README", "source": str(readme), "rel_path": "README.md"}),
            Document(page_content="MOC", metadata={"title": "Runbooks MOC", "source": str(moc), "rel_path": "runbooks/Runbooks MOC.md", "is_moc": True}),
            Document(page_content="SELECT B.PAYACCOUNTNO", metadata={"title": "财司系统 - 对公付款表周报导出", "source": str(note), "rel_path": "runbooks/财司系统 - 对公付款表周报导出.md", "chunk_index": 0}),
        ]

        prepared = retriever._prepare_synthesis_docs(docs)

        assert len(prepared) == 1
        assert prepared[0].metadata["rel_path"] == "runbooks/财司系统 - 对公付款表周报导出.md"
        assert prepared[0].metadata["hydrated_source"] is True
        assert "title:" not in prepared[0].page_content
        assert "FROM BS_BANKINSTRUCTIONINFO" in prepared[0].page_content
        assert "TO_DATE('2026-05-11', 'YYYY-MM-DD')" in prepared[0].page_content

    def test_synthesize_uses_hydrated_full_note_context(
        self,
        tmp_path: Path,
        llm_config: LLMProviderConfig,
    ):
        from langchain_core.documents import Document
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        from aiops_agent.knowledge.retriever import KnowledgeRetriever

        note = tmp_path / "runbooks" / "财司系统 - 对私付款表周报导出.md"
        note.parent.mkdir()
        note.write_text("""\
# 财司系统 - 对私付款表周报导出

## 原始资料

```sql
SELECT a.PAY_ACCOUNT_NO,
       b.receive_account_no,
       b.amount
  FROM bs_reimburse_instruction_main a, BS_REIMBURSE_INSTR_DETAIL b
 WHERE a.batch_no = b.main_batch_no
   AND b.send_time >= TO_DATE('2026-05-04', 'YYYY-MM-DD')
   AND b.send_time < TO_DATE('2026-05-11', 'YYYY-MM-DD')
ORDER BY b.send_time, b.AMOUNT DESC;
```
""", encoding="utf-8")
        docs = [
            Document(
                page_content="ORDER BY b.send_time, b.AMOUNT DESC;",
                metadata={"title": "财司系统 - 对私付款表周报导出", "source": str(note), "rel_path": "runbooks/财司系统 - 对私付款表周报导出.md", "chunk_index": 2},
            )
        ]
        captured_messages: list[str] = []

        def fake_invoke(prompt_value):
            text = "\n".join(str(message.content) for message in prompt_value.messages)
            captured_messages.append(text)
            return AIMessage(content="已包含完整 SQL")

        retriever = KnowledgeRetriever(KnowledgeConfig(vault_path=str(tmp_path)), llm_config)
        with patch.object(retriever, "_build_synthesis_model", return_value=RunnableLambda(fake_invoke)):
            answer = retriever.synthesize("如何导出财司系统对私付款表", docs)

        assert answer.answer == "已包含完整 SQL"
        assert captured_messages
        assert "SELECT a.PAY_ACCOUNT_NO" in captured_messages[0]
        assert "FROM bs_reimburse_instruction_main" in captured_messages[0]
        assert "必须完整保留相关代码块" in captured_messages[0]
        assert len(answer.sources) == 1
        assert answer.sources[0].section == "runbooks/财司系统 - 对私付款表周报导出.md"

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
# KnowledgeEngine vector lifecycle tests
# ---------------------------------------------------------------------------

class TestKnowledgeEngineVectorLifecycle:
    class FakeClient:
        def __init__(self):
            self.closed = False
            self.cache_cleared = False

        def close(self):
            self.closed = True

        def clear_system_cache(self):
            self.cache_cleared = True

    class FakeVectorDB:
        def __init__(self):
            self._client = TestKnowledgeEngineVectorLifecycle.FakeClient()
            self.persisted = False

        def persist(self):
            self.persisted = True

    def test_invalidate_cache_closes_chroma_client(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        from aiops_agent.knowledge.engine import KnowledgeEngine

        engine = KnowledgeEngine(knowledge_config, llm_config)
        vector_db = self.FakeVectorDB()
        engine._vector_db = vector_db

        engine.invalidate_cache()

        assert engine._vector_db is None
        assert vector_db.persisted is True
        assert vector_db._client.closed is True
        assert vector_db._client.cache_cleared is False

    def test_reindex_after_write_retries_readonly_chroma_rebuild(
        self,
        tmp_path: Path,
        llm_config: LLMProviderConfig,
    ):
        from aiops_agent.knowledge.engine import KnowledgeEngine

        class FlakyIndexer:
            def __init__(self):
                self.build_vector_calls = 0

            def build_keyword(self):
                return object(), []

            def build_vector(self):
                self.build_vector_calls += 1
                if self.build_vector_calls == 1:
                    raise RuntimeError("Query error: Database error: attempt to write a readonly database")
                return TestKnowledgeEngineVectorLifecycle.FakeVectorDB()

            def is_vector_stale(self):
                return True

        config = KnowledgeConfig(vault_path=str(tmp_path), index_mode="hybrid")
        engine = KnowledgeEngine(config, llm_config)
        engine.VECTOR_RETRY_DELAY_SECONDS = 0
        engine._indexer = FlakyIndexer()

        status = engine.reindex_after_write()

        assert status == "rebuilt"
        assert engine._indexer.build_vector_calls == 2
        assert engine._vector_db is None

    def test_hybrid_query_reopens_vector_db_after_sqlite_handle_error(
        self,
        tmp_path: Path,
        llm_config: LLMProviderConfig,
    ):
        from langchain_core.documents import Document
        from aiops_agent.knowledge.engine import KnowledgeEngine

        class FakeIndexer:
            def __init__(self):
                self.load_vector_calls = 0

            def build_keyword(self):
                return object(), []

            def load_vector(self):
                self.load_vector_calls += 1
                return "reopened-vector"

            def is_vector_stale(self):
                return False

        class FakeRetriever:
            def __init__(self):
                self.hybrid_calls = 0

            def rewrite_query(self, question, history):
                return question

            def retrieve_hybrid(self, question, bm25, bm25_docs, vector_db):
                self.hybrid_calls += 1
                if self.hybrid_calls == 1:
                    raise RuntimeError("Database error: unable to open database file")
                assert vector_db == "reopened-vector"
                return [Document(page_content="命中文档", metadata={"title": "doc", "source": "", "rel_path": "doc.md"})]

            def synthesize_with_history(self, question, docs, history):
                return KnowledgeAnswer(answer="ok", sources=[], confidence=1.0)

        (tmp_path / ".chroma").mkdir()
        config = KnowledgeConfig(vault_path=str(tmp_path), index_mode="hybrid")
        engine = KnowledgeEngine(config, llm_config)
        engine.VECTOR_RETRY_DELAY_SECONDS = 0
        engine._indexer = FakeIndexer()
        engine._retriever = FakeRetriever()
        stale_vector = self.FakeVectorDB()
        engine._vector_db = stale_vector

        answer = engine.query("test")

        assert answer.answer == "ok"
        assert stale_vector._client.closed is True
        assert engine._indexer.load_vector_calls == 1
        assert engine._retriever.hybrid_calls == 2


# ---------------------------------------------------------------------------
# Knowledge writer tests
# ---------------------------------------------------------------------------

class FakeKnowledgeEngine:
    def __init__(self):
        self.reindex_count = 0

    def reindex_after_write(self):
        self.reindex_count += 1
        return "cache_refreshed"


def _fake_draft(self, instruction, history, *, default_system=None, default_env=None):
    return {
        "title": "OOM 处理步骤",
        "aliases": ["JVM OOM"],
        "system": default_system or "WebLogic",
        "type": "runbooks",
        "env": default_env or "prod",
        "severity": "P2",
        "tags": ["weblogic", "oom"],
        "summary": "WebLogic OOM 的处理步骤",
        "body": "## 背景\n\nJVM 堆内存耗尽。\n\n## 处理步骤\n\n1. 查看堆使用情况\n2. 重启 Managed Server",
        "related_links": ["weblogic-oom", "runbooks MOC", "WebLogic - JVM 参数", "[[bad]]", "../secret"],
    }


class TestKnowledgeWriteTool:
    def test_writer_creates_note_moc_and_reindexes(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        engine = FakeKnowledgeEngine()
        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=engine)

        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", _fake_draft):
            result = tool.execute({
                "instruction": "记录到知识库：WebLogic OOM 怎么处理",
                "conversation_history": [{"question": "WebLogic OOM 怎么处理", "answer": "重启"}],
                "system": "WebLogic",
                "env": "prod",
            })

        assert result.success is True
        data = result.data
        note_path = Path(data["note_path"])
        assert note_path.exists()
        assert note_path.name == "WebLogic - OOM 处理步骤.md"
        raw = note_path.read_text(encoding="utf-8")
        assert "title: WebLogic - OOM 处理步骤" in raw
        assert "type: runbooks" in raw
        assert "system/WebLogic" in raw
        assert "type/runbook" in raw
        assert "component/weblogic" in raw
        assert "env/prod" in raw
        assert "severity/P2" in raw
        assert "- oom" not in raw
        assert "# 相关知识" in raw
        assert "[[weblogic-oom]]" in raw
        assert "[[runbooks MOC]]" not in raw
        assert "[[WebLogic - JVM 参数]]" not in raw
        assert "[[bad]]" not in raw
        moc_path = Path(data["moc_path"])
        assert moc_path.exists()
        assert "- [[WebLogic - OOM 处理步骤]]：WebLogic OOM 的处理步骤" in moc_path.read_text(encoding="utf-8")
        assert data["reindex_status"] == "cache_refreshed"
        assert engine.reindex_count == 1

    def test_writer_preserves_fenced_sql_when_llm_omits_it(
        self,
        knowledge_config: KnowledgeConfig,
        llm_config: LLMProviderConfig,
    ):
        def sql_draft(self, instruction, history, *, default_system=None, default_env=None):
            return {
                "title": "对公付款表导出",
                "aliases": [],
                "system": "财司系统",
                "type": "runbooks",
                "env": "prod",
                "severity": "unknown",
                "tags": ["system/财司系统", "type/runbook"],
                "summary": "每周一导出对公付款表并发送给石林禾",
                "body": "## 背景\n\n每周一导出上周一到本周一的对公付款表。",
                "related_links": [],
            }

        instruction = """将以下内容写入知识库：
财司系统对公付款表：将 sql 日期改为上周一到这周一，每周一导出 excel 文件发给石林禾
```sql
SELECT B.PAYACCOUNTNO 付款方账户号,
       B.SENDTIME 发送时间
  FROM BS_BANKINSTRUCTIONINFO B
 WHERE B.SENDTIME >= TO_DATE('2026-05-04', 'YYYY-MM-DD')
   AND B.SENDTIME < TO_DATE('2026-05-11', 'YYYY-MM-DD')
 ORDER BY B.SENDTIME, B.AMOUNT DESC;
```
"""
        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())
        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", sql_draft):
            result = tool.execute({"instruction": instruction})

        assert result.success is True
        raw = Path(result.data["note_path"]).read_text(encoding="utf-8")
        assert "## 原始资料" in raw
        assert "```sql" in raw
        assert "SELECT B.PAYACCOUNTNO" in raw
        assert "TO_DATE('2026-05-04', 'YYYY-MM-DD')" in raw
        assert "TO_DATE('2026-05-11', 'YYYY-MM-DD')" in raw

    def test_writer_does_not_append_original_material_when_equivalent_sql_exists(
        self,
        knowledge_config: KnowledgeConfig,
        llm_config: LLMProviderConfig,
    ):
        def duplicated_sql_draft(self, instruction, history, *, default_system=None, default_env=None):
            sql = """```sql
select ID, SerialNo, OfficeID, CurrencyID, TransNo,
       case when transactiontypeid = 90 and transno like '%DSFK%'
            then (select sum(d.mamount) from sett_transcurrentdeposit d where d.banchno = transno and d.nstatusid = 3)
            else ReceiveAmount end ReceiveAmount
  from SETT_VTRANSACTION_ENTERPRISE
 where Execute >= to_date('2021-06-01','yyyy-mm-dd')
   and Execute <= to_date('2021-06-30','yyyy-mm-dd')
   and StatusID >= 1
   and OperationTypeID not in (1101, 1102, 1111)
```"""
            return {
                "title": "财司老系统交易数据查询SQL",
                "aliases": [],
                "system": "财司系统",
                "type": "guidance",
                "env": "prod",
                "severity": "P3",
                "tags": ["system/财司系统", "type/guidance"],
                "summary": "财司老系统交易数据查询 SQL",
                "body": f"## 处理步骤\n\n执行以下 SQL：\n\n{sql}\n\n## 原始资料\n\n{sql}",
                "related_links": [],
            }

        instruction = """将以下内容写入知识库:
财司老系统交易数据查询
```sql
select  ID,SerialNo,OfficeID,CurrencyID,TransNo,
case when transactiontypeid = 90 and transno like '%DSFK%' then (select sum(d.mamount) from sett_transcurrentdeposit d where d.banchno = transno and d.nstatusid = 3)
else ReceiveAmount end ReceiveAmount
from SETT_VTRANSACTION_ENTERPRISE
where Execute>=to_date('2021-06-01','yyyy-mm-dd')
and Execute<=to_date('2021-06-30','yyyy-mm-dd') and StatusID>=1
and OperationTypeID not in(1101,1102,1111)]
```
"""
        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())
        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", duplicated_sql_draft):
            result = tool.execute({"instruction": instruction})

        assert result.success is True
        raw = Path(result.data["note_path"]).read_text(encoding="utf-8")
        assert "select ID, SerialNo" in raw
        assert "## 原始资料" not in raw
        assert raw.count("```sql") == 1

    def test_writer_removes_plain_original_material_when_body_is_structured(
        self,
        knowledge_config: KnowledgeConfig,
        llm_config: LLMProviderConfig,
    ):
        def structured_draft(self, instruction, history, *, default_system=None, default_env=None):
            return {
                "title": "老系统查询说明",
                "aliases": [],
                "system": "财司系统",
                "type": "guidance",
                "env": "prod",
                "severity": "unknown",
                "tags": ["system/财司系统", "type/guidance"],
                "summary": "财司老系统交易数据查询说明",
                "body": (
                    "## 背景\n\n财司老系统交易数据查询用于日常数据核对。\n\n"
                    "## 处理步骤\n\n1. 打开 PL/SQL 连接财司数据库。\n"
                    "2. 按业务日期范围调整查询条件后执行。\n\n"
                    "## 原始资料\n\n"
                    "财司老系统交易数据查询，打开 PL/SQL 后按照日期范围查询交易数据。"
                ),
                "related_links": [],
            }

        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())
        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", structured_draft):
            result = tool.execute({"instruction": "将以下内容写入知识库：财司老系统交易数据查询"})

        assert result.success is True
        raw = Path(result.data["note_path"]).read_text(encoding="utf-8")
        assert "财司老系统交易数据查询用于日常数据核对" in raw
        assert "## 原始资料" not in raw

    def test_writer_collision_does_not_overwrite(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        target_dir = Path(knowledge_config.vault_path) / "runbooks"
        target_dir.mkdir(exist_ok=True)
        existing = target_dir / "WebLogic - OOM 处理步骤.md"
        existing.write_text("old content", encoding="utf-8")
        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())

        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", _fake_draft):
            result = tool.execute({"instruction": "记录到知识库：OOM"})

        assert result.success is False
        assert "不会覆盖" in (result.error or "")
        assert existing.read_text(encoding="utf-8") == "old content"

    def test_moc_update_does_not_duplicate_wikilink(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        engine = FakeKnowledgeEngine()
        writer = KnowledgeNoteWriter(knowledge_config, llm_config, engine=engine)
        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", _fake_draft):
            result = writer.write(instruction="记录到知识库：OOM")

        moc_path = Path(result.moc_path)
        note_stem = Path(result.note_path).stem
        assert writer._update_moc(moc_path, note_stem, "更新后的说明") is True

        text = moc_path.read_text(encoding="utf-8")
        assert text.count(f"[[{note_stem}]]") == 1
        assert "更新后的说明" in text

    def test_writer_requires_enabled_llm(self, knowledge_config: KnowledgeConfig):
        disabled = LLMProviderConfig(enabled=False)
        tool = KnowledgeWriteTool(knowledge_config, llm_config=disabled)

        result = tool.execute({"instruction": "记录到知识库：OOM"})

        assert result.success is False
        assert "llm.enabled" in result.data["missing_info"]

    def test_writer_rejects_empty_following_content(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())

        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", side_effect=AssertionError("should not call LLM")):
            result = tool.execute({"instruction": "请将以下内容添加入知识库"})

        assert result.success is False
        assert "knowledge_write.content" in result.data["missing_info"]
        assert not (Path(knowledge_config.vault_path) / "runbooks" / "未命名笔记.md").exists()

    def test_writer_rejects_empty_write_to_knowledge_phrase(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())

        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", side_effect=AssertionError("should not call LLM")):
            result = tool.execute({"instruction": "将以下内容写入知识库："})

        assert result.success is False
        assert "knowledge_write.content" in result.data["missing_info"]

    def test_writer_infers_caishi_system_and_hierarchical_tags(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        def caishi_draft(self, instruction, history, *, default_system=None, default_env=None):
            return {
                "title": "接口",
                "aliases": [],
                "system": default_system or "WebLogic",
                "type": "guidance",
                "env": "prod",
                "severity": "P1",
                "tags": ["icip", "type/guidance"],
                "summary": "财司系统前置机接口配置",
                "body": "财司测试系统网银端对接，sp.Finance.interface.ip 配置为 172.16.222.52。",
                "related_links": [],
            }

        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())
        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", caishi_draft):
            result = tool.execute({"instruction": "将以下内容整理到知识库：财司的测试系统网银端对接，component 是 icip"})

        assert result.success is True
        raw = Path(result.data["note_path"]).read_text(encoding="utf-8")
        assert "title: 财司系统 - 接口" in raw
        assert "system: 财司系统" in raw
        assert "system/财司系统" in raw
        assert "type/guidance" in raw
        assert "component/icip" in raw
        assert "env/prod" in raw
        assert "severity/P1" in raw

    def test_writer_does_not_keep_default_weblogic_when_content_is_unrelated(self, knowledge_config: KnowledgeConfig, llm_config: LLMProviderConfig):
        def unrelated_draft(self, instruction, history, *, default_system=None, default_env=None):
            return {
                "title": "前置机接口",
                "aliases": [],
                "system": default_system or "WebLogic",
                "type": "guidance",
                "env": "prod",
                "severity": "P2",
                "tags": ["weblogic", "component/weblogic"],
                "summary": "外围系统支付前置机接口配置",
                "body": "前置机接口地址为 http://10.60.143.160:8000/FrontEnd/FrontEndServlet。",
                "related_links": [],
            }

        tool = KnowledgeWriteTool(knowledge_config, llm_config=llm_config, engine=FakeKnowledgeEngine())
        with patch.object(KnowledgeNoteWriter, "_draft_with_llm", unrelated_draft):
            result = tool.execute({"instruction": "将以下内容整理到知识库：外围系统支付前置机接口配置"})

        assert result.success is True
        raw = Path(result.data["note_path"]).read_text(encoding="utf-8")
        assert "system: unknown" in raw
        assert "system/WebLogic" not in raw
        assert "component/weblogic" not in raw


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
