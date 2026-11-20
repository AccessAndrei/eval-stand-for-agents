"""Контрактные проверки менеджера с фиктивным Docker, не доказательство изоляции."""

import asyncio
import base64
import json
from copy import deepcopy
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Coroutine, ParamSpec, TypeVar

import pytest

from services.sandbox_manager import manager as manager_module
from services.sandbox_manager.config import Limits
from services.sandbox_manager.docker import DockerError, DockerReply
from services.sandbox_manager.helpers import collect
from services.sandbox_manager.manager import Manager, ManagerError
from services.sandbox_manager.provenance import (
    CODEX_COMMIT,
    CODEX_VERSION,
    publish_provenance,
)
from services.sandbox_manager.server import MANAGER, health
from services.sandbox_manager.tunnel import _request, connection

P = ParamSpec("P")
R = TypeVar("R")


def run_async(function: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(function(*args, **kwargs))

    return wrapped


class FakeDocker:
    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.volumes: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.failure: str | None = None

    async def inspect(self, kind: str, name: str) -> dict | None:
        return deepcopy(
            (self.volumes if kind == "volume" else self.containers).get(name)
        )

    def labels(self, args: list[str]) -> dict[str, str]:
        return dict(
            args[i + 1].split("=", 1)
            for i, value in enumerate(args)
            if value == "--label"
        )

    async def command(
        self, args: list[str], data: bytes = b"", **kwargs
    ) -> DockerReply:
        self.calls.append(args)
        if self.failure == args[0]:
            raise DockerError("injected")
        if args[:2] in (["container", "ls"], ["volume", "ls"]):
            resources = self.containers if args[0] == "container" else self.volumes
            return DockerReply("\n".join(resources).encode(), b"", 0)
        if args[:2] == ["volume", "create"]:
            self.volumes.setdefault(
                args[-1],
                {
                    "Labels": self.labels(args),
                    "Driver": args[args.index("--driver") + 1]
                    if "--driver" in args
                    else "local",
                    "Options": dict(
                        args[i + 1].split("=", 1)
                        for i, option in enumerate(args)
                        if option == "--opt"
                    ),
                },
            )
        elif args[:2] == ["volume", "rm"]:
            self.volumes.pop(args[-1], None)
        elif args[0] in {"create", "run"}:
            name = args[args.index("--name") + 1]
            mounts = []
            if "--mount" in args:
                mount = args[args.index("--mount") + 1]
                parts = dict(p.split("=", 1) for p in mount.split(",") if "=" in p)
                mounts = [
                    {
                        "Destination": parts["dst"],
                        "Name": parts["src"],
                        "RW": "readonly" not in mount,
                    }
                ]
            self.containers[name] = {
                "Id": name,
                "Name": "/" + name,
                "Config": {"Labels": self.labels(args)},
                "Mounts": mounts,
            }
        elif args[0] == "rm":
            self.containers.pop(args[-1], None)
        elif args[0] == "start":
            container = self.containers[args[-1]]
            container["State"] = {"Running": True, "Paused": False}
            for mount in container["Mounts"]:
                if mount["Destination"] == "/work":
                    self.volumes[mount["Name"]]["Status"] = {"Ready": True}
        elif args[0] in {"pause", "unpause"}:
            self.containers[args[-1]]["State"]["Paused"] = args[0] == "pause"
        if args[0] == "run" and args[-3] == "collect_trusted":
            return DockerReply(
                json.dumps(
                    {
                        "status": "complete",
                        "files": [
                            {
                                "path": "answer.txt",
                                "content_base64": base64.b64encode(b"8080").decode(),
                            }
                        ],
                        "manifest": {},
                    }
                ).encode(),
                b"",
                0,
            )
        return DockerReply(b"", b"", 0)


@pytest.fixture
def request_data() -> dict:
    return {
        "version": "v0",
        "project_id": "test",
        "run_id": "run-1",
        "fingerprint": "f" * 64,
        "task_id": "B1.1",
        "files": [{"path": "input.txt", "content": "test"}],
        "read_only_paths": [],
        "timeout_s": 30,
    }


@pytest.fixture
def manager(tmp_path: Path) -> Manager:
    return Manager(tmp_path, "test", "sandbox:test", docker=FakeDocker())


def control(result: dict) -> dict:
    return {key: result[key] for key in ("version", "project_id", "run_id", "handle")}


@pytest.fixture
def mounted_request(manager: Manager) -> dict:
    manager.workspace_driver = SimpleNamespace(name="driver:local")
    return {
        "version": "v1",
        "project_id": "test",
        "session_id": "b" * 32,
        "workspace_ref": "a" * 32,
        "mode": "ro",
    }


@run_async
async def test_mounted_provision_and_release_keep_workspace_out_of_legacy_collection(
    manager: Manager, mounted_request: dict
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    assert opened["state"] == "ready"
    assert await manager.provision_mounted(mounted_request) == opened
    record = manager._load(opened["session_id"])
    assert record["workspace"]["workspace_ref"] == mounted_request["workspace_ref"]
    [create] = [call for call in manager.docker.calls if call[0] == "create"]
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--cap-drop") + 1] == "ALL"
    assert not any(arg.startswith("/work:") for arg in create)
    assert ",dst=/work,volume-nocopy,readonly" in create[create.index("--mount") + 1]
    with pytest.raises(ManagerError, match="mounted_contract_required"):
        await manager.collect(
            {
                **{key: opened[key] for key in ("project_id", "handle")},
                "version": "v0",
                "run_id": opened["session_id"],
            }
        )
    request = {
        key: opened[key] for key in ("version", "project_id", "session_id", "handle")
    }
    released = await manager.release_mounted(request, opened["handle"])
    assert released["cleanup_status"] == "success"
    assert await manager.release_mounted(request, opened["handle"]) == released
    assert not manager.docker.volumes and not manager.docker.containers
    assert [
        call[0] for call in manager.docker.calls if call[0] in {"run", "exec"}
    ] == []
    remove = [call for call in manager.docker.calls if "rm" in call[:2]]
    assert [call[0] for call in remove] == ["rm", "volume"]


@pytest.mark.parametrize("timed_out", [False, True])
@run_async
async def test_mounted_turn_is_bound_once_and_collects_only_metadata(
    manager: Manager,
    mounted_request: dict,
    monkeypatch: pytest.MonkeyPatch,
    timed_out: bool,
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    control = {
        key: opened[key] for key in ("version", "project_id", "session_id", "handle")
    }
    assert await manager.lookup_mounted(control["session_id"], "test") == opened
    with pytest.raises(ManagerError, match="foreign_project"):
        await manager.lookup_mounted(control["session_id"], "other")
    calls = []
    processes = []

    async def spawn(args: list[str]) -> Any:
        calls.append(args)
        # Менеджер пишет журнал до принятия Docker первого вызова исполнителя.
        assert manager._load(control["session_id"])["execution_started"]
        process = SimpleNamespace(stdout=asyncio.StreamReader())
        process.stdout.feed_data(bytes.fromhex(args[-2]) + b"native-output")
        processes.append(process)
        return process

    async def stop(owned_process: Any, *, timeout: float) -> bool:
        assert owned_process in processes and timeout == 3
        calls.append("stopped")
        return True

    monkeypatch.setattr(manager.docker, "spawn", spawn, raising=False)
    monkeypatch.setattr(manager_module, "stop_group", stop)
    request = {**control, "run_id": "c" * 32}
    reserve = {**request, "timeout_s": 25}
    handle = control["handle"]
    first = await manager.reserve_mounted_run(reserve, handle)
    assert first["run_id"] == request["run_id"]
    assert await manager.reserve_mounted_run(reserve, handle) == first
    with pytest.raises(ManagerError, match="workspace_run_conflict"):
        await manager.reserve_mounted_run({**reserve, "timeout_s": 26}, handle)
    with pytest.raises(ManagerError, match="invalid_workspace_request"):
        await manager.reserve_mounted_run({**reserve, "timeout_s": 601}, handle)

    reader = asyncio.StreamReader()
    reader.feed_data(
        (
            f"CONNECT /v1/environments/{handle} HTTP/1.1\r\n"
            f"Authorization: Bearer {'t' * 32}\r\nX-Project-ID: test\r\n"
            f"X-Session-ID: {control['session_id']}\r\nX-Run-ID: {request['run_id']}\r\n\r\n"
        ).encode()
    )
    connection = await _request(reader, "t" * 32)
    with pytest.raises(ManagerError, match="unknown_workspace_run"):
        await manager.connect_mounted(handle, "d" * 32, "test", control["session_id"])
    link_a = await manager.connect_mounted(*connection)
    assert link_a.process is processes[0] and link_a.timeout_s == 25
    assert await link_a.process.stdout.readexactly(13) == b"native-output"
    assert calls[0][-3] == manager_module.MOUNTED_EXEC_WRAPPER
    assert calls[0][-1] == "/usr/local/bin/codex"
    assert "/work" in calls[0]
    with pytest.raises(ManagerError, match="execution_cannot_replay"):
        await manager.connect_mounted(*connection)
    if timed_out:
        await manager.mounted_transport_failure(link_a, timed_out=True)
    collect = asyncio.create_task(manager.collect_mounted_run(request, handle))
    async with asyncio.timeout(1):
        await link_a.stop_requested.wait()
        request_b = {**request, "run_id": "d" * 32}
        with pytest.raises(ManagerError, match="busy"):
            await manager.reserve_mounted_run({**request_b, "timeout_s": 25}, handle)
        await manager.close_mounted_executor(link_a)
        result = await collect
    assert result == {
        "version": "v1",
        "project_id": "test",
        "session_id": control["session_id"],
        "run_id": request["run_id"],
        "collection_status": "complete",
        "writers_stopped": True,
        "execution_started": True,
        "errors": (
            [
                {
                    "stage": "execution",
                    "code": "timeout",
                    "message": "Истёк срок канала exec-server",
                }
            ]
            if timed_out
            else []
        ),
    }
    assert calls[1:] == ["stopped"]
    barrier = [
        call for call in manager.docker.calls if call[0] in {"run", "pause", "unpause"}
    ]
    assert [call[-1] if call[0] == "run" else call[0] for call in barrier] == [
        "pause",
        "kill_frozen",
        "unpause",
        "verify_quiescent",
    ]
    assert not list(manager.root.glob("*.collection"))
    old_status = await manager.reserve_mounted_run(reserve, handle)
    old_sandbox = manager._load(control["session_id"])["mounted_sandbox_id"]
    old_volumes = set(manager.docker.volumes)
    second = await manager.reserve_mounted_run({**request_b, "timeout_s": 25}, handle)
    assert second["run_id"] == request_b["run_id"] and second["errors"] == []
    link_b = await manager.connect_mounted(
        handle, request_b["run_id"], "test", control["session_id"]
    )
    with pytest.raises(ManagerError, match="unknown_workspace_run"):
        await manager.connect_mounted(*connection)
    await manager.mounted_transport_failure(link_a, timed_out=True)
    await manager.close_mounted_executor(link_a)
    assert await manager.collect_mounted_run(request, handle) == result
    assert await manager.reserve_mounted_run(reserve, handle) == old_status
    assert not link_b.stop_requested.is_set() and not link_b.drained.is_set()
    assert (await manager.lookup_mounted(control["session_id"], "test"))["errors"] == []
    assert old_sandbox == manager._load(control["session_id"])["mounted_sandbox_id"]
    assert old_volumes == set(manager.docker.volumes)
    await manager.close_mounted_executor(link_b)
    assert (await manager.collect_mounted_run(request_b, handle))["writers_stopped"]
    assert (await manager.release_mounted(control, handle))["state"] == "released"
    assert await manager.collect_mounted_run(request, handle) == result
    restarted = Manager(manager.root, "test", manager.image, docker=manager.docker)
    assert await restarted.collect_mounted_run(request, handle) == result
    assert await restarted.reserve_mounted_run(reserve, handle) == old_status


@run_async
async def test_mounted_lookup_reports_lost_mount_without_changing_resources(
    manager: Manager, mounted_request: dict
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    path = manager._path(opened["session_id"])
    before = path.read_bytes()
    [volume] = manager.docker.volumes.values()
    volume["Status"]["Ready"] = False
    status = await manager.lookup_mounted(opened["session_id"], "test")
    assert status["state"] == "failed" and status["handle"] == opened["handle"]
    assert status["errors"][-1]["code"] == "workspace_mount_lost"
    assert path.read_bytes() == before
    assert manager.docker.volumes and manager.docker.containers


@run_async
async def test_cancel_before_executor_admission_can_release_turn_without_new_exec(
    manager: Manager, mounted_request: dict
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    handle = opened["handle"]
    control = {
        key: opened[key] for key in ("version", "project_id", "session_id", "handle")
    }
    a, b = {**control, "run_id": "c" * 32}, {**control, "run_id": "d" * 32}
    await manager.reserve_mounted_run({**a, "timeout_s": 10}, handle)
    result = await manager.collect_mounted_run(a, handle)
    assert result["writers_stopped"] and not result["execution_started"]
    assert not {"exec", "pause", "unpause"}.intersection(
        call[0] for call in manager.docker.calls
    )
    assert (await manager.reserve_mounted_run({**b, "timeout_s": 10}, handle))[
        "run_id"
    ] == b["run_id"]
    with pytest.raises(ManagerError, match="unknown_workspace_run"):
        await manager.connect_mounted(
            handle, a["run_id"], "test", control["session_id"]
        )


@run_async
async def test_collect_drains_connected_executor_before_freezing(
    manager: Manager,
    mounted_request: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    session_id, handle = opened["session_id"], opened["handle"]
    run_id, token = "c" * 32, "t" * 32
    control = {
        **{
            key: opened[key]
            for key in ("version", "project_id", "session_id", "handle")
        },
        "run_id": run_id,
    }
    await manager.reserve_mounted_run({**control, "timeout_s": 10}, handle)

    class Sink:
        def __init__(self) -> None:
            self.connected = asyncio.Event()
            self.closed = asyncio.Event()

        def write(self, chunk: bytes) -> None:
            if chunk.startswith(b"HTTP/1.1 200 "):
                self.connected.set()

        async def drain(self) -> None:
            return None

        def can_write_eof(self) -> bool:
            return False

        def close(self) -> None:
            self.closed.set()

        async def wait_closed(self) -> None:
            await self.closed.wait()

    finished = asyncio.Event()
    process = SimpleNamespace(
        stdin=Sink(),
        stdout=asyncio.StreamReader(),
        stderr=asyncio.StreamReader(),
        returncode=None,
    )

    async def wait() -> int:
        await finished.wait()
        return 0

    async def spawn(args: list[str]) -> Any:
        process.stdout.feed_data(bytes.fromhex(args[-2]))
        return process

    writer = Sink()

    async def stop(old: Any, *, timeout: float) -> bool:
        assert old is process and writer.closed.is_set() and timeout == 3
        finished.set()
        return True

    process.wait = wait
    monkeypatch.setattr(manager.docker, "spawn", spawn, raising=False)
    monkeypatch.setattr(manager_module, "stop_group", stop)
    reader = asyncio.StreamReader()
    reader.feed_data(
        (
            f"CONNECT /v1/environments/{handle} HTTP/1.1\r\n"
            f"Authorization: Bearer {token}\r\nX-Project-ID: test\r\n"
            f"X-Session-ID: {session_id}\r\nX-Run-ID: {run_id}\r\n\r\n"
        ).encode()
    )
    tunnel = asyncio.create_task(connection(reader, writer, manager, token))
    async with asyncio.timeout(2):
        await writer.connected.wait()
        result = await manager.collect_mounted_run(control, handle)
        await tunnel
    assert result["writers_stopped"] and result["errors"] == []
    assert writer.closed.is_set() and not manager.mounted_executors


@run_async
async def test_mounted_connect_is_fenced_if_stop_intent_cannot_be_persisted(
    manager: Manager,
    mounted_request: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    session_id, handle, run_id = opened["session_id"], opened["handle"], "c" * 32
    control = {
        key: opened[key] for key in ("version", "project_id", "session_id", "handle")
    }
    await manager.reserve_mounted_run(
        {**control, "run_id": run_id, "timeout_s": 25}, handle
    )

    def write_failure(*_: Any) -> None:
        raise OSError("injected journal failure")

    record = manager._load(session_id)
    record["state"] = "quiescing"
    with monkeypatch.context() as patch:
        patch.setattr(manager_module, "atomic_json", write_failure)
        with pytest.raises(OSError):
            manager._save(record)
    assert manager._load(session_id)["state"] == "ready"
    with pytest.raises(ManagerError, match="storage_budget_exceeded"):
        await manager.connect_mounted(handle, run_id, "test", session_id)
    assert not manager.mounted_executors


@pytest.mark.parametrize("failure", ["marker", "kill_frozen", "verify_quiescent"])
@run_async
async def test_unconfirmed_mounted_stop_closes_session_and_refuses_new_turn(
    manager: Manager,
    mounted_request: dict,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    session_id, handle = opened["session_id"], opened["handle"]
    control = {
        **{
            key: opened[key]
            for key in ("version", "project_id", "session_id", "handle")
        },
        "run_id": "c" * 32,
    }
    await manager.reserve_mounted_run({**control, "timeout_s": 25}, handle)
    command = manager.docker.command
    failed_actions = []

    async def injected(args: list[str], **kwargs: Any) -> DockerReply:
        if args[0] == "run" and args[-1] == failure:
            failed_actions.append(args[-1])
            raise DockerError("injected phase failure")
        return await command(args, **kwargs)

    async def spawn(args: list[str]) -> Any:
        process = SimpleNamespace(stdout=asyncio.StreamReader())
        marker = bytes.fromhex(args[-2])
        process.stdout.feed_data(b"x" * len(marker) if failure == "marker" else marker)
        return process

    async def stop(owned_process: Any, *, timeout: float) -> bool:
        assert owned_process.stdout is not None and timeout == 3
        return True

    monkeypatch.setattr(manager.docker, "spawn", spawn, raising=False)
    monkeypatch.setattr(manager.docker, "command", injected)
    monkeypatch.setattr(manager_module, "stop_group", stop)
    connect = (handle, control["run_id"], "test", session_id)
    if failure == "marker":
        with pytest.raises(ManagerError, match="executor_entry_unconfirmed"):
            await manager.connect_mounted(*connect)
    else:
        link = await manager.connect_mounted(*connect)
        await manager.close_mounted_executor(link)

    result = await manager.collect_mounted_run(control, handle)
    assert result["collection_status"] == "incomplete" and not result["writers_stopped"]
    assert result["errors"][-1]["code"] == "writers_unconfirmed"
    assert not manager.docker.containers and not manager.docker.volumes
    assert not manager.mounted_executors
    if failure != "marker":
        assert failed_actions == [failure]
        assert any(call[0] == "unpause" for call in manager.docker.calls)
    else:
        assert not any(call[0] == "pause" for call in manager.docker.calls)
    with pytest.raises(ManagerError, match="execution_cannot_replay"):
        await manager.reserve_mounted_run(
            {**control, "run_id": "d" * 32, "timeout_s": 25}, handle
        )
    assert await manager.collect_mounted_run(control, handle) == result


@run_async
async def test_lost_provision_release_fences_delayed_post(
    manager: Manager, mounted_request: dict
) -> None:
    session_id = mounted_request["session_id"]
    with pytest.raises(ManagerError) as missing:
        await manager.lookup_mounted(session_id, "test")
    assert missing.value.http_status == 404
    fenced = await manager.fence_mounted_release(mounted_request, session_id)
    assert fenced["state"] == "released" and fenced["cleanup_status"] == "success"
    assert await manager.provision_mounted(mounted_request) == fenced
    assert await manager.fence_mounted_release(mounted_request, session_id) == fenced
    with pytest.raises(ManagerError, match="run_conflict"):
        await manager.fence_mounted_release(
            {**mounted_request, "mode": "rw"}, session_id
        )
    assert manager.docker.calls == []

    later = {**mounted_request, "session_id": "e" * 32}
    assert (await manager.provision_mounted(later))["state"] == "ready"
    released = await manager.fence_mounted_release(later, later["session_id"])
    assert released["state"] == "released" and released["cleanup_status"] == "success"
    assert not manager.docker.volumes and not manager.docker.containers
    assert await manager.provision_mounted(later) == released


@run_async
async def test_release_fence_does_not_claim_unrecorded_resource(
    manager: Manager, mounted_request: dict
) -> None:
    session_id = mounted_request["session_id"]
    name = manager_module.resource_name(manager.project, session_id)
    manager.docker.containers[name] = {"Id": "foreign"}
    with pytest.raises(ManagerError, match="workspace_cleanup_unconfirmed"):
        await manager.fence_mounted_release(mounted_request, session_id)
    assert manager.docker.containers[name]["Id"] == "foreign"
    assert not manager._path(session_id).exists()
    assert manager.docker.calls == []


@run_async
async def test_mounted_mount_failure_is_recoverable_without_file_copy(
    manager: Manager, mounted_request: dict
) -> None:
    manager.docker.failure = "start"
    with pytest.raises(ManagerError, match="provision_failed"):
        await manager.provision_mounted(mounted_request)
    failed = await manager.provision_mounted(mounted_request)
    assert failed["state"] == "failed"
    assert failed["cleanup_status"] == "success"
    manager.docker.failure = None
    assert not manager.docker.volumes and not manager.docker.containers
    assert not list(manager.root.glob("*.collection"))
    assert len([call for call in manager.docker.calls if call[0] == "start"]) == 1


@run_async
async def test_lost_mount_is_not_replayed_and_an_orphan_is_removed(
    manager: Manager, mounted_request: dict
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    [volume] = manager.docker.volumes.values()
    volume["Status"]["Ready"] = False
    lost = await manager.provision_mounted(mounted_request)
    assert lost["handle"] == opened["handle"]
    assert lost["state"] == "failed" and lost["cleanup_status"] == "success"
    assert lost["errors"][0]["code"] == "workspace_mount_lost"
    assert len([call for call in manager.docker.calls if call[0] == "start"]) == 1
    next_request = {**mounted_request, "session_id": "f" * 32}
    await manager.provision_mounted(next_request)
    manager._path(next_request["session_id"]).unlink()
    await manager.recover()
    assert manager._load(next_request["session_id"])["cleanup_status"] == "success"
    assert not manager.docker.volumes and not manager.docker.containers
    assert not list(manager.root.glob("*.collection"))


@run_async
async def test_unconfirmed_mounted_cleanup_blocks_readiness(
    manager: Manager, mounted_request: dict
) -> None:
    opened = await manager.provision_mounted(mounted_request)
    request = {
        key: opened[key] for key in ("version", "project_id", "session_id", "handle")
    }
    manager.docker.failure = "rm"
    assert (await manager.release_mounted(request, opened["handle"]))[
        "cleanup_status"
    ] == "failed"
    with pytest.raises(ManagerError, match="cleanup_failed") as error:
        await health(SimpleNamespace(app={MANAGER: manager}))
    assert error.value.http_status == 503
    manager.docker.failure = None
    assert (await manager.release_mounted(request, opened["handle"]))[
        "cleanup_status"
    ] == "success"
    assert not manager.unclean_workspaces


@run_async
async def test_provenance_uses_confirmed_pinned_source_image(tmp_path: Path) -> None:
    docker = FakeDocker()
    digest = "sha256:" + "f" * 64
    docker.containers["codex"] = {
        "Id": digest,
        "Os": "linux",
        "Architecture": "arm64",
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": CODEX_COMMIT,
                "org.eval-stand.codex-origin": "source_build",
                "org.eval-stand.codex-version": CODEX_VERSION,
            }
        },
    }
    path = tmp_path / "provenance.json"
    await publish_provenance(docker, "codex", path)
    assert json.loads(path.read_text())["image_digest"] == digest
    assert path.stat().st_mode & 0o777 == 0o644
    docker.containers["codex"]["Config"]["Labels"].clear()
    with pytest.raises(DockerError, match="codex_source_image_mismatch"):
        await publish_provenance(docker, "codex", path)


@run_async
async def test_repeated_provision_recovery_and_conflict(
    manager: Manager, request_data: dict
) -> None:
    first = await manager.provision(request_data)
    assert first["state"] == "ready"
    assert await manager.provision(request_data) == first
    restored = Manager(manager.root, "test", "sandbox:test", docker=manager.docker)
    assert await restored.lookup("run-1", "test") == first
    assert len([call for call in manager.docker.calls if call[0] == "create"]) == 1
    request_data["files"][0]["content"] = "different"
    with pytest.raises(ManagerError, match="run_conflict"):
        await manager.provision(request_data)


@run_async
async def test_foreign_project_handle_and_container_are_preserved(
    manager: Manager, request_data: dict
) -> None:
    first = await manager.provision(request_data)
    with pytest.raises(ManagerError, match="foreign_project"):
        await manager.lookup("run-1", "other")
    with pytest.raises(ManagerError, match="foreign_handle"):
        await manager.release({**control(first), "handle": "other"})
    name = manager._load("run-1")["name"]
    manager.docker.containers[name]["Config"]["Labels"] = {}
    with pytest.raises(ManagerError, match="foreign_resource"):
        await manager.release(control(first))
    assert name in manager.docker.containers


@run_async
async def test_busy_until_release_and_idempotent_release(
    manager: Manager, request_data: dict
) -> None:
    first = await manager.provision(request_data)
    with pytest.raises(ManagerError, match="busy"):
        await manager.provision({**request_data, "run_id": "run-2"})
    released = await manager.release(control(first))
    assert released["cleanup_status"] == "success"
    assert await manager.release(control(first)) == released
    assert (await manager.provision({**request_data, "run_id": "run-2"}))[
        "state"
    ] == "ready"


@run_async
async def test_failed_creation_is_journaled_and_never_replayed(
    manager: Manager, request_data: dict
) -> None:
    manager.docker.failure = "start"
    with pytest.raises(ManagerError, match="provision_failed"):
        await manager.provision(request_data)
    failed = await manager.provision(request_data)
    assert failed["state"] == "failed"
    assert len([call for call in manager.docker.calls if call[0] == "create"]) == 1
    manager.docker.failure = None
    await manager.recover()
    assert (await manager.lookup("run-1", "test"))["cleanup_status"] == "success"
    assert not manager.docker.containers


@run_async
async def test_collection_persists_after_release(
    manager: Manager, request_data: dict
) -> None:
    first = await manager.provision(request_data)
    copied = await manager.collect(control(first))
    assert copied["status"] == "complete"
    await manager.release(control(first))
    assert await manager.collect(control(first)) == copied
    assert not manager.docker.containers


@run_async
async def test_collection_failure_keeps_cleanup_possible(
    manager: Manager, request_data: dict
) -> None:
    first = await manager.provision(request_data)
    manager.docker.failure = "run"
    assert (await manager.collect(control(first)))["status"] == "incomplete"
    manager.docker.failure = None
    released = await manager.release(control(first))
    assert released["collection_status"] == "incomplete"
    assert released["cleanup_status"] == "success"


@run_async
async def test_b5_inputs_are_readonly_mount_with_separate_identity(
    manager: Manager, request_data: dict
) -> None:
    request_data.update(
        task_id="B5.1",
        read_only_paths=["input"],
        files=[
            {"path": "input/stats.py", "content": "pass\n"},
            {"path": "input/data.csv", "content": "value\n1\n"},
        ],
    )
    first = await manager.provision(request_data)
    record = manager._load("run-1")
    assert record["protected_inputs"] is True
    assert len(record["input_manifest"]) == 2
    create = next(call for call in manager.docker.calls if call[0] == "create")
    assert "readonly,volume-nocopy" in create[create.index("--mount") + 1]
    assert create[create.index("--user") + 1] == "1001:1001"
    assert "--cap-drop" in create and "ALL" in create
    assert "none" == create[create.index("--network") + 1]
    assert "com.docker.compose.project=" in create
    assert "com.docker.compose.service=" in create
    await manager.release(control(first))
    assert not manager.docker.volumes


@run_async
async def test_collection_uses_separate_trusted_container_with_bounded_caps(
    manager: Manager,
    request_data: dict,
) -> None:
    first = await manager.provision(request_data)
    await manager.collect(control(first))
    sandbox = next(call for call in manager.docker.calls if call[0] == "create")
    collector = next(call for call in manager.docker.calls if call[0] == "run")
    assert "--privileged" not in collector
    assert "--cap-add" not in sandbox
    capabilities = {
        collector[i + 1] for i, part in enumerate(collector) if part == "--cap-add"
    }
    assert capabilities == {"KILL", "SYS_PTRACE", "DAC_READ_SEARCH"}
    assert collector[collector.index("--user") + 1] == "0:0"
    assert collector[collector.index("--pid") + 1].startswith("container:eval-")
    assert (
        float(sandbox[sandbox.index("--cpus") + 1])
        + float(collector[collector.index("--cpus") + 1])
        <= manager.limits.cpu
    )
    assert (
        int(sandbox[sandbox.index("--memory") + 1])
        + int(collector[collector.index("--memory") + 1])
        == manager.limits.memory
    )


@pytest.mark.parametrize("operation", ["shutdown", "recover"])
@run_async
async def test_enospc_in_history_never_prevents_active_cleanup(
    manager: Manager,
    request_data: dict,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    first = await manager.provision(request_data)
    await manager.release(control(first))
    second = await manager.provision({**request_data, "run_id": "run-2"})

    def disk_full(path: Path, value: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("services.sandbox_manager.manager.atomic_json", disk_full)
    await getattr(manager, operation)()
    assert not manager.docker.containers
    assert manager.persistence_failed
    assert manager._load(second["run_id"])["cleanup_status"] == "not_started"


@run_async
async def test_orphan_cleanup_proceeds_when_journal_cannot_be_recreated(
    manager: Manager,
    request_data: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await manager.provision(request_data)
    manager._path("run-1").unlink()

    def disk_full(path: Path, value: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("services.sandbox_manager.manager.atomic_json", disk_full)
    await manager.recover()
    assert not manager.docker.containers
    assert manager.persistence_failed


@pytest.mark.parametrize("operation", ["recover", "shutdown"])
@run_async
async def test_recovery_collects_before_deletion_and_keeps_cached_copy(
    manager: Manager,
    request_data: dict,
    operation: str,
) -> None:
    first = await manager.provision(request_data)
    name = manager._load("run-1")["name"]
    await getattr(manager, operation)()
    calls = manager.docker.calls
    collect_index = next(i for i, call in enumerate(calls) if "collect_trusted" in call)
    delete_index = calls.index(["rm", "-f", name])
    assert collect_index < delete_index
    count = len(calls)
    copied = await manager.collect(control(first))
    assert copied["status"] == "complete"
    assert copied["files"][0]["path"] == "answer.txt"
    assert len(calls) == count
    assert (await manager.lookup("run-1", "test"))["cleanup_status"] == "success"


@run_async
async def test_unexpected_executor_exit_survives_collection_and_release(
    manager: Manager,
    request_data: dict,
) -> None:
    first = await manager.provision(request_data)
    record = manager._load("run-1")
    record["state"] = "executing"
    manager._save(record)
    await manager.executor_exited("run-1", 137)
    await manager.collect(control(first))
    released = await manager.release(control(first))
    assert released["cleanup_status"] == "success"
    assert any(error["code"] == "transport_error" for error in released["errors"])
    assert manager._load("run-1")["errors"] == released["errors"]


@run_async
async def test_expected_executor_exit_during_collection_is_not_transport_failure(
    manager: Manager,
    request_data: dict,
) -> None:
    first = await manager.provision(request_data)
    record = manager._load("run-1")
    record["state"] = "quiescing"
    manager._save(record)
    await manager.executor_exited("run-1", 137)
    assert (await manager.release(control(first)))["errors"] == []


def test_safe_collector_rejects_links_and_limits(tmp_path: Path) -> None:
    (tmp_path / "answer.txt").write_text("42")
    output = collect(tmp_path, 10, 2)
    assert base64.b64decode(output["files"][0]["content_base64"]) == b"42"
    (tmp_path / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="unsafe_file_type"):
        collect(tmp_path, 10, 2)
    (tmp_path / "link").unlink()
    with pytest.raises(ValueError, match="collection_limit"):
        collect(tmp_path, 1, 2)


def test_tmpfs_cannot_exceed_memory() -> None:
    with pytest.raises(ValueError, match="tmpfs"):
        Limits(memory=1_000, work=800, temporary=800)


@run_async
async def test_orphan_cleanup_without_journal_preserves_foreign_container(
    manager: Manager,
    request_data: dict,
) -> None:
    first = await manager.provision(request_data)
    manager._path("run-1").unlink()
    manager.docker.containers["foreign"] = {"Config": {"Labels": {}}}
    restored = Manager(manager.root, "test", "sandbox:test", docker=manager.docker)
    await restored.recover()
    assert set(manager.docker.containers) == {"foreign"}
    result = await restored.lookup("run-1", "test")
    assert result["handle"] == first["handle"]
    assert result["cleanup_status"] == "success"
