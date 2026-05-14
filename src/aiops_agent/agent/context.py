from __future__ import annotations

from typing import Any

from aiops_agent.sessions.models import AgentSession
from aiops_agent.tasks.models import Task


class ContextCompressor:
    def compress(self, session: AgentSession, task: Task) -> AgentSession:
        data: dict[str, Any] = {}
        if task.result:
            data = task.result.get("data") or {}
        last_observation = data.get("last_observation") or {}
        if last_observation:
            session.recent_observations.append(
                {
                    "task_id": task.id,
                    "url": str(last_observation.get("url", "")),
                    "title": str(last_observation.get("title", "")),
                    "page_type": str(last_observation.get("page_type", "")),
                }
            )
            session.recent_observations = session.recent_observations[-5:]
            session.metadata["last_url"] = str(last_observation.get("url", ""))
            session.metadata["last_page_type"] = str(last_observation.get("page_type", ""))
        if data.get("session_state_path"):
            session.metadata["browser_state_path"] = str(data["session_state_path"])
        if task.status == "success" and task.intent == "web_action":
            session.metadata["browser_last_success_task_id"] = task.id
        completed = [
            step.get("action", {}).get("type", "")
            for step in data.get("steps", [])
            if step.get("result") == "success"
        ]
        current_page = ""
        if last_observation:
            current_page = last_observation.get("title") or last_observation.get("url") or ""
        task_input = "知识库写入请求" if task.intent == "knowledge_write" else task.input
        session.rolling_summary = (
            f"用户目标={task_input}; 已完成动作={','.join(completed[-8:]) or '无'}; "
            f"当前页面={current_page or '无'}; 状态={task.status}"
        )
        session.summary = f"last_intent={task.intent}; last_status={task.status}; {session.rolling_summary}"
        return session
