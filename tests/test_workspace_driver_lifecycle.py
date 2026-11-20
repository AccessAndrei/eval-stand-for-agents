import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from services.sandbox_manager.docker import Docker, DockerError, DockerReply
from services.sandbox_manager.workspace_driver import WorkspaceDriver

CONTEXT = Path(__file__).resolve().parents[1] / "services" / "workspace_driver"
IPC_VOLUME = "eval-stand-workspace-ipc"
MOUNTPOINT = f"/var/lib/docker/volumes/{IPC_VOLUME}/_data"
PLUGIN_ID = "a" * 64


class Engine(Docker):
    def __init__(self, state: Path) -> None:
        self.state = state
        self.plugin: dict | None = None
        self.commands: list[list[str]] = []
        self.volume = {
            "Name": IPC_VOLUME,
            "Driver": "local",
            "Scope": "local",
            "Options": None,
            "Labels": {"io.eval-stand.role": "workspace-ipc"},
            "Mountpoint": MOUNTPOINT,
        }
        self.lose_create_response = False
        self.lose_remove_response = False
        self.in_use = False

    async def inspect(self, kind: str, name: str) -> dict | None:
        assert (kind, name) == ("volume", IPC_VOLUME)
        return self.volume

    async def command(
        self,
        args: list[str],
        data: bytes = b"",
        limit: int = 2_000_000,
        timeout: float | None = None,
        *,
        check: bool = True,
    ) -> DockerReply:
        assert args[0] == "plugin"
        self.commands.append(args)
        operation = args[1]
        if operation == "ls":
            name = self.plugin["Name"] if self.plugin else ""
            return DockerReply(name.encode(), b"", 0)
        if operation == "create":
            assert self.plugin is None
            name = args[2]
            identity = json.loads(self.state.read_text())
            assert identity["name"] == name and identity["plugin_id"] is None
            config = json.loads((CONTEXT / "config.json").read_text())
            config["rootfs"] = {"type": "layers", "diff_ids": ["sha256:" + "b" * 64]}
            config["Linux"]["Devices"][0]["Settable"] = None
            self.plugin = {
                "Name": name,
                "Id": PLUGIN_ID,
                "Enabled": False,
                "Config": config,
                "Settings": {
                    "Mounts": deepcopy(config["Mounts"]),
                    "Devices": deepcopy(config["Linux"]["Devices"]),
                    "Env": None,
                    "Args": None,
                },
            }
            if self.lose_create_response:
                raise DockerError("docker_transport_or_timeout")
            return DockerReply(b"", b"", 0)
        assert self.plugin is not None
        if operation == "inspect":
            assert args[2] == self.plugin["Name"]
            return DockerReply(json.dumps([self.plugin]).encode(), b"", 0)
        identity = json.loads(self.state.read_text())
        assert identity["plugin_id"] == self.plugin["Id"]
        if operation == "set":
            assert args[2:] == [self.plugin["Id"], f"ipc.source={MOUNTPOINT}"]
            assert not self.plugin["Enabled"]
            for section in ("Config", "Settings"):
                self.plugin[section]["Mounts"][0]["Source"] = MOUNTPOINT
        elif operation == "enable":
            assert args[2:] == ["--timeout", "30", self.plugin["Id"]]
            assert timeout == 35
            self.plugin["Enabled"] = True
        elif operation == "disable":
            assert args[2:] == [self.plugin["Id"]]
            assert identity["removing"]
            if self.in_use:
                raise DockerError("workspace_driver_in_use")
            self.plugin["Enabled"] = False
        elif operation == "rm":
            assert args[2:] == [self.plugin["Id"]]
            assert identity["removing"] and not self.plugin["Enabled"]
            self.plugin = None
            if self.lose_remove_response:
                raise DockerError("docker_transport_or_timeout")
        else:
            raise AssertionError(args)
        return DockerReply(b"", b"", 0)

    def mutations(self) -> list[str]:
        return [call[1] for call in self.commands if call[1] not in {"ls", "inspect"}]


def create_driver(engine: Engine) -> WorkspaceDriver:
    return WorkspaceDriver(
        engine, engine.state, "eval-stand", IPC_VOLUME, context=CONTEXT
    )


def test_lifecycle_recovers_create_and_remove_with_lost_response(
    tmp_path: Path,
) -> None:
    engine = Engine(tmp_path / "workspace-driver" / "identity.json")

    async def scenario() -> None:
        driver = create_driver(engine)
        assert not await driver.ready()
        engine.lose_create_response = True
        with pytest.raises(DockerError, match="docker_transport_or_timeout"):
            await driver.start()
        engine.lose_create_response = False
        restored = create_driver(engine)
        first_name = await restored.start()
        assert restored.name == first_name
        assert await restored.ready()
        assert await create_driver(engine).start() == first_name
        assert engine.mutations() == ["create", "set", "enable"]

        engine.lose_remove_response = True
        with pytest.raises(DockerError, match="docker_transport_or_timeout"):
            await restored.stop()
        assert not await restored.ready()
        await restored.stop()
        engine.lose_remove_response = False
        assert await restored.start() != first_name
        await restored.stop()
        assert engine.plugin is None

    asyncio.run(scenario())


def test_recognizes_replacement_by_engine_id_and_refuses_to_remove(
    tmp_path: Path,
) -> None:
    engine = Engine(tmp_path / "identity.json")

    async def scenario() -> None:
        original = create_driver(engine)
        await original.start()
        assert engine.plugin is not None
        engine.plugin["Id"] = "c" * 64
        assert not await original.ready()
        replacement = create_driver(engine)
        with pytest.raises(DockerError, match="foreign_workspace_driver"):
            await replacement.start()
        with pytest.raises(DockerError, match="foreign_workspace_driver"):
            await replacement.stop()
        assert engine.mutations() == ["create", "set", "enable"]
        assert engine.plugin["Enabled"]

    asyncio.run(scenario())


def test_untrusted_ipc_volume_rejected_before_registration(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "identity.json")
    engine.volume["Options"] = {"type": "none", "o": "bind", "device": "/"}
    with pytest.raises(DockerError, match="workspace_ipc_volume_mismatch"):
        asyncio.run(create_driver(engine).start())
    assert engine.commands == []
    assert not engine.state.exists()


def test_driver_is_never_force_removed_while_in_use(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "identity.json")

    async def scenario() -> None:
        driver = create_driver(engine)
        name = await driver.start()
        engine.in_use = True
        with pytest.raises(DockerError, match="workspace_driver_in_use"):
            await driver.stop()
        assert engine.mutations() == ["create", "set", "enable", "disable"]
        assert engine.plugin is not None and engine.plugin["Enabled"]
        restored = create_driver(engine)
        assert await restored.start() == name
        engine.in_use = False
        await restored.stop()
        assert engine.plugin is None

    asyncio.run(scenario())


def test_offline_cleanup_only_restores_existing_driver(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "identity.json")

    async def scenario() -> None:
        driver = create_driver(engine)
        assert await driver.start_existing() is None
        assert engine.commands == []

        name = await driver.start()
        assert engine.plugin is not None
        engine.plugin["Enabled"] = False
        cleanup = create_driver(engine)
        assert await cleanup.start_existing() == name
        assert engine.mutations() == ["create", "set", "enable", "enable"]
        await cleanup.stop()

        engine.volume["Options"] = {"type": "nfs"}
        assert await cleanup.start_existing() is None
        assert engine.mutations() == [
            "create",
            "set",
            "enable",
            "enable",
            "disable",
            "rm",
        ]

    asyncio.run(scenario())
