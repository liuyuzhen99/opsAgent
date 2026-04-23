from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiops_agent.audit.models import AuditEvent


class FileAuditLogger:
    def __init__(self, path: str | Path = "storage/audit/events.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: AuditEvent) -> None:
        payload = asdict(event)
        payload["details"] = self._sanitize(payload.get("details", {}))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if any(secret in key.lower() for secret in ("token", "password", "cookie", "secret")):
                    sanitized[key] = "***"
                else:
                    sanitized[key] = self._sanitize(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value
