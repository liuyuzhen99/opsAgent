from __future__ import annotations

from typing import Any

from aiops_agent.tasks.models import Task


class ResultSummarizer:
    def summarize(self, task: Task, tool_result: dict[str, Any]) -> str:
        data = tool_result.get("data") or {}
        if task.intent == "general_chat":
            return str(data.get("reply") or "你好，我是 opsAgent。")
        if task.intent == "ops_qa":
            answer_block = data.get("answer") or {}
            answer_text = answer_block.get("answer") or ""
            sources = answer_block.get("sources") or []
            evaluation = answer_block.get("evaluation")
            if answer_text:
                lines = [answer_text]
                if sources:
                    lines.append("\n来源文档：")
                    for src in sources:
                        lines.append(f"  - {src.get('title', '')} ({src.get('section', '')})")
                if evaluation:
                    faith = evaluation.get("faithfulness", 0)
                    rel = evaluation.get("relevance", 0)
                    conf = round((faith + rel) / 2, 2)
                    lines.append(f"\n置信度：{conf} | 忠实度：{faith} | 相关性：{rel}")
                return "\n".join(lines)
        if task.intent == "knowledge_write":
            if tool_result.get("success"):
                mode = "Dry-run 预览完成" if data.get("dry_run") else "知识笔记已写入"
                return "\n".join(
                    [
                        f"{mode}：{data.get('title') or '-'}",
                        f"笔记路径：{data.get('note_path') or '-'}",
                        f"类型：{data.get('type') or '-'}",
                        f"MOC：{data.get('moc_path') or '-'}",
                        f"MOC 更新：{'是' if data.get('moc_updated') else '否'}",
                        f"索引状态：{data.get('reindex_status') or '-'}",
                    ]
                )
            missing = data.get("missing_info") or []
            lines = [
                "知识笔记写入失败。",
                f"原因：{tool_result.get('error') or '未知错误'}",
            ]
            if missing:
                lines.append(f"缺失配置：{', '.join(missing)}")
            if data.get("note_path"):
                lines.append(f"目标路径：{data.get('note_path')}")
            return "\n".join(lines)
        if task.intent == "web_action":
            answer_block = data.get("answer") or {}
            answer_text = answer_block.get("answer") if isinstance(answer_block, dict) else ""
            if answer_text:
                return str(answer_text)
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
