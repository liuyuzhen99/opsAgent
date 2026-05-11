from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from aiops_agent.config import KnowledgeConfig, LLMProviderConfig
from aiops_agent.tasks.models import ToolExecutionResult
from aiops_agent.tools.base import BaseTool


@dataclass(slots=True)
class KnowledgeSource:
    title: str
    path: str
    section: str = ""
    matched_text: str = ""


@dataclass(slots=True)
class KnowledgeAnswer:
    answer: str
    sources: list[KnowledgeSource] = field(default_factory=list)
    confidence: float = 0.0
    missing_info: list[str] = field(default_factory=list)
    evaluation: dict | None = None


class KnowledgeTool(BaseTool):
    def __init__(self, config: KnowledgeConfig, llm_config: LLMProviderConfig | None = None):
        self.config = config
        self._engine = None
        self._llm_config = llm_config

    @property
    def engine(self):
        if self._engine is None:
            from aiops_agent.knowledge.engine import KnowledgeEngine
            self._engine = KnowledgeEngine(self.config, self._llm_config)
        return self._engine

    def execute(self, params: dict) -> ToolExecutionResult:
        question = str(params.get("question", ""))
        conversation_history = list(params.get("conversation_history") or [])

        if not self.config.vault_path:
            answer = KnowledgeAnswer(
                answer="Obsidian vault 尚未配置，请在 configs/rpa.json 中设置 knowledge.vault_path。",
                missing_info=["knowledge.vault_path"],
            )
            return ToolExecutionResult(success=True, data={"question": question, "answer": self._to_dict(answer)})

        vault_path = Path(self.config.vault_path)
        if not vault_path.exists():
            answer = KnowledgeAnswer(
                answer="Obsidian vault 路径不存在，请确认 knowledge.vault_path 配置。",
                missing_info=["valid knowledge.vault_path"],
            )
            return ToolExecutionResult(
                success=False,
                error="Obsidian vault 路径不存在",
                data={"question": question, "answer": self._to_dict(answer)},
            )

        if self._llm_config is None or not self._llm_config.enabled:
            answer = KnowledgeAnswer(
                answer="LLM 未启用，知识库合成需要配置并启用 LLM provider（llm.json 中 enabled=true）。",
                missing_info=["llm.enabled"],
            )
            return ToolExecutionResult(success=True, data={"question": question, "answer": self._to_dict(answer)})

        try:
            result = self.engine.query(question, conversation_history=conversation_history)
        except Exception as exc:
            answer = KnowledgeAnswer(
                answer=f"知识库查询失败：{exc}",
                confidence=0.0,
            )
            return ToolExecutionResult(
                success=False,
                error=str(exc),
                data={"question": question, "answer": self._to_dict(answer)},
            )

        return ToolExecutionResult(
            success=True,
            data={"question": question, "answer": self._to_dict(result)},
        )

    def _to_dict(self, answer: KnowledgeAnswer) -> dict:
        return {
            "answer": answer.answer,
            "sources": [asdict(source) for source in answer.sources],
            "confidence": answer.confidence,
            "missing_info": answer.missing_info,
            "evaluation": answer.evaluation,
        }
