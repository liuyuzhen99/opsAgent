from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from aiops_agent.audit.models import AuditEvent


class FileAuditLogger:
    def __init__(self, path: str | Path = "storage/audit/events.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: AuditEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False))
            handle.write("\n")
