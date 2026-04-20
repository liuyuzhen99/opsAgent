from __future__ import annotations

import json
import logging
import platform
import subprocess
from pathlib import Path
from typing import Any
from urllib import error, request

from aiops_agent.config import RPAConfig
from aiops_agent.support.logging import log_kv
from aiops_agent.tasks.models import ToolResult
from aiops_agent.tools.base import BaseTool


class InspectionTool(BaseTool):
    def __init__(self, config: RPAConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)

    def execute(self, params: dict[str, Any]) -> ToolResult:
        validation_error = self._validate_config()
        if validation_error:
            return ToolResult(success=False, data={}, error=validation_error)

        system = params.get("system") or self.config.inspection.default_system
        env = params.get("env") or self.config.inspection.default_env
        flow_id = self.config.inspection.flow_map.get(system)

        if not flow_id:
            return ToolResult(success=False, data={}, error=f"配置缺失: 未找到系统 {system} 的巡检流程映射")

        payload = {
            "flow_id": flow_id,
            "system": system,
            "env": env,
            "task_text": params.get("raw_text", ""),
        }
        if self.config.execution_mode == "shadowbot_local":
            return self._execute_shadowbot_local(system, env, flow_id, payload)

        endpoint = self._build_endpoint(flow_id)
        timeout = self.config.timeout_seconds

        headers = {"Content-Type": "application/json"}
        token = self.config.auth.token
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            return ToolResult(
                success=False,
                data={"status_code": exc.code},
                error=f"RPA 平台调用失败: HTTP {exc.code}",
            )
        except error.URLError as exc:
            return ToolResult(success=False, data={}, error=f"RPA 平台不可达: {exc.reason}")

        try:
            response_data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return ToolResult(success=False, data={}, error="RPA 平台返回了非 JSON 数据")

        if not response_data:
            return ToolResult(success=False, data={}, error="RPA 平台返回空结果")

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
        return ToolResult(
            success=normalized["success"],
            data=normalized["data"],
            error=normalized["error"],
        )

    def _validate_config(self) -> str | None:
        if not self.config.inspection.flow_map:
            return "配置缺失: inspection.flow_map 未设置"
        if self.config.execution_mode == "api":
            if not self.config.platform_url:
                return "配置缺失: platform_url 未设置"
            if self.config.auth.type == "bearer" and not self.config.auth.token:
                return "配置缺失: bearer token 未设置"
        elif self.config.execution_mode == "shadowbot_local":
            if not self.config.shadowbot.executable_path:
                return "配置缺失: shadowbot.executable_path 未设置"
        else:
            return "配置缺失: execution_mode 无效"
        return None

    def _build_endpoint(self, flow_id: str) -> str:
        platform_url = self.config.platform_url.rstrip("/")
        provider = self.config.provider.lower()
        if provider == "yidao":
            return f"{platform_url}/api/v1/flows/{flow_id}/run"
        return f"{platform_url}/api/v1/inspection/run"

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

    def _execute_shadowbot_local(
        self,
        system: str,
        env: str,
        flow_id: str,
        payload: dict[str, Any],
    ) -> ToolResult:
        if platform.system() != "Windows":
            return ToolResult(
                success=False,
                data={},
                error="ShadowBot 免费版本地启动模式仅支持在 Windows 上执行",
            )

        robot_uuid = self.config.shadowbot.robot_uuid or flow_id
        shadowbot_uri = f"shadowbot:Run?robot-uuid={robot_uuid}"
        command = [
            "cmd",
            "/c",
            "start",
            "",
            self.config.shadowbot.executable_path,
            shadowbot_uri,
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.config.shadowbot.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data={}, error="ShadowBot 启动命令超时")
        except subprocess.CalledProcessError as exc:
            error_text = (exc.stderr or exc.stdout or "").strip()
            return ToolResult(
                success=False,
                data={"returncode": exc.returncode},
                error=f"ShadowBot 启动失败: {error_text or exc.returncode}",
            )

        response_data = self._load_shadowbot_result_file(system, env, flow_id)
        if response_data is None:
            response_data = {
                "success": True,
                "system": system,
                "env": env,
                "flow_id": flow_id,
                "result": "launched",
                "operation_log": [
                    "ShadowBot free edition launched via local Windows command."
                ],
                "launch_command": command,
                "stdout": (completed.stdout or "").strip(),
                "stderr": (completed.stderr or "").strip(),
                "task_payload": payload,
            }

        normalized = self._normalize_response(system, env, flow_id, response_data)
        log_kv(
            self.logger,
            logging.INFO,
            "ShadowBot local inspection launched",
            system=system,
            env=env,
            flow_id=flow_id,
            robot_uuid=robot_uuid,
        )
        return ToolResult(
            success=normalized["success"],
            data=normalized["data"],
            error=normalized["error"],
        )

    def _load_shadowbot_result_file(
        self, system: str, env: str, flow_id: str
    ) -> dict[str, Any] | None:
        result_file = self.config.shadowbot.result_file
        if not result_file:
            return None

        path = Path(result_file)
        if not path.exists() or not path.is_file():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "success": False,
                "system": system,
                "env": env,
                "flow_id": flow_id,
                "result": "failed",
                "error": "ShadowBot 结果文件不是有效 JSON",
            }

        raw.setdefault("system", system)
        raw.setdefault("env", env)
        raw.setdefault("flow_id", flow_id)
        return raw
