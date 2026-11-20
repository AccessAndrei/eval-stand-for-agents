"""Headless-клиент против mock Gateway, включая сохранение SSH-идентичности."""

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import asyncssh
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from client import session as mounted
from client.session import ClientError, MountedClient

TOKEN = "test-mount-token-that-is-long-enough"
SLOT = "a" * 32
RUN = "b" * 32


class Gateway:
    def __init__(self, calls: list[str]) -> None:
        self.app = web.Application()
        self.app.router.add_route("*", "/{path:.*}", self.handle)
        self.calls = calls
        self.ssh_port = 2222
        self.ssh_host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.export_client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.opens: list[tuple[str, dict[str, Any]]] = []
        self.turns: list[dict[str, Any]] = []
        self.registrations: list[dict[str, Any]] = []
        self.cursors: list[int] = []
        self.cancellations: list[str] = []
        self.drop_next_open = False
        self.drop_stream = False
        self.fail_cleanup = False
        self.history_unavailable = False
        self.ready = False
        self.export_key = public_key(self.export_client_key)

    async def handle(self, request: web.Request) -> web.Response:
        self.calls.append(f"{request.method} {request.path}")
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "should-never-be-echoed"}, status=401)
        proof = request.headers["X-Session-Proof"]
        assert len(proof) == 64 and int(proof, 16)
        if request.path == "/v1/sessions":
            body = await request.json()
            self.opens.append((proof, body))
            if self.drop_next_open:
                self.drop_next_open = False
                assert request.transport is not None
                request.transport.close()
            return web.json_response(
                {
                    "version": "v1",
                    "session_id": body["session_id"],
                    "state": "pending_mount",
                }
            )
        path = request.path.split("/")
        session_id = path[3]
        assert all(
            proof == owner
            for owner, body in self.opens
            if session_id == body["session_id"]
        )
        session = {"version": "v1", "session_id": session_id, "state": "ready"}
        run = {"version": "v1", "session_id": session_id, "run_id": RUN}
        if request.method == "DELETE":
            if self.fail_cleanup:
                return web.json_response(
                    {
                        **session,
                        "state": "failed",
                        "cleanup_status": "failed",
                        "errors": [{"code": "cleanup_unconfirmed"}],
                    },
                    status=502,
                )
            return web.json_response(
                {**session, "state": "closed", "cleanup_status": "success"}
            )
        if path[-1] == "tunnel":
            self.registrations.append(await request.json())
            return web.json_response(
                {
                    "ssh_username": SLOT,
                    "ssh_port": self.ssh_port,
                    "ssh_host_key": public_key(self.ssh_host_key),
                    "export_client_key": self.export_key,
                }
            )
        if path[-1] == "mount":
            self.ready = True
            assert await request.json() == {}
            return web.json_response({**session, "state": "mounting"})
        if len(path) == 4:
            assert self.ready
            return web.json_response(session)
        if path[-1] == "runs":
            body = await request.json()
            self.turns.append(body)
            return web.json_response(
                {**run, "run_id": body["run_id"], "state": "running"}
            )
        if path[-1] == "cancel":
            assert await request.json() == {}
            self.cancellations.append(path[-2])
            return web.json_response({**run, "run_id": path[-2], "state": "running"})
        if path[-1] == "events":
            cursor = int(request.query["after"])
            self.cursors.append(cursor)
            if self.history_unavailable:
                return web.json_response(
                    {"error": "history_unavailable", "earliest_seq": 2}, status=409
                )
            replay = [
                {"seq": 1, "type": "message", "text": "Файл обработан"},
                {
                    "seq": 2,
                    "type": "tool",
                    "tool": {
                        "type": "commandExecution",
                        "phase": "completed",
                        "exit_code": 0,
                    },
                },
                {
                    "seq": 3,
                    "type": "result",
                    "result": {
                        **run,
                        "execution_status": "cancelled"
                        if path[-2] in self.cancellations
                        else "success",
                        "collection_status": "complete",
                        "writers_stopped": True,
                        "evidence_status": "not_required",
                        "errors": [],
                        "tokens": {},
                        "provenance": {},
                    },
                },
            ]
            if self.drop_stream:
                replay = replay[:1]
                self.drop_stream = False
            return web.Response(
                text="".join(
                    json.dumps(event) + "\n"
                    for event in replay
                    if event["seq"] > cursor
                ),
                content_type="application/x-ndjson",
            )
        return web.json_response({**run, "run_id": path[-1], "state": "running"})


def public_key(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode("ascii").strip()


def patch_export(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    expected_key: str,
    *,
    folder: Path,
    mode: str = "rw",
) -> None:
    class Export:
        host_public_key = public_key(asyncssh.generate_private_key("ssh-ed25519"))

        def __init__(self, root: Path, **options: Any) -> None:
            assert root == folder and root.is_dir()
            assert options["mode"] == mode

        async def start(self, export_client_key: str) -> int:
            assert export_client_key == expected_key
            calls.append("export_start")
            return 12_345

        async def close(self, timeout: float = 5.0) -> bool:
            assert timeout > 0
            calls.append("export_drain")
            return True

    monkeypatch.setattr(mounted, "WorkspaceExport", Export)


def patch_tunnel(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], gateway: Gateway
) -> None:
    class Forward:
        def close(self) -> None:
            calls.append("forward_close")

        async def wait_closed(self) -> None:
            calls.append("forward_closed")

    class Connection:
        async def forward_remote_path_to_port(
            self, path: str, local_host: str, local_port: int
        ) -> Forward:
            assert (path, local_host, local_port) == (
                f"/ipc/{SLOT}/reverse.sock",
                "127.0.0.1",
                12_345,
            )
            calls.append("forward_ready")
            return Forward()

        def close(self) -> None:
            calls.append("ssh_close")

        async def wait_closed(self) -> None:
            calls.append("ssh_closed")

    async def connect(host: str, port: int, **options: Any) -> Connection:
        assert (host, port) == ("127.0.0.1", gateway.ssh_port)
        assert options["username"] == SLOT
        assert options["config"] is None and options["agent_path"] is None
        assert (
            public_key(options["client_keys"][0])
            == gateway.registrations[-1]["tunnel_public_key"]
        )
        assert [public_key(key) for key in options["known_hosts"][0]] == [
            public_key(gateway.ssh_host_key)
        ]
        calls.append("ssh_ready")
        return Connection()

    monkeypatch.setattr(mounted.asyncssh, "connect", connect)


def test_mount_run_reconnect_and_close_share_the_same_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    gateway = Gateway(calls)
    patch_export(monkeypatch, calls, gateway.export_key, folder=tmp_path)
    patch_tunnel(monkeypatch, calls, gateway)

    async def scenario() -> None:
        async with (
            TestServer(gateway.app) as server,
            MountedClient(str(server.make_url("/")), TOKEN) as client,
        ):
            # Идентификаторы сохраняются при потерянном ответе на POST.
            session = client.session(tmp_path, profile="mock", mode="rw")
            gateway.drop_next_open = True
            with pytest.raises(ClientError, match="gateway_transport_error"):
                await session.mount()
            assert len(gateway.opens) == 1
            assert (await session.mount())["state"] == "ready"
            assert gateway.opens[0] == gateway.opens[1]
            assert gateway.opens[0][1] == {
                "session_id": session.session_id,
                "profile": "mock",
                "mode": "rw",
            }
            path = f"/v1/sessions/{session.session_id}"
            assert calls.index("forward_ready") < calls.index(f"POST {path}/mount")

            run = await session.start_run(
                "обработай файл", timeout_s=5, task_id="B5.1", run_id=RUN
            )
            assert run["run_id"] == RUN
            assert (await session.run_status(RUN))["state"] == "running"
            gateway.drop_stream = True
            received = []
            with pytest.raises(ClientError, match="stream_incomplete"):
                async for event in session.events(RUN):
                    received.append(event)
            assert (await session.cancel_run(RUN))["run_id"] == RUN
            assert calls[-1] == f"POST {path}/runs/{RUN}/cancel"
            async for event in session.events(RUN, after=received[-1]["seq"]):
                received.append(event)
            assert [event["type"] for event in received] == [
                "message",
                "tool",
                "result",
            ]
            assert received[0]["text"] == "Файл обработан"
            assert received[1]["tool"]["exit_code"] == 0
            assert received[2]["result"]["execution_status"] == "cancelled"
            assert gateway.cancellations == [RUN]
            assert gateway.cursors == [0, 1]
            assert gateway.turns == [
                {
                    "run_id": RUN,
                    "prompt": "обработай файл",
                    "timeout_s": 5,
                    "task_id": "B5.1",
                }
            ]

            gateway.history_unavailable = True
            with pytest.raises(ClientError, match="history_unavailable") as history:
                async for _ in session.events(RUN):
                    pytest.fail("История недоступна")
            assert history.value.earliest_seq == 2

            report = await session.close()
            assert report.ok and report.export_drained and report.tunnel_closed
            assert await session.close() == report
            revoked = calls.index(f"DELETE {path}")
            assert revoked < calls.index("forward_close") < calls.index("export_drain")
            assert calls.count(f"DELETE {path}") == 1

    asyncio.run(scenario())


def test_wrong_token_fails_before_tunnel_registration(tmp_path: Path) -> None:
    calls: list[str] = []
    gateway = Gateway(calls)

    async def scenario() -> None:
        async with (
            TestServer(gateway.app) as server,
            MountedClient(
                str(server.make_url("/")), "wrong-mount-token-that-is-long-enough"
            ) as client,
        ):
            with pytest.raises(
                ClientError, match="authorization: unauthorized"
            ) as error:
                await client.open_session(tmp_path, profile="mock")
            assert error.value.http_status == 401
            assert "should-never-be-echoed" not in str(error.value)

    asyncio.run(scenario())
    assert calls == ["POST /v1/sessions"]
    assert gateway.registrations == []


def test_ssh_broker_with_a_different_host_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    gateway = Gateway(calls)
    patch_export(monkeypatch, calls, gateway.export_key, folder=tmp_path)

    async def scenario() -> None:
        ssh_server = await asyncssh.listen(
            "127.0.0.1",
            0,
            config=None,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        )
        try:
            gateway.ssh_port = ssh_server.get_port()
            async with (
                TestServer(gateway.app) as server,
                MountedClient(str(server.make_url("/")), TOKEN, timeout=5) as client,
            ):
                with pytest.raises(ClientError, match="ssh_host_key_mismatch"):
                    await client.open_session(tmp_path, profile="mock")
        finally:
            ssh_server.close()
            await ssh_server.wait_closed()

    asyncio.run(scenario())
    assert not any(call.endswith("/mount") for call in calls)
    assert len([call for call in calls if call.startswith("DELETE")]) == 1
    assert calls[-1] == "export_drain"


def test_read_and_write_through_the_real_ssh_reverse_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gateway = Gateway([])
    monkeypatch.chdir(tmp_path)
    work = tmp_path / "files"
    work.mkdir()
    fixture = work / "bytes.bin"
    fixture.write_bytes(b"\xff\x00")

    class Broker(asyncssh.SSHServer):
        def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
            self.connection = conn

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            return (
                username == SLOT
                and public_key(key) == gateway.registrations[-1]["tunnel_public_key"]
            )

        async def unix_server_requested(self, listen_path: str) -> asyncssh.SSHListener:
            assert listen_path == f"/ipc/{SLOT}/reverse.sock"
            # Отображаем серверный /ipc в изолированный каталог теста.
            return await self.connection.forward_local_path("reverse.sock", listen_path)

    async def scenario() -> None:
        ssh_server = await asyncssh.listen(
            "127.0.0.1",
            0,
            config=None,
            server_factory=Broker,
            server_host_keys=[gateway.ssh_host_key],
        )
        try:
            gateway.ssh_port = ssh_server.get_port()
            async with (
                TestServer(gateway.app) as server,
                MountedClient(str(server.make_url("/")), TOKEN, timeout=5) as client,
            ):
                session = await client.open_session(work, profile="mock")
                export_key = asyncssh.import_public_key(
                    gateway.registrations[-1]["export_host_public_key"]
                )
                with socket.socket(socket.AF_UNIX) as unix:
                    unix.setblocking(False)
                    await asyncio.get_running_loop().sock_connect(unix, "reverse.sock")
                    async with (
                        asyncssh.connect(
                            "export.test",
                            sock=unix,
                            username="export",
                            config=None,
                            agent_path=None,
                            client_keys=[gateway.export_client_key],
                            known_hosts=([export_key], [], [], [], [], [], []),
                            login_timeout=5,
                        ) as connection,
                        connection.start_sftp_client() as sftp,
                    ):
                        async with sftp.open("bytes.bin", "rb") as handle:
                            assert await handle.read() == b"\xff\x00"
                        async with sftp.open("bytes.bin", "wb") as handle:
                            await handle.write(b"\xfe\x00")
                assert fixture.read_bytes() == b"\xfe\x00"
                assert (await session.close()).ok
        finally:
            ssh_server.close()
            await ssh_server.wait_closed()

    asyncio.run(scenario())


def test_failed_server_cleanup_does_not_hide_local_export_revocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    gateway = Gateway(calls)
    patch_export(monkeypatch, calls, gateway.export_key, folder=tmp_path, mode="ro")
    patch_tunnel(monkeypatch, calls, gateway)

    async def scenario() -> None:
        async with (
            TestServer(gateway.app) as server,
            MountedClient(str(server.make_url("/")), TOKEN) as client,
        ):
            session = await client.open_session(tmp_path, profile="mock", mode="ro")
            assert gateway.opens[0][1]["mode"] == "ro"
            gateway.fail_cleanup = True
            report = await session.close()
            assert report.export_drained and report.tunnel_closed
            assert report.server_error is not None
            assert report.server_error.code == "cleanup_unconfirmed"
            assert report.server and report.server["cleanup_status"] == "failed"
            assert not report.ok
            gateway.fail_cleanup = False
            assert (await session.close()).ok

    asyncio.run(scenario())
