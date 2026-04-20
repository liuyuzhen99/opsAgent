from __future__ import annotations

from aiops_agent.tasks.models import Task


class TaskManager:
    def __init__(self, store):
        self.store = store

    def create_task(self, task_type: str, task_input: str, trace_id: str) -> Task:
        task = Task(type=task_type, input=task_input, trace_id=trace_id)
        self.persist(task)
        return task

    def mark_running(self, task: Task) -> None:
        task.status = "running"
        self.persist(task)

    def mark_success(self, task: Task, result: dict) -> None:
        task.status = "success"
        task.result = result
        self.persist(task)

    def mark_failed(self, task: Task, result: dict) -> None:
        task.status = "failed"
        task.result = result
        self.persist(task)

    def persist(self, task: Task) -> None:
        self.store.save(task)
