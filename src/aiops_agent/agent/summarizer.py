from __future__ import annotations

from typing import Any

from aiops_agent.tasks.models import Task


class ResultSummarizer:
    def summarize(self, task: Task, tool_result: dict[str, Any]) -> str:
        data = tool_result.get("data") or {}
        error = tool_result.get("error") or "无"
        suggestions = self._build_suggestion(task.status, data, error)
        lines = [
            f"任务类型：{task.intent}",
            f"执行状态：{task.status}",
            f"风险等级：{task.risk_level}",
            f"异常信息：{error}",
            f"执行计划：{self._format_plan(task)}",
            f"建议操作：{suggestions}",
        ]
        return "\n".join(lines)

    def _format_plan(self, task: Task) -> str:
        if task.plan is None:
            return "无"
        return "；".join(task.plan.steps)

    def _build_suggestion(self, status: str, data: dict[str, Any], error: str) -> str:
        if status == "success":
            if data.get("anomalies"):
                return "请根据异常列表逐项复核并安排处置。"
            return "巡检通过，无需额外处理。"

        if status == "awaiting_confirmation":
            return "请先完成人工确认，再继续执行高风险任务。"

        if status == "blocked":
            return "当前任务已被策略阻断，请根据提示调整任务或等待后续能力开放。"

        if "暂不支持" in error:
            return "请改为巡检类指令，或等待后续阶段开放该能力。"

        if "配置" in error:
            return "请补齐 RPA 平台地址、认证信息和流程映射后重试。"

        if "知识库" in error or "问答" in error:
            return "知识库工具尚未接入，可先扩展检索工具后再执行。"

        return "请检查 RPA 平台连通性、流程配置和输入参数后重试。"
