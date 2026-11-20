"""Проверки контракта и аварийных состояний без выдачи FUSE тестам."""

import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from services.workspace_driver import volume_driver as plugin

REF = "a" * 32
OTHER_REF = "b" * 32


def request(
    driver: plugin.WorkspaceDriver, operation: str, **payload: Any
) -> dict[str, Any]:
    return driver.request(f"/VolumeDriver.{operation}", payload)


def create(driver: plugin.WorkspaceDriver, name: str, ref: str, mode: str) -> None:
    request(driver, "Create", Name=name, Opts={"workspace_ref": ref, "mode": mode})


@pytest.fixture
def driver(tmp_path: Path) -> Iterator[plugin.WorkspaceDriver]:
    with closing(sqlite3.connect(tmp_path / "registry.sqlite3")) as database:
        yield plugin.WorkspaceDriver(
            database, tmp_path / "mounts", tmp_path / "ipc", tmp_path / "runtime"
        )


class Child:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, *, timeout: float) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("sshfs", timeout)
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class StuckChild(Child):
    def kill(self) -> None:
        # После SIGKILL процесс ещё может ждать завершения запроса ядром.
        pass


class Filesystem:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mounts: dict[Path, Child] = {}
        self.launches: list[list[str]] = []
        self.detach_fails = False
        self.detach_child: Child | None = None
        self.probe_hangs = False
        self.probe_count = 0
        monkeypatch.setattr(plugin, "mounted", self.mounts.__contains__)
        monkeypatch.setattr(plugin, "_slot_ready", lambda _: True)
        monkeypatch.setattr(plugin, "_spawn", self.launch)
        monkeypatch.setattr(plugin, "_stop", self.stop)

    def launch(self, command: list[str]) -> Child:
        if command[0] == "sshfs":
            self.launches.append(command)
            child = Child()
            self.mounts[Path(command[5])] = child
            return child
        if command[:2] == ["fusermount3", "-u"]:
            if self.detach_child is not None:
                return self.detach_child
            if self.detach_fails:
                return Child()
            self.mounts.pop(Path(command[2])).returncode = 0
            return Child(0)
        assert "os.O_DIRECTORY" in command[2]
        assert Path(command[-1]) in self.mounts
        self.probe_count += 1
        return Child(None if self.probe_hangs else 0)

    def stop(self, child: Child) -> None:
        child.kill()


def test_volume_reference_is_private_unique_and_bound_to_fixed_options(
    driver: plugin.WorkspaceDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "mounted", lambda _: False)
    create(driver, "volume-a", REF, "rw")
    create(driver, "volume-a", REF, "rw")
    with pytest.raises(plugin.DriverError, match="workspace_volume_conflict"):
        create(driver, "other", REF, "rw")
    with pytest.raises(plugin.DriverError, match="workspace_volume_conflict"):
        create(driver, "volume-a", REF, "ro")
    with pytest.raises(plugin.DriverError, match="invalid_workspace_options"):
        create(driver, "volume-b", "../escape", "rw")
    with pytest.raises(plugin.DriverError, match="invalid_volume_name"):
        create(driver, "../outside", OTHER_REF, "rw")
    with pytest.raises(plugin.DriverError, match="invalid_workspace_options"):
        request(
            driver,
            "Create",
            Name="volume-b",
            Opts={
                "workspace_ref": OTHER_REF,
                "mode": "rw",
                "IdentityFile": "/some/key",
            },
        )
    report = request(driver, "List")
    assert len(report["Volumes"]) == 1
    assert report["Volumes"][0]["Status"] == {
        "Mounted": False,
        "Ready": False,
        "Mode": "rw",
    }
    assert REF not in repr(report)


def test_mounts_are_distinct_read_only_checked_and_cleanup_is_explicit(
    driver: plugin.WorkspaceDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = Filesystem(monkeypatch)
    create(driver, "volume-a", REF, "ro")
    create(driver, "volume-b", OTHER_REF, "rw")
    mount = request(driver, "Mount", Name="volume-a", ID="container-a")
    assert request(driver, "Mount", Name="volume-a", ID="container-a") == mount
    with pytest.raises(plugin.DriverError, match="workspace_already_claimed"):
        request(driver, "Mount", Name="volume-a", ID="other-container")
    request(driver, "Mount", Name="volume-b", ID="container-b")
    assert len(fs.launches) == 2
    assert fs.probe_count == 2
    for command, name, ref, mode in zip(
        fs.launches,
        ("volume-a", "volume-b"),
        (REF, OTHER_REF),
        ("ro", "rw"),
        strict=True,
    ):
        assert command[-1].split(",")[0] == mode
        assert "dir_cache=no" in command[-1]
        assert "attr_timeout=0" in command[-1]
        assert "entry_timeout=0" in command[-1]
        assert "noexec" not in command[-1]
        ssh_config = Path(command[3]).read_text()
        assert "StrictHostKeyChecking yes" in ssh_config
        assert f"{tmp_path}/ipc/{ref}/reverse.sock" in ssh_config
        assert request(driver, "Get", Name=name)["Volume"]["Status"]["Ready"] is True
    with pytest.raises(plugin.DriverError, match="workspace_volume_in_use"):
        request(driver, "Remove", Name="volume-a")
    fs.detach_fails = True
    with pytest.raises(plugin.DriverError, match="workspace_unmount_failed"):
        request(driver, "Unmount", Name="volume-a", ID="container-a")
    with pytest.raises(plugin.DriverError, match="workspace_mount_lost"):
        request(driver, "Mount", Name="volume-a", ID="container-a")
    with pytest.raises(plugin.DriverError, match="workspace_already_claimed"):
        request(driver, "Unmount", Name="volume-a", ID="container-b")
    fs.detach_fails = False
    request(driver, "Unmount", Name="volume-a", ID="container-a")
    request(driver, "Unmount", Name="volume-a", ID="container-a")
    assert (
        request(driver, "Get", Name="volume-a")["Volume"]["Status"]["Mounted"] is False
    )
    assert request(driver, "Get", Name="volume-b")["Volume"]["Status"]["Ready"] is True
    request(driver, "Remove", Name="volume-a")
    request(driver, "Remove", Name="volume-a")
    assert len(request(driver, "List")["Volumes"]) == 1


def test_driver_restart_recognizes_old_mount_but_never_reconnects_it(
    driver: plugin.WorkspaceDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = Filesystem(monkeypatch)
    create(driver, "volume-a", REF, "rw")
    request(driver, "Mount", Name="volume-a", ID="previous-owner")
    with closing(sqlite3.connect(tmp_path / "registry.sqlite3")) as database:
        restarted = plugin.WorkspaceDriver(
            database, tmp_path / "mounts", tmp_path / "ipc", tmp_path / "runtime"
        )
        status = request(restarted, "Get", Name="volume-a")["Volume"]["Status"]
        assert status == {"Mounted": True, "Ready": False, "Mode": "rw"}
        with pytest.raises(plugin.DriverError, match="workspace_mount_lost"):
            request(restarted, "Mount", Name="volume-a", ID="previous-owner")
        with pytest.raises(plugin.DriverError, match="workspace_already_claimed"):
            request(restarted, "Mount", Name="volume-a", ID="new-owner")
        assert len(fs.launches) == 1
        request(restarted, "Unmount", Name="volume-a", ID="previous-owner")
        request(restarted, "Remove", Name="volume-a")
    assert request(driver, "List") == {"Volumes": []}


def test_failed_sshfs_keeps_claim_until_explicit_cleanup(
    driver: plugin.WorkspaceDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = Filesystem(monkeypatch)
    attempts: list[list[str]] = []

    def fail(command: list[str]) -> Child:
        attempts.append(command)
        return Child(1)

    monkeypatch.setattr(plugin, "_spawn", fail)
    create(driver, "volume-a", REF, "ro")
    with pytest.raises(plugin.DriverError, match="workspace_sshfs_connect_failed"):
        request(driver, "Mount", Name="volume-a", ID="container-a")
    with pytest.raises(plugin.DriverError, match="workspace_mount_lost"):
        request(driver, "Mount", Name="volume-a", ID="container-a")
    assert len(attempts) == 1
    assert not fs.mounts
    request(driver, "Remove", Name="volume-a")
    assert request(driver, "List") == {"Volumes": []}


def test_kernel_mount_without_working_sftp_never_becomes_ready(
    driver: plugin.WorkspaceDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = Filesystem(monkeypatch)
    fs.probe_hangs = True
    create(driver, "volume-a", REF, "rw")
    with pytest.raises(plugin.DriverError, match="workspace_mount_timeout"):
        request(driver, "Mount", Name="volume-a", ID="container-a")
    assert fs.probe_count == 1
    assert request(driver, "Get", Name="volume-a")["Volume"]["Status"] == {
        "Mounted": False,
        "Ready": False,
        "Mode": "rw",
    }
    assert not fs.mounts
    with pytest.raises(plugin.DriverError, match="workspace_mount_lost"):
        request(driver, "Mount", Name="volume-a", ID="container-a")
    assert len(fs.launches) == 1


def test_remove_waits_for_unmount_helper_even_when_kernel_mount_disappears(
    driver: plugin.WorkspaceDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = Filesystem(monkeypatch)
    fs.detach_child = StuckChild()
    create(driver, "volume-a", REF, "rw")
    mount = request(driver, "Mount", Name="volume-a", ID="container-a")
    with pytest.raises(plugin.DriverError, match="workspace_unmount_failed"):
        request(driver, "Unmount", Name="volume-a", ID="container-a")
    fs.mounts.pop(Path(mount["Mountpoint"])).returncode = 0
    with pytest.raises(plugin.DriverError, match="workspace_volume_in_use"):
        request(driver, "Remove", Name="volume-a")
    fs.detach_child.returncode = -9
    request(driver, "Remove", Name="volume-a")
    assert request(driver, "List") == {"Volumes": []}
