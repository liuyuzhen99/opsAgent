from __future__ import annotations

from unittest.mock import patch

import pytest

from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from aiops_agent.config import LLMProviderConfig
from aiops_agent.knowledge.evaluator import EvalResult, RAGEvaluator


@pytest.fixture()
def llm_config() -> LLMProviderConfig:
    return LLMProviderConfig(provider="anthropic", enabled=True, api_key="test-key")


@pytest.fixture()
def evaluator(llm_config: LLMProviderConfig) -> RAGEvaluator:
    return RAGEvaluator(llm_config)


@pytest.fixture()
def sample_docs() -> list[Document]:
    return [
        Document(
            page_content="重启 WebLogic Managed Server 可释放堆内存，调整 -Xmx 扩大堆上限。",
            metadata={"source": "/tmp/a.md", "rel_path": "a.md"},
        )
    ]


class TestEvalResult:
    def test_confidence_is_average(self):
        result = EvalResult(faithfulness=0.8, relevance=0.6)
        assert result.confidence == pytest.approx(0.7, abs=1e-4)

    def test_confidence_perfect(self):
        result = EvalResult(faithfulness=1.0, relevance=1.0)
        assert result.confidence == 1.0

    def test_confidence_zero(self):
        result = EvalResult(faithfulness=0.0, relevance=0.0)
        assert result.confidence == 0.0


class TestRAGEvaluator:
    def _make_fake_model(self, score: str):
        return RunnableLambda(lambda _: AIMessage(content=score))

    def test_evaluate_returns_eval_result(self, evaluator: RAGEvaluator, sample_docs: list[Document]):
        with patch.object(evaluator, "_build_model", return_value=self._make_fake_model("0.9")):
            result = evaluator.evaluate(
                question="WebLogic OOM 如何处理",
                answer="重启 Managed Server 并调整 -Xmx",
                docs=sample_docs,
            )
        assert isinstance(result, EvalResult)
        assert result.faithfulness == pytest.approx(0.9)
        assert result.relevance == pytest.approx(0.9)

    def test_faithfulness_high_when_answer_grounded(self, evaluator: RAGEvaluator, sample_docs: list[Document]):
        with patch.object(evaluator, "_build_model", return_value=self._make_fake_model("0.95")):
            result = evaluator._check_faithfulness(
                answer="重启 Managed Server 并调整 -Xmx",
                context=sample_docs[0].page_content,
            )
        assert result == pytest.approx(0.95)

    def test_relevance_high_when_answer_addresses_question(self, evaluator: RAGEvaluator):
        with patch.object(evaluator, "_build_model", return_value=self._make_fake_model("0.88")):
            result = evaluator._check_relevance(
                question="WebLogic OOM 如何处理",
                answer="重启 Managed Server 并调整 -Xmx",
            )
        assert result == pytest.approx(0.88)

    def test_score_clamps_above_1(self, evaluator: RAGEvaluator):
        with patch.object(evaluator, "_build_model", return_value=self._make_fake_model("1.5")):
            result = evaluator._check_relevance("q", "a")
        assert result == 1.0

    def test_score_clamps_below_0(self, evaluator: RAGEvaluator):
        with patch.object(evaluator, "_build_model", return_value=self._make_fake_model("-0.3")):
            result = evaluator._check_relevance("q", "a")
        assert result == 0.0

    def test_score_fallback_on_non_numeric(self, evaluator: RAGEvaluator):
        with patch.object(evaluator, "_build_model", return_value=self._make_fake_model("无效输出")):
            result = evaluator._check_relevance("q", "a")
        assert result == 0.5

    def test_score_fallback_on_exception(self, evaluator: RAGEvaluator):
        def raise_exc(_):
            raise RuntimeError("LLM timeout")

        with patch.object(evaluator, "_build_model", return_value=RunnableLambda(raise_exc)):
            result = evaluator._check_relevance("q", "a")
        assert result == 0.5

    def test_evaluate_uses_top5_docs(self, evaluator: RAGEvaluator):
        docs = [
            Document(page_content=f"CHUNK_MARKER_{i}", metadata={"source": f"{i}.md"})
            for i in range(10)
        ]
        captured_messages: list[str] = []

        def fake_invoke(prompt_value):
            text = " ".join(str(m.content) for m in prompt_value.messages)
            captured_messages.append(text)
            return AIMessage(content="0.8")

        with patch.object(evaluator, "_build_model", return_value=RunnableLambda(fake_invoke)):
            evaluator.evaluate("问题", "答案", docs)

        assert captured_messages, "应调用 LLM"
        context_calls = [m for m in captured_messages if "CHUNK_MARKER" in m]
        assert context_calls, "faithfulness 调用应包含 context"
        # 仅使用前 5 个 doc（0-4），不应出现 CHUNK_MARKER_5 ~ _9
        for i in range(5, 10):
            assert f"CHUNK_MARKER_{i}" not in context_calls[0]
