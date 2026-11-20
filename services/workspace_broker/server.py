"""Привязка обратного SSH-туннеля к зарегистрированной рабочей области."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import asyncssh
from aiohttp import web

from services.common.http import authenticated
from services.common.storage import atomic_json

WORKSPACE_REF = re.compile(r"[0-9a-f]{32}")
TRANSIENT_DIR = re.compile(r"\.[0-9a-f]{32}\.(?:pending|revoked)")
REVOKE_TIMEOUT = 5


def public_key(value: object) -> asyncssh.SSHKey:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        raise ValueError("Некорректный ключ Ed25519")
    fields = value.split(maxsplit=2)
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise ValueError("Некорректный ключ Ed25519")
    try:
        key = asyncssh.import_public_key(" ".join(fields[:2]))
    except asyncssh.KeyImportError as error:
        raise ValueError("Некорректный ключ Ed25519") from error
    if key.get_algorithm() != "ssh-ed25519":
        raise ValueError("Некорректный ключ Ed25519")
    return key


def openssh(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_key(path: Path) -> asyncssh.SSHKey:
    if not path.exists():
        key = asyncssh.generate_private_key("ssh-ed25519")
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key.export_private_key("openssh"))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Некорректные права на секретный SSH-ключ")
    return asyncssh.read_private_key(path)


@dataclass
class Slot:
    path: Path
    tunnel_key: asyncssh.SSHKey
    export_host_key: str
    export_client_key: str
    owner: asyncssh.SSHServerConnection | None = None
    listener: ReverseListener | None = None
    forwarding: bool = False
    revoked: bool = False

    @property
    def socket(self) -> Path:
        return self.path / "reverse.sock"


class ReverseListener(asyncssh.SSHListener):
    """Освобождает только inode собственного сокета после остановки listener."""

    def __init__(self, slot: Slot, listener: asyncssh.SSHListener) -> None:
        super().__init__()
        self.slot = slot
        self.listener = listener
        self.identity = self.slot.socket.lstat()

    def close(self) -> None:
        self.listener.close()
        if self.slot.listener is self:
            self.slot.listener = None
        try:
            current = self.slot.socket.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(current.st_mode) and os.path.samestat(self.identity, current):
            self.slot.socket.unlink(missing_ok=True)

    async def wait_closed(self) -> None:
        await self.listener.wait_closed()


class Broker:
    def __init__(self, ipc: Path, state: Path) -> None:
        self.ipc = ipc
        self.ipc.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.host_key = private_key(state / "ssh_host_ed25519_key")
        self.closed_refs = state / "closed_refs.sqlite3"
        with closing(sqlite3.connect(self.closed_refs)) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS closed_refs (ref TEXT PRIMARY KEY)"
            )
        self.connections: set[asyncssh.SSHServerConnection] = set()
        self.closing = False
        self.slots: dict[str, Slot] = {}
        self.lock = asyncio.Lock()
        for path in self.ipc.iterdir():
            if TRANSIENT_DIR.fullmatch(path.name):
                shutil.rmtree(path)
            elif WORKSPACE_REF.fullmatch(path.name):
                if self.is_closed(path.name):
                    shutil.rmtree(path)
                else:
                    self.slots[path.name] = self.load_slot(path)

    def is_closed(self, ref: str) -> bool:
        with closing(sqlite3.connect(self.closed_refs)) as database:
            return (
                database.execute(
                    "SELECT 1 FROM closed_refs WHERE ref = ?", (ref,)
                ).fetchone()
                is not None
            )

    def load_slot(self, path: Path) -> Slot:
        payload = json.loads((path / "binding.json").read_bytes())
        tunnel_key = public_key(payload["tunnel_public_key"])
        host_key = openssh(public_key(payload["export_host_public_key"]))
        if (path / "export_known_hosts").read_text() != f"export.test {host_key}\n":
            raise ValueError("Нарушена привязка SSH-ключа экспорта")
        key_path = path / "export_client_key"
        if not key_path.exists():
            raise ValueError("Отсутствует SSH-ключ для подключения к экспорту")
        socket_path = path / "reverse.sock"
        if socket_path.exists():
            if not stat.S_ISSOCK(socket_path.lstat().st_mode):
                raise ValueError("Некорректный адрес обратного SSH-туннеля")
            socket_path.unlink()
        return Slot(path, tunnel_key, host_key, openssh(private_key(key_path)))

    async def put(self, ref: str, payload: object) -> dict[str, str]:
        if not isinstance(payload, dict) or payload.keys() != {
            "tunnel_public_key",
            "export_host_public_key",
        }:
            raise ValueError("Некорректная привязка рабочей области")
        tunnel_key = public_key(payload["tunnel_public_key"])
        export_host_key = openssh(public_key(payload["export_host_public_key"]))
        async with self.lock:
            if self.is_closed(ref):
                raise web.HTTPConflict(text="Рабочая область закрыта")
            slot = self.slots.get(ref)
            if slot is None:
                path = self.ipc / ref
                staging = self.ipc / f".{ref}.pending"
                staging.mkdir(mode=0o700)
                try:
                    key = private_key(staging / "export_client_key")
                    with (staging / "export_known_hosts").open("x") as known_hosts:
                        known_hosts.write(f"export.test {export_host_key}\n")
                        known_hosts.flush()
                        os.fsync(known_hosts.fileno())
                    atomic_json(
                        staging / "binding.json",
                        {
                            "tunnel_public_key": openssh(tunnel_key),
                            "export_host_public_key": export_host_key,
                        },
                    )
                    staging.rename(path)
                    self.slots[ref] = slot = Slot(
                        path, tunnel_key, export_host_key, openssh(key)
                    )
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            elif (
                slot.revoked
                or slot.tunnel_key.public_data != tunnel_key.public_data
                or slot.export_host_key != export_host_key
            ):
                raise web.HTTPConflict(text="Конфликт привязки SSH-туннеля")
            sync_directory(self.ipc)
        return {
            "ssh_host_key": openssh(self.host_key),
            "export_client_key": slot.export_client_key,
        }

    def ready(self, ref: str) -> bool:
        slot = self.slots.get(ref)
        return bool(
            slot and not slot.revoked and slot.listener and slot.socket.is_socket()
        )

    async def revoke(self, ref: str) -> None:
        async with self.lock:
            slot = self.slots.get(ref)
            owner = slot.owner if slot else None
            if slot:
                slot.revoked = True
            if owner:
                owner.abort()
            try:
                with closing(sqlite3.connect(self.closed_refs)) as database, database:
                    database.execute(
                        "INSERT OR IGNORE INTO closed_refs VALUES (?)", (ref,)
                    )
            finally:
                if owner:
                    async with asyncio.timeout(REVOKE_TIMEOUT):
                        await owner.wait_closed()
            if not slot:
                return
            revoked = self.ipc / f".{ref}.revoked"
            if slot.path.exists():
                slot.path.rename(revoked)
            if revoked.exists():
                shutil.rmtree(revoked)
            sync_directory(self.ipc)
            del self.slots[ref]

    async def shutdown(self) -> None:
        self.closing = True
        connections = tuple(self.connections)
        for connection in connections:
            connection.abort()
        async with asyncio.timeout(REVOKE_TIMEOUT):
            await asyncio.gather(
                *(connection.wait_closed() for connection in connections)
            )


class TunnelServer(asyncssh.SSHServer):
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.connection: asyncssh.SSHServerConnection
        self.slot: Slot | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self.connection = conn
        if self.broker.closing:
            conn.abort()
        else:
            self.broker.connections.add(conn)

    def connection_lost(self, exc: Exception | None) -> None:
        self.broker.connections.discard(self.connection)
        if self.slot and self.slot.owner is self.connection:
            self.slot.owner = None

    def begin_auth(self, username: str) -> bool:
        self.slot = self.broker.slots.get(username)
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        slot = self.slot
        return bool(
            slot
            and not slot.revoked
            and username == slot.path.name
            and slot.owner is None
            and secrets.compare_digest(slot.tunnel_key.public_data, key.public_data)
        )

    def auth_completed(self) -> None:
        if not self.slot or self.slot.revoked or self.slot.owner is not None:
            self.connection.abort()
        else:
            self.slot.owner = self.connection

    async def unix_server_requested(
        self, listen_path: str
    ) -> ReverseListener | Literal[False]:
        slot = self.slot
        if (
            not slot
            or slot.revoked
            or slot.owner is not self.connection
            or slot.listener
            or slot.forwarding
            or listen_path != str(slot.socket)
        ):
            return False
        slot.forwarding = True
        try:
            reverse = await self.connection.forward_local_path(listen_path, listen_path)
            try:
                listener = ReverseListener(slot, reverse)
            except OSError:
                reverse.close()
                raise
            if slot.revoked or slot.owner is not self.connection:
                listener.close()
                return False
            slot.listener = listener
            return listener
        finally:
            slot.forwarding = False


def create_app(
    broker: Broker, token: str, *, ssh_host: str = "0.0.0.0", ssh_port: int = 2222
) -> web.Application:
    async def lifecycle(_app: web.Application) -> AsyncIterator[None]:
        listener = await asyncssh.create_server(
            lambda: TunnelServer(broker),
            ssh_host,
            ssh_port,
            config=None,
            server_host_keys=[broker.host_key],
            login_timeout=8,
            keepalive_interval=3,
            keepalive_count_max=2,
        )
        try:
            yield
        finally:
            listener.close()
            await broker.shutdown()
            async with asyncio.timeout(REVOKE_TIMEOUT):
                await listener.wait_closed()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ready"})

    async def slot(request: web.Request) -> web.Response:
        ref = request.match_info["workspace_ref"]
        if not WORKSPACE_REF.fullmatch(ref):
            raise web.HTTPBadRequest(text="Некорректная ссылка рабочей области")
        try:
            if request.method == "PUT":
                return web.json_response(await broker.put(ref, await request.json()))
            if request.method == "GET":
                return web.json_response({"ready": broker.ready(ref)})
            await broker.revoke(ref)
            return web.Response(status=204)
        except (OSError, TimeoutError, sqlite3.Error) as error:
            raise web.HTTPServiceUnavailable(
                text="Операция с SSH-туннелем не завершена"
            ) from error

    app = web.Application(middlewares=[authenticated(token)], client_max_size=4096)
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/health", health)
    for method in ("PUT", "GET", "DELETE"):
        app.router.add_route(method, "/v1/slots/{workspace_ref}", slot)
    return app


def main() -> None:
    broker = Broker(
        Path(os.environ.get("BROKER_IPC", "/ipc")),
        Path(os.environ.get("BROKER_STATE", "/state/workspace-broker")),
    )
    web.run_app(
        create_app(broker, os.environ["BROKER_TOKEN"]),
        host="0.0.0.0",
        port=8084,
        access_log=None,
    )


if __name__ == "__main__":
    main()
