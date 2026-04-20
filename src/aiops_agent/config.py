from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("configs/rpa.json")
DEFAULT_ANTHROPIC_CONFIG_PATH = Path("configs/llm.json")


class ConfigError(Exception):
    """Raised when runtime configuration is invalid."""


@dataclass(slots=True)
class AuthConfig:
    type: str = "bearer"
    token: str = ""


@dataclass(slots=True)
class InspectionConfig:
    default_system: str = "WebLogic"
    default_env: str = "prod"
    flow_map: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ShadowBotConfig:
    executable_path: str = ""
    robot_uuid: str = ""
    command_timeout_seconds: int = 10
    result_file: str = ""


@dataclass(slots=True)
class RPAConfig:
    provider: str = "yidao"
    execution_mode: str = "api"
    platform_url: str = ""
    timeout_seconds: int = 10
    auth: AuthConfig = field(default_factory=AuthConfig)
    inspection: InspectionConfig = field(default_factory=InspectionConfig)
    shadowbot: ShadowBotConfig = field(default_factory=ShadowBotConfig)

    def validate_for_startup(self) -> None:
        errors: list[str] = []
        if not self.inspection.flow_map:
            errors.append("RPA inspection.flow_map 未设置")
        if self.timeout_seconds <= 0:
            errors.append("RPA timeout_seconds 必须大于 0")
        if self.execution_mode == "api":
            if not self.platform_url:
                errors.append("RPA platform_url 未设置")
            if self.auth.type == "bearer" and not self.auth.token:
                errors.append("RPA bearer token 未设置")
        elif self.execution_mode == "shadowbot_local":
            if not self.shadowbot.executable_path:
                errors.append("ShadowBot executable_path 未设置")
            if self.shadowbot.command_timeout_seconds <= 0:
                errors.append("ShadowBot command_timeout_seconds 必须大于 0")
        else:
            errors.append("RPA execution_mode 必须为 api 或 shadowbot_local")
        if errors:
            raise ConfigError("；".join(errors))


@dataclass(slots=True)
class AnthropicConfig:
    provider: str = "anthropic"
    enabled: bool = False
    api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    base_url: str = ""
    api_version: str = "2023-06-01"
    timeout_seconds: int = 20
    max_retries: int = 2
    max_tokens: int = 512

    @property
    def default_headers(self) -> dict[str, str]:
        headers = {"anthropic-version": self.api_version}
        return headers

    def validate_for_startup(self) -> None:
        if not self.enabled:
            return
        errors: list[str] = []
        if self.provider != "anthropic":
            errors.append("LLM provider 必须为 anthropic")
        if not self.api_key:
            errors.append("ANTHROPIC_API_KEY 未设置")
        if not self.model:
            errors.append("ANTHROPIC_MODEL 未设置")
        if self.timeout_seconds <= 0:
            errors.append("Anthropic timeout_seconds 必须大于 0")
        if self.max_retries < 0:
            errors.append("Anthropic max_retries 不能小于 0")
        if self.max_tokens <= 0:
            errors.append("Anthropic max_tokens 必须大于 0")
        if errors:
            raise ConfigError("；".join(errors))


def load_rpa_config(config_path: str | None = None) -> RPAConfig:
    resolved_path = Path(
        config_path or os.environ.get("AIOPS_RPA_CONFIG") or DEFAULT_CONFIG_PATH
    )
    if not resolved_path.exists():
        raise ConfigError(f"RPA 配置文件不存在: {resolved_path}")

    try:
        with resolved_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"RPA 配置文件格式错误: {resolved_path}") from exc

    auth = raw.get("auth", {})
    inspection = raw.get("inspection", {})
    shadowbot = raw.get("shadowbot", {})
    return RPAConfig(
        provider=raw.get("provider", "yidao"),
        execution_mode=raw.get("execution_mode", "api"),
        platform_url=raw.get("platform_url", ""),
        timeout_seconds=int(raw.get("timeout_seconds", 10)),
        auth=AuthConfig(
            type=auth.get("type", "bearer"),
            token=auth.get("token", ""),
        ),
        inspection=InspectionConfig(
            default_system=inspection.get("default_system", "WebLogic"),
            default_env=inspection.get("default_env", "prod"),
            flow_map=dict(inspection.get("flow_map", {})),
        ),
        shadowbot=ShadowBotConfig(
            executable_path=shadowbot.get("executable_path", ""),
            robot_uuid=shadowbot.get("robot_uuid", ""),
            command_timeout_seconds=int(shadowbot.get("command_timeout_seconds", 10)),
            result_file=shadowbot.get("result_file", ""),
        ),
    )


def load_anthropic_config(config_path: str | None = None) -> AnthropicConfig:
    resolved_path = Path(
        config_path or os.environ.get("AIOPS_LLM_CONFIG") or DEFAULT_ANTHROPIC_CONFIG_PATH
    )
    if not resolved_path.exists():
        raw: dict[str, Any] = {}
    else:
        try:
            with resolved_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"LLM 配置文件格式错误: {resolved_path}") from exc

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("AIOPS_LLM_API_KEY")
        or raw.get("api_key", "")
    )
    base_url = (
        os.environ.get("ANTHROPIC_BASE_URL")
        or os.environ.get("AIOPS_LLM_BASE_URL")
        or raw.get("base_url", "")
    )
    model = (
        os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("AIOPS_LLM_MODEL")
        or raw.get("model", "claude-sonnet-4-20250514")
    )
    api_version = (
        os.environ.get("ANTHROPIC_VERSION")
        or raw.get("api_version", "2023-06-01")
    )
    enabled = raw.get("enabled", False)
    if os.environ.get("AIOPS_LLM_ENABLED"):
        enabled = os.environ["AIOPS_LLM_ENABLED"].lower() in {"1", "true", "yes", "on"}

    return AnthropicConfig(
        provider=raw.get("provider", "anthropic"),
        enabled=bool(enabled),
        api_key=api_key,
        model=model,
        base_url=base_url,
        api_version=api_version,
        timeout_seconds=int(raw.get("timeout_seconds", 20)),
        max_retries=int(raw.get("max_retries", 2)),
        max_tokens=int(raw.get("max_tokens", 512)),
    )


def load_llm_config(config_path: str | None = None) -> AnthropicConfig:
    return load_anthropic_config(config_path)


def validate_startup_config(rpa_config: RPAConfig, anthropic_config: AnthropicConfig) -> None:
    rpa_config.validate_for_startup()
    anthropic_config.validate_for_startup()
