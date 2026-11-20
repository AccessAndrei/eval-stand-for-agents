"""Владелец восстанавливает сессию после потери ответа двумя уровнями API."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ServerDisconnectedError, web
from aiohttp.test_utils import TestClient, TestServer

from services.gateway.clients import RemoteError
from services.gateway.runtime import Runtime
from services.gateway.server import create_app
from services.gateway.sessions import MountedSessions
from services.gateway.store import RunStore

TOKEN = "gateway-session-api-test-token"
SESSION_A = "a" * 32
SESSION_B = "b" * 32
PROOF_A = "1" * 64
PROOF_B = "2" * 64
PROJECT = "test-project"


def auth(proof: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + TOKEN, "X-Session-Proof": proof}


def key(value: int) -> str:
    encoded = base64.b64encode(
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([value]) * 32
    )
    return "ssh-ed25519 " + encoded.decode()


OPEN_A = {"session_id": SESSION_A, "profile": "mock", "mode": "rw"}
OPEN_B = {"session_id": SESSION_B, "profile": "mock", "mode": "ro"}
SSH_KEYS = {"tunnel_public_key": key(1), "export_host_public_key": key(2)}
BROKER_KEYS = {"ssh_host_key": key(3), "export_client_key": key(4)}


class Dependencies:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str]] = []
        self.mounts: dict[str, dict[str, Any]] = {}
        self.slots: dict[str, dict[str, Any]] = {}
        self.lost_manager_response = False
        self.delayed_provision: dict[str, Any] | None = None
        self.delay_manager_admission = False
        self.block_release = False

    def saved(self, session_id: str) -> dict[str, Any]:
        return json.loads(
            (self.root / ".mounted-sessions" / f"{session_id}.json").read_text()
        )

    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 0,
    ) -> dict[str, Any]:
        self.calls.append((method, path))
        if path == "/v0/profiles/mock":
            return {
                "compatible": True,
                "provider_profile": "mock",
                "model": "codex-mock",
                "endpoint": "http://mock-provider:8080",
                "protocol": "responses",
                "auth": "none",
                "timeout_s": 30,
                "output_limit": 1000,
                "budget_mode": "strict",
                "budget_mechanism": "mock",
            }
        if path.startswith("/v1/slots/"):
            ref = path.rsplit("/", 1)[-1]
            if method == "PUT":
                records = [
                    json.loads(file.read_text())
                    for file in (self.root / ".mounted-sessions").glob("*.json")
                ]
                matching = [
                    record for record in records if record["workspace_ref"] == ref
                ]
                assert len(matching) == 1 and matching[0]["tunnel_fingerprint"]
                assert body is not None
                assert self.slots.setdefault(ref, body) == body
                return BROKER_KEYS
            if method == "GET":
                return {"ready": ref in self.slots}
            self.slots.pop(ref, None)
            return {}
        if path == "/v1/environments" and method == "POST":
            assert body is not None
            sid = body["session_id"]
            record = self.saved(sid)
            assert (
                record["mount_requested"]
                and record["workspace_ref"] == body["workspace_ref"]
            )
            if sid in self.mounts and self.mounts[sid]["state"] == "released":
                return self.mounts[sid]
            assert self.slots[record["workspace_ref"]]
            assert sid not in self.mounts
            if self.delay_manager_admission:
                self.delayed_provision = body
                raise TimeoutError()
            environment = {
                "version": "v1",
                "session_id": sid,
                "project_id": PROJECT,
                "state": "ready",
                "handle": sid[0] * 48,
                "cleanup_status": "not_started",
            }
            self.mounts[sid] = environment
            if self.lost_manager_response:
                self.lost_manager_response = False
                raise TimeoutError()
            return environment
        if path.startswith("/v1/environments/by-session/") and method == "GET":
            try:
                return self.mounts[path.rsplit("/", 1)[-1]]
            except KeyError as error:
                raise RemoteError(404, "unknown_workspace") from error
        if path.startswith("/v1/environments/by-session/") and method == "DELETE":
            assert body == self.delayed_provision
            sid = path.rsplit("/", 1)[-1]
            assert self.saved(sid)["state"] == "closing"
            assert body is not None and body["workspace_ref"] in self.slots
            self.mounts[sid] = {
                "version": "v1",
                "session_id": sid,
                "project_id": PROJECT,
                "state": "released",
                "handle": sid[0] * 48,
                "cleanup_status": "success",
            }
            return self.mounts[sid]
        if path.endswith("/release") and method == "POST":
            assert body is not None
            if self.block_release:
                raise TimeoutError()
            existing = self.mounts[body["session_id"]]
            assert body["handle"] == existing["handle"]
            saved = self.saved(body["session_id"])
            assert saved["state"] == "closing" or saved["cleanup_status"] == "failed"
            existing.update(state="released", cleanup_status="success")
            return existing
        raise AssertionError((method, path))


def app(dependencies: Dependencies, limit: int = 8_000_000) -> web.Application:
    store = RunStore(dependencies.root, limit)
    runtime = Runtime(store, dependencies, dependencies, dependencies, PROJECT, {})
    mounted = MountedSessions(
        store, dependencies, dependencies, dependencies, PROJECT, 2222
    )
    return create_app(runtime, TOKEN, mounted=mounted)


def test_ownership_and_same_mount_after_lost_open_and_manager_response(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        dependencies = Dependencies(tmp_path)
        first = app(dependencies)

        async def drop_first_response(
            request: web.Request, response: web.StreamResponse
        ) -> None:
            if (
                request.path == "/v1/sessions"
                and response.status == 201
                and request.headers["X-Session-Proof"] == PROOF_A
            ):
                assert request.transport is not None
                request.transport.close()

        first.on_response_prepare.append(drop_first_response)
        path_a, path_b = f"/v1/sessions/{SESSION_A}", f"/v1/sessions/{SESSION_B}"
        async with TestClient(TestServer(first)) as client:
            with pytest.raises(ServerDisconnectedError):
                await client.post("/v1/sessions", json=OPEN_A, headers=auth(PROOF_A))
            saved = dependencies.saved(SESSION_A)
            assert saved["owner_digest"] == hashlib.sha256(PROOF_A.encode()).hexdigest()
            assert saved["fingerprint"] and saved["state"] == "pending_mount"
            assert not dependencies.slots and not dependencies.mounts

            other = await client.post(
                "/v1/sessions", json=OPEN_B, headers=auth(PROOF_B)
            )
            assert other.status == 201
            assert (await other.json())["session_id"] == SESSION_B
            calls = list(dependencies.calls)
            for method, path, data in [
                ("POST", "/v1/sessions", {**OPEN_A, "mode": "ro"}),
                ("GET", path_a, None),
                ("POST", path_a + "/tunnel", SSH_KEYS),
                ("POST", path_a + "/mount", {}),
                ("DELETE", path_a, None),
            ]:
                response = await client.request(
                    method, path, json=data, headers=auth(PROOF_B)
                )
                assert response.status == 404
                assert await response.json() == {
                    "version": "v1",
                    "error": "session_not_found",
                }
            for sid in (SESSION_A, "c" * 32):
                response = await client.post(
                    "/v1/sessions",
                    json={**OPEN_A, "session_id": sid},
                    headers=auth("malformed"),
                )
                assert response.status == 404
                assert await response.json() == {
                    "version": "v1",
                    "error": "session_not_found",
                }
            assert dependencies.calls == calls
            conflict = await client.post(
                "/v1/sessions", json={**OPEN_A, "mode": "ro"}, headers=auth(PROOF_A)
            )
            assert conflict.status == 409
            replay = await client.post(
                "/v1/sessions", json=OPEN_A, headers=auth(PROOF_A)
            )
            assert (
                replay.status == 200
                and (await replay.json())["state"] == "pending_mount"
            )
            assert dependencies.calls == calls

            ready_b = await client.post(
                path_b + "/tunnel",
                json={**SSH_KEYS, "tunnel_public_key": key(5)},
                headers=auth(PROOF_B),
            )
            assert ready_b.status == 200
            assert (await ready_b.json())["ssh_username"] != saved["workspace_ref"]
            opened_a = await client.post(
                path_a + "/tunnel", json=SSH_KEYS, headers=auth(PROOF_A)
            )
            assert opened_a.status == 200
            assert await opened_a.json() == {
                **BROKER_KEYS,
                "ssh_username": saved["workspace_ref"],
                "ssh_port": 2222,
            }
            dependencies.lost_manager_response = True
            lost = await client.post(path_a + "/mount", json={}, headers=auth(PROOF_A))
            assert lost.status == 502

        async with TestClient(TestServer(app(dependencies))) as client:
            repeated = await client.post(
                "/v1/sessions", json=OPEN_A, headers=auth(PROOF_A)
            )
            assert (
                repeated.status == 200 and (await repeated.json())["state"] == "ready"
            )
            still_ready = await client.post(
                path_a + "/mount", json={}, headers=auth(PROOF_A)
            )
            assert (
                still_ready.status == 200
                and (await still_ready.json())["state"] == "ready"
            )
            assert dependencies.calls.count(("POST", "/v1/environments")) == 1
            assert (
                dependencies.saved(SESSION_A)["manager_handle"]
                == dependencies.mounts[SESSION_A]["handle"]
            )
            assert len(dependencies.slots) == 2

            mounted_b = await client.post(
                path_b + "/mount", json={}, headers=auth(PROOF_B)
            )
            assert (
                mounted_b.status == 200 and (await mounted_b.json())["state"] == "ready"
            )
            assert set(dependencies.mounts) == {SESSION_A, SESSION_B}
            assert all(
                PROOF_A not in file.read_text() and PROOF_B not in file.read_text()
                for file in (tmp_path / ".mounted-sessions").glob("*.json")
            )

    asyncio.run(scenario())


def test_unconfirmed_release_does_not_revoke_broker_slot(tmp_path: Path) -> None:
    async def scenario() -> None:
        dependencies = Dependencies(tmp_path)
        headers = auth(PROOF_A)
        path = f"/v1/sessions/{SESSION_A}"
        async with TestClient(TestServer(app(dependencies))) as client:
            assert (
                await client.post("/v1/sessions", json=OPEN_A, headers=headers)
            ).status == 201
            assert (
                await client.post(path + "/tunnel", json=SSH_KEYS, headers=headers)
            ).status == 200
            assert (
                await client.post(path + "/mount", json={}, headers=headers)
            ).status == 200
            dependencies.mounts[SESSION_A]["state"] = "failed"
            status = await client.get(path, headers=headers)
            mount_error = {"stage": "mount", "code": "mount_failed"}
            assert (await status.json())["errors"] == [mount_error]
            dependencies.block_release = True
            failed = await client.delete(path, headers=headers)
            failed_status = await failed.json()
            assert failed.status == 502 and failed_status["cleanup_status"] == "failed"
            assert failed_status["errors"] == [
                mount_error,
                {"stage": "cleanup", "code": "cleanup_unconfirmed"},
            ]
            [reserve] = tmp_path.glob("*.reserve")
            remaining = reserve.stat().st_size
            failed_again = await client.delete(path, headers=headers)
            assert failed_again.status == 502
            assert reserve.stat().st_size == remaining
            assert len(dependencies.slots) == 1
            assert (
                await client.post(path + "/mount", json={}, headers=headers)
            ).status == 409
            dependencies.block_release = False
            closed = await client.delete(path, headers=headers)
            assert (
                closed.status == 200
                and (await closed.json())["cleanup_status"] == "success"
            )
            assert (await closed.json())["errors"] == [mount_error]
            assert not dependencies.slots and not list(tmp_path.glob("*.reserve"))
            before = list(dependencies.calls)
            assert (await client.delete(path, headers=headers)).status == 200
            assert dependencies.calls == before

    asyncio.run(scenario())


def test_shared_disk_limit_rejects_open_before_any_resource(tmp_path: Path) -> None:
    async def scenario() -> None:
        dependencies = Dependencies(tmp_path)
        async with TestClient(TestServer(app(dependencies, 1000))) as client:
            rejected = await client.post(
                "/v1/sessions", json=OPEN_A, headers=auth(PROOF_A)
            )
            assert rejected.status == 507
            assert await rejected.json() == {
                "version": "v1",
                "error": "storage_budget_exceeded",
            }
        assert dependencies.calls == [("GET", "/v0/profiles/mock")]
        assert not list((tmp_path / ".mounted-sessions").glob("*"))
        assert not list(tmp_path.glob("*.reserve"))

    asyncio.run(scenario())


def test_close_fences_delayed_provision_before_revoking_tunnel(tmp_path: Path) -> None:
    async def scenario() -> None:
        dependencies = Dependencies(tmp_path)
        dependencies.delay_manager_admission = True
        headers = auth(PROOF_A)
        path = f"/v1/sessions/{SESSION_A}"
        async with TestClient(TestServer(app(dependencies))) as client:
            assert (
                await client.post("/v1/sessions", json=OPEN_A, headers=headers)
            ).status == 201
            assert (
                await client.post(path + "/tunnel", json=SSH_KEYS, headers=headers)
            ).status == 200
            assert (
                await client.post(path + "/mount", json={}, headers=headers)
            ).status == 502
            assert not dependencies.mounts
            closed = await client.delete(path, headers=headers)
            assert closed.status == 200 and (await closed.json())["state"] == "closed"
            assert not dependencies.slots
            assert dependencies.calls[-3:] == [
                ("GET", f"/v1/environments/by-session/{SESSION_A}"),
                ("DELETE", f"/v1/environments/by-session/{SESSION_A}"),
                (
                    "DELETE",
                    f"/v1/slots/{dependencies.saved(SESSION_A)['workspace_ref']}",
                ),
            ]
            delayed = await dependencies.request(
                "POST", "/v1/environments", dependencies.delayed_provision
            )
            assert delayed["state"] == "released"

    asyncio.run(scenario())
