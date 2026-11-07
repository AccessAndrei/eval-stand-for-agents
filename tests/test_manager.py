"""Контрактные проверки менеджера с фиктивным Docker, не доказательство изоляции."""

import asyncio
import base64
import json
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Coroutine, ParamSpec, TypeVar

import pytest

from services.sandbox_manager.config import Limits
from services.sandbox_manager.docker import DockerError, DockerReply
from services.sandbox_manager.helpers import collect
from services.sandbox_manager.manager import Manager, ManagerError

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
            self.volumes[args[-1]] = {"Labels": self.labels(args)}
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
