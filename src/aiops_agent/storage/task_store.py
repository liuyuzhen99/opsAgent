from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from aiops_agent.tasks.models import Task


class FileTaskStore:
    def __init__(self, root: str | Path = "storage/tasks"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, task: Task) -> Path:
        path = self.root / f"{task.id}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(task), handle, ensure_ascii=False, indent=2)
        return path
