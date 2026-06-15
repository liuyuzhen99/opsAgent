from __future__ import annotations

from dataclasses import asdict
import json
import re
from typing import Any

from langgraph.store.base import BaseStore

from aiops_agent.browser.action_trace import build_canonical_action_trace
from aiops_agent.sessions.models import (
    AgentSession,
    BrowserMemory,
    PageMemory,
    QATurn,
    SessionTaskIndexEntry,
    ShortTermTurn,
)
from aiops_agent.tasks.models import Task


WEB_NAMESPACE = "web"
KNOWLEDGE_NAMESPACE = "knowledge"
TASK_INDEX_NAMESPACE = "task_index"
LEGACY_MIGRATION_KEY = "legacy_v1"


class MemorySanitizer:
    sensitive_key_parts = ("password", "token", "credential", "secret", "api_key", "apikey", "cookie")

    def sanitize(self, value: Any, *, text_limit: int = 1200) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if self._is_sensitive_key(key_text):
                    sanitized[key_text] = "***"
                else:
                    sanitized[key_text] = self.sanitize(item, text_limit=text_limit)
            return sanitized
        if isinstance(value, list):
            return [self.sanitize(item, text_limit=text_limit) for item in value[:50]]
        if isinstance(value, (bool, int, float)):
            return value
        return self._truncate(self._redact(self._safe_text(value)), text_limit)

    def _is_sensitive_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(part in lowered for part in self.sensitive_key_parts)

    def _safe_text(self, value: Any) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    def _truncate(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _redact(self, text: str) -> str:
        patterns = [
            r"(?i)(password|token|credential|secret|api[_-]?key|cookie)\s*[:=]\s*[^,\s;]+",
            r"(?i)(bearer)\s+[a-z0-9._\-]+",
        ]
        for pattern in patterns:
            text = re.sub(pattern, r"\1=***", text)
        return text


class WebMemoryStrategy:
    def __init__(self, sanitizer: MemorySanitizer | None = None):
        self.sanitizer = sanitizer or MemorySanitizer()

    def context(self, session: AgentSession, task: Task | None = None) -> dict[str, Any]:
        browser = getattr(session, "browser_memory", None) or BrowserMemory()
        value: dict[str, Any] = {
            "browser_state_path": browser.state_path,
            "state_path": browser.state_path,
            "last_url": browser.last_url,
            "last_page_type": browser.last_page_type,
            "recent_pages": [asdict(page) for page in browser.recent_pages[-5:]],
            "last_success_task_id": browser.last_success_task_id,
            "last_success_site_key": browser.last_success_site_key,
            "workflow_hints": [],
            "skill_refs": [],
        }
        if task is not None:
            params = self._tool_params(task)
            hints = {
                "task_id": task.id,
                "site_key": task.entities.get("site_key") or params.get("site_key"),
                "workflow": task.entities.get("workflow") or params.get("workflow"),
                "workflow_fields": task.entities.get("workflow_fields") or params.get("workflow_fields") or {},
            }
            if any(hints.values()):
                value["workflow_hints"] = [hints]
            skill_name = params.get("skill_name")
            if skill_name:
                value["skill_refs"] = [{"task_id": task.id, "skill_name": str(skill_name)}]
        return self.sanitizer.sanitize(value)

    def trace_item(self, task: Task) -> dict[str, Any] | None:
        data = _task_data(task)
        trace = data.get("canonical_action_trace")
        if not isinstance(trace, dict) or not trace:
            return None
        canonical_trace = build_canonical_action_trace(
            list(trace.get("steps") or []),
            status=str(trace.get("status") or data.get("status") or task.status),
            task_id=str(trace.get("task_id") or task.id),
            session_id=str(trace.get("session_id") or task.session_id or ""),
            pending_action=trace.get("pending_action") if isinstance(trace.get("pending_action"), dict) else None,
        )
        return self.sanitizer.sanitize({"task_id": task.id, "canonical_action_trace": canonical_trace})

    def _tool_params(self, task: Task) -> dict[str, Any]:
        if not task.tool_calls:
            return {}
        return dict(task.tool_calls[0].params or {})


class KnowledgeMemoryStrategy:
    def __init__(self, sanitizer: MemorySanitizer | None = None):
        self.sanitizer = sanitizer or MemorySanitizer()

    def context(self, session: AgentSession, task: Task | None = None) -> dict[str, Any]:
        qa_turns = [
            {
                "task_id": turn.task_id,
                "question": turn.question,
                "answer": turn.answer,
                "created_at": turn.created_at,
            }
            for turn in (getattr(session, "qa_memory", []) or [])[-5:]
        ]
        value: dict[str, Any] = {
            "recent_qa_summaries": qa_turns,
            "recent_sources": [],
            "last_answer_summary": qa_turns[-1]["answer"] if qa_turns else "",
            "rewritten_query_history": [],
            "health": {},
        }
        if task is not None:
            data = _task_data(task)
            answer = data.get("answer") if isinstance(data.get("answer"), dict) else {}
            if answer:
                value["recent_sources"] = list(answer.get("sources") or [])[:10]
                value["last_answer_summary"] = str(answer.get("answer") or "")
                value["health"] = {
                    "missing_info": list(answer.get("missing_info") or []),
                    "confidence": answer.get("confidence", 0.0),
                    "evaluation": answer.get("evaluation"),
                }
            if task.intent == "knowledge_write":
                value["last_write"] = {
                    "task_id": task.id,
                    "title": data.get("title"),
                    "note_path": data.get("note_path"),
                    "type": data.get("type"),
                    "reindex_status": data.get("reindex_status"),
                }
        return self.sanitizer.sanitize(value)


class LegacySessionMemoryMigrator:
    def __init__(
        self,
        *,
        web_strategy: WebMemoryStrategy | None = None,
        knowledge_strategy: KnowledgeMemoryStrategy | None = None,
        sanitizer: MemorySanitizer | None = None,
    ):
        self.sanitizer = sanitizer or MemorySanitizer()
        self.web_strategy = web_strategy or WebMemoryStrategy(self.sanitizer)
        self.knowledge_strategy = knowledge_strategy or KnowledgeMemoryStrategy(self.sanitizer)

    def migrate(self, store: BaseStore, session: AgentSession) -> bool:
        marker_namespace = self._namespace(session.id, "migration")
        if store.get(marker_namespace, LEGACY_MIGRATION_KEY) is not None:
            return False
        store.put(self._namespace(session.id, WEB_NAMESPACE), "context", self.web_strategy.context(session))
        store.put(
            self._namespace(session.id, KNOWLEDGE_NAMESPACE),
            "context",
            self.knowledge_strategy.context(session),
        )
        for entry in getattr(session, "task_index", []) or []:
            if not entry.task_id:
                continue
            namespace = self._namespace(session.id, TASK_INDEX_NAMESPACE, entry.intent or "unknown")
            store.put(namespace, entry.task_id, self.sanitizer.sanitize(asdict(entry)))
        store.put(marker_namespace, LEGACY_MIGRATION_KEY, {"migrated": True, "session_id": session.id})
        return True

    def _namespace(self, session_id: str, *parts: str) -> tuple[str, ...]:
        return ("sessions", session_id, *parts)


class LegacySessionMemoryWriter:
    def __init__(self, sanitizer: MemorySanitizer | None = None, summary_strategy=None):
        self.sanitizer = sanitizer or MemorySanitizer()
        self.summary_strategy = summary_strategy

    def sync(self, session: AgentSession, task: Task) -> AgentSession:
        # 把完整 task 压成 session 内的短窗口记忆。完整事实仍在 task JSON，
        # session 里只保留下一轮规划最常用的摘要、最近页面、QA 和索引。
        data = _task_data(task)
        entities = task.entities if isinstance(task.entities, dict) else {}
        report = self._safe_text(task.report)
        task_input = self._memory_input(task, data)
        session.short_term = [turn for turn in getattr(session, "short_term", []) if turn.task_id != task.id]
        session.short_term.append(
            ShortTermTurn(
                task_id=task.id,
                intent=self._safe_text(task.intent),
                status=self._safe_text(task.status),
                input=self._truncate(task_input, 500),
                report=self._truncate(report, 700),
                created_at=self._safe_text(task.created_at),
            )
        )
        session.short_term = session.short_term[-10:]

        observation = data.get("last_observation") if isinstance(data.get("last_observation"), dict) else {}
        self._sync_browser_memory(session, task, data, observation, entities)
        self._sync_qa_memory(session, task, data)
        self._sync_task_index(session, task, observation, entities, report, task_input)
        session.rolling_summary = self._rolling_summary(session)
        session.summary = self._summary(session)
        self._sync_legacy_fields(session)
        return session

    def _sync_browser_memory(
        self,
        session: AgentSession,
        task: Task,
        data: dict[str, Any],
        observation: dict[str, Any],
        entities: dict[str, Any],
    ) -> None:
        browser = getattr(session, "browser_memory", None) or BrowserMemory()
        page = self._page_from_observation(task, observation)
        if page is not None:
            browser.recent_pages = [item for item in browser.recent_pages if item.task_id != task.id]
            browser.recent_pages.append(page)
            browser.recent_pages = browser.recent_pages[-5:]
            browser.last_url = page.url
            browser.last_page_type = page.page_type
        state_path = self._safe_text(data.get("session_state_path"))
        if state_path:
            browser.state_path = state_path
        if task.intent == "web_action" and task.status == "success":
            browser.last_success_task_id = task.id
            site_key = self._safe_text(entities.get("site_key"))
            if site_key:
                browser.last_success_site_key = site_key
        session.browser_memory = browser

    def _sync_qa_memory(self, session: AgentSession, task: Task, data: dict[str, Any]) -> None:
        answer_block = data.get("answer") if isinstance(data.get("answer"), dict) else {}
        answer = self._safe_text(answer_block.get("answer"))
        if task.intent != "ops_qa" or task.status != "success" or not answer:
            return
        session.qa_memory = [turn for turn in getattr(session, "qa_memory", []) if turn.task_id != task.id]
        session.qa_memory.append(
            QATurn(
                task_id=task.id,
                question=self._truncate(self._safe_text(task.input), 600),
                answer=self._truncate(answer, 1000),
                created_at=task.created_at,
            )
        )
        session.qa_memory = session.qa_memory[-5:]

    def _sync_task_index(
        self,
        session: AgentSession,
        task: Task,
        observation: dict[str, Any],
        entities: dict[str, Any],
        report: str,
        task_input: str,
    ) -> None:
        data = _task_data(task)
        session.task_index = [entry for entry in getattr(session, "task_index", []) if entry.task_id != task.id]
        session.task_index.append(
            SessionTaskIndexEntry(
                task_id=task.id,
                intent=self._safe_text(task.intent),
                status=self._safe_text(task.status),
                system=self._safe_text(entities.get("system")),
                env=self._safe_text(entities.get("env")),
                target=self._safe_text(data.get("target") or entities.get("target")),
                capability=self._safe_text(data.get("capability") or entities.get("capability")),
                site_key=self._safe_text(entities.get("site_key")),
                url=self._safe_text(observation.get("url")),
                title=self._safe_text(observation.get("title")),
                summary=self._truncate(report or task_input, 500),
                created_at=self._safe_text(task.created_at),
            )
        )
        session.task_index = session.task_index[-50:]

    def _page_from_observation(self, task: Task, observation: dict[str, Any]) -> PageMemory | None:
        url = self._safe_text(observation.get("url"))
        title = self._safe_text(observation.get("title"))
        page_type = self._safe_text(observation.get("page_type"))
        if not (url or title or page_type):
            return None
        return PageMemory(
            task_id=task.id,
            url=url,
            title=title,
            page_type=page_type,
            observed_at=self._safe_text(task.updated_at or task.created_at),
        )

    def _sync_legacy_fields(self, session: AgentSession) -> None:
        browser = getattr(session, "browser_memory", None) or BrowserMemory()
        session.metadata = getattr(session, "metadata", {}) or {}
        session.recent_observations = [
            {
                "task_id": page.task_id,
                "url": page.url,
                "title": page.title,
                "page_type": page.page_type,
            }
            for page in browser.recent_pages[-5:]
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
        if browser.last_url:
            session.metadata["last_url"] = browser.last_url
        if browser.last_page_type:
            session.metadata["last_page_type"] = browser.last_page_type
        if browser.state_path:
            session.metadata["browser_state_path"] = browser.state_path
        if browser.last_success_task_id:
            session.metadata["browser_last_success_task_id"] = browser.last_success_task_id
        if browser.last_success_site_key:
            session.metadata["browser_last_success_site_key"] = browser.last_success_site_key

    def _rolling_summary(self, session: AgentSession) -> str:
        # 规则摘要：不依赖 LLM，稳定地把短期任务、QA、页面和 web 成功任务压成一句话。
        recent_tasks = [
            f"{turn.intent}:{turn.status}"
            for turn in getattr(session, "short_term", [])[-5:]
            if turn.intent or turn.status
        ]
        recent_qa = [
            f"Q:{self._truncate(self._redact(turn.question), 60)} A:{self._truncate(self._redact(turn.answer), 80)}"
            for turn in getattr(session, "qa_memory", [])[-2:]
        ]
        browser = getattr(session, "browser_memory", None) or BrowserMemory()
        recent_pages = [
            page.title or page.url or page.page_type
            for page in browser.recent_pages[-3:]
            if page.title or page.url or page.page_type
        ]
        recent_actions = [
            self._truncate(self._redact(turn.input), 80)
            for turn in getattr(session, "short_term", [])[-3:]
            if turn.status in {"success", "blocked", "failed", "awaiting_confirmation"}
        ]
        return (
            f"最近任务={'; '.join(recent_tasks) or '无'}; "
            f"最近QA={'; '.join(recent_qa) or '无'}; "
            f"最近页面={'; '.join(recent_pages) or '无'}; "
            f"最近成功web任务={browser.last_success_task_id or '无'}; "
            f"动作概览={'; '.join(recent_actions) or '无'}"
        )

    def _summary(self, session: AgentSession) -> str:
        strategy = self.summary_strategy
        if strategy is not None:
            try:
                # 可选 langmem/LLM 摘要。失败时静默降级到下面的规则摘要。
                summary = strategy.summarize(session)
                if summary:
                    return self._truncate(self._redact(summary), 1200)
            except Exception:
                pass
        latest = session.short_term[-1] if session.short_term else None
        parts = []
        if latest:
            parts.append(f"最近任务是 {latest.intent}，状态 {latest.status}，目标：{self._truncate(self._redact(latest.input), 120)}。")
        if session.qa_memory:
            parts.append(f"最近保留了 {len(session.qa_memory[-5:])} 轮运维问答上下文。")
        browser = getattr(session, "browser_memory", None) or BrowserMemory()
        if browser.last_url or browser.last_success_task_id:
            page = browser.recent_pages[-1].title or browser.last_url if browser.recent_pages else browser.last_url
            parts.append(
                f"浏览器最近页面：{self._truncate(self._redact(page), 120) or '无'}；"
                f"最近成功 web 任务：{browser.last_success_task_id or '无'}。"
            )
        return "".join(parts) or session.rolling_summary or "当前 session 暂无可总结的上下文。"

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

    def _redact(self, text: str) -> str:
        return self.sanitizer.sanitize(text, text_limit=5000)

    def _truncate(self, text: str, limit: int) -> str:
        text = self._safe_text(text).strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _safe_text(self, value: Any) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)


class LangMemSummaryStrategy:
    def __init__(self, model=None, *, max_tokens: int = 1024, max_summary_tokens: int = 256):
        self.model = model
        self.max_tokens = max_tokens
        self.max_summary_tokens = max_summary_tokens

    def summarize(self, session: AgentSession) -> str:
        if self.model is None:
            return ""
        try:
            from langchain_core.messages import HumanMessage
            from langmem.short_term import RunningSummary, summarize_messages
        except Exception:
            return ""
        messages = [
            HumanMessage(
                content=(
                    f"任务 {turn.task_id}: intent={turn.intent}, status={turn.status}, "
                    f"input={turn.input}, report={turn.report}"
                ),
                id=turn.task_id,
            )
            for turn in getattr(session, "short_term", [])[-10:]
        ]
        if not messages:
            return ""
        running_summary = None
        if session.summary:
            running_summary = RunningSummary(
                summary=session.summary,
                summarized_message_ids=set(),
                last_summarized_message_id=None,
            )
        result = summarize_messages(
            messages,
            running_summary=running_summary,
            model=self.model,
            max_tokens=self.max_tokens,
            max_tokens_before_summary=0,
            max_summary_tokens=self.max_summary_tokens,
        )
        summary = getattr(getattr(result, "running_summary", None), "summary", "")
        return str(summary or "")


class SessionMemoryManager:
    def __init__(
        self,
        store: BaseStore,
        *,
        sanitizer: MemorySanitizer | None = None,
        web_strategy: WebMemoryStrategy | None = None,
        knowledge_strategy: KnowledgeMemoryStrategy | None = None,
        migrator: LegacySessionMemoryMigrator | None = None,
    ):
        self.store = store
        self.sanitizer = sanitizer or MemorySanitizer()
        self.web_strategy = web_strategy or WebMemoryStrategy(self.sanitizer)
        self.knowledge_strategy = knowledge_strategy or KnowledgeMemoryStrategy(self.sanitizer)
        self.migrator = migrator or LegacySessionMemoryMigrator(
            web_strategy=self.web_strategy,
            knowledge_strategy=self.knowledge_strategy,
            sanitizer=self.sanitizer,
        )

    def sync(self, session: AgentSession, task: Task) -> dict[str, Any]:
        # LangGraph Store 里的结构化记忆：按 namespace 拆开 web/knowledge/task_index，
        # 方便下一轮按 intent/query 检索，而不是只依赖 session.summary。
        migrated = self.migrator.migrate(self.store, session)
        namespaces: list[tuple[str, ...]] = []
        if task.intent == "web_action" or getattr(session, "browser_memory", None):
            namespace = self._namespace(session.id, WEB_NAMESPACE)
            self.store.put(namespace, "context", self.web_strategy.context(session, task))
            namespaces.append(namespace)
            trace_item = self.web_strategy.trace_item(task)
            if trace_item:
                self.store.put(namespace, f"trace:{task.id}", trace_item)
        if task.intent in {"ops_qa", "knowledge_write"} or getattr(session, "qa_memory", None):
            namespace = self._namespace(session.id, KNOWLEDGE_NAMESPACE)
            self.store.put(namespace, "context", self.knowledge_strategy.context(session, task))
            namespaces.append(namespace)
        self._sync_task_index(session)
        return {"migrated_legacy": migrated, "namespaces": namespaces}

    def retrieve(
        self,
        session: AgentSession,
        intent: str,
        query: str,
        *,
        limit: int = 5,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 规划前调用：把 Store 中的结构化上下文合并回 session_memory，
        # planner 再据此恢复浏览器 state_path、最近 QA 或历史任务匹配。
        self.migrator.migrate(self.store, session)
        self._refresh_contexts_from_session(session, intent)
        memory = dict(fallback or {})
        web_context = self._get_value(self._namespace(session.id, WEB_NAMESPACE), "context")
        if web_context:
            existing = dict(memory.get("browser_memory") or {})
            existing.update(web_context)
            memory["browser_memory"] = existing

        knowledge_context = self._get_value(self._namespace(session.id, KNOWLEDGE_NAMESPACE), "context")
        if knowledge_context and intent in {"ops_qa", "knowledge_write"}:
            turns = knowledge_context.get("recent_qa_summaries") or []
            if turns:
                memory["qa_memory"] = turns[-limit:]

        matches = self._task_matches(session.id, intent, query, limit)
        if matches:
            memory["task_matches"] = matches
        return memory

    def _refresh_contexts_from_session(self, session: AgentSession, intent: str) -> None:
        if intent == "web_action" or getattr(session, "browser_memory", None):
            self.store.put(self._namespace(session.id, WEB_NAMESPACE), "context", self.web_strategy.context(session))
        if intent in {"ops_qa", "knowledge_write"} or getattr(session, "qa_memory", None):
            namespace = self._namespace(session.id, KNOWLEDGE_NAMESPACE)
            existing = self._get_value(namespace, "context")
            context = self.knowledge_strategy.context(session)
            if (
                existing.get("last_answer_summary")
                and existing.get("last_answer_summary") == context.get("last_answer_summary")
                and not context.get("recent_sources")
            ):
                context["recent_sources"] = list(existing.get("recent_sources") or [])
                context["health"] = dict(existing.get("health") or {})
            self.store.put(
                namespace,
                "context",
                context,
            )
        self._sync_task_index(session)

    def _sync_task_index(self, session: AgentSession) -> None:
        for entry in getattr(session, "task_index", []) or []:
            if not entry.task_id:
                continue
            namespace = self._namespace(session.id, TASK_INDEX_NAMESPACE, entry.intent or "unknown")
            self.store.put(namespace, entry.task_id, self.sanitizer.sanitize(asdict(entry)))

    def _task_matches(self, session_id: str, intent: str, query: str, limit: int) -> list[dict[str, Any]]:
        namespace = self._namespace(session_id, TASK_INDEX_NAMESPACE, intent or "unknown")
        matches = [item.value for item in self.store.search(namespace, query=query, limit=limit)]
        if matches:
            return matches
        prefix = self._namespace(session_id, TASK_INDEX_NAMESPACE)
        return [item.value for item in self.store.search(prefix, query=query, limit=limit)]

    def _get_value(self, namespace: tuple[str, ...], key: str) -> dict[str, Any]:
        item = self.store.get(namespace, key)
        if item is None or not isinstance(item.value, dict):
            return {}
        return dict(item.value)

    def _namespace(self, session_id: str, *parts: str) -> tuple[str, ...]:
        return ("sessions", session_id, *parts)


def _task_data(task: Task) -> dict[str, Any]:
    result = task.result if isinstance(task.result, dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return data


def qa_turns_from_legacy_metadata(raw_turns: str | None) -> list[dict[str, Any]]:
    if not raw_turns:
        return []
    try:
        parsed = json.loads(raw_turns)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]
