from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path
from typing import Any
from urllib import error, request

from aiops_agent.config import RPAConfig
from aiops_agent.tasks.models import ToolExecutionResult


class RPARunner:
    def __init__(self, config: RPAConfig):
        self.config = config

    def validate_runtime(self) -> str | None:
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

    def invoke_flow(
        self,
        flow_id: str,
        payload: dict[str, Any],
        *,
        fallback_endpoint: str = "rpa",
        allow_global_robot_uuid: bool = False,
    ) -> ToolExecutionResult:
        if self.config.execution_mode == "shadowbot_local":
            return self._invoke_shadowbot_local(
                flow_id,
                payload,
                allow_global_robot_uuid=allow_global_robot_uuid,
            )
        return self._invoke_api(flow_id, payload, fallback_endpoint=fallback_endpoint)

    def _invoke_api(
        self,
        flow_id: str,
        payload: dict[str, Any],
        *,
        fallback_endpoint: str,
    ) -> ToolExecutionResult:
        endpoint = self._build_endpoint(flow_id, fallback_endpoint)
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
            return ToolExecutionResult(
                success=False,
                data={"status_code": exc.code},
                error=f"RPA 平台调用失败: HTTP {exc.code}",
            )
        except error.URLError as exc:
            return ToolExecutionResult(success=False, data={}, error=f"RPA 平台不可达: {exc.reason}")

        try:
            response_data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return ToolExecutionResult(success=False, data={}, error="RPA 平台返回了非 JSON 数据")

        if not response_data:
            return ToolExecutionResult(success=False, data={}, error="RPA 平台返回空结果")

        return ToolExecutionResult(success=True, data=response_data)

    def _build_endpoint(self, flow_id: str, fallback_endpoint: str) -> str:
        platform_url = self.config.platform_url.rstrip("/")
        provider = self.config.provider.lower()
        if provider == "yidao":
            return f"{platform_url}/api/v1/flows/{flow_id}/run"
        return f"{platform_url}/api/v1/{fallback_endpoint}/run"

    def _invoke_shadowbot_local(
        self,
        flow_id: str,
        payload: dict[str, Any],
        *,
        allow_global_robot_uuid: bool,
    ) -> ToolExecutionResult:
        if platform.system() != "Windows":
            return ToolExecutionResult(
                success=False,
                data={},
                error="ShadowBot 免费版本地启动模式仅支持在 Windows 上执行",
            )

        robot_uuid = self.config.shadowbot.robot_uuid if allow_global_robot_uuid else ""
        robot_uuid = robot_uuid or flow_id
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
            return ToolExecutionResult(success=False, data={}, error="ShadowBot 启动命令超时")
        except subprocess.CalledProcessError as exc:
            error_text = (exc.stderr or exc.stdout or "").strip()
            return ToolExecutionResult(
                success=False,
                data={"returncode": exc.returncode},
                error=f"ShadowBot 启动失败: {error_text or exc.returncode}",
            )

        response_data = self._load_shadowbot_result_file()
        if response_data is None:
            response_data = {
                "success": True,
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
        response_data.setdefault("flow_id", flow_id)
        return ToolExecutionResult(success=True, data=response_data)

    def _load_shadowbot_result_file(self) -> dict[str, Any] | None:
        result_file = self.config.shadowbot.result_file
        if not result_file:
            return None

        path = Path(result_file)
        if not path.exists() or not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "success": False,
                "result": "failed",
                "error": "ShadowBot 结果文件不是有效 JSON",
            }
