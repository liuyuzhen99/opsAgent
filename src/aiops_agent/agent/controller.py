from __future__ import annotations

import logging

from aiops_agent.audit.models import AuditEvent
from aiops_agent.support.logging import log_kv
from aiops_agent.support.trace import get_trace_id
from aiops_agent.tasks.manager import TaskManager
from aiops_agent.tasks.models import Task
from aiops_agent.tools.base import ToolError
from aiops_agent.tools.registry import ToolRegistry


class AgentController:
    def __init__(
        self,
        parser,
        task_manager: TaskManager,
        tool_registry: ToolRegistry,
        summarizer,
        audit_logger,
        logger=None,
    ):
        self.parser = parser
        self.task_manager = task_manager
        self.tool_registry = tool_registry
        self.summarizer = summarizer
        self.audit_logger = audit_logger
        self.logger = logger or logging.getLogger(__name__)

    def run(self, task_input: str) -> Task:
        trace_id = get_trace_id()
        log_kv(self.logger, logging.INFO, "Received task input", trace_id=trace_id)
        intent_result = self.parser.parse(task_input)
        task = self.task_manager.create_task(
            task_type=intent_result.intent,
            task_input=task_input,
            trace_id=trace_id,
        )
        self.audit_logger.record(
            AuditEvent(
                event_type="task.created",
                trace_id=trace_id,
                task_id=task.id,
                status=task.status,
                details={"intent": intent_result.intent},
            )
        )
        log_kv(
            self.logger,
            logging.INFO,
            "Intent parsed",
            intent=intent_result.intent,
            task_id=task.id,
        )

        if intent_result.intent != "inspection":
            self.task_manager.mark_failed(
                task,
                {
                    "error": f"暂不支持的任务类型: {intent_result.intent}",
                    "data": {"entities": intent_result.entities},
                },
            )
            task.report = self.summarizer.summarize(task, task.result or {})
            self.task_manager.persist(task)
            self.audit_logger.record(
                AuditEvent(
                    event_type="task.rejected",
                    trace_id=trace_id,
                    task_id=task.id,
                    status=task.status,
                    details={"reason": task.result["error"]},
                )
            )
            return task

        self.task_manager.mark_running(task)
        self.audit_logger.record(
            AuditEvent(
                event_type="task.started",
                trace_id=trace_id,
                task_id=task.id,
                status=task.status,
                details={"tool": "inspection"},
            )
        )

        try:
            tool_result = self.tool_registry.execute("inspection", intent_result.entities)
            if tool_result.success:
                self.task_manager.mark_success(task, tool_result.to_dict())
            else:
                self.task_manager.mark_failed(task, tool_result.to_dict())
        except ToolError as exc:
            self.task_manager.mark_failed(task, {"success": False, "error": str(exc), "data": {}})

        task.report = self.summarizer.summarize(task, task.result or {})
        self.task_manager.persist(task)
        self.audit_logger.record(
            AuditEvent(
                event_type="task.completed",
                trace_id=trace_id,
                task_id=task.id,
                status=task.status,
                details={
                    "result_success": bool(task.result and task.result.get("success")),
                    "error": (task.result or {}).get("error"),
                },
            )
        )
        log_kv(
            self.logger,
            logging.INFO,
            "Task finished",
            task_id=task.id,
            status=task.status,
        )
        return task
