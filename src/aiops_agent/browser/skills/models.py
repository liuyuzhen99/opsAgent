from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiops_agent.browser.models import BrowserAction


class WebSkillError(Exception):
    """Base error for persisted web skills."""


class WebSkillValidationError(WebSkillError):
    """Raised when a skill file does not follow the expected format."""


class WebSkillGenerationError(WebSkillError):
    """Raised when a successful task cannot be converted into a skill."""


@dataclass(slots=True)
class WebSkill:
    name: str
    description: str
    root: Path
    frontmatter: dict[str, Any]
    workflow: dict[str, Any]
    body: str = ""


@dataclass(slots=True)
class WebSkillMatch:
    skill: WebSkill
    score: float
    parameters: dict[str, str]
    actions: list[BrowserAction]
    matched_keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WebSkillSaveResult:
    name: str
    path: Path
    inputs: list[str]
    action_count: int
    matched_keywords: list[str]
