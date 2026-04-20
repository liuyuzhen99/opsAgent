from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class LLMError(Exception):
    """Raised when an LLM request cannot be completed successfully."""


@dataclass(slots=True)
class IntentClassification:
    intent: str
    entities: dict[str, Any]
    provider: str
    model: str
    request_id: str | None = None


class BaseLLMProvider:
    @property
    def enabled(self) -> bool:
        raise NotImplementedError

    def classify_intent(
        self, text: str, defaults: dict[str, str]
    ) -> IntentClassification:
        raise NotImplementedError
