"""Проверки привязки, обратного потока и завершения SSH-туннеля."""

import asyncio
import stat
import tempfile
from pathlib import Path
from typing import Any

import asyncssh
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import HTTPConflict

from services.workspace_broker.server import Broker, create_app, openssh

TOKEN = "workspace-broker-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
REF = "a" * 32
OTHER_REF = "b" * 32


def registration(tunnel: asyncssh.SSHKey, host: asyncssh.SSHKey) -> dict[str, str]:
    return {
        "tunnel_public_key": openssh(tunnel),
        "export_host_public_key": openssh(host),
    }


def test_slot_keeps_keys_across_restart_until_revoked(tmp_path: Path) -> None:
    async def scenario() -> None:
        ipc, state = tmp_path / "ipc", tmp_path / "state"
        broker = Broker(ipc, state)
        tunnel, host = (asyncssh.generate_private_key("ssh-ed25519") for _ in range(2))
        payload = registration(tunnel, host)
        first = await broker.put(REF, payload)
        assert first == await broker.put(REF, payload)
        slot = ipc / REF
        assert (
            stat.S_IMODE((slot / "export_client_key").stat().st_mode)
            == stat.S_IMODE((state / "ssh_host_ed25519_key").stat().st_mode)
            == 0o600
        )
        assert (
            slot / "export_known_hosts"
        ).read_text() == f"export.test {openssh(host)}\n"

        restarted = Broker(ipc, state)
        assert first == await restarted.put(REF, payload)
        assert not restarted.ready(REF)
        await restarted.revoke(REF)
        await restarted.revoke(REF)
        await restarted.revoke(OTHER_REF)
        assert not list(ipc.iterdir())
        restored = Broker(ipc, state)
        for ref in (REF, OTHER_REF):
            assert ref not in restored.slots
            with pytest.raises(HTTPConflict):
                await restored.put(ref, payload)

    asyncio.run(scenario())


def test_only_slot_owner_can_forward_and_delete_closes_active_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(ipc: Path, state: Path) -> None:
        broker = Broker(ipc, state)
        tunnel, other, host = (
            asyncssh.generate_private_key("ssh-ed25519") for _ in range(3)
        )
        original_create_server = asyncssh.create_server
        ssh_port = 0

        async def capture_port(*args: Any, **kwargs: Any) -> asyncssh.SSHAcceptor:
            nonlocal ssh_port
            acceptor = await original_create_server(*args, **kwargs)
            ssh_port = acceptor.get_port()
            return acceptor

        monkeypatch.setattr(asyncssh, "create_server", capture_port)

        async def echo(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                while data := await reader.read(128):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        app = create_app(broker, TOKEN, ssh_host="127.0.0.1", ssh_port=0)
        async with target, TestClient(TestServer(app)) as api:
            uri = f"/v1/slots/{REF}"
            other_uri = f"/v1/slots/{OTHER_REF}"
            payload = registration(tunnel, host)
            assert (await api.put(uri, json=payload)).status == 401
            assert (await api.put(uri, headers=HEADERS, json=payload)).status == 200
            assert (
                await api.put(
                    other_uri, headers=HEADERS, json=registration(other, host)
                )
            ).status == 200
            assert (
                await api.put(uri, headers=HEADERS, json=registration(other, host))
            ).status == 409
            known_hosts = asyncssh.import_known_hosts(
                f"[127.0.0.1]:{ssh_port} {openssh(broker.host_key)}"
            )

            async def connect(
                key: asyncssh.SSHKey, ref: str = REF
            ) -> asyncssh.SSHClientConnection:
                return await asyncssh.connect(
                    "127.0.0.1",
                    ssh_port,
                    username=ref,
                    known_hosts=known_hosts,
                    client_keys=[key],
                    agent_path=None,
                    config=None,
                )

            with pytest.raises(asyncssh.PermissionDenied):
                await connect(other)

            reverse = ipc / REF / "reverse.sock"
            async with await connect(tunnel) as conn:
                with pytest.raises(asyncssh.ChannelListenError):
                    await conn.forward_remote_path_to_port(
                        str(ipc / OTHER_REF / "reverse.sock"), "127.0.0.1", target_port
                    )
                with pytest.raises(asyncssh.ChannelListenError):
                    await conn.forward_remote_port(
                        "127.0.0.1", 0, "127.0.0.1", target_port
                    )
                with pytest.raises(asyncssh.ChannelOpenError):
                    await conn.run("true")
                listener = await conn.forward_remote_path_to_port(
                    str(reverse), "127.0.0.1", target_port
                )
                assert await (await api.get(uri, headers=HEADERS)).json() == {
                    "ready": True
                }
                listener.close()
                await listener.wait_closed()
                assert await (await api.get(uri, headers=HEADERS)).json() == {
                    "ready": False
                }
                assert not reverse.exists()

                await conn.forward_remote_path_to_port(
                    str(reverse), "127.0.0.1", target_port
                )
                reader, writer = await asyncio.open_unix_connection(reverse)
                writer.write(b"live bytes")
                await writer.drain()
                assert (
                    await asyncio.wait_for(reader.readexactly(10), 5) == b"live bytes"
                )
                assert (await api.delete(uri, headers=HEADERS)).status == 204
                assert await asyncio.wait_for(reader.read(), 5) == b""
                writer.close()
                await writer.wait_closed()
                assert not reverse.parent.exists()
                assert await (await api.get(uri, headers=HEADERS)).json() == {
                    "ready": False
                }
                assert (await api.delete(uri, headers=HEADERS)).status == 204
                assert (ipc / OTHER_REF).is_dir()

            with pytest.raises(asyncssh.PermissionDenied):
                await connect(tunnel)

            async with await connect(other, OTHER_REF) as conn:
                other_socket = ipc / OTHER_REF / "reverse.sock"
                await conn.forward_remote_path_to_port(
                    str(other_socket), "127.0.0.1", target_port
                )
                response = await api.get(other_uri, headers=HEADERS)
                assert await response.json() == {"ready": True}
                await asyncio.wait_for(api.close(), 3)
                await asyncio.wait_for(conn.wait_closed(), 3)
                assert not other_socket.exists()

    # Unix domain socket endpoints have a short platform-dependent path limit.
    with tempfile.TemporaryDirectory(prefix="ws-broker-", dir="/tmp") as root:
        asyncio.run(scenario(Path(root) / "ipc", Path(root) / "state"))
