"""Регистрация и удаление собственного Docker-драйвера рабочих папок."""

import asyncio
import hashlib
import json
import posixpath
import re
import secrets
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from services.common.storage import atomic_json

from .config import PREFIX
from .docker import Docker, DockerError

ENGINE_RPC_TIMEOUT_S = 30


@dataclass(frozen=True)
class Identity:
    project: str
    name: str
    plugin_id: str | None = None
    removing: bool = False


def _matches(actual: Any, expected: Any) -> bool:
    """Engine добавляет поля по умолчанию к десериализованному манифесту."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_matches(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return actual == expected


class WorkspaceDriver:
    def __init__(
        self,
        docker: Docker,
        state: Path,
        project: str,
        ipc_volume: str,
        *,
        context: Path = Path("/opt/eval-stand-workspace-driver"),
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}", ipc_volume):
            raise ValueError("invalid_workspace_ipc_volume")
        self.docker = docker
        self.state = state
        self.project = project
        self.ipc_volume = ipc_volume
        self.context = context
        project_digest = hashlib.sha256(project.encode()).hexdigest()[:16]
        self.namespace = f"eval-workspace-{project_digest}-"
        self.lock = asyncio.Lock()
        self._name: str | None = None

    @property
    def name(self) -> str:
        if self._name is None:
            raise DockerError("workspace_driver_not_ready")
        return self._name

    def _save(self, identity: Identity) -> None:
        self.state.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        atomic_json(self.state, asdict(identity))

    def _new(self) -> Identity:
        identity = Identity(
            self.project, f"{self.namespace}{secrets.token_hex(16)}:local"
        )
        self._save(identity)
        return identity

    def _load(self) -> Identity | None:
        try:
            data = json.loads(self.state.read_text())
            identity = Identity(**data)
            if (
                identity.project != self.project
                or not re.fullmatch(
                    f"{self.namespace}[0-9a-f]{{32}}:local", identity.name
                )
                or not isinstance(identity.removing, bool)
                or (
                    identity.plugin_id is not None
                    and not re.fullmatch(r"[0-9a-f]{64}", identity.plugin_id)
                )
            ):
                raise ValueError("workspace_driver_identity_invalid")
            return identity
        except FileNotFoundError:
            return None
        except (ValueError, TypeError) as error:
            raise DockerError("workspace_driver_identity_invalid") from error

    async def _ipc_mountpoint(self) -> str:
        volume = await self.docker.inspect("volume", self.ipc_volume)
        if (
            volume is None
            or volume.get("Name") != self.ipc_volume
            or volume.get("Driver") != "local"
            or volume.get("Scope") != "local"
            or volume.get("Options")
            or (volume.get("Labels") or {}).get(f"{PREFIX}.role") != "workspace-ipc"
        ):
            raise DockerError("workspace_ipc_volume_mismatch")
        mountpoint = volume.get("Mountpoint")
        if (
            not isinstance(mountpoint, str)
            or not mountpoint.startswith("/")
            or mountpoint.startswith("//")
            or mountpoint == "/"
            or posixpath.normpath(mountpoint) != mountpoint
        ):
            raise DockerError("workspace_ipc_mountpoint_invalid")
        return mountpoint

    async def _inspect(self, name: str) -> dict[str, Any] | None:
        listing = await self.docker.command(
            ["plugin", "ls", "--format", "{{.Name}}"], limit=65536
        )
        if name.encode() not in listing.stdout.splitlines():
            return None
        reply = await self.docker.command(["plugin", "inspect", name], limit=65536)
        try:
            [plugin] = json.loads(reply.stdout)
            if (
                plugin["Name"] != name
                or not re.fullmatch(r"[0-9a-f]{64}", plugin["Id"])
                or not isinstance(plugin["Enabled"], bool)
            ):
                raise ValueError("workspace_driver_inspect_invalid")
        except (ValueError, TypeError, KeyError) as error:
            raise DockerError("workspace_driver_inspect_invalid") from error
        return plugin

    def _manifest_matches(self, plugin: dict[str, Any], mountpoint: str | None) -> bool:
        try:
            expected = json.loads((self.context / "config.json").read_text())
            [ipc] = expected["Mounts"]
            source = ipc["Source"]
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise DockerError("workspace_driver_manifest_invalid") from error
        config = plugin.get("Config", {})
        if _matches(config, expected):
            return True
        # Engine может заменить Source в Config вместе со Settings.
        if mountpoint is None or source == mountpoint:
            return False
        ipc["Source"] = mountpoint
        return _matches(config, expected)

    @staticmethod
    def _bound(plugin: dict[str, Any], mountpoint: str) -> bool:
        settings = plugin.get("Settings")
        config = plugin["Config"]
        [configured_mount] = config["Mounts"]
        return (
            isinstance(settings, dict)
            and _matches(
                settings.get("Mounts"), [{**configured_mount, "Source": mountpoint}]
            )
            and _matches(settings.get("Devices"), config["Linux"]["Devices"])
            and not settings.get("Env")
            and not settings.get("Args")
        )

    def _claim(
        self, identity: Identity, plugin: dict[str, Any], mountpoint: str | None
    ) -> Identity:
        plugin_id = plugin["Id"]
        if identity.plugin_id is not None:
            if identity.plugin_id != plugin_id:
                raise DockerError("foreign_workspace_driver")
            return identity
        if not self._manifest_matches(plugin, mountpoint):
            raise DockerError("foreign_workspace_driver")
        # Имя с nonce сохранено до create, а ID подтверждается до set/enable.
        identity = replace(identity, plugin_id=plugin_id)
        self._save(identity)
        return identity

    def _verify_binding(
        self, identity: Identity, plugin: dict[str, Any] | None, mountpoint: str
    ) -> dict[str, Any]:
        if (
            plugin is None
            or plugin["Id"] != identity.plugin_id
            or not self._manifest_matches(plugin, mountpoint)
            or not self._bound(plugin, mountpoint)
        ):
            raise DockerError("workspace_driver_binding_unconfirmed")
        return plugin

    async def _activate(
        self, identity: Identity, plugin: dict[str, Any], mountpoint: str
    ) -> str:
        identity = self._claim(identity, plugin, mountpoint)
        if not self._manifest_matches(plugin, mountpoint):
            raise DockerError("workspace_driver_config_mismatch")
        if not plugin["Enabled"]:
            if not self._bound(plugin, mountpoint):
                await self.docker.command(
                    ["plugin", "set", plugin["Id"], f"ipc.source={mountpoint}"]
                )
                plugin = self._verify_binding(
                    identity, await self._inspect(identity.name), mountpoint
                )
            await self.docker.command(
                [
                    "plugin",
                    "enable",
                    "--timeout",
                    str(ENGINE_RPC_TIMEOUT_S),
                    plugin["Id"],
                ],
                timeout=ENGINE_RPC_TIMEOUT_S + 5,
            )
            plugin = await self._inspect(identity.name)
        if not self._verify_binding(identity, plugin, mountpoint)["Enabled"]:
            raise DockerError("workspace_driver_not_ready")
        if identity.removing:
            self._save(replace(identity, removing=False))
        self._name = identity.name
        return identity.name

    async def start(self) -> str:
        async with self.lock:
            self._name = None
            mountpoint = await self._ipc_mountpoint()
            identity = self._load() or self._new()
            plugin = await self._inspect(identity.name)
            if plugin is None:
                if identity.removing:
                    identity = self._new()
                elif identity.plugin_id is not None:
                    raise DockerError("workspace_driver_missing")
                await self.docker.command(
                    ["plugin", "create", identity.name, str(self.context)], timeout=45
                )
                plugin = await self._inspect(identity.name)
                if plugin is None:
                    raise DockerError("workspace_driver_create_unconfirmed")
            return await self._activate(identity, plugin, mountpoint)

    async def start_existing(self) -> str | None:
        """Включает зарегистрированный драйвер для очистки без нового create."""
        async with self.lock:
            self._name = None
            identity = self._load()
            if identity is None:
                return None
            plugin = await self._inspect(identity.name)
            if plugin is None:
                return None
            return await self._activate(identity, plugin, await self._ipc_mountpoint())

    async def ready(self) -> bool:
        async with self.lock:
            if self._name is None:
                return False
            identity = self._load()
            if identity is None or identity.name != self._name or identity.removing:
                return False
            plugin = await self._inspect(identity.name)
            if (
                plugin is None
                or plugin["Id"] != identity.plugin_id
                or not plugin["Enabled"]
            ):
                return False
            mountpoint = await self._ipc_mountpoint()
            return self._manifest_matches(plugin, mountpoint) and self._bound(
                plugin, mountpoint
            )

    async def stop(self) -> None:
        async with self.lock:
            self._name = None
            identity = self._load()
            if identity is None:
                return
            plugin = await self._inspect(identity.name)
            if plugin is None:
                return
            identity = self._claim(identity, plugin, mountpoint=None)
            if not identity.removing:
                self._save(replace(identity, removing=True))
            # Engine откажет, если у драйвера остались подключённые тома.
            if plugin["Enabled"]:
                await self.docker.command(["plugin", "disable", plugin["Id"]])
            await self.docker.command(["plugin", "rm", plugin["Id"]])
            remaining = await self._inspect(identity.name)
            if remaining is not None and remaining["Id"] == identity.plugin_id:
                raise DockerError("workspace_driver_remove_unconfirmed")
