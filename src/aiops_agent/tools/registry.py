from __future__ import annotations

from dataclasses import dataclass, field

from aiops_agent.tasks.models import ToolCallSpec, ToolExecutionResult
from aiops_agent.tools.base import ToolError


@dataclass(slots=True)
class ToolDefinition:
    # 这里保存的是工具目录元数据，不是强 schema/capability manifest。
    # 当前 executor 不会根据 risk_level/timeout_seconds 做统一拦截。
    name: str
    tool: object
    risk_level: str = "read_only"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    timeout_seconds: int | None = None


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        tool_name: str,
        tool: object,
        *,
        risk_level: str = "read_only",
        description: str = "",
        tags: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self._tools[tool_name] = ToolDefinition(
            name=tool_name,
            tool=tool,
            risk_level=risk_level,
            description=description,
            tags=tags or [],
            timeout_seconds=timeout_seconds,
        )

    def get(self, tool_name: str) -> ToolDefinition:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolError(f"未注册的工具: {tool_name}")
        return tool

    def execute(
        self, tool_or_spec: str | ToolCallSpec, params: dict | None = None
    ) -> ToolExecutionResult:
        if isinstance(tool_or_spec, ToolCallSpec):
            spec = tool_or_spec
        else:
            spec = ToolCallSpec(tool_name=tool_or_spec, action="execute", params=params or {})
        definition = self.get(spec.tool_name)
        # 轻量调用边界：真正的 input schema、权限、timeout 目前由上游 planner/policy
        # 或具体 tool 自己处理；registry 只负责按名字找到 tool 并调用 execute(params)。
        return definition.tool.execute(spec.params)
