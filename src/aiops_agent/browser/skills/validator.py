from __future__ import annotations

import json
import re
from typing import Any

import yaml

from aiops_agent.browser.skills.models import WebSkillValidationError


SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$|^[a-z0-9]$")
SECRET_MARKERS = (
    "password",
    "passwd",
    "authorization",
    "bearer ",
    "cookie",
    "token",
    "secret",
    "sessionstorage",
    "localstorage",
    "storage_state",
    "凭据",
    "密码",
)


def validate_skill_name(name: str) -> str:
    normalized = name.strip()
    if len(normalized) > 64:
        raise WebSkillValidationError("skill name must be at most 64 characters")
    if "--" in normalized:
        raise WebSkillValidationError("skill name must not contain consecutive hyphens")
    if not SKILL_NAME_RE.fullmatch(normalized):
        raise WebSkillValidationError(
            "skill name must use lowercase letters, numbers, and hyphens, without leading or trailing hyphens"
        )
    return normalized


def validate_description(description: str) -> str:
    value = description.strip()
    if not value:
        raise WebSkillValidationError("skill description is required")
    if len(value) > 1024:
        raise WebSkillValidationError("skill description must be at most 1024 characters")
    return value


def parse_skill_markdown(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise WebSkillValidationError("SKILL.md must start with YAML frontmatter")
    marker = "\n---"
    end = text.find(marker, 3)
    if end == -1:
        raise WebSkillValidationError("SKILL.md frontmatter is not closed")
    raw_frontmatter = text[3:end].strip()
    body = text[end + len(marker):].lstrip("\n")
    try:
        frontmatter = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        raise WebSkillValidationError("SKILL.md frontmatter is invalid YAML") from exc
    if not isinstance(frontmatter, dict):
        raise WebSkillValidationError("SKILL.md frontmatter must be a mapping")
    return frontmatter, body


def validate_frontmatter(frontmatter: dict[str, Any], directory_name: str) -> None:
    name = str(frontmatter.get("name") or "")
    validate_skill_name(name)
    if name != directory_name:
        raise WebSkillValidationError("frontmatter name must match the skill directory name")
    validate_description(str(frontmatter.get("description") or ""))
    metadata = frontmatter.get("metadata") or {}
    if metadata and not isinstance(metadata, dict):
        raise WebSkillValidationError("metadata must be a mapping of string keys to string values")
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise WebSkillValidationError("metadata must be a mapping of string keys to string values")


def validate_workflow(workflow: dict[str, Any], skill_name: str) -> None:
    if workflow.get("schema_version") != "opsagent.web_skill.workflow.v1":
        raise WebSkillValidationError("unsupported workflow schema_version")
    if workflow.get("skill_name") != skill_name:
        raise WebSkillValidationError("workflow skill_name must match SKILL.md name")
    actions = workflow.get("actions")
    if not isinstance(actions, list) or not actions:
        raise WebSkillValidationError("workflow actions must be a non-empty list")
    for action in actions:
        if not isinstance(action, dict) or not action.get("type"):
            raise WebSkillValidationError("each workflow action must be an object with type")
    _assert_no_sensitive_payload(workflow)


def _assert_no_sensitive_payload(payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    for marker in SECRET_MARKERS:
        if marker in serialized:
            raise WebSkillValidationError(f"workflow contains sensitive marker: {marker.strip()}")
    if "data:image/" in serialized or "base64," in serialized:
        raise WebSkillValidationError("workflow must not contain screenshot or base64 image content")
