from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from aiops_agent.sessions.models import (
    AgentSession,
    BrowserMemory,
    PageMemory,
    QATurn,
    SessionTaskIndexEntry,
    ShortTermTurn,
)
from aiops_agent.tasks.models import Task

if TYPE_CHECKING:
    from aiops_agent.llm.base import BaseLLMProvider


class ContextCompressor:
    def __init__(self, llm_provider: BaseLLMProvider | None = None):
        self.llm_provider = llm_provider

    def compress(self, session: AgentSession, task: Task) -> AgentSession:
        facts = self.extract_memory_facts(session, task)
        session = self.apply_memory_facts(session, task, facts)
        session.rolling_summary = self.render_rolling_summary(session)
        session.summary = self.summarize_session(session, facts)
        return session

    def extract_memory_facts(self, session: AgentSession, task: Task) -> dict[str, Any]:
        data = self._task_data(task)
        last_observation = data.get("last_observation") if isinstance(data.get("last_observation"), dict) else {}
        answer_block = data.get("answer") if isinstance(data.get("answer"), dict) else {}
        entities = task.entities if isinstance(task.entities, dict) else {}
        completed_actions = [
            self._safe_text(step.get("action", {}).get("type"))
            for step in data.get("steps", [])
            if isinstance(step, dict) and step.get("result") == "success"
        ]
        return {
            "task": {
                "task_id": task.id,
                "intent": task.intent,
                "status": task.status,
                "input": self._memory_input(task, data),
                "report": self._safe_text(task.report),
                "created_at": task.created_at,
                "system": self._safe_text(entities.get("system")),
                "env": self._safe_text(entities.get("env")),
                "target": self._safe_text(data.get("target") or entities.get("target")),
                "capability": self._safe_text(data.get("capability") or entities.get("capability")),
                "site_key": self._safe_text(entities.get("site_key")),
            },
            "last_observation": {
                "task_id": task.id,
                "url": self._safe_text(last_observation.get("url")),
                "title": self._safe_text(last_observation.get("title")),
                "page_type": self._safe_text(last_observation.get("page_type")),
                "observed_at": task.updated_at or task.created_at,
            },
            "browser": {
                "state_path": self._safe_text(data.get("session_state_path")),
                "completed_actions": [action for action in completed_actions if action],
            },
            "qa": {
                "question": self._safe_text(task.input),
                "answer": self._safe_text(answer_block.get("answer")),
            },
        }

    def apply_memory_facts(self, session: AgentSession, task: Task, facts: dict[str, Any]) -> AgentSession:
        task_facts = facts.get("task") if isinstance(facts.get("task"), dict) else {}
        observation = facts.get("last_observation") if isinstance(facts.get("last_observation"), dict) else {}
        browser_facts = facts.get("browser") if isinstance(facts.get("browser"), dict) else {}
        qa_facts = facts.get("qa") if isinstance(facts.get("qa"), dict) else {}

        session.short_term = [turn for turn in getattr(session, "short_term", []) if turn.task_id != task.id]
        session.short_term.append(
            ShortTermTurn(
                task_id=task.id,
                intent=self._safe_text(task_facts.get("intent")),
                status=self._safe_text(task_facts.get("status")),
                input=self._truncate(self._safe_text(task_facts.get("input")), 500),
                report=self._truncate(self._safe_text(task_facts.get("report")), 700),
                created_at=self._safe_text(task_facts.get("created_at")),
            )
        )
        session.short_term = session.short_term[-10:]

        page = self._page_from_observation(observation)
        if page is not None:
            browser_memory = getattr(session, "browser_memory", None) or BrowserMemory()
            browser_memory.recent_pages = [item for item in browser_memory.recent_pages if item.task_id != task.id]
            browser_memory.recent_pages.append(page)
            browser_memory.recent_pages = browser_memory.recent_pages[-5:]
            browser_memory.last_url = page.url
            browser_memory.last_page_type = page.page_type
            state_path = self._safe_text(browser_facts.get("state_path"))
            if state_path:
                browser_memory.state_path = state_path
            session.browser_memory = browser_memory

        state_path = self._safe_text(browser_facts.get("state_path"))
        if state_path:
            session.browser_memory.state_path = state_path

        if task.intent == "web_action" and task.status == "success":
            session.browser_memory.last_success_task_id = task.id
            site_key = self._safe_text(task_facts.get("site_key"))
            if site_key:
                session.browser_memory.last_success_site_key = site_key

        answer = self._safe_text(qa_facts.get("answer"))
        if task.intent == "ops_qa" and task.status == "success" and answer:
            session.qa_memory = [turn for turn in getattr(session, "qa_memory", []) if turn.task_id != task.id]
            session.qa_memory.append(
                QATurn(
                    task_id=task.id,
                    question=self._truncate(self._safe_text(qa_facts.get("question")), 600),
                    answer=self._truncate(answer, 1000),
                    created_at=task.created_at,
                )
            )
            session.qa_memory = session.qa_memory[-5:]

        session.task_index = [entry for entry in getattr(session, "task_index", []) if entry.task_id != task.id]
        session.task_index.append(
            SessionTaskIndexEntry(
                task_id=task.id,
                intent=self._safe_text(task_facts.get("intent")),
                status=self._safe_text(task_facts.get("status")),
                system=self._safe_text(task_facts.get("system")),
                env=self._safe_text(task_facts.get("env")),
                target=self._safe_text(task_facts.get("target")),
                capability=self._safe_text(task_facts.get("capability")),
                site_key=self._safe_text(task_facts.get("site_key")),
                url=self._safe_text(observation.get("url")),
                title=self._safe_text(observation.get("title")),
                summary=self._truncate(self._safe_text(task_facts.get("report") or task_facts.get("input")), 500),
                created_at=self._safe_text(task_facts.get("created_at")),
            )
        )
        session.task_index = session.task_index[-50:]

        self._sync_legacy_fields(session)
        return session

    def render_rolling_summary(self, session: AgentSession) -> str:
        recent_tasks = [
            f"{turn.intent}:{turn.status}"
            for turn in getattr(session, "short_term", [])[-5:]
            if turn.intent or turn.status
        ]
        recent_qa = [
            f"Q:{self._truncate(self._redact_sensitive(turn.question), 60)} "
            f"A:{self._truncate(self._redact_sensitive(turn.answer), 80)}"
            for turn in getattr(session, "qa_memory", [])[-2:]
        ]
        browser_memory = getattr(session, "browser_memory", None) or BrowserMemory()
        recent_pages = [
            (page.title or page.url or page.page_type)
            for page in browser_memory.recent_pages[-3:]
            if page.title or page.url or page.page_type
        ]
        last_success = browser_memory.last_success_task_id or "无"
        recent_actions = [
            self._truncate(self._redact_sensitive(turn.input), 80)
            for turn in getattr(session, "short_term", [])[-3:]
            if turn.status in {"success", "blocked", "failed", "awaiting_confirmation"}
        ]
        return (
            f"最近任务={'; '.join(recent_tasks) or '无'}; "
            f"最近QA={'; '.join(recent_qa) or '无'}; "
            f"最近页面={'; '.join(recent_pages) or '无'}; "
            f"最近成功web任务={last_success}; "
            f"动作概览={'; '.join(recent_actions) or '无'}"
        )

    def summarize_session(self, session: AgentSession, facts: dict[str, Any]) -> str:
        provider = self.llm_provider
        if provider and getattr(provider, "enabled", False) and hasattr(provider, "summarize_session_memory"):
            try:
                summary = provider.summarize_session_memory(self._sanitized_facts(session, facts))
                if summary.strip():
                    return summary.strip()
            except Exception:
                pass
        return self._rule_summary(session)

    def retrieve(self, session: AgentSession, intent: str, query: str, limit: int = 5) -> dict[str, Any]:
        limit = max(0, int(limit))
        matches = self._rank_task_index(session, intent, query, limit)
        return {
            "summary": getattr(session, "summary", ""),
            "rolling_summary": getattr(session, "rolling_summary", ""),
            "qa_memory": [asdict(turn) for turn in self._qa_for_intent(session, intent)],
            "short_term": [asdict(turn) for turn in self._short_term_for_intent(session, intent, query)],
            "browser_memory": asdict(getattr(session, "browser_memory", None) or BrowserMemory()),
            "task_matches": [asdict(entry) for entry in matches],
        }

    def _task_data(self, task: Task) -> dict[str, Any]:
        result = task.result if isinstance(task.result, dict) else {}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return data

    def _memory_input(self, task: Task, data: dict[str, Any]) -> str:
        if task.intent != "knowledge_write":
            return self._safe_text(task.input)
        title = self._safe_text(data.get("title"))
        note_type = self._safe_text(data.get("type"))
        if title and note_type:
            return f"知识库写入请求：{note_type}/{title}"
        if title:
            return f"知识库写入请求：{title}"
        return "知识库写入请求"

    def _page_from_observation(self, observation: dict[str, Any]) -> PageMemory | None:
        url = self._safe_text(observation.get("url"))
        title = self._safe_text(observation.get("title"))
        page_type = self._safe_text(observation.get("page_type"))
        if not (url or title or page_type):
            return None
        return PageMemory(
            task_id=self._safe_text(observation.get("task_id")),
            url=url,
            title=title,
            page_type=page_type,
            observed_at=self._safe_text(observation.get("observed_at")),
        )

    def _sync_legacy_fields(self, session: AgentSession) -> None:
        session.metadata = getattr(session, "metadata", {}) or {}
        session.recent_observations = [
            {
                "task_id": page.task_id,
                "url": page.url,
                "title": page.title,
                "page_type": page.page_type,
            }
            for page in session.browser_memory.recent_pages[-5:]
        ]
        if session.qa_memory:
            session.metadata["qa_turns"] = json.dumps(
                [
                    {
                        "question": turn.question,
                        "answer": turn.answer,
                        "task_id": turn.task_id,
                        "created_at": turn.created_at,
                    }
                    for turn in session.qa_memory[-5:]
                ],
                ensure_ascii=False,
            )
        if session.browser_memory.last_url:
            session.metadata["last_url"] = session.browser_memory.last_url
        if session.browser_memory.last_page_type:
            session.metadata["last_page_type"] = session.browser_memory.last_page_type
        if session.browser_memory.state_path:
            session.metadata["browser_state_path"] = session.browser_memory.state_path
        if session.browser_memory.last_success_task_id:
            session.metadata["browser_last_success_task_id"] = session.browser_memory.last_success_task_id
        if session.browser_memory.last_success_site_key:
            session.metadata["browser_last_success_site_key"] = session.browser_memory.last_success_site_key

    def _sanitized_facts(self, session: AgentSession, facts: dict[str, Any]) -> dict[str, Any]:
        browser = getattr(session, "browser_memory", None) or BrowserMemory()
        return {
            "summary": self._truncate(self._redact_sensitive(session.summary), 1000),
            "rolling_summary": self._truncate(self._redact_sensitive(session.rolling_summary), 1200),
            "latest_task": self._sanitize_value(facts.get("task"), 700),
            "recent_short_term": [
                {
                    "task_id": turn.task_id,
                    "intent": turn.intent,
                    "status": turn.status,
                    "input": self._truncate(self._redact_sensitive(turn.input), 240),
                    "report": self._truncate(self._redact_sensitive(turn.report), 300),
                }
                for turn in session.short_term[-5:]
            ],
            "recent_qa": [
                {
                    "task_id": turn.task_id,
                    "question": self._truncate(self._redact_sensitive(turn.question), 240),
                    "answer": self._truncate(self._redact_sensitive(turn.answer), 360),
                }
                for turn in session.qa_memory[-5:]
            ],
            "browser": {
                "last_url": self._truncate(self._redact_sensitive(browser.last_url), 240),
                "last_page_type": self._truncate(browser.last_page_type, 80),
                "last_success_task_id": browser.last_success_task_id,
                "last_success_site_key": browser.last_success_site_key,
                "recent_pages": [
                    {
                        "task_id": page.task_id,
                        "url": self._truncate(self._redact_sensitive(page.url), 240),
                        "title": self._truncate(self._redact_sensitive(page.title), 120),
                        "page_type": self._truncate(page.page_type, 80),
                    }
                    for page in browser.recent_pages[-5:]
                ],
            },
            "task_index": [
                {
                    "task_id": entry.task_id,
                    "intent": entry.intent,
                    "status": entry.status,
                    "system": entry.system,
                    "env": entry.env,
                    "target": entry.target,
                    "capability": entry.capability,
                    "site_key": entry.site_key,
                    "url": self._truncate(self._redact_sensitive(entry.url), 240),
                    "title": self._truncate(self._redact_sensitive(entry.title), 120),
                    "summary": self._truncate(self._redact_sensitive(entry.summary), 280),
                }
                for entry in session.task_index[-10:]
            ],
        }

    def _sanitize_value(self, value: Any, limit: int) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if self._is_sensitive_key(key_text) or key_text == "state_path":
                    continue
                sanitized[key_text] = self._sanitize_value(item, limit)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize_value(item, limit) for item in value[:10]]
        return self._truncate(self._redact_sensitive(self._safe_text(value)), limit)

    def _rule_summary(self, session: AgentSession) -> str:
        latest = session.short_term[-1] if session.short_term else None
        parts = []
        if latest:
            parts.append(
                f"最近任务是 {latest.intent}，状态 {latest.status}，目标："
                f"{self._truncate(self._redact_sensitive(latest.input), 120)}。"
            )
        if session.qa_memory:
            parts.append(f"最近保留了 {len(session.qa_memory[-5:])} 轮运维问答上下文。")
        browser = getattr(session, "browser_memory", None) or BrowserMemory()
        if browser.last_url or browser.last_success_task_id:
            page = browser.recent_pages[-1].title or browser.last_url if browser.recent_pages else browser.last_url
            parts.append(
                f"浏览器最近页面：{self._truncate(self._redact_sensitive(page), 120) or '无'}；"
                f"最近成功 web 任务：{browser.last_success_task_id or '无'}。"
            )
        return "".join(parts) or session.rolling_summary or "当前 session 暂无可总结的上下文。"

    def _qa_for_intent(self, session: AgentSession, intent: str) -> list[QATurn]:
        qa_memory = getattr(session, "qa_memory", []) or []
        if intent in {"ops_qa", "knowledge_write"}:
            return qa_memory[-5:]
        return qa_memory[-2:]

    def _short_term_for_intent(self, session: AgentSession, intent: str, query: str) -> list[ShortTermTurn]:
        turns = (getattr(session, "short_term", []) or [])[-10:]
        if intent in {"ops_qa", "knowledge_write"}:
            return [turn for turn in turns if turn.intent in {"ops_qa", "knowledge_write"}][-5:] or turns[-5:]
        if intent == "general_chat":
            return turns[-5:]
        query_lower = query.lower()
        related = [
            turn
            for turn in turns
            if turn.intent == intent or (query_lower and query_lower in f"{turn.input} {turn.report}".lower())
        ]
        return related[-5:] or turns[-5:]

    def _rank_task_index(
        self,
        session: AgentSession,
        intent: str,
        query: str,
        limit: int,
    ) -> list[SessionTaskIndexEntry]:
        query_lower = query.lower()
        browser = getattr(session, "browser_memory", None) or BrowserMemory()

        def score(indexed: tuple[int, SessionTaskIndexEntry]) -> tuple[int, int]:
            index, entry = indexed
            value = 0
            if entry.intent == intent:
                value += 80
            if entry.system and entry.system.lower() in query_lower:
                value += 30
            if entry.env and entry.env.lower() in query_lower:
                value += 20
            if entry.target and entry.target.lower() in query_lower:
                value += 40
            if entry.capability and entry.capability.lower() in query_lower:
                value += 30
            if entry.site_key and entry.site_key.lower() in query_lower:
                value += 30
            if intent == "web_action":
                if entry.task_id == browser.last_success_task_id:
                    value += 60
                if entry.site_key and entry.site_key == browser.last_success_site_key:
                    value += 30
            text = f"{entry.summary} {entry.title} {entry.url}".lower()
            for token in self._query_tokens(query_lower):
                if token in text:
                    value += 5
            return value, index

        scored = [(score((index, entry)), entry) for index, entry in enumerate(getattr(session, "task_index", []) or [])]
        ranked = sorted(scored, key=lambda item: item[0], reverse=True)
        return [entry for (value, _index), entry in ranked if value > 0][:limit]

    def _query_tokens(self, query_lower: str) -> list[str]:
        return [token for token in re.split(r"[\s,，。:：;；/\\]+", query_lower) if len(token) >= 2][:20]

    def _safe_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _truncate(self, value: str, limit: int) -> str:
        value = self._safe_text(value).strip()
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)].rstrip() + "…"

    def _is_sensitive_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(part in lowered for part in ("password", "token", "credential", "secret", "api_key", "apikey"))

    def _redact_sensitive(self, value: str) -> str:
        text = self._safe_text(value)
        patterns = [
            r"(?i)(password|token|credential|secret|api[_-]?key)\s*[:=]\s*[^,\s;]+",
            r"(?i)(bearer)\s+[a-z0-9._\-]+",
        ]
        for pattern in patterns:
            text = re.sub(pattern, r"\1=***", text)
        return text
