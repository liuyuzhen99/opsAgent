from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4


@dataclass(slots=True)
class AgentSession:
    id: str = field(default_factory=lambda: str(uuid4()))
    task_ids: list[str] = field(default_factory=list)
    last_task_id: str | None = None
    summary: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
