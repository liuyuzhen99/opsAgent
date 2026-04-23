from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from aiops_agent.sessions.models import AgentSession


class FileSessionStore:
    def __init__(self, root: str | Path = "storage/sessions"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create_or_resume(self, session_id: str | None = None) -> AgentSession:
        if session_id:
            session = self.load(session_id)
            if session is not None:
                return session
            return AgentSession(id=session_id)
        return AgentSession()

    def load(self, session_id: str) -> AgentSession | None:
        path = self.root / f"{session_id}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return AgentSession(**raw)

    def save(self, session: AgentSession) -> Path:
        session.updated_at = datetime.now(UTC).isoformat()
        path = self.root / f"{session.id}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(session), handle, ensure_ascii=False, indent=2)
        return path
