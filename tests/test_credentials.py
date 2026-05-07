import json

from aiops_agent.browser.credentials import CredentialError, CredentialStore


def test_credential_store_loads_named_credential(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"credentials": {"demo": {"username": "alice", "password": "secret"}}}),
        encoding="utf-8",
    )

    credential = CredentialStore(path).get("demo")

    assert credential.username == "alice"
    assert credential.password == "secret"
    assert credential.redacted()["password"] == "***"


def test_credential_store_rejects_missing_ref(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"credentials": {}}), encoding="utf-8")

    store = CredentialStore(path)

    try:
        store.get("missing")
    except CredentialError as exc:
        assert "凭据引用不存在" in str(exc)
        return

    raise AssertionError("expected missing credential ref to fail")


def test_credential_store_rejects_missing_password(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"credentials": {"demo": {"username": "alice"}}}),
        encoding="utf-8",
    )

    try:
        CredentialStore(path)
    except CredentialError as exc:
        assert "缺少 password" in str(exc)
        return

    raise AssertionError("expected missing password to fail")

