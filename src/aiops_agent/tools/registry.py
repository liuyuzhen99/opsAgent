from __future__ import annotations

from aiops_agent.tools.base import ToolError


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, object] = {}

    def register(self, tool_name: str, tool: object) -> None:
        self._tools[tool_name] = tool

    def execute(self, tool_name: str, params: dict):
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolError(f"未注册的工具: {tool_name}")
        return tool.execute(params)
