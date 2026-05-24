from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4


@dataclass(slots=True)
class ShortTermTurn:
    task_id: str
    intent: str # 任务类型
    status: str
    input: str # 任务的具体内容
    report: str
    created_at: str


@dataclass(slots=True)
class PageMemory:
    task_id: str
    url: str
    title: str
    page_type: str
    observed_at: str


@dataclass(slots=True)
class BrowserMemory:
    recent_pages: list[PageMemory] = field(default_factory=list)
    state_path: str = "" # Path to the most recent browser state snapshot
    last_url: str = ""
    last_page_type: str = ""
    last_success_task_id: str = ""
    last_success_site_key: str = ""


@dataclass(slots=True)
class QATurn:
    task_id: str
    question: str
    answer: str
    created_at: str


@dataclass(slots=True)
class SessionTaskIndexEntry:
    task_id: str
    intent: str
    status: str
    system: str = ""
    env: str = ""
    target: str = ""
    capability: str = ""
    site_key: str = ""
    url: str = ""
    title: str = ""
    summary: str = ""
    created_at: str = ""


@dataclass(slots=True)
class AgentSession:
    id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "active"
    task_ids: list[str] = field(default_factory=list)
    last_task_id: str | None = None
    short_term: list[ShortTermTurn] = field(default_factory=list)
    browser_memory: BrowserMemory = field(default_factory=BrowserMemory)
    qa_memory: list[QATurn] = field(default_factory=list)
    task_index: list[SessionTaskIndexEntry] = field(default_factory=list)
    summary: str = ""
    rolling_summary: str = ""
    recent_observations: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
