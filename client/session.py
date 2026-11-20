"""Общий клиент v1 для headless-режима, GUI и раннера БАФ."""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import asyncssh

from contracts import ContractError
from contracts.mounted import validate_mounted

from .export import WorkspaceExport

_IDENTIFIER = re.compile(r"[0-9a-f]{32}")
_MAX_RESPONSE_BYTES = 4_000_000
_MAX_EVENT_BYTES = 1_048_576
_ERROR_CODES = frozenset(
    {
        "auth_error",
        "budget_exceeded",
        "busy",
        "cleanup_failed",
        "cleanup_unconfirmed",
        "conflict",
        "history_expired",
        "history_unavailable",
        "invalid_request",
        "mount_failed",
        "mount_lost",
        "not_found",
        "profile_incompatible",
        "profile_incomplete",
        "session_not_ready",
        "session_conflict",
        "session_inactive",
        "session_not_found",
        "storage_budget_exceeded",
        "tunnel_not_ready",
        "unauthorized",
        "workspace_unavailable",
    }
)


class ClientError(RuntimeError):
    """Диагностика содержит только известный код и стадию."""

    def __init__(
        self,
        stage: str,
        code: str,
        http_status: int | None = None,
        *,
        earliest_seq: int | None = None,
    ) -> None:
        super().__init__(f"{stage}: {code}")
        self.stage = stage
        self.code = code
        self.http_status = http_status
        self.earliest_seq = earliest_seq


@dataclass(frozen=True)
class CloseReport:
    server: dict[str, Any] | None
    server_error: ClientError | None
    tunnel_closed: bool
    export_drained: bool

    @property
    def ok(self) -> bool:
        return (
            self.server_error is None
            and (self.server is None or self.server.get("cleanup_status") == "success")
            and self.tunnel_closed
            and self.export_drained
        )


def _time_limit(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Таймаут должен быть конечным положительным числом")
    return value


def _object(raw: bytes, stage: str, code: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        raise ClientError(stage, code) from None
    if not isinstance(parsed, dict):
        raise ClientError(stage, code)
    return parsed


async def _read(response: aiohttp.ClientResponse, stage: str) -> bytes:
    payload = bytearray()
    async for chunk in response.content.iter_chunked(65_536):
        if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise ClientError(stage, "response_too_large")
        payload.extend(chunk)
    return bytes(payload)


async def _require_success(response: aiohttp.ClientResponse, stage: str) -> None:
    status = response.status
    if 200 <= status < 300:
        return
    if 300 <= status < 400:
        raise ClientError(stage, "redirect_rejected", status)
    if status in (401, 403):
        raise ClientError("authorization", "unauthorized", status)
    code = {404: "not_found", 409: "conflict", 507: "storage_budget_exceeded"}.get(
        status, "gateway_rejected"
    )
    try:
        payload = _object(await _read(response, stage), stage, "invalid_response")
    except ClientError:
        payload = {}
    candidate = payload.get("error")
    if isinstance(candidate, str) and candidate in _ERROR_CODES:
        code = candidate
    earliest = payload.get("earliest_seq")
    seq = (
        earliest
        if code == "history_unavailable" and type(earliest) is int and earliest >= 0
        else None
    )
    raise ClientError(stage, code, status, earliest_seq=seq)


class MountedClient:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        *,
        timeout: float = 90.0,
        ssh_host: str | None = None,
    ) -> None:
        url = urlsplit(gateway_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or any(character.isspace() for character in gateway_url)
        ):
            raise ValueError("Нужен адрес Gateway без credentials и пути")
        if url.port is not None and not 1 <= url.port <= 65_535:
            raise ValueError("Недопустимый порт Gateway")
        if (
            len(token) < 24
            or not token.isascii()
            or not token.isprintable()
            or any(c.isspace() for c in token)
        ):
            raise ValueError("CLIENT_TOKEN отсутствует либо недопустим")
        if ssh_host is not None and (
            not ssh_host or any(c.isspace() or c in "/@?#" for c in ssh_host)
        ):
            raise ValueError("Недопустимый адрес SSH-брокера")
        self.gateway_url = gateway_url.rstrip("/")
        self.ssh_host = ssh_host if ssh_host is not None else url.hostname
        self.timeout = _time_limit(timeout)
        self._token = token
        self._http: aiohttp.ClientSession | None = None
        self._sessions: set[MountedSession] = set()

    async def __aenter__(self) -> MountedClient:
        if self._http is not None:
            raise RuntimeError("Клиент уже открыт")
        self._http = aiohttp.ClientSession(
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            read_bufsize=_MAX_EVENT_BYTES // 2,
        )
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        assert self._http is not None
        try:
            reports = [await session.close() for session in tuple(self._sessions)]
            if exc_type is None and any(not report.ok for report in reports):
                raise ClientError("cleanup", "cleanup_unconfirmed")
        finally:
            await self._http.close()
            self._http = None

    def session(
        self,
        root: Path,
        *,
        profile: str,
        mode: str = "rw",
        write_limit: int = 64_000_000,
        protected_paths: Iterable[str] = (),
        entry_limit: int = 256,
    ) -> MountedSession:
        """Создать локальные идентификаторы до первого запроса."""
        self._connection()
        if not profile or not profile.strip():
            raise ValueError("Нужно указать профиль модели")
        if mode not in {"ro", "rw"}:
            raise ValueError("Режим должен быть ro или rw")
        exporter = WorkspaceExport(
            root,
            mode=mode,
            write_limit=write_limit,
            protected_paths=protected_paths,
            entry_limit=entry_limit,
        )
        session = MountedSession(self, profile, mode, exporter)
        self._sessions.add(session)
        return session

    async def open_session(
        self,
        root: Path,
        *,
        profile: str,
        mode: str = "rw",
        write_limit: int = 64_000_000,
        protected_paths: Iterable[str] = (),
        entry_limit: int = 256,
    ) -> MountedSession:
        session = self.session(
            root,
            profile=profile,
            mode=mode,
            write_limit=write_limit,
            protected_paths=protected_paths,
            entry_limit=entry_limit,
        )
        try:
            await session.mount()
        except (ClientError, asyncio.CancelledError) as error:
            report = await session.close()
            if not report.ok:
                error.add_note("Очистка после ошибки открытия не подтверждена")
            raise
        return session

    def _connection(self) -> aiohttp.ClientSession:
        if self._http is None:
            raise RuntimeError("Откройте MountedClient через async with")
        return self._http

    def _headers(self, proof: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "X-Session-Proof": proof}

    def _deadline(self, timeout: float | None = None) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=self.timeout if timeout is None else _time_limit(timeout),
            ceil_threshold=math.inf,
        )


class MountedSession:
    def __init__(
        self,
        client: MountedClient,
        profile: str,
        mode: str,
        exporter: WorkspaceExport,
    ) -> None:
        self.session_id = secrets.token_hex(16)
        self.profile = profile
        self.mode = mode
        self._proof = secrets.token_hex(32)
        self._key = asyncssh.generate_private_key("ssh-ed25519")
        self._client = client
        self._exporter = exporter
        self._path = f"/v1/sessions/{self.session_id}"
        self._port: int | None = None
        self._ssh: asyncssh.SSHClientConnection | None = None
        self._forward: asyncssh.SSHListener | None = None
        self._opening_sent = False
        self._ready = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._close_report: CloseReport | None = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        stage: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with self._client._connection().request(
                method,
                self._client.gateway_url + path,
                json=payload,
                headers=self._client._headers(self._proof),
                timeout=self._client._deadline(),
                allow_redirects=False,
            ) as response:
                if method == "DELETE" and response.status == 502:
                    raw = await _read(response, stage)
                    snapshot = _object(raw, stage, "invalid_response")
                    if snapshot.get("cleanup_status") == "failed":
                        return snapshot
                    raise ClientError(stage, "cleanup_unconfirmed", 502)
                await _require_success(response, stage)
                raw = await _read(response, stage)
                return _object(raw, stage, "invalid_response")
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise ClientError(stage, "gateway_transport_error") from None

    def _session_state(self, value: dict[str, Any], stage: str) -> str:
        state = value.get("state")
        if (
            value.get("version") != "v1"
            or value.get("session_id") != self.session_id
            or not isinstance(state, str)
        ):
            raise ClientError(stage, "invalid_session_response")
        return state

    async def mount(self) -> dict[str, Any]:
        """Открыть сессию и подтвердить mount до запуска сообщения."""
        async with self._lock:
            if self._closed:
                raise ClientError("prepare", "session_closed")
            if self._ready:
                return await self.status()
            try:
                async with asyncio.timeout(self._client.timeout):
                    previous_open = self._opening_sent
                    self._opening_sent = True
                    try:
                        opened = await self._request(
                            "POST",
                            "/v1/sessions",
                            stage="prepare",
                            payload={
                                "session_id": self.session_id,
                                "profile": self.profile,
                                "mode": self.mode,
                            },
                        )
                    except ClientError as error:
                        if error.http_status in (400, 401, 403, 404, 409, 507):
                            self._opening_sent = previous_open
                        raise
                    if self._session_state(opened, "prepare") in {"closed", "failed"}:
                        raise ClientError("prepare", "session_unavailable")
                    if self._forward is None:
                        await self._open_tunnel()
                    mounted = await self._request(
                        "POST", f"{self._path}/mount", stage="prepare", payload={}
                    )
                    while self._session_state(mounted, "prepare") in {
                        "pending_mount",
                        "mounting",
                    }:
                        await asyncio.sleep(0.1)
                        mounted = await self.status()
                    if self._session_state(mounted, "prepare") != "ready":
                        raise ClientError("prepare", "mount_failed")
                    self._ready = True
                    return mounted
            except TimeoutError:
                raise ClientError("prepare", "mount_timeout") from None

    async def _open_tunnel(self) -> None:
        registration = await self._request(
            "POST",
            f"{self._path}/tunnel",
            stage="prepare",
            payload={
                "tunnel_public_key": self._key.export_public_key("openssh")
                .decode("ascii")
                .strip(),
                "export_host_public_key": self._exporter.host_public_key,
            },
        )
        username = registration.get("ssh_username")
        port = registration.get("ssh_port")
        if (
            not isinstance(username, str)
            or not _IDENTIFIER.fullmatch(username)
            or type(port) is not int
            or not 1 <= port <= 65_535
        ):
            raise ClientError("prepare", "invalid_tunnel_registration")
        broker_key = self._ssh_public_key(registration.get("ssh_host_key"))
        export_key = registration.get("export_client_key")
        self._ssh_public_key(export_key)
        try:
            if self._port is None:
                self._port = await self._exporter.start(export_key)
        except (asyncssh.Error, OSError, RuntimeError, ValueError):
            raise ClientError("prepare", "export_failed") from None

        try:
            if self._ssh is None:
                self._ssh = await asyncssh.connect(
                    self._client.ssh_host,
                    port,
                    username=username,
                    config=None,
                    agent_path=None,
                    client_keys=[self._key],
                    preferred_auth="publickey",
                    known_hosts=([broker_key], [], [], [], [], [], []),
                    x509_trusted_certs=None,
                    connect_timeout=self._client.timeout,
                    login_timeout=self._client.timeout,
                    keepalive_interval=5,
                    keepalive_count_max=3,
                )
            self._forward = await self._ssh.forward_remote_path_to_port(
                f"/ipc/{username}/reverse.sock", "127.0.0.1", self._port
            )
        except asyncssh.HostKeyNotVerifiable:
            raise ClientError("authorization", "ssh_host_key_mismatch") from None
        except asyncssh.PermissionDenied:
            raise ClientError("authorization", "ssh_key_rejected") from None
        except (asyncssh.Error, OSError):
            raise ClientError("prepare", "ssh_tunnel_failed") from None
        if self._forward is None:
            raise ClientError("prepare", "ssh_forward_rejected")

    @staticmethod
    def _ssh_public_key(value: object) -> asyncssh.SSHKey:
        if (
            not isinstance(value, str)
            or len(value) > 1024
            or not value.isascii()
            or len(value.splitlines()) != 1
            or not value.startswith("ssh-ed25519 ")
        ):
            raise ClientError("prepare", "invalid_ssh_public_key")
        try:
            key = asyncssh.import_public_key(value)
        except asyncssh.KeyImportError:
            raise ClientError("prepare", "invalid_ssh_public_key") from None
        if key.get_algorithm() != "ssh-ed25519":
            raise ClientError("prepare", "invalid_ssh_public_key")
        return key

    async def status(self) -> dict[str, Any]:
        snapshot = await self._request("GET", self._path, stage="session")
        self._session_state(snapshot, "session")
        return snapshot

    def _run_path(self, run_id: str) -> str:
        if not _IDENTIFIER.fullmatch(run_id):
            raise ValueError("run_id должен содержать 32 строчных hex-символа")
        return f"{self._path}/runs/{run_id}"

    def _check_run(self, response: dict[str, Any], run_id: str, stage: str) -> None:
        if (
            response.get("version") != "v1"
            or response.get("session_id") != self.session_id
            or response.get("run_id") != run_id
        ):
            raise ClientError(stage, "invalid_run_response")

    async def start_run(
        self,
        prompt: str,
        *,
        timeout_s: int,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Принять один turn. Для явного повтора передайте исходный run_id."""
        if not self._ready or self._closed:
            raise ClientError("execution", "session_not_ready")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Сообщение пустое")
        if type(timeout_s) is not int or timeout_s <= 0:
            raise ValueError("Таймаут запуска должен быть положительным целым")
        run_id = secrets.token_hex(16) if run_id is None else run_id
        self._run_path(run_id)
        body = {"run_id": run_id, "prompt": prompt, "timeout_s": timeout_s}
        if task_id is not None:
            body["task_id"] = task_id
        result = await self._request(
            "POST", f"{self._path}/runs", stage="execution", payload=body
        )
        self._check_run(result, run_id, "execution")
        return result

    async def run_status(self, run_id: str) -> dict[str, Any]:
        response = await self._request("GET", self._run_path(run_id), stage="execution")
        self._check_run(response, run_id, "execution")
        return response

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        response = await self._request(
            "POST", f"{self._run_path(run_id)}/cancel", stage="execution", payload={}
        )
        self._check_run(response, run_id, "execution")
        return response

    async def events(
        self, run_id: str, *, after: int = 0, timeout: float | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Открыть поток исходного запуска с последнего полученного seq."""
        if type(after) is not int or after < 0:
            raise ValueError("Курсор событий должен быть неотрицательным целым")
        url = f"{self._client.gateway_url}{self._run_path(run_id)}/events"
        terminal = False
        last = after
        try:
            async with self._client._connection().get(
                url,
                params={"after": str(after)},
                headers=self._client._headers(self._proof),
                timeout=self._client._deadline(timeout),
                allow_redirects=False,
            ) as response:
                await _require_success(response, "events")
                async for line in response.content:
                    if terminal or len(line) > _MAX_EVENT_BYTES:
                        raise ClientError("events", "invalid_event")
                    event = _object(line, "events", "invalid_event")
                    try:
                        validate_mounted("event", event)
                    except ContractError:
                        raise ClientError("events", "invalid_event") from None
                    sequence = event["seq"]
                    if sequence != last + 1:
                        raise ClientError("events", "invalid_event")
                    if event["type"] == "result":
                        self._check_run(event["result"], run_id, "events")
                        terminal = True
                    last = sequence
                    yield event
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            raise ClientError("events", "stream_interrupted") from None
        if not terminal:
            raise ClientError("events", "stream_incomplete")

    async def close(self) -> CloseReport:
        """Остановить серверную среду до отзыва доступа к локальной папке."""
        async with self._lock:
            self._closed = True
            previous = self._close_report
            server = previous.server if previous else None
            server_error = None
            try:
                if self._opening_sent and (
                    server is None or server.get("cleanup_status") != "success"
                ):
                    server = await self._request("DELETE", self._path, stage="cleanup")
                    state = self._session_state(server, "cleanup")
                    if server.get("cleanup_status") != "success":
                        server_error = ClientError("cleanup", "cleanup_unconfirmed")
                    elif state != "closed":
                        raise ClientError("cleanup", "invalid_session_response")
            except ClientError as error:
                server = None
                server_error = error
            finally:
                tunnel_closed = await self._close_tunnel()
                export_drained = await self._drain_export()
                self._client._sessions.discard(self)
                self._close_report = CloseReport(
                    server, server_error, tunnel_closed, export_drained
                )
            return self._close_report

    async def _close_tunnel(self) -> bool:
        if self._ssh is None:
            return True
        try:
            if self._forward is not None:
                self._forward.close()
            self._ssh.close()
            async with asyncio.timeout(self._client.timeout):
                await self._ssh.wait_closed()
                if self._forward is not None:
                    await self._forward.wait_closed()
        except (asyncssh.Error, OSError, RuntimeError, TimeoutError):
            return False
        self._forward = None
        self._ssh = None
        return True

    async def _drain_export(self) -> bool:
        try:
            async with asyncio.timeout(self._client.timeout):
                return await self._exporter.close(timeout=self._client.timeout)
        except (asyncssh.Error, OSError, RuntimeError, TimeoutError):
            return False
