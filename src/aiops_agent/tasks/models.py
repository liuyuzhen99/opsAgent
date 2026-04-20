from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class Task:
    type: str
    input: str
    trace_id: str
    id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "pending"
    result: dict[str, Any] | None = None
    report: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(slots=True)
class ToolResult:
    success: bool
    data: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
        }
