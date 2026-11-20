"""Владение рабочей сессией и устойчивые переходы к подключённой папке."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from aiohttp import ClientError, web

from contracts import ContractError
from contracts.mounted import validate_mounted
from services.common.storage import StorageBudgetExceeded, encode
from services.gateway.clients import Client, RemoteError
from services.gateway.store import RunStore

SESSION_ID = re.compile(r"[a-f0-9]{32}")
SESSION_PROOF = re.compile(r"[a-f0-9]{64}")
MANAGER_HANDLE = re.compile(r"[a-f0-9]{48}")
OWNER_HEADER = "X-Session-Proof"
SESSION_RESERVE_BYTES = 128 * 1024
ACTIVE_ENVIRONMENTS = {"ready", "executing", "quiescing", "collected"}
PROFILE_FIELDS = (
    "model",
    "endpoint",
    "protocol",
    "auth",
    "timeout_s",
    "output_limit",
    "budget_mode",
    "budget_mechanism",
)


class SessionError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


@web.middleware
async def session_errors(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        return await handler(request)
    except SessionError as error:
        return web.json_response(
            {"version": "v1", "error": error.code}, status=error.status
        )
    except StorageBudgetExceeded:
        if not request.path.startswith("/v1/"):
            raise
        return web.json_response(
            {"version": "v1", "error": "storage_budget_exceeded"}, status=507
        )


@dataclass(frozen=True)
class MountedSession:
    session_id: str
    profile: str
    mode: str
    owner_digest: str
    fingerprint: str
    workspace_ref: str
    profile_snapshot: dict[str, Any]
    state: str = "pending_mount"
    tunnel_fingerprint: str | None = None
    mount_requested: bool = False
    manager_handle: str | None = None
    cleanup_status: str = "not_started"
    mount_error: str | None = None
    cleanup_error: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "version": "v1",
            "session_id": self.session_id,
            "profile": self.profile,
            "mode": self.mode,
            "state": self.state,
            "cleanup_status": self.cleanup_status,
            "errors": [
                {"stage": stage, "code": code}
                for stage, code in (
                    ("mount", self.mount_error),
                    ("cleanup", self.cleanup_error),
                )
                if code is not None
            ],
        }


def _digest(value: object) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def _proof(request: web.Request) -> str:
    proof = request.headers.get(OWNER_HEADER, "")
    if not SESSION_PROOF.fullmatch(proof):
        raise SessionError(404, "session_not_found")
    return hashlib.sha256(proof.encode("ascii")).hexdigest()


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as error:
        raise SessionError(400, "invalid_request") from error
    if not isinstance(body, dict):
        raise SessionError(400, "invalid_request")
    return body


def _ed25519(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 1024
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("invalid_ssh_key")
    kind, public, *_ = value.split(maxsplit=2)
    wire = base64.b64decode(public, validate=True)
    if (
        kind != "ssh-ed25519"
        or len(wire) != 51
        or not wire.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20")
    ):
        raise ValueError("invalid_ssh_key")
    return f"{kind} {base64.b64encode(wire).decode('ascii')}"


class MountedSessions:
    def __init__(
        self,
        store: RunStore,
        manager: Client,
        broker: Client,
        proxy: Client,
        project_id: str,
        ssh_port: int,
    ) -> None:
        if not 1 <= ssh_port <= 65535:
            raise ValueError("invalid_workspace_ssh_port")
        self.disk = store.disk
        self.root = store.path / ".mounted-sessions"
        self.root.mkdir(mode=0o700, exist_ok=True)
        # Устойчивая запись в подкаталоге опирается на его имя в общем томе.
        directory = os.open(store.path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.manager, self.broker, self.proxy = manager, broker, proxy
        self.project_id, self.ssh_port = project_id, ssh_port
        self.locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self.require_inactive: Callable[[str], None] | None = None
        self.dispose_context: Callable[[str], Awaitable[None]] | None = None

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self.locks.get(session_id)
        if lock is None:
            lock = self.locks[session_id] = asyncio.Lock()
        return lock

    def _record(self, session_id: str) -> Path:
        return self.root / (session_id + ".json")

    def _load(self, session_id: str) -> MountedSession | None:
        try:
            return MountedSession(**json.loads(self._record(session_id).read_text()))
        except FileNotFoundError:
            return None

    @staticmethod
    def _reservation(record: MountedSession) -> str:
        # Имя резерва длиннее любого имени, получаемого из run_id протокола v0.
        return (
            "mounted-session-state-and-recovery-reservation-"
            f"{record.session_id}-{record.owner_digest}"
        )

    def _save(self, record: MountedSession) -> MountedSession:
        self.disk.write_reserved(
            self.disk.root / (self._reservation(record) + ".reserve"),
            self._record(record.session_id),
            asdict(record),
        )
        return record

    def _accept(self, record: MountedSession) -> MountedSession:
        path = self._record(record.session_id)
        name = self._reservation(record)
        reserve = self.disk.root / (name + ".reserve")
        # Если первая запись оборвалась до commit, внешние ресурсы ещё не запрашивались.
        reserve.unlink(missing_ok=True)
        try:
            self.disk.reserve(name, SESSION_RESERVE_BYTES)
            return self._save(record)
        except (OSError, StorageBudgetExceeded):
            if not path.exists():
                reserve.unlink(missing_ok=True)
                path.with_suffix(".json.pending").unlink(missing_ok=True)
            raise

    def _change(self, record: MountedSession, **changes: Any) -> MountedSession:
        changed = replace(record, **changes)
        return record if changed == record else self._save(changed)

    def owned(self, request: web.Request) -> MountedSession:
        session_id = request.match_info.get("session_id", "")
        if not SESSION_ID.fullmatch(session_id):
            raise SessionError(404, "session_not_found")
        return self._check_owner(self._load(session_id), _proof(request))

    @staticmethod
    def _check_owner(record: MountedSession | None, digest: str) -> MountedSession:
        existing = record.owner_digest if record is not None else "0" * 64
        if not secrets.compare_digest(existing, digest) or record is None:
            raise SessionError(404, "session_not_found")
        return record

    @asynccontextmanager
    async def locked(self, request: web.Request) -> AsyncIterator[MountedSession]:
        record = self.owned(request)
        async with self._lock(record.session_id):
            yield self.owned(request)

    @staticmethod
    async def _remote(
        client: Client,
        component: str,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: float = 15,
    ) -> dict[str, Any]:
        try:
            reply = await client.request(method, path, data, timeout)
        except RemoteError as error:
            if error.status == 507:
                raise SessionError(507, "storage_budget_exceeded") from error
            if component == "profile" and 400 <= error.status < 500:
                code = (
                    "auth_error"
                    if error.status in {401, 403} or error.code == "auth_error"
                    else "profile_incompatible"
                )
                raise SessionError(422, code) from error
            if error.status == 404:
                raise SessionError(404, f"{component}_not_found") from error
            if error.status == 409:
                code = "busy" if error.code == "busy" else f"{component}_conflict"
                raise SessionError(409, code) from error
            raise SessionError(502, f"{component}_unavailable") from error
        except (TimeoutError, ClientError, OSError, ValueError) as error:
            raise SessionError(502, f"{component}_unavailable") from error
        if not isinstance(reply, dict):
            raise SessionError(502, f"{component}_invalid_reply")
        return reply

    async def _preflight(self, profile: str) -> dict[str, Any]:
        reply = await self._remote(
            self.proxy, "profile", "GET", f"/v0/profiles/{profile}"
        )
        if (
            reply.get("compatible") is not True
            or reply.get("provider_profile") != profile
        ):
            raise SessionError(422, "profile_incompatible")
        try:
            snapshot = {key: reply[key] for key in PROFILE_FIELDS}
            size = len(encode(snapshot))
        except (KeyError, ValueError, TypeError) as error:
            raise SessionError(502, "profile_invalid_reply") from error
        if (
            not all(type(value) in {str, int, float} for value in snapshot.values())
            or size > 4096
        ):
            raise SessionError(502, "profile_invalid_reply")
        return {"name": profile, **snapshot}

    def _validate_environment(
        self, record: MountedSession, reply: dict[str, Any]
    ) -> str:
        handle = reply.get("handle")
        if (
            reply.get("version") != "v1"
            or reply.get("session_id") != record.session_id
            or reply.get("project_id") != self.project_id
            or not isinstance(handle, str)
            or not MANAGER_HANDLE.fullmatch(handle)
            or (record.manager_handle is not None and record.manager_handle != handle)
        ):
            raise SessionError(502, "manager_invalid_reply")
        return handle

    def _observe(self, record: MountedSession, reply: dict[str, Any]) -> MountedSession:
        handle = self._validate_environment(record, reply)
        state = reply.get("state")
        if not isinstance(state, str):
            raise SessionError(502, "manager_invalid_reply")
        if state in ACTIVE_ENVIRONMENTS:
            return self._change(
                record, state="ready", manager_handle=handle, mount_error=None
            )
        if state in {"failed", "released"}:
            return self._change(
                record,
                state="failed",
                manager_handle=handle,
                mount_error="mount_failed",
            )
        if state == "provisioning":
            return self._change(record, manager_handle=handle)
        raise SessionError(502, "manager_invalid_reply")

    async def _lookup(self, record: MountedSession) -> dict[str, Any] | None:
        try:
            return await self._remote(
                self.manager,
                "manager",
                "GET",
                f"/v1/environments/by-session/{record.session_id}",
                timeout=40,
            )
        except SessionError as error:
            if error.status != 404:
                raise
            return None

    async def _reconcile(self, record: MountedSession) -> MountedSession:
        if not record.mount_requested or record.state not in {"pending_mount", "ready"}:
            return record
        reply = await self._lookup(record)
        if reply is not None:
            return self._observe(record, reply)
        if record.manager_handle is not None:
            return self._change(record, state="failed", mount_error="mount_missing")
        return record

    async def open(self, request: web.Request) -> web.Response:
        owner = _proof(request)
        data = await _body(request)
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise SessionError(400, "invalid_session_id")
        async with self._lock(session_id):
            previous = self._load(session_id)
            if previous is not None:
                self._check_owner(previous, owner)
            try:
                validate_mounted("session_open", data)
            except ContractError as error:
                raise SessionError(400, "invalid_request") from error
            profile, mode = data["profile"], data["mode"]
            if profile != profile.strip():
                raise SessionError(400, "invalid_request")
            fingerprint = _digest(data)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise SessionError(409, "session_conflict")
                return web.json_response((await self._reconcile(previous)).public())
            snapshot = await self._preflight(profile)
            record = self._accept(
                MountedSession(
                    session_id,
                    profile,
                    mode,
                    owner,
                    fingerprint,
                    secrets.token_hex(16),
                    snapshot,
                )
            )
            return web.json_response(record.public(), status=201)

    async def status(self, request: web.Request) -> web.Response:
        async with self.locked(request) as record:
            return web.json_response((await self._reconcile(record)).public())

    async def tunnel(self, request: web.Request) -> web.Response:
        async with self.locked(request) as record:
            if record.state not in {"pending_mount", "ready"}:
                raise SessionError(409, "session_inactive")
            body = await _body(request)
            if set(body) != {"tunnel_public_key", "export_host_public_key"}:
                raise SessionError(400, "invalid_request")
            try:
                validate_mounted("session_tunnel", body)
                keys = {name: _ed25519(value) for name, value in body.items()}
            except ValueError as error:
                raise SessionError(400, "invalid_ssh_key") from error
            fingerprint = _digest(keys)
            if (
                record.tunnel_fingerprint is not None
                and record.tunnel_fingerprint != fingerprint
            ):
                raise SessionError(409, "tunnel_conflict")
            record = self._change(record, tunnel_fingerprint=fingerprint)
            reply = await self._remote(
                self.broker,
                "broker",
                "PUT",
                f"/v1/slots/{record.workspace_ref}",
                keys,
            )
            try:
                ssh_host_key = _ed25519(reply.get("ssh_host_key"))
                export_client_key = _ed25519(reply.get("export_client_key"))
            except ValueError as error:
                raise SessionError(502, "broker_invalid_reply") from error
            return web.json_response(
                {
                    "ssh_username": record.workspace_ref,
                    "ssh_port": self.ssh_port,
                    "ssh_host_key": ssh_host_key,
                    "export_client_key": export_client_key,
                }
            )

    def _provision_body(self, record: MountedSession) -> dict[str, str]:
        return {
            "version": "v1",
            "project_id": self.project_id,
            "session_id": record.session_id,
            "workspace_ref": record.workspace_ref,
            "mode": record.mode,
        }

    async def mount(self, request: web.Request) -> web.Response:
        async with self.locked(request) as record:
            if await _body(request) != {}:
                raise SessionError(400, "invalid_request")
            record = await self._reconcile(record)
            if record.state == "ready":
                return web.json_response(record.public())
            if record.state != "pending_mount":
                raise SessionError(409, "session_inactive")
            if record.tunnel_fingerprint is None:
                raise SessionError(409, "tunnel_not_ready")
            slot = await self._remote(
                self.broker, "broker", "GET", f"/v1/slots/{record.workspace_ref}"
            )
            if slot.get("ready") is not True:
                raise SessionError(409, "tunnel_not_ready")
            record = self._change(record, mount_requested=True)
            reply = await self._remote(
                self.manager,
                "manager",
                "POST",
                "/v1/environments",
                self._provision_body(record),
                timeout=80,
            )
            return web.json_response(self._observe(record, reply).public())

    async def _release(self, record: MountedSession) -> None:
        if record.mount_requested:
            environment = await self._lookup(record)
            if environment is None:
                if record.manager_handle is not None:
                    raise SessionError(502, "manager_cleanup_unconfirmed")
                # У Manager нет записи: под его lock запрещаем запоздалый provision
                # перед отзывом файлового канала.
                environment = await self._remote(
                    self.manager,
                    "manager",
                    "DELETE",
                    f"/v1/environments/by-session/{record.session_id}",
                    self._provision_body(record),
                    timeout=80,
                )
                if (
                    environment.get("state") != "released"
                    or environment.get("cleanup_status") != "success"
                ):
                    raise SessionError(502, "manager_cleanup_unconfirmed")
            handle = self._validate_environment(record, environment)
            if (
                environment.get("state") not in ("released", "failed")
                or environment.get("cleanup_status") != "success"
            ):
                released = await self._remote(
                    self.manager,
                    "manager",
                    "POST",
                    f"/v1/environments/{handle}/release",
                    {
                        "version": "v1",
                        "project_id": self.project_id,
                        "session_id": record.session_id,
                        "handle": handle,
                    },
                    timeout=80,
                )
                self._validate_environment(record, released)
                if (
                    released.get("state") != "released"
                    or released.get("cleanup_status") != "success"
                ):
                    raise SessionError(502, "manager_cleanup_unconfirmed")
        if record.tunnel_fingerprint is not None:
            await self._remote(
                self.broker, "broker", "DELETE", f"/v1/slots/{record.workspace_ref}"
            )

    async def close(self, request: web.Request) -> web.Response:
        async with self.locked(request) as record:
            if record.state == "closed":
                return web.json_response(record.public())
            if self.require_inactive is not None:
                self.require_inactive(record.session_id)
            if record.cleanup_status != "failed":
                record = self._change(record, state="closing")
            failed = False
            try:
                await self._release(record)
            except SessionError:
                failed = True
            if self.dispose_context is not None:
                try:
                    await self.dispose_context(record.session_id)
                except (RemoteError, ClientError, TimeoutError, OSError, ValueError):
                    failed = True
            if failed:
                record = self._change(
                    record,
                    state="failed",
                    cleanup_status="failed",
                    cleanup_error="cleanup_unconfirmed",
                )
                return web.json_response(record.public(), status=502)
            record = self._change(
                record, state="closed", cleanup_status="success", cleanup_error=None
            )
            (self.disk.root / (self._reservation(record) + ".reserve")).unlink(
                missing_ok=True
            )
            return web.json_response(record.public())

    def routes(self) -> list[web.RouteDef]:
        session = "/v1/sessions/{session_id}"
        return [
            web.post("/v1/sessions", self.open),
            web.get(session, self.status),
            web.post(session + "/tunnel", self.tunnel),
            web.post(session + "/mount", self.mount),
            web.delete(session, self.close),
        ]
