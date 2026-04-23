from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class TaskArtifact:
    kind: str
    path: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCallSpec:
    tool_name: str
    action: str
    params: dict[str, Any]
    idempotency_key: str | None = None
    risk_level: str = "read_only"
    timeout_seconds: int | None = None


@dataclass(slots=True)
class ToolExecutionResult:
    success: bool
    data: dict[str, Any]
    error: str | None = None
    retryable: bool = False
    artifacts: list[TaskArtifact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "retryable": self.retryable,
            "artifacts": [artifact.__dict__ for artifact in self.artifacts],
        }


@dataclass(slots=True)
class ExecutionPlan:
    goal: str
    steps: list[str]
    selected_tools: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallSpec] = field(default_factory=list)
    risk_level: str = "read_only"
    confirmation_required: bool = False
    success_criteria: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    requires_confirmation: bool
    risk_level: str
    reason: str = ""
    status: str = "approved"


@dataclass(slots=True)
class AgentTaskState:
    trace_id: str
    input: str
    id: str = field(default_factory=lambda: str(uuid4()))
    intent: str = "unknown"
    status: str = "pending"
    session_id: str | None = None
    current_stage: str = "pending"
    entities: dict[str, Any] = field(default_factory=dict)
    plan: ExecutionPlan | None = None
    selected_tools: list[str] = field(default_factory=list)
    risk_level: str = "read_only"
    confirmation_required: bool = False
    artifacts: list[TaskArtifact] = field(default_factory=list)
    audit_refs: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallSpec] = field(default_factory=list)
    llm_profile: str | None = None
    max_steps: int = 20
    requires_explicit_confirmation: bool = False
    result: dict[str, Any] | None = None
    report: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def type(self) -> str:
        return self.intent

    @type.setter
    def type(self, value: str) -> None:
        self.intent = value


Task = AgentTaskState
ToolResult = ToolExecutionResult
