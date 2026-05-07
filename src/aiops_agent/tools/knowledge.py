from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from aiops_agent.config import KnowledgeConfig
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


class KnowledgeTool(BaseTool):
    def __init__(self, config: KnowledgeConfig):
        self.config = config

    def execute(self, params: dict) -> ToolExecutionResult:
        question = str(params.get("question", ""))
        if not self.config.vault_path:
            answer = KnowledgeAnswer(
                answer="Obsidian vault 尚未配置，Phase 2 仅预留知识库工具契约。",
                missing_info=["knowledge.vault_path"],
            )
            return ToolExecutionResult(success=True, data={"question": question, "answer": self._to_dict(answer)})
        vault_path = Path(self.config.vault_path)
        if not vault_path.exists():
            answer = KnowledgeAnswer(
                answer="Obsidian vault 路径不存在，未执行真实检索。",
                missing_info=["valid knowledge.vault_path"],
            )
            return ToolExecutionResult(success=False, error="Obsidian vault 路径不存在", data={"question": question, "answer": self._to_dict(answer)})
        answer = KnowledgeAnswer(
            answer="Phase 2 已识别到 Obsidian vault 配置，但完整索引和检索将在后续阶段实现。",
            confidence=0.0,
            missing_info=["keyword/vector/hybrid index"],
        )
        return ToolExecutionResult(success=True, data={"question": question, "answer": self._to_dict(answer)})

    def _to_dict(self, answer: KnowledgeAnswer) -> dict:
        return {
            "answer": answer.answer,
            "sources": [asdict(source) for source in answer.sources],
            "confidence": answer.confidence,
            "missing_info": answer.missing_info,
        }
