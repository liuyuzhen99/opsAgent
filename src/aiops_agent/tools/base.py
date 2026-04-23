from __future__ import annotations

from aiops_agent.tasks.models import ToolExecutionResult


class ToolError(Exception):
    """Raised when a tool cannot be invoked correctly."""


class BaseTool:
    def execute(self, params: dict) -> ToolExecutionResult:
        raise NotImplementedError
