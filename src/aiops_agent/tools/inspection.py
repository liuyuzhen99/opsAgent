from __future__ import annotations

import logging
from typing import Any

from aiops_agent.config import RPAConfig
from aiops_agent.support.logging import log_kv
from aiops_agent.tasks.models import ToolExecutionResult
from aiops_agent.tools.base import BaseTool
from aiops_agent.tools.rpa_runner import RPARunner


class InspectionTool(BaseTool):
    def __init__(self, config: RPAConfig):
        self.config = config
        self.runner = RPARunner(config)
        self.logger = logging.getLogger(__name__)

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        validation_error = self._validate_config()
        if validation_error:
            return ToolExecutionResult(success=False, data={}, error=validation_error)

        system = params.get("system") or self.config.inspection.default_system
        env = params.get("env") or self.config.inspection.default_env
        flow_id = self.config.inspection.flow_map.get(system)

        if not flow_id:
            return ToolExecutionResult(
                success=False,
                data={},
                error=f"配置缺失: 未找到系统 {system} 的巡检流程映射",
            )

        payload = {
            "flow_id": flow_id,
            "system": system,
            "env": env,
            "task_text": params.get("raw_text", ""),
        }
        invocation = self.runner.invoke_flow(
            flow_id,
            payload,
            fallback_endpoint="inspection",
            allow_global_robot_uuid=True,
        )
        if not invocation.success:
            return invocation

        response_data = dict(invocation.data)
        response_data.setdefault("system", system)
        response_data.setdefault("env", env)
        response_data.setdefault("flow_id", flow_id)
        normalized = self._normalize_response(system, env, flow_id, response_data)
        log_kv(
            self.logger,
            logging.INFO,
            "Inspection tool executed",
            system=system,
            env=env,
            flow_id=flow_id,
            success=normalized["success"],
        )
        return ToolExecutionResult(
            success=normalized["success"],
            data=normalized["data"],
            error=normalized["error"],
        )

    def _validate_config(self) -> str | None:
        if not self.config.inspection.flow_map:
            return "配置缺失: inspection.flow_map 未设置"
        return self.runner.validate_runtime()

    def _normalize_response(
        self, system: str, env: str, flow_id: str, response_data: dict[str, Any]
    ) -> dict[str, Any]:
        raw_success = response_data.get("success")
        success = raw_success if isinstance(raw_success, bool) else response_data.get("status") == "success"
        data = {
            "system": response_data.get("system", system),
            "env": response_data.get("env", env),
            "flow_id": response_data.get("flow_id", flow_id),
            "inspection_result": response_data.get("result", "completed"),
            "anomalies": response_data.get("anomalies", []),
            "operation_log": response_data.get("operation_log", []),
        }
        error_message = response_data.get("error")
        if not success and not error_message:
            error_message = "巡检执行失败"
        return {"success": bool(success), "data": data, "error": error_message}
