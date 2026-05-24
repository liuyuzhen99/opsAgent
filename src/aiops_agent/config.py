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
class RPATargetConfig:
    ssh: str = ""
    sftp: str = ""
    db: str = ""

    def flow_for(self, capability: str) -> str:
        normalized = capability.strip().lower()
        if normalized in {"database", "sql", "plsql", "pl/sql"}:
            normalized = "db"
        if normalized not in {"ssh", "sftp", "db"}:
            return ""
        return getattr(self, normalized)


@dataclass(slots=True)
class RPAActionsConfig:
    targets: dict[str, RPATargetConfig] = field(default_factory=dict)


@dataclass(slots=True)
class ShadowBotConfig:
    executable_path: str = ""
    robot_uuid: str = ""
    command_timeout_seconds: int = 10
    result_file: str = ""


@dataclass(slots=True)
class KnowledgeConfig:
    vault_path: str = ""
    include_patterns: list[str] = field(default_factory=lambda: ["*.md"])
    exclude_patterns: list[str] = field(
        default_factory=lambda: [".obsidian/**", "attachments/**", "archive/**", "secrets/**"]
    )
    index_mode: str = "keyword"
    embedding_provider: str = "openai"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str = ""
    enable_eval: bool = False
    obsidian_graph_enabled: bool = True
    link_context_enabled: bool = True
    graph_expand_depth: int = 1
    graph_boost: float = 0.15
    moc_patterns: list[str] = field(default_factory=lambda: ["*MOC.md", "**/README.md"])
    write_enabled: bool = True
    auto_reindex_after_write: bool = True
    note_type_dirs: dict[str, str] = field(
        default_factory=lambda: {
            "incident": "incident",
            "runbooks": "runbooks",
            "architecture": "architecture",
            "guidance": "guidance",
        }
    )


@dataclass(slots=True)
class RPAConfig:
    provider: str = "yidao"
    execution_mode: str = "api"
    platform_url: str = ""
    timeout_seconds: int = 10
    auth: AuthConfig = field(default_factory=AuthConfig)
    inspection: InspectionConfig = field(default_factory=InspectionConfig)
    rpa_actions: RPAActionsConfig = field(default_factory=RPAActionsConfig)
    shadowbot: ShadowBotConfig = field(default_factory=ShadowBotConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)

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
class LLMProviderConfig:
    provider: str = "anthropic"
    enabled: bool = False
    api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    base_url: str = ""
    api_version: str = "2023-06-01"
    timeout_seconds: int = 20
    max_retries: int = 2
    max_tokens: int = 512
    temperature: float = 0.0
    fallback_provider: str = ""
    fallback_model: str = ""
    role_models: dict[str, str] = field(default_factory=dict)

    @property
    def default_headers(self) -> dict[str, str]:
        headers = {"anthropic-version": self.api_version}
        return headers

    def validate_for_startup(self) -> None:
        if not self.enabled:
            return
        errors: list[str] = []
        if self.provider not in {"anthropic", "openai", "private"}:
            errors.append("LLM provider 必须为 anthropic、openai 或 private")
        if not self.api_key:
            if self.provider == "anthropic":
                errors.append("ANTHROPIC_API_KEY 未设置")
            elif self.provider == "openai":
                errors.append("OPENAI_API_KEY 未设置")
            else:
                errors.append("LLM API_KEY 未设置")
        if not self.model:
            if self.provider == "anthropic":
                errors.append("ANTHROPIC_MODEL 未设置")
            elif self.provider == "openai":
                errors.append("OPENAI_MODEL 未设置")
            else:
                errors.append("LLM MODEL 未设置")
        if self.timeout_seconds <= 0:
            errors.append("LLM timeout_seconds 必须大于 0")
        if self.max_retries < 0:
            errors.append("LLM max_retries 不能小于 0")
        if self.max_tokens <= 0:
            errors.append("LLM max_tokens 必须大于 0")
        if errors:
            raise ConfigError("；".join(errors))


AnthropicConfig = LLMProviderConfig


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
    rpa_actions = raw.get("rpa_actions", {})
    shadowbot = raw.get("shadowbot", {})
    knowledge = raw.get("knowledge", {})
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
        rpa_actions=RPAActionsConfig(
            targets=_load_rpa_action_targets(rpa_actions.get("targets", {})),
        ),
        shadowbot=ShadowBotConfig(
            executable_path=shadowbot.get("executable_path", ""),
            robot_uuid=shadowbot.get("robot_uuid", ""),
            command_timeout_seconds=int(shadowbot.get("command_timeout_seconds", 10)),
            result_file=shadowbot.get("result_file", ""),
        ),
        knowledge=KnowledgeConfig(
            vault_path=knowledge.get("vault_path", ""),
            include_patterns=list(knowledge.get("include_patterns", ["*.md"])),
            exclude_patterns=list(
                knowledge.get("exclude_patterns", [".obsidian/**", "attachments/**", "archive/**", "secrets/**"])
            ),
            index_mode=knowledge.get("index_mode", "keyword"),
            embedding_provider=knowledge.get("embedding_provider", "openai"),
            embedding_api_key=(
                os.environ.get("OPENAI_API_KEY")
                or knowledge.get("embedding_api_key", "")
            ),
            embedding_model=knowledge.get("embedding_model", "text-embedding-3-small"),
            embedding_base_url=knowledge.get("embedding_base_url", ""),
            enable_eval=bool(knowledge.get("enable_eval", False)),
            obsidian_graph_enabled=bool(knowledge.get("obsidian_graph_enabled", True)),
            link_context_enabled=bool(knowledge.get("link_context_enabled", True)),
            graph_expand_depth=int(knowledge.get("graph_expand_depth", 1)),
            graph_boost=float(knowledge.get("graph_boost", 0.15)),
            moc_patterns=list(knowledge.get("moc_patterns", ["*MOC.md", "**/README.md"])),
            write_enabled=bool(knowledge.get("write_enabled", True)),
            auto_reindex_after_write=bool(knowledge.get("auto_reindex_after_write", True)),
            note_type_dirs=dict(
                knowledge.get(
                    "note_type_dirs",
                    {
                        "incident": "incident",
                        "runbooks": "runbooks",
                        "architecture": "architecture",
                        "guidance": "guidance",
                    },
                )
            ),
        ),
    )


def _load_rpa_action_targets(raw_targets: Any) -> dict[str, RPATargetConfig]:
    if not isinstance(raw_targets, dict):
        return {}
    targets: dict[str, RPATargetConfig] = {}
    for target, raw_value in raw_targets.items():
        if not isinstance(raw_value, dict):
            continue
        targets[str(target)] = RPATargetConfig(
            ssh=str(raw_value.get("ssh", "") or ""),
            sftp=str(raw_value.get("sftp", "") or ""),
            db=str(raw_value.get("db", "") or raw_value.get("database", "") or ""),
        )
    return targets


def load_anthropic_config(config_path: str | None = None) -> LLMProviderConfig:
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

    role_models = raw.get("role_models", {})
    profiles = raw.get("profiles", {})
    if not role_models and isinstance(profiles, dict):
        default_profile = profiles.get("default", {})
        if isinstance(default_profile, dict):
            role_models = dict(default_profile.get("role_models", {}))

    return LLMProviderConfig(
        provider=raw.get("provider", "anthropic"),
        enabled=bool(enabled),
        api_key=api_key,
        model=model,
        base_url=base_url,
        api_version=api_version,
        timeout_seconds=int(raw.get("timeout_seconds", 20)),
        max_retries=int(raw.get("max_retries", 2)),
        max_tokens=int(raw.get("max_tokens", 512)),
        temperature=float(raw.get("temperature", 0.0)),
        fallback_provider=raw.get("fallback_provider", ""),
        fallback_model=raw.get("fallback_model", ""),
        role_models=dict(role_models),
    )


def load_llm_config(config_path: str | None = None) -> LLMProviderConfig:
    return load_anthropic_config(config_path)


def validate_startup_config(rpa_config: RPAConfig, anthropic_config: LLMProviderConfig) -> None:
    rpa_config.validate_for_startup()
    anthropic_config.validate_for_startup()
