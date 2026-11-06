import json
import subprocess

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from qualityflow.api import create_app
from qualityflow.db import AuditRow
from qualityflow.dsh_console import DshConsole, PACKAGE, PLUGIN_ID, PROFILE
from qualityflow.seed import DEMO_USERS, seed_demo


@pytest.fixture
def workbench(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'test.db'}",
                     storage_root=tmp_path / "storage", create_schema=True)
    passwords = {name: f"long-test-password-for-{name}" for name in DEMO_USERS}
    with app.state.session_factory() as db:
        seed_demo(db, tenant_id="demo-factory", passwords=passwords)
    with TestClient(app) as client:
        def auth(username):
            response = client.post("/api/v1/auth/login",
                                   json={"username": username, "password": passwords[username]})
            assert response.status_code == 200
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        yield app, client, auth
    app.state.engine.dispose()


def test_catalog_permissions_and_verified_profile_change(workbench, monkeypatch):
    app, client, auth = workbench
    console = app.state.dsh_console
    catalog = "/api/v1/dsh/plugins"
    endpoint = f"{catalog}/{PLUGIN_ID}"
    admin = auth("admin.demo")
    engineer = auth("engineer.demo")
    monkeypatch.setattr(console, "runtime", lambda: "available")
    commands = []

    def cli(home, action):
        commands.append(action)
        profile = home / "profiles" / PROFILE
        package_manifest = profile / "node_modules" / PACKAGE / "package.json"
        if action == "install":
            package_manifest.parent.mkdir(parents=True)
            package_manifest.write_text(json.dumps({"name": PACKAGE, "version": "0.1.0"}), encoding="utf-8")
        else:
            package_manifest.unlink()
        manifest = {
            "dependencies": {PACKAGE: "file:connector"} if action == "install" else {},
            "dsh": {"profile": {"bundles": [PACKAGE] if action == "install" else []}},
        }
        (profile / "package.json").write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(console, "run_cli", cli)
    assert client.get(catalog).status_code == 401
    assert client.get(catalog, headers=engineer).status_code == 403
    assert client.post(endpoint, headers=engineer, json={"action": "install"}).status_code == 403
    assert client.post(f"{catalog}/unapproved", headers=admin, json={"action": "install"}).status_code == 404
    assert client.post(endpoint, headers=admin, json={"action": "install", "spec": "unapproved"}).status_code == 422
    assert client.get(catalog, headers=admin).json()["plugins"][0]["installed_version"] is None
    assert not commands

    response = client.post(endpoint, headers=admin, json={"action": "install"})
    assert response.status_code == 200
    plugin = response.json()["plugins"][0]
    assert plugin["installed_version"] == "0.1.0"
    assert plugin["selected"] and plugin["activation"] == "unverified"
    assert not console.inventory("another-tenant")["plugins"][0]["selected"]
    assert client.post(endpoint, headers=admin, json={"action": "install"}).status_code == 200
    removed = client.post(endpoint, headers=admin, json={"action": "remove"})
    assert removed.status_code == 200
    assert removed.json()["plugins"][0]["activation"] == "not_selected"
    assert commands == ["install", "remove"]
    with app.state.session_factory() as db:
        outcomes = db.scalars(select(AuditRow).where(AuditRow.action == "DSH_PLUGIN_CHANGE")).all()
        assert sorted(row.payload["outcome"] for row in outcomes) == ["changed", "changed", "unchanged"]

    monkeypatch.setattr(console, "run_cli", lambda home, action: None)
    response = client.post(endpoint, headers=admin, json={"action": "install"})
    assert response.status_code == 502
    assert response.json()["error_code"] == "DSH_STATE_UNCONFIRMED"
    with console.mutation:
        assert client.post(endpoint, headers=admin, json={"action": "install"}).status_code == 409


def test_cli_arguments_and_environment_are_isolated(tmp_path, monkeypatch):
    console = DshConsole(tmp_path)
    home = console.home("../untrusted-tenant")
    assert home.parent == tmp_path / "dsh-console"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-secret")
    monkeypatch.setenv("DATABASE_URL", "dummy-private-url")
    monkeypatch.setenv("NODE_OPTIONS", "dummy-injection")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["cwd"] == home
        assert kwargs["env"]["DSH_HOME"] == str(home)
        assert {"DEEPSEEK_API_KEY", "DATABASE_URL", "NODE_OPTIONS"}.isdisjoint(kwargs["env"])
        assert not kwargs.get("shell")

    monkeypatch.setattr(subprocess, "run", run)
    console.run_cli(home, "install")
    console.run_cli(home, "remove")
    assert calls[0][2:7] == ["plugin", "--profile", PROFILE, "add", f"file:{console.source.as_posix()}"]
    assert "--ignore-scripts" in calls[0]
    assert calls[1][2:7] == ["plugin", "--profile", PROFILE, "remove", PACKAGE]
    assert "--ignore-scripts" not in calls[1]
