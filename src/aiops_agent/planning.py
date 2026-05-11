from __future__ import annotations

from urllib.parse import urlparse

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
            conversation_history = list(entities.get("conversation_history") or [])
            return ExecutionPlan(
                goal="通过 Obsidian vault 知识库检索并合成运维问答",
                steps=[
                    "改写查询（消解多轮指代）",
                    "混合检索知识库文档（BM25 + 向量 RRF 融合）",
                    "LLM 合成答案并附来源文档",
                ],
                selected_tools=["knowledge"],
                tool_calls=[
                    ToolCallSpec(
                        tool_name="knowledge",
                        action="query",
                        params={
                            "question": entities.get("raw_text", task_input),
                            "conversation_history": conversation_history,
                        },
                        risk_level="read_only",
                    )
                ],
                risk_level="read_only",
                confirmation_required=False,
                success_criteria=["返回知识库检索答案及来源文档"],
            )

        if intent == "general_chat":
            return ExecutionPlan(
                goal="在 chat 模式下回复普通对话",
                steps=[
                    "识别为非运维执行任务",
                    "调用聊天回复工具生成自然语言反馈",
                    "提示用户可继续输入运维任务",
                ],
                selected_tools=["chat"],
                tool_calls=[
                    ToolCallSpec(
                        tool_name="chat",
                        action="reply",
                        params={"message": entities.get("raw_text", task_input)},
                        risk_level="read_only",
                    )
                ],
                risk_level="read_only",
                confirmation_required=False,
                success_criteria=["返回自然语言回复", "不误触发运维工具"],
            )

        if intent == "web_action":
            start_url = entities.get("start_url")
            raw_text = str(entities.get("raw_text", task_input))
            allowed_domains = list(entities.get("allowed_domains") or [])
            if start_url and not allowed_domains:
                host = urlparse(str(start_url)).netloc
                if host:
                    allowed_domains.append(host)
            has_side_effect = bool(entities.get("has_side_effect"))
            workflow = None
            workflow_fields = {}
            risk_level = "unsafe_mutation" if has_side_effect else "safe_read"
            params = {
                "start_url": start_url,
                "user_goal": raw_text,
                "success_criteria": ["完成用户描述的网页任务", "保存最终页面 observation 与截图"],
                "forbidden_actions": ["绕过验证码/MFA", "访问非允许域名", "未确认执行远端写入"],
                "allowed_domains": allowed_domains,
                "credential_ref": entities.get("credential_ref"),
                "requires_login": bool(entities.get("requires_login")),
                "requires_remote_mutation": has_side_effect,
                "auto_plan": True,
                "site_key": entities.get("site_key"),
                "workflow": None,
                "workflow_fields": workflow_fields,
                "site_config": entities.get("site_config") or {},
                "browser_config_error": entities.get("browser_config_error"),
                "browser_channel": entities.get("browser_channel"),
                "browser_slow_mo_ms": int(entities.get("browser_slow_mo_ms", 0)),
                "trace_enabled": bool(entities.get("trace_enabled")),
                "video_enabled": bool(entities.get("video_enabled")),
                "max_steps": int(entities.get("max_steps", 20)),
                "actions": [],
            }
            return ExecutionPlan(
                goal="在受控动作集合内执行网页自动化任务",
                steps=[
                    "生成结构化网页任务规格",
                    "启动单 session 浏览器上下文",
                    "每步依据最新 observation 规划下一步受限动作",
                    "遇到远端副作用动作前进入人工确认",
                ],
                selected_tools=["browser_agent"],
                tool_calls=[
                    ToolCallSpec(
                        tool_name="browser_agent",
                        action="run_browser_task",
                        params=params,
                        risk_level=risk_level,
                    )
                ],
                risk_level=risk_level,
                confirmation_required=False,
                success_criteria=["返回结构化 observation", "保存关键 artifact", "审计记录覆盖每一步动作"],
                notes=["第一版采用规则生成初始动作；后续可接入 LLM Planner 生成同一动作协议。"],
            )

        return ExecutionPlan(
            goal="处理未知任务类型",
            steps=["拒绝当前未知任务并提示能力边界"],
            risk_level="read_only",
            confirmation_required=False,
            success_criteria=["返回明确错误信息"],
        )
