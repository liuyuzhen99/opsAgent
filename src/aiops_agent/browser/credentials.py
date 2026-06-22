from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CREDENTIAL_CONFIG_PATH = Path("configs/credentials.local.json")


class CredentialError(Exception):
    """Raised when browser credentials cannot be loaded safely."""


@dataclass(slots=True)
class BrowserCredential:
    ref: str
    username: str
    password: str
    site_key: str | None = None
    user: str | None = None

    def redacted(self) -> dict[str, str]:
        data = {"ref": self.ref, "username": self.username, "password": "***"}
        if self.site_key:
            data["site_key"] = self.site_key
        if self.user:
            data["user"] = self.user
        return data


class CredentialStore:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = self._resolve_config_path(config_path)
        self._credentials: dict[str, BrowserCredential] = {}
        self._site_default_users: dict[str, str] = {}
        if self.config_path:
            self._credentials = self._load(self.config_path)

    def get(self, ref: str | None) -> BrowserCredential | None:
        if not ref:
            return None
        credential = self._credentials.get(ref)
        if credential is None:
            raise CredentialError(f"凭据引用不存在: {ref}")
        return credential

    def refs(self) -> list[str]:
        return sorted(self._credentials)

    def ref_from_text(self, text: str) -> str | None:
        lowered = text.lower()
        for ref in sorted(self._credentials, key=len, reverse=True):
            if ref.lower() in lowered:
                return ref
        return None

    def site_key_for_ref(self, ref: str | None) -> str | None:
        if not ref:
            return None
        credential = self._credentials.get(ref)
        if credential is None:
            return None
        return credential.site_key

    def default_user_for_site(self, site_key: str | None) -> str | None:
        if not site_key:
            return None
        return self._site_default_users.get(site_key)

    def ref_for_site_user(self, site_key: str | None, user: str | None = None) -> str | None:
        if not site_key:
            return None
        resolved_user = user or self.default_user_for_site(site_key)
        if not resolved_user:
            return None
        for credential in self._credentials.values():
            if credential.site_key != site_key:
                continue
            if credential.user == resolved_user or credential.ref == resolved_user:
                return credential.ref
        return None

    def default_ref_for_site(self, site_key: str | None) -> str | None:
        if not site_key:
            return None
        site_user_ref = self.ref_for_site_user(site_key)
        if site_user_ref:
            return site_user_ref
        explicitly_mapped = [
            credential.ref
            for credential in self._credentials.values()
            if credential.site_key == site_key
        ]
        if len(explicitly_mapped) == 1:
            return explicitly_mapped[0]
        candidates = (
            site_key,
            f"{site_key}_admin",
            f"{site_key}-admin",
        )
        for candidate in candidates:
            if candidate in self._credentials:
                return candidate
        prefixed = [ref for ref in self.refs() if ref.startswith(f"{site_key}_") or ref.startswith(f"{site_key}-")]
        if len(prefixed) == 1:
            return prefixed[0]
        return None

    def _resolve_config_path(self, config_path: str | Path | None) -> Path | None:
        if config_path:
            return Path(config_path)
        env_path = os.environ.get("AIOPS_BROWSER_CREDENTIAL_CONFIG")
        if env_path:
            return Path(env_path)
        if DEFAULT_CREDENTIAL_CONFIG_PATH.exists():
            return DEFAULT_CREDENTIAL_CONFIG_PATH
        return None

    def _load(self, path: Path) -> dict[str, BrowserCredential]:
        if not path.exists():
            raise CredentialError(f"凭据配置文件不存在: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CredentialError(f"凭据配置文件格式错误: {path}") from exc
        records = self._records(raw)
        credentials: dict[str, BrowserCredential] = {}
        credentials.update(self._site_records(raw))
        for ref, item in records.items():
            if not isinstance(item, dict):
                raise CredentialError(f"凭据 {ref} 必须是对象")
            username = str(item.get("username", ""))
            password = str(item.get("password", ""))
            if not username:
                raise CredentialError(f"凭据 {ref} 缺少 username")
            if not password:
                raise CredentialError(f"凭据 {ref} 缺少 password")
            site_key = str(item.get("site_key") or "").strip() or None
            user = str(item.get("user") or ref).strip() or None
            credentials[ref] = BrowserCredential(ref=ref, username=username, password=password, site_key=site_key, user=user)
        return credentials

    def _records(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict) and isinstance(raw.get("credentials"), dict):
            return dict(raw["credentials"])
        if isinstance(raw, dict) and isinstance(raw.get("sites"), dict):
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        raise CredentialError("凭据配置必须是对象")

    def _site_records(self, raw: Any) -> dict[str, BrowserCredential]:
        if not isinstance(raw, dict) or not isinstance(raw.get("sites"), dict):
            return {}
        credentials: dict[str, BrowserCredential] = {}
        for site_key, site in raw["sites"].items():
            site_key = str(site_key)
            if not isinstance(site, dict):
                raise CredentialError(f"站点凭据 {site_key} 必须是对象")
            default_user = str(site.get("default_user") or "").strip()
            if default_user:
                self._site_default_users[site_key] = default_user
            users = site.get("users") or {}
            if not isinstance(users, dict):
                raise CredentialError(f"站点 {site_key} 的 users 必须是对象")
            for user, item in users.items():
                user = str(user)
                if not isinstance(item, dict):
                    raise CredentialError(f"站点 {site_key} 用户 {user} 必须是对象")
                ref = str(item.get("ref") or f"{site_key}-{user}")
                username = str(item.get("username", ""))
                password = str(item.get("password", ""))
                if not username:
                    raise CredentialError(f"站点 {site_key} 用户 {user} 缺少 username")
                if not password:
                    raise CredentialError(f"站点 {site_key} 用户 {user} 缺少 password")
                credentials[ref] = BrowserCredential(
                    ref=ref,
                    username=username,
                    password=password,
                    site_key=site_key,
                    user=user,
                )
        return credentials
