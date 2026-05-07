from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CredentialError(Exception):
    """Raised when browser credentials cannot be loaded safely."""


@dataclass(slots=True)
class BrowserCredential:
    ref: str
    username: str
    password: str

    def redacted(self) -> dict[str, str]:
        return {"ref": self.ref, "username": self.username, "password": "***"}


class CredentialStore:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else None
        self._credentials: dict[str, BrowserCredential] = {}
        if self.config_path:
            self._credentials = self._load(self.config_path)

    def get(self, ref: str | None) -> BrowserCredential | None:
        if not ref:
            return None
        credential = self._credentials.get(ref)
        if credential is None:
            raise CredentialError(f"凭据引用不存在: {ref}")
        return credential

    def _load(self, path: Path) -> dict[str, BrowserCredential]:
        if not path.exists():
            raise CredentialError(f"凭据配置文件不存在: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CredentialError(f"凭据配置文件格式错误: {path}") from exc
        records = self._records(raw)
        credentials: dict[str, BrowserCredential] = {}
        for ref, item in records.items():
            if not isinstance(item, dict):
                raise CredentialError(f"凭据 {ref} 必须是对象")
            username = str(item.get("username", ""))
            password = str(item.get("password", ""))
            if not username:
                raise CredentialError(f"凭据 {ref} 缺少 username")
            if not password:
                raise CredentialError(f"凭据 {ref} 缺少 password")
            credentials[ref] = BrowserCredential(ref=ref, username=username, password=password)
        return credentials

    def _records(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict) and isinstance(raw.get("credentials"), dict):
            return dict(raw["credentials"])
        if isinstance(raw, dict):
            return dict(raw)
        raise CredentialError("凭据配置必须是对象")

