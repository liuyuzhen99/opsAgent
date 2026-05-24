from __future__ import annotations

import logging
from typing import Any

from aiops_agent.config import RPAConfig
from aiops_agent.support.logging import log_kv
from aiops_agent.tasks.models import ToolExecutionResult
from aiops_agent.tools.base import BaseTool
from aiops_agent.tools.rpa_runner import RPARunner


class RPAActionTool(BaseTool):
    SUPPORTED_CAPABILITIES = {"ssh", "sftp", "db"}

    def __init__(self, config: RPAConfig):
        self.config = config
        self.runner = RPARunner(config)
        self.logger = logging.getLogger(__name__)

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        validation_error = self.runner.validate_runtime()
        if validation_error:
            return ToolExecutionResult(success=False, data={}, error=validation_error)

        operation = str(params.get("operation") or "login").strip().lower()
        if operation != "login":
            return ToolExecutionResult(
                success=False,
                data={"operation": operation},
                error=f"暂不支持 RPA 动作: {operation}",
            )

        target = str(params.get("target") or "").strip()
        capability = self._normalize_capability(str(params.get("capability") or ""))
        if not target:
            return ToolExecutionResult(success=False, data={}, error="配置缺失: 未识别 RPA 登录目标")
        if capability not in self.SUPPORTED_CAPABILITIES:
            return ToolExecutionResult(
                success=False,
                data={"target": target, "capability": capability},
                error=f"配置缺失: 未识别 {target} 的 RPA 登录类型",
            )

        target_config = self.config.rpa_actions.targets.get(target)
        if target_config is None:
            return ToolExecutionResult(
                success=False,
                data={"target": target, "capability": capability},
                error=f"配置缺失: 未配置 {target} 的 RPA 登录目标",
            )

        flow_id = target_config.flow_for(capability)
        if not flow_id:
            return ToolExecutionResult(
                success=False,
                data={"target": target, "capability": capability},
                error=f"配置缺失: 未配置 {target} 的 {capability} 登录 RPA",
            )

        payload = {
            "flow_id": flow_id,
            "target": target,
            "capability": capability,
            "operation": operation,
            "task_text": params.get("raw_text", ""),
        }
        invocation = self.runner.invoke_flow(
            flow_id,
            payload,
            fallback_endpoint="rpa",
            allow_global_robot_uuid=False,
        )
        if not invocation.success:
            return invocation

        normalized = self._normalize_response(target, capability, operation, flow_id, dict(invocation.data))
        log_kv(
            self.logger,
            logging.INFO,
            "RPA action tool executed",
            target=target,
            capability=capability,
            operation=operation,
            flow_id=flow_id,
            success=normalized["success"],
        )
        return ToolExecutionResult(
            success=normalized["success"],
            data=normalized["data"],
            error=normalized["error"],
        )

    def _normalize_response(
        self,
        target: str,
        capability: str,
        operation: str,
        flow_id: str,
        response_data: dict[str, Any],
    ) -> dict[str, Any]:
        raw_success = response_data.get("success")
        success = raw_success if isinstance(raw_success, bool) else response_data.get("status") == "success"
        action_result = response_data.get("result") or ("completed" if success else "failed")
        data = {
            "target": response_data.get("target", target),
            "capability": response_data.get("capability", capability),
            "operation": response_data.get("operation", operation),
            "flow_id": response_data.get("flow_id", flow_id),
            "action_result": action_result,
            "operation_log": response_data.get("operation_log", []),
        }
        error_message = response_data.get("error")
        if not success and not error_message:
            error_message = f"{target} 的 {capability} 登录 RPA 执行失败"
        return {"success": bool(success), "data": data, "error": error_message}

    def _normalize_capability(self, capability: str) -> str:
        normalized = capability.strip().lower()
        if normalized in {"database", "sql", "plsql", "pl/sql", "数据库"}:
            return "db"
        return normalized
