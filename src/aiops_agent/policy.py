from __future__ import annotations

from aiops_agent.tasks.models import ExecutionPlan, PolicyDecision, Task


class PolicyEngine:
    def evaluate(self, task: Task, plan: ExecutionPlan) -> PolicyDecision:
        requires_confirmation = plan.confirmation_required or task.requires_explicit_confirmation
        risk_level = "high_risk_change" if task.intent == "permission_change" else plan.risk_level

        if task.intent == "web_action":
            return PolicyDecision(
                allowed=False,
                requires_confirmation=True,
                risk_level=risk_level,
                reason="web_action 能力已完成接口预留，但本阶段尚未开放自动执行。",
                status="blocked",
            )

        if requires_confirmation:
            return PolicyDecision(
                allowed=False,
                requires_confirmation=True,
                risk_level=risk_level,
                reason="检测到高风险或需人工确认的任务，已进入等待确认状态。",
                status="awaiting_confirmation",
            )

        return PolicyDecision(
            allowed=True,
            requires_confirmation=False,
            risk_level=risk_level,
            reason="策略检查通过。",
            status="approved",
        )
