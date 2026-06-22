import json

from aiops_agent.browser.credentials import CredentialError, CredentialStore


def test_credential_store_loads_named_credential(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"credentials": {"demo": {"site_key": "ifinance", "username": "alice", "password": "secret"}}}),
        encoding="utf-8",
    )

    credential = CredentialStore(path).get("demo")

    assert credential.username == "alice"
    assert credential.password == "secret"
    assert credential.site_key == "ifinance"
    assert credential.redacted()["password"] == "***"
    assert credential.redacted()["site_key"] == "ifinance"


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


def test_credential_store_finds_default_ref_for_site(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "credentials": {
                    "ifinance_admin": {"username": "alice", "password": "secret"},
                    "other": {"username": "bob", "password": "secret"},
                }
            }
        ),
        encoding="utf-8",
    )

    store = CredentialStore(path)

    assert store.default_ref_for_site("ifinance") == "ifinance_admin"
    assert store.default_ref_for_site("missing") is None


def test_credential_store_maps_explicit_refs_to_sites(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "credentials": {
                    "ifinance-check-admin": {"site_key": "ifinance", "username": "check", "password": "secret"},
                    "ifinance-init-admin": {"site_key": "ifinance", "username": "init", "password": "secret"},
                }
            }
        ),
        encoding="utf-8",
    )

    store = CredentialStore(path)

    assert store.ref_from_text("使用 ifinance-check-admin 登录系统") == "ifinance-check-admin"
    assert store.site_key_for_ref("ifinance-check-admin") == "ifinance"
    assert store.default_ref_for_site("ifinance") is None


def test_credential_store_loads_site_user_credentials(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "sites": {
                    "ifinance": {
                        "default_user": "check-admin",
                        "users": {
                            "check-admin": {"username": "check", "password": "secret"},
                            "init-admin": {"username": "init", "password": "secret"},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    store = CredentialStore(path)

    assert store.default_user_for_site("ifinance") == "check-admin"
    assert store.default_ref_for_site("ifinance") == "ifinance-check-admin"
    assert store.ref_for_site_user("ifinance", "init-admin") == "ifinance-init-admin"
    assert store.ref_from_text("使用 ifinance-check-admin 登录系统") == "ifinance-check-admin"
    credential = store.get("ifinance-check-admin")
    assert credential.site_key == "ifinance"
    assert credential.user == "check-admin"
    assert credential.username == "check"
    assert credential.password == "secret"
    assert credential.redacted()["user"] == "check-admin"


def test_credential_store_loads_default_local_file_when_present(tmp_path, monkeypatch):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "credentials.local.json").write_text(
        json.dumps({"credentials": {"demo": {"username": "alice", "password": "secret"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert CredentialStore().get("demo").username == "alice"
