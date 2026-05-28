from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class BrowserSiteConfigError(Exception):
    """Raised when browser site configuration is missing or invalid."""


WorkflowName = Literal["search_user", "create_user", "assign_role", "create_user_and_assign_role"]


class BrowserWorkflowConfig(BaseModel):
    entry_url: str | None = None
    navigation: list[str] = Field(default_factory=list)
    open_button: str | None = None
    submit_button: str
    success_signals: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)


class BrowserSiteConfig(BaseModel):
    site_key: str
    aliases: list[str] = Field(default_factory=list)
    base_url: str
    login_url: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    login_fields: dict[str, str] = Field(default_factory=dict)
    workflows: dict[WorkflowName, BrowserWorkflowConfig] = Field(default_factory=dict)

    @field_validator("base_url", "login_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return value

    @model_validator(mode="after")
    def _default_allowed_domain(self):
        if not self.allowed_domains:
            host = urlparse(self.base_url).netloc
            if host:
                self.allowed_domains = [host]
        return self

    def workflow_config(self, workflow: str) -> BrowserWorkflowConfig:
        if workflow not in self.workflows:
            raise BrowserSiteConfigError(f"站点 {self.site_key} 不支持 workflow: {workflow}")
        return self.workflows[workflow]  # type: ignore[index]

    def to_runtime_dict(self) -> dict[str, Any]:
        return self.model_dump()


class BrowserSitesConfig(BaseModel):
    sites: dict[str, BrowserSiteConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_site_keys(self):
        for key, site in self.sites.items():
            if site.site_key != key:
                raise ValueError(f"site_key mismatch for {key}")
        return self

    def get(self, site_key: str) -> BrowserSiteConfig:
        site = self.sites.get(site_key)
        if site is None:
            raise BrowserSiteConfigError(f"浏览器站点配置不存在: {site_key}")
        return site


def load_browser_sites_config(path: str | Path | None = None) -> BrowserSitesConfig:
    config_path = Path(path or "configs/browser_sites.json")
    if not config_path.exists():
        return BrowserSitesConfig()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        return BrowserSitesConfig.model_validate(raw)
    except json.JSONDecodeError as exc:
        raise BrowserSiteConfigError(f"浏览器站点配置格式错误: {config_path}") from exc
    except ValidationError as exc:
        raise BrowserSiteConfigError(f"浏览器站点配置校验失败: {exc}") from exc
