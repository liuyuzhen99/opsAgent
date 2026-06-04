from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiops_agent.browser.models import BrowserAction, BrowserObservation


class LLMError(Exception):
    """Raised when an LLM request cannot be completed successfully."""


@dataclass(slots=True)
class IntentClassification:
    intent: str
    entities: dict[str, Any]
    provider: str
    model: str
    request_id: str | None = None


@dataclass(slots=True)
class PlannedTask:
    goal: str
    steps: list[str]
    risk_level: str
    confirmation_required: bool


class BaseLLMProvider:
    @property
    def enabled(self) -> bool:
        raise NotImplementedError

    def classify_intent(
        self, text: str, defaults: dict[str, str]
    ) -> IntentClassification:
        raise NotImplementedError

    def plan_task(
        self, text: str, intent: str, entities: dict[str, Any]
    ) -> PlannedTask:
        raise NotImplementedError

    def generate_chat_reply(self, text: str, context: dict[str, Any] | None = None) -> str:
        raise NotImplementedError

    def summarize_session_memory(self, memory_facts: dict[str, Any]) -> str:
        raise NotImplementedError

    def build_summary_model(self):
        raise NotImplementedError

    def plan_browser_action(
        self,
        *,
        goal: str,
        observation: BrowserObservation,
        steps: list[dict[str, Any]],
        allowed_domains: list[str],
        success_criteria: list[str],
        forbidden_actions: list[str],
    ) -> BrowserAction:
        raise NotImplementedError
