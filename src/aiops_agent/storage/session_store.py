from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops_agent.sessions.models import (
    AgentSession,
    BrowserMemory,
    PageMemory,
    QATurn,
    SessionTaskIndexEntry,
    ShortTermTurn,
)


class FileSessionStore:
    def __init__(self, root: str | Path = "storage/sessions"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create_or_resume(self, session_id: str | None = None) -> AgentSession:
        if session_id:
            session = self.load(session_id)
            if session is not None:
                session.status = "active"
                return session
            return AgentSession(id=session_id)
        return AgentSession()

    def load(self, session_id: str) -> AgentSession | None:
        path = self.root / f"{session_id}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            return None
        return self._session_from_dict(raw, session_id)

    def save(self, session: AgentSession) -> Path:
        session.updated_at = datetime.now(UTC).isoformat()
        path = self.root / f"{session.id}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(session), handle, ensure_ascii=False, indent=2)
        return path

    def list(self, *, active_only: bool = False) -> list[AgentSession]:
        sessions = []
        for path in sorted(self.root.glob("*.json")):
            session = self.load(path.stem)
            if session is None:
                continue
            if active_only and session.status != "active":
                continue
            sessions.append(session)
        return sessions

    def close(self, session_id: str) -> AgentSession | None:
        session = self.load(session_id)
        if session is None:
            return None
        session.status = "closed"
        self.save(session)
        return session

    def _session_from_dict(self, raw: dict[str, Any], session_id: str) -> AgentSession:
        metadata = self._string_dict(raw.get("metadata"))
        created_at = self._string(raw.get("created_at"))
        updated_at = self._string(raw.get("updated_at"))
        session = AgentSession(
            id=self._string(raw.get("id")) or session_id,
            status=self._string(raw.get("status")) or "active",
            task_ids=self._string_list(raw.get("task_ids")),
            last_task_id=self._optional_string(raw.get("last_task_id")),
            short_term=self._short_term_from_raw(raw.get("short_term")),
            browser_memory=self._browser_memory_from_raw(raw.get("browser_memory")),
            qa_memory=self._qa_memory_from_raw(raw.get("qa_memory")),
            task_index=self._task_index_from_raw(raw.get("task_index")),
            summary=self._string(raw.get("summary")),
            rolling_summary=self._string(raw.get("rolling_summary")),
            recent_observations=self._recent_observations_from_raw(raw.get("recent_observations")),
            metadata=metadata,
            created_at=created_at or datetime.now(UTC).isoformat(),
            updated_at=updated_at or datetime.now(UTC).isoformat(),
        )
        self._migrate_legacy_memory(session, raw)
        return session

    def _migrate_legacy_memory(self, session: AgentSession, raw: dict[str, Any]) -> None:
        metadata = session.metadata
        if not session.qa_memory:
            session.qa_memory = self._qa_memory_from_legacy(metadata.get("qa_turns"), session.created_at)
        if not session.browser_memory.recent_pages and session.recent_observations:
            session.browser_memory.recent_pages = [
                PageMemory(
                    task_id=self._string(item.get("task_id")),
                    url=self._string(item.get("url")),
                    title=self._string(item.get("title")),
                    page_type=self._string(item.get("page_type")),
                    observed_at=self._string(item.get("observed_at")) or session.updated_at,
                )
                for item in session.recent_observations
                if isinstance(item, dict)
            ][-5:]
        browser = session.browser_memory
        browser.last_url = browser.last_url or self._string(metadata.get("last_url"))
        browser.last_page_type = browser.last_page_type or self._string(metadata.get("last_page_type"))
        browser.state_path = browser.state_path or self._string(metadata.get("browser_state_path"))
        browser.last_success_task_id = browser.last_success_task_id or self._string(
            metadata.get("browser_last_success_task_id")
        )
        browser.last_success_site_key = browser.last_success_site_key or self._string(
            metadata.get("browser_last_success_site_key")
        )
        if not session.recent_observations and browser.recent_pages:
            session.recent_observations = [
                {
                    "task_id": page.task_id,
                    "url": page.url,
                    "title": page.title,
                    "page_type": page.page_type,
                }
                for page in browser.recent_pages[-5:]
            ]
        if "id" not in raw or not session.id:
            session.id = self._string(raw.get("id")) or session.id

    def _short_term_from_raw(self, value: Any) -> list[ShortTermTurn]:
        if not isinstance(value, list):
            return []
        turns: list[ShortTermTurn] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            turns.append(
                ShortTermTurn(
                    task_id=self._string(item.get("task_id")),
                    intent=self._string(item.get("intent")),
                    status=self._string(item.get("status")),
                    input=self._string(item.get("input")),
                    report=self._string(item.get("report")),
                    created_at=self._string(item.get("created_at")),
                )
            )
        return turns[-10:]

    def _browser_memory_from_raw(self, value: Any) -> BrowserMemory:
        if not isinstance(value, dict):
            return BrowserMemory()
        recent_pages = []
        if isinstance(value.get("recent_pages"), list):
            for item in value["recent_pages"]:
                if not isinstance(item, dict):
                    continue
                recent_pages.append(
                    PageMemory(
                        task_id=self._string(item.get("task_id")),
                        url=self._string(item.get("url")),
                        title=self._string(item.get("title")),
                        page_type=self._string(item.get("page_type")),
                        observed_at=self._string(item.get("observed_at")),
                    )
                )
        return BrowserMemory(
            recent_pages=recent_pages[-5:],
            state_path=self._string(value.get("state_path")),
            last_url=self._string(value.get("last_url")),
            last_page_type=self._string(value.get("last_page_type")),
            last_success_task_id=self._string(value.get("last_success_task_id")),
            last_success_site_key=self._string(value.get("last_success_site_key")),
        )

    def _qa_memory_from_raw(self, value: Any) -> list[QATurn]:
        if not isinstance(value, list):
            return []
        turns: list[QATurn] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            turns.append(
                QATurn(
                    task_id=self._string(item.get("task_id")),
                    question=self._string(item.get("question")),
                    answer=self._string(item.get("answer")),
                    created_at=self._string(item.get("created_at")),
                )
            )
        return turns[-5:]

    def _qa_memory_from_legacy(self, raw_turns: str | None, created_at: str) -> list[QATurn]:
        if not raw_turns:
            return []
        try:
            parsed = json.loads(raw_turns)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        turns: list[QATurn] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            turns.append(
                QATurn(
                    task_id=self._string(item.get("task_id")),
                    question=self._string(item.get("question")),
                    answer=self._string(item.get("answer")),
                    created_at=self._string(item.get("created_at")) or created_at,
                )
            )
        return turns[-5:]

    def _task_index_from_raw(self, value: Any) -> list[SessionTaskIndexEntry]:
        if not isinstance(value, list):
            return []
        entries: list[SessionTaskIndexEntry] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            entries.append(
                SessionTaskIndexEntry(
                    task_id=self._string(item.get("task_id")),
                    intent=self._string(item.get("intent")),
                    status=self._string(item.get("status")),
                    system=self._string(item.get("system")),
                    env=self._string(item.get("env")),
                    site_key=self._string(item.get("site_key")),
                    url=self._string(item.get("url")),
                    title=self._string(item.get("title")),
                    summary=self._string(item.get("summary")),
                    created_at=self._string(item.get("created_at")),
                )
            )
        return entries[-50:]

    def _recent_observations_from_raw(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        observations: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            observations.append(
                {
                    "task_id": self._string(item.get("task_id")),
                    "url": self._string(item.get("url")),
                    "title": self._string(item.get("title")),
                    "page_type": self._string(item.get("page_type")),
                }
            )
        return observations[-5:]

    def _string(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _optional_string(self, value: Any) -> str | None:
        text = self._string(value)
        return text or None

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [self._string(item) for item in value if item is not None]

    def _string_dict(self, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {self._string(key): self._string(item) for key, item in value.items()}
