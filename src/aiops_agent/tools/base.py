from __future__ import annotations


class ToolError(Exception):
    """Raised when a tool cannot be invoked correctly."""


class BaseTool:
    def execute(self, params: dict):
        raise NotImplementedError
