from __future__ import annotations

from aiops_agent.tasks.models import ToolCallSpec, ToolExecutionResult
from aiops_agent.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def execute(self, call_spec: ToolCallSpec) -> ToolExecutionResult:
        return self.registry.execute(call_spec)
