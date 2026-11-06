"""Install the repository's approved bundle through the official DSH CLI."""

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .db import AuditRow, UserRow
from .domain import Role


PROFILE = "qualityflow"
PLUGIN_ID = "qualityflow-connector"
PACKAGE = "@qualityflow/dsh-connector"
PLUGIN_SOURCE = Path(__file__).resolve().parents[2] / "integrations/dsh/qualityflow-connector"
CLI_ENTRY = Path("dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js")
logger = logging.getLogger(__name__)


class PluginChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["install", "remove"]


class DshConsoleError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


def read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        pass
    raise DshConsoleError(503, "DSH_MANIFEST_UNREADABLE", "无法读取 DSH 插件清单，请检查服务端配置。")


class DshConsole:
    def __init__(self, storage_root: Path):
        self.storage_root = storage_root.resolve()
        override = os.environ.get("DSH_CONSOLE_CLI")
        self.cli = Path(override).resolve() if override else self.storage_root / CLI_ENTRY
        self.source = PLUGIN_SOURCE
        self.mutation = Lock()
        runtime_bin = self.storage_root / "dsh-runtime/node_modules/.bin"
        self.path = os.pathsep.join((str(runtime_bin), os.environ.get("PATH", "")))

    def home(self, tenant_id: str) -> Path:
        tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        return self.storage_root / "dsh-console" / tenant_key

    def inventory(self, tenant_id: str) -> dict:
        profile = self.home(tenant_id) / "profiles" / PROFILE
        manifest = read_manifest(profile / "package.json")
        installed = read_manifest(profile / "node_modules" / PACKAGE / "package.json")
        offered = read_manifest(self.source / "package.json")
        declared = PACKAGE in manifest.get("dependencies", {})
        selected = PACKAGE in manifest.get("dsh", {}).get("profile", {}).get("bundles", [])
        runtime = self.runtime()
        installed_version = installed.get("version") if installed.get("name") == PACKAGE else None
        return {
            "profile": PROFILE,
            "runtime": runtime,
            "busy": self.mutation.locked(),
            "plugins": [{
                "id": PLUGIN_ID, "name": "QualityFlow 健康检查",
                "package": PACKAGE, "available_version": offered.get("version"),
                "installed_version": installed_version,
                "declared": declared, "selected": selected,
                "activation": "unverified" if selected else "not_selected",
            }],
        }

    def runtime(self) -> str:
        if not self.cli.is_file():
            return "cli_missing"
        if not shutil.which("node"):
            return "node_missing"
        if not shutil.which("pnpm", path=self.path):
            return "pnpm_missing"
        return "available"

    def change(self, tenant_id: str, action: str) -> tuple[dict, bool]:
        if not self.mutation.acquire(blocking=False):
            raise DshConsoleError(409, "DSH_BUSY", "插件操作正在进行，请稍后刷新。")
        try:
            before = self.inventory(tenant_id)
            plugin = before["plugins"][0]
            installed = bool(plugin["installed_version"] and plugin["declared"] and plugin["selected"])
            present = bool(plugin["installed_version"] or plugin["declared"] or plugin["selected"])
            if (action == "install" and installed) or (action == "remove" and not present):
                return {**before, "busy": False}, False
            if before["runtime"] != "available":
                raise DshConsoleError(503, "DSH_RUNTIME_UNAVAILABLE", "请先在服务端配置 DSH CLI、Node.js 和 pnpm。")
            if action == "install" and not plugin["available_version"]:
                raise DshConsoleError(503, "DSH_BUNDLE_UNAVAILABLE", "服务端缺少 QualityFlow 插件包。")
            self.run_cli(self.home(tenant_id), action)
            after = self.inventory(tenant_id)
            plugin = after["plugins"][0]
            installed = bool(plugin["installed_version"] and plugin["declared"] and plugin["selected"])
            present = bool(plugin["installed_version"] or plugin["declared"] or plugin["selected"])
            if (action == "install" and not installed) or (action == "remove" and present):
                raise DshConsoleError(502, "DSH_STATE_UNCONFIRMED", "DSH 返回后未能确认插件状态，请刷新后检查。")
            return {**after, "busy": False}, True
        finally:
            self.mutation.release()

    def run_cli(self, home: Path, action: str) -> None:
        if action == "install":
            args = ["add", f"file:{self.source.as_posix()}", "--offline", "--ignore-scripts"]
        else:
            args = ["remove", PACKAGE]
        store = self.storage_root / "dsh-console" / "pnpm-store"
        args.append(f"--store-dir={store.as_posix()}")
        # Pass platform basics only. The installer needs no model or business credentials.
        permitted = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
            "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in permitted}
        env.update(
            PATH=self.path, DSH_HOME=str(home), HOME=str(home), USERPROFILE=str(home),
            APPDATA=str(home / "appdata"), LOCALAPPDATA=str(home / "localappdata"),
            npm_config_userconfig=str(home / ".npmrc"),
            npm_config_ignore_scripts="true", pnpm_config_ignore_scripts="true",
            npm_config_offline="true", pnpm_config_offline="true",
        )
        try:
            home.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [shutil.which("node") or "node", str(self.cli), "plugin", "--profile", PROFILE, *args],
                cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            raise DshConsoleError(504, "DSH_TIMEOUT", "DSH 操作超时，请刷新查看插件状态。") from None
        except subprocess.CalledProcessError as exc:
            logger.warning("DSH plugin %s exited with status %s", action, exc.returncode)
            raise DshConsoleError(502, "DSH_OPERATION_FAILED", "DSH CLI 执行失败，请检查运行环境后重试。") from None
        except OSError:
            logger.warning("Could not start the DSH plugin command")
            raise DshConsoleError(503, "DSH_START_FAILED", "无法启动 DSH 插件命令，请检查服务端配置。") from None


def register_dsh_console_routes(app, *, storage_root, db_session, current_user, require_role, api_error):
    app.state.dsh_console = DshConsole(storage_root)

    @app.get("/api/v1/dsh/plugins")
    def get_plugins(user: UserRow = Depends(current_user), db: Session = Depends(db_session)):
        require_role(user, {Role.ADMIN}, db, "dsh.plugins.list")
        try:
            return app.state.dsh_console.inventory(user.tenant_id)
        except DshConsoleError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None

    @app.post("/api/v1/dsh/plugins/{plugin_id}")
    def change_plugin(plugin_id: str, payload: PluginChange, user: UserRow = Depends(current_user),
                      db: Session = Depends(db_session)):
        require_role(user, {Role.ADMIN}, db, "dsh.plugins.change")
        if plugin_id != PLUGIN_ID:
            raise api_error(404, "DSH_PLUGIN_NOT_FOUND", "该插件不在受支持目录中。")
        tenant_id, actor_id = user.tenant_id, user.id
        # Do not keep an authentication transaction open while pnpm runs.
        db.rollback()
        audit_payload = {"action": payload.action, "outcome": "error"}
        try:
            result, changed = app.state.dsh_console.change(tenant_id, payload.action)
            audit_payload["outcome"] = "changed" if changed else "unchanged"
        except DshConsoleError as exc:
            audit_payload["code"] = exc.code
            raise api_error(exc.status, exc.code, str(exc)) from None
        finally:
            db.add(AuditRow(id=str(uuid4()), tenant_id=tenant_id, actor_id=actor_id,
                            action="DSH_PLUGIN_CHANGE", resource_type="dsh_plugin",
                            resource_id=PLUGIN_ID, payload=audit_payload))
            db.commit()
        return result
