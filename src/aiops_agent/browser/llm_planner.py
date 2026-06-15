from __future__ import annotations

from typing import TypeAlias
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aiops_agent.browser.models import BrowserAction, BrowserActionType


AllowedLLMAction: TypeAlias = BrowserActionType


class BrowserPlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AllowedLLMAction
    target_hint: str = ""
    target_id: str | None = None
    value: str | None = None
    expected_outcome: str = ""
    timeout_ms: int = Field(default=5000, ge=100, le=60000)

    @model_validator(mode="after")
    def _validate_action_shape(self):
        if self.type == "open_url":
            parsed = urlparse(self.value or self.target_hint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("open_url requires an absolute http(s) URL")
        targeted_actions = {"click", "hover", "type", "type_username", "type_password", "login_submit", "select"}
        if self.type in targeted_actions and not (self.target_hint or self.target_id):
            raise ValueError(f"{self.type} requires target_hint or target_id")
        if self.type in {"type", "select"} and self.value is None:
            raise ValueError(f"{self.type} requires value")
        if self.type == "press" and not (self.value or self.target_hint):
            raise ValueError("press requires value or target_hint")
        return self

    def to_action(self) -> BrowserAction:
        return BrowserAction(
            type=self.type,
            target_hint=self.target_hint,
            target_id=self.target_id,
            value=self.value,
            expected_outcome=self.expected_outcome,
            timeout_ms=self.timeout_ms,
        )


class BrowserPlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thought: str = Field(default="", max_length=1000)
    action: BrowserPlannerOutput
