"""Docker VolumeDriver v2 для SSHFS через локальный сокет доверенного брокера."""

import json
import logging
import os
import re
import signal
import socketserver
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import FrameType
from typing import Any

LOG = logging.getLogger(__name__)
VOLUME_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
WORKSPACE_REF = re.compile(r"[a-f0-9]{32}")
MOUNT_CALLER = re.compile(r"[a-zA-Z0-9_.-]{1,128}")
MOUNT_TIMEOUT = 12.0
UNMOUNT_TIMEOUT = 6.0
PROCESS_TIMEOUT = 3.0
SOCKET = Path("/run/docker/plugins/workspace.sock")


class DriverError(RuntimeError):
    """Код ошибки, безопасный для ответа Docker и журнала."""


@dataclass(frozen=True)
class Volume:
    name: str
    workspace_ref: str
    mode: str
    caller: str | None


@dataclass
class MountProcesses:
    sshfs: subprocess.Popen[bytes]
    probe: subprocess.Popen[bytes] | None = None
    ready: bool = False

    def stop(self) -> None:
        _stop(self.sshfs)
        if self.probe is not None:
            _stop(self.probe)

    def live(self) -> bool:
        return self.sshfs.poll() is None or (
            self.probe is not None and self.probe.poll() is None
        )


def mounted(path: Path) -> bool:
    """Читает таблицу mount без файлового запроса в потенциально зависший FUSE."""
    filesystems = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        location, filesystem = line.split(" - ", 1)
        if location.split()[4] == str(path):
            filesystems.append(filesystem.split()[0])
    if filesystems and filesystems != ["fuse.sshfs"]:
        raise DriverError("workspace_mount_conflict")
    return bool(filesystems)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=PROCESS_TIMEOUT)


def _spawn(command: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _slot_ready(slot: Path) -> bool:
    try:
        if not stat.S_ISDIR(slot.lstat().st_mode):
            return False
        key = (slot / "export_client_key").lstat()
        return (
            stat.S_ISSOCK((slot / "reverse.sock").lstat().st_mode)
            and stat.S_ISREG((slot / "export_known_hosts").lstat().st_mode)
            and stat.S_ISREG(key.st_mode)
            and key.st_mode & 0o077 == 0
        )
    except FileNotFoundError:
        return False


def _ssh_config(slot: Path) -> str:
    return f"""\
Host export.test
    HostKeyAlias export.test
    StrictHostKeyChecking yes
    UserKnownHostsFile {slot}/export_known_hosts
    GlobalKnownHostsFile /dev/null
    IdentityFile {slot}/export_client_key
    IdentityAgent none
    IdentitiesOnly yes
    BatchMode yes
    ConnectTimeout 5
    ConnectionAttempts 1
    ServerAliveInterval 3
    ServerAliveCountMax 2
    UpdateHostKeys no
    ProxyCommand /usr/bin/socat STDIO UNIX-CONNECT:{slot}/reverse.sock
"""


class WorkspaceDriver:
    def __init__(
        self, database: sqlite3.Connection, mounts: Path, ipc: Path, runtime: Path
    ) -> None:
        self._database = database
        self._mounts = mounts
        self._ipc = ipc
        self._runtime = runtime
        self._processes: dict[str, MountProcesses] = {}
        self._detaches: dict[str, subprocess.Popen[bytes]] = {}
        for directory in (mounts, runtime):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with database:
            database.execute(
                """CREATE TABLE IF NOT EXISTS volumes (
                    name TEXT PRIMARY KEY,
                    workspace_ref TEXT NOT NULL UNIQUE,
                    mode TEXT NOT NULL CHECK(mode IN ('ro', 'rw')),
                    caller TEXT
                )"""
            )

    def _find(self, name: str) -> Volume | None:
        row = self._database.execute(
            "SELECT name, workspace_ref, mode, caller FROM volumes WHERE name = ?",
            (name,),
        ).fetchone()
        return Volume(*row) if row is not None else None

    def _claim(self, volume: Volume, caller: str | None) -> None:
        with self._database:
            self._database.execute(
                "UPDATE volumes SET caller = ? WHERE name = ?", (caller, volume.name)
            )

    def _create(self, name: str, options: object) -> None:
        if not isinstance(options, dict) or options.keys() != {"workspace_ref", "mode"}:
            raise DriverError("invalid_workspace_options")
        workspace_ref, mode = options["workspace_ref"], options["mode"]
        if (
            not isinstance(workspace_ref, str)
            or not WORKSPACE_REF.fullmatch(workspace_ref)
            or mode not in ("ro", "rw")
        ):
            raise DriverError("invalid_workspace_options")
        existing = self._find(name)
        if existing is not None:
            if (existing.workspace_ref, existing.mode) != (workspace_ref, mode):
                raise DriverError("workspace_volume_conflict")
            return
        if self._database.execute(
            "SELECT 1 FROM volumes WHERE workspace_ref = ?", (workspace_ref,)
        ).fetchone():
            raise DriverError("workspace_volume_conflict")
        target = self._mounts / name
        if mounted(target) or target.is_symlink():
            raise DriverError("workspace_mount_conflict")
        target.mkdir(mode=0o700, exist_ok=True)
        with self._database:
            self._database.execute(
                "INSERT INTO volumes(name, workspace_ref, mode) VALUES (?, ?, ?)",
                (name, workspace_ref, mode),
            )

    def _ready(self, volume: Volume) -> bool:
        processes = self._processes.get(volume.name)
        return (
            volume.caller is not None
            and processes is not None
            and processes.ready
            and processes.sshfs.poll() is None
        )

    def _describe(self, volume: Volume) -> dict[str, Any]:
        target = self._mounts / volume.name
        active = mounted(target)
        return {
            "Name": volume.name,
            "Mountpoint": str(target),
            "Status": {
                "Mounted": active,
                "Ready": active and self._ready(volume),
                "Mode": volume.mode,
            },
        }

    def _mount(self, volume: Volume, caller: str) -> None:
        target = self._mounts / volume.name
        if volume.caller is not None:
            if volume.caller != caller:
                raise DriverError("workspace_already_claimed")
            if not mounted(target) or not self._ready(volume):
                raise DriverError("workspace_mount_lost")
            return
        if mounted(target):
            raise DriverError("workspace_mount_conflict")
        slot = self._ipc / volume.workspace_ref
        if not _slot_ready(slot):
            raise DriverError("workspace_tunnel_unavailable")
        config = self._runtime / f"{volume.name}.ssh"
        config.write_text(_ssh_config(slot))
        self._claim(volume, caller)
        options = (
            f"{volume.mode},allow_other,default_permissions,uid=1000,gid=1000,"
            "dir_cache=no,attr_timeout=0,entry_timeout=0,direct_io,sshfs_sync,"
            "nodev,nosuid"
        )
        try:
            sshfs = _spawn(
                [
                    "sshfs",
                    "-f",
                    "-F",
                    str(config),
                    "export@export.test:/",
                    str(target),
                    "-o",
                    options,
                ]
            )
        except OSError as error:
            raise DriverError("workspace_sshfs_start_failed") from error
        processes = MountProcesses(sshfs)
        self._processes[volume.name] = processes
        try:
            deadline = time.monotonic() + MOUNT_TIMEOUT
            while time.monotonic() < deadline:
                if sshfs.poll() is not None:
                    raise DriverError("workspace_sshfs_connect_failed")
                if mounted(target):
                    self._probe(target, processes, deadline - time.monotonic())
                    if sshfs.poll() is not None or not mounted(target):
                        raise DriverError("workspace_mount_lost")
                    processes.ready = True
                    return
                time.sleep(0.05)
            raise DriverError("workspace_mount_timeout")
        except (DriverError, OSError):
            # Сохраняем durable-claim до отдельного Unmount/Remove.
            processes.stop()
            self._detach(volume.name)
            raise

    @staticmethod
    def _probe(target: Path, processes: MountProcesses, remaining: float) -> None:
        # Запись mountinfo появляется до SSH. OPENDIR проверяет пустой экспорт
        # без чтения его содержимого.
        probe = _spawn(
            [
                sys.executable,
                "-c",
                "import os, sys; os.close(os.open(sys.argv[1], os.O_RDONLY|os.O_DIRECTORY))",
                str(target),
            ]
        )
        processes.probe = probe
        try:
            exit_code = probe.wait(timeout=max(0, remaining))
        except subprocess.TimeoutExpired as error:
            raise DriverError("workspace_mount_timeout") from error
        if exit_code:
            raise DriverError("workspace_sshfs_connect_failed")
        processes.probe = None

    def _detach_finished(self, name: str) -> bool:
        process = self._detaches.get(name)
        if process is None:
            return True
        if process.poll() is None:
            return False
        del self._detaches[name]
        return True

    def _detach(self, name: str) -> None:
        if not self._detach_finished(name):
            raise DriverError("workspace_unmount_failed")
        target = self._mounts / name
        if not mounted(target):
            return
        try:
            process = _spawn(["fusermount3", "-u", str(target)])
        except OSError as error:
            raise DriverError("workspace_unmount_failed") from error
        self._detaches[name] = process
        try:
            exit_code = process.wait(timeout=UNMOUNT_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            process.kill()
            # Запрос в FUSE может ждать ответа и после SIGKILL хелпера.
            raise DriverError("workspace_unmount_failed") from error
        del self._detaches[name]
        if exit_code or mounted(target):
            raise DriverError("workspace_unmount_failed")

    def _unmount(self, volume: Volume, caller: str) -> None:
        if volume.caller is None:
            if mounted(self._mounts / volume.name):
                raise DriverError("workspace_mount_conflict")
            return
        if volume.caller != caller:
            raise DriverError("workspace_already_claimed")
        processes = self._processes.get(volume.name)
        if processes is not None and not processes.ready:
            processes.stop()
        if processes is not None:
            processes.ready = False
        self._detach(volume.name)
        if processes is not None:
            try:
                processes.sshfs.wait(timeout=PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                processes.stop()
            del self._processes[volume.name]
        (self._runtime / f"{volume.name}.ssh").unlink(missing_ok=True)
        self._claim(volume, None)

    def _remove(self, volume: Volume) -> None:
        target = self._mounts / volume.name
        processes = self._processes.get(volume.name)
        if (
            not self._detach_finished(volume.name)
            or mounted(target)
            or (processes is not None and processes.live())
        ):
            raise DriverError("workspace_volume_in_use")
        try:
            target.rmdir()
        except FileNotFoundError:
            pass
        (self._runtime / f"{volume.name}.ssh").unlink(missing_ok=True)
        with self._database:
            self._database.execute("DELETE FROM volumes WHERE name = ?", (volume.name,))
        self._processes.pop(volume.name, None)

    def request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/Plugin.Activate":
            return {"Implements": ["VolumeDriver"]}
        if path == "/VolumeDriver.Capabilities":
            return {"Capabilities": {"Scope": "local"}}
        if path == "/VolumeDriver.List":
            volumes = self._database.execute(
                "SELECT name, workspace_ref, mode, caller FROM volumes ORDER BY name"
            )
            return {"Volumes": [self._describe(Volume(*row)) for row in volumes]}
        operation = path.removeprefix("/VolumeDriver.")
        if path != f"/VolumeDriver.{operation}" or operation not in {
            "Create",
            "Remove",
            "Get",
            "Path",
            "Mount",
            "Unmount",
        }:
            raise DriverError("unknown_volume_operation")
        name = payload.get("Name")
        if not isinstance(name, str) or not VOLUME_NAME.fullmatch(name):
            raise DriverError("invalid_volume_name")
        if operation == "Create":
            self._create(name, payload.get("Opts"))
            return {}
        volume = self._find(name)
        if volume is None:
            if operation in {"Remove", "Unmount"}:
                if mounted(self._mounts / name):
                    raise DriverError("workspace_mount_conflict")
                return {}
            raise DriverError("unknown_workspace_volume")
        if operation == "Remove":
            self._remove(volume)
            return {}
        if operation == "Get":
            return {"Volume": self._describe(volume)}
        target = {"Mountpoint": str(self._mounts / name)}
        if operation == "Path":
            return target
        caller = payload.get("ID")
        if not isinstance(caller, str) or not MOUNT_CALLER.fullmatch(caller):
            raise DriverError("invalid_mount_caller")
        if operation == "Mount":
            self._mount(volume, caller)
            return target
        self._unmount(volume, caller)
        return {}


class PluginServer(socketserver.UnixStreamServer):
    def __init__(self, socket: Path, driver: WorkspaceDriver) -> None:
        self.driver = driver
        super().__init__(str(socket), PluginHandler)


class PluginHandler(BaseHTTPRequestHandler):
    server: PluginServer
    timeout = 5

    def log_message(self, format: str, *args: Any) -> None:
        # BaseHTTPRequestHandler ожидает TCP-адрес клиента.
        pass

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                raise DriverError("invalid_volume_request")
            body = self.rfile.read(length)
            if len(body) != length:
                raise DriverError("invalid_volume_request")
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                raise DriverError("invalid_volume_request")
            response = {"Err": "", **self.server.driver.request(self.path, payload)}
        except DriverError as error:
            LOG.warning("Запрос к VolumeDriver отклонён (%s)", error)
            response = {"Err": str(error)}
        except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
            LOG.warning("Запрос к VolumeDriver завершился технической ошибкой")
            response = {"Err": "workspace_driver_error"}
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.docker.plugins.v1.1+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)
    state = Path("/var/lib/eval-stand-workspace")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    stopping = False

    def stop(_signal: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    with closing(sqlite3.connect(state / "volumes.sqlite3")) as database:
        driver = WorkspaceDriver(
            database,
            Path("/mnt/volumes"),
            Path("/ipc"),
            Path("/run/eval-stand-workspace"),
        )
        with PluginServer(SOCKET, driver) as server:
            server.timeout = 0.5
            while not stopping:
                server.handle_request()


if __name__ == "__main__":
    main()
