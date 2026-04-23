from __future__ import annotations

from datetime import UTC, datetime

from aiops_agent.tasks.models import Task


class TaskManager:
    def __init__(self, store):
        self.store = store

    def create_task(
        self,
        task_input: str,
        trace_id: str,
        *,
        session_id: str | None = None,
        llm_profile: str | None = None,
        max_steps: int = 20,
        requires_explicit_confirmation: bool = False,
    ) -> Task:
        task = Task(
            input=task_input,
            trace_id=trace_id,
            session_id=session_id,
            llm_profile=llm_profile,
            max_steps=max_steps,
            requires_explicit_confirmation=requires_explicit_confirmation,
        )
        self.persist(task)
        return task

    def mark_running(self, task: Task) -> None:
        task.status = "running"
        task.current_stage = "running"
        self.persist(task)

    def mark_success(self, task: Task, result: dict) -> None:
        task.status = "success"
        task.current_stage = "completed"
        task.result = result
        self.persist(task)

    def mark_failed(self, task: Task, result: dict) -> None:
        task.status = "failed"
        task.current_stage = "failed"
        task.result = result
        self.persist(task)

    def mark_awaiting_confirmation(self, task: Task, result: dict) -> None:
        task.status = "awaiting_confirmation"
        task.current_stage = "awaiting_confirmation"
        task.result = result
        self.persist(task)

    def mark_blocked(self, task: Task, result: dict) -> None:
        task.status = "blocked"
        task.current_stage = "blocked"
        task.result = result
        self.persist(task)

    def persist(self, task: Task) -> None:
        task.updated_at = datetime.now(UTC).isoformat()
        self.store.save(task)
