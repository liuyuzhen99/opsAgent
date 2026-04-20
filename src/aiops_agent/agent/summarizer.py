from __future__ import annotations

from typing import Any

from aiops_agent.tasks.models import Task


class ResultSummarizer:
    def summarize(self, task: Task, tool_result: dict[str, Any]) -> str:
        data = tool_result.get("data") or {}
        error = tool_result.get("error") or "无"
        suggestions = self._build_suggestion(task.status, data, error)
        lines = [
            f"任务类型：{task.type}",
            f"执行状态：{task.status}",
            f"异常信息：{error}",
            f"建议操作：{suggestions}",
        ]
        return "\n".join(lines)

    def _build_suggestion(self, status: str, data: dict[str, Any], error: str) -> str:
        if status == "success":
            if data.get("anomalies"):
                return "请根据异常列表逐项复核并安排处置。"
            return "巡检通过，无需额外处理。"

        if "暂不支持" in error:
            return "请改为巡检类指令，或等待后续阶段开放该能力。"

        if "配置" in error:
            return "请补齐 RPA 平台地址、认证信息和流程映射后重试。"

        return "请检查 RPA 平台连通性、流程配置和输入参数后重试。"
