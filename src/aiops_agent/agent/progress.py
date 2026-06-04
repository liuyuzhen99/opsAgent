from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProgressEvent:
    stage: str
    message: str
    task_id: str | None = None
    session_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def with_details(self, **updates: Any) -> "ProgressEvent":
        details = dict(self.details or {})
        for key, value in updates.items():
            if value is not None and key not in details:
                details[key] = value
        self.details = details
        return self
