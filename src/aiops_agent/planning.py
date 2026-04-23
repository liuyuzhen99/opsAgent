from __future__ import annotations

from aiops_agent.tasks.models import ExecutionPlan, ToolCallSpec


class PlanningService:
    def plan(self, task_input: str, intent: str, entities: dict[str, object]) -> ExecutionPlan:
        if intent == "inspection":
            params = {
                "system": entities.get("system"),
                "env": entities.get("env"),
                "raw_text": entities.get("raw_text", task_input),
            }
            return ExecutionPlan(
                goal="完成目标系统巡检并返回结构化结果",
                steps=[
                    "解析巡检目标系统与环境",
                    "调用 inspection 工具执行巡检",
                    "整理巡检结果、异常与建议",
                ],
                selected_tools=["inspection"],
                tool_calls=[
                    ToolCallSpec(
                        tool_name="inspection",
                        action="run_inspection",
                        params=params,
                        risk_level="read_only",
                    )
                ],
                risk_level="read_only",
                confirmation_required=False,
                success_criteria=["返回巡检结果", "输出异常列表或确认系统健康"],
            )

        if intent == "permission_change":
            return ExecutionPlan(
                goal="规划权限变更任务并等待人工确认",
                steps=[
                    "提取权限变更对象、环境和目标权限",
                    "执行风险评估",
                    "在确认后再进入实际变更工具链",
                ],
                risk_level="high_risk_change",
                confirmation_required=True,
                success_criteria=["生成人工确认摘要", "阻止未确认的高风险执行"],
                notes=["本阶段仅规划与治理，不执行真实权限变更。"],
            )

        if intent == "ops_qa":
            return ExecutionPlan(
                goal="提供运维知识问答入口占位响应",
                steps=[
                    "记录问答请求",
                    "为后续知识检索工具保留统一执行接口",
                    "返回当前阶段能力说明",
                ],
                risk_level="read_only",
                confirmation_required=False,
                success_criteria=["给出明确占位反馈，且不误执行任何变更动作"],
                notes=["知识检索工具将在后续阶段接入。"],
            )

        if intent == "web_action":
            return ExecutionPlan(
                goal="为后续网页自动化能力保留统一入口",
                steps=[
                    "识别网页任务目标",
                    "预留统一编排与风险控制接口",
                    "当前阶段返回能力边界说明",
                ],
                risk_level="controlled_change",
                confirmation_required=True,
                success_criteria=["明确告知能力已预留但未完全开放"],
                notes=["Playwright 自主执行不在本阶段交付范围内。"],
            )

        return ExecutionPlan(
            goal="处理未知任务类型",
            steps=["拒绝当前未知任务并提示能力边界"],
            risk_level="read_only",
            confirmation_required=False,
            success_criteria=["返回明确错误信息"],
        )
