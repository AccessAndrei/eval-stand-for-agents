"""Read-only доступ к выделенной авторизации и узкий нативный refresh-маршрут."""

import asyncio
import base64
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp
from aiohttp import web

from services.common.http import valid_bearer
from services.harness.credentials import CredentialError, _auth, _directory, _read

NATIVE_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_ENDPOINT = "https://auth.openai.com/oauth/token"
MAX_NATIVE_BODY = 65_536
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class NativeAuthError(RuntimeError):
    """Безопасный код ошибки; исходные токены и ответы в сообщение не включаются."""


def validate_native_grant(body: object, expected_refresh: str) -> dict:
    if not isinstance(body, dict) or set(body) != {
        "grant_type",
        "client_id",
        "refresh_token",
    }:
        raise NativeAuthError("invalid_native_refresh")
    if body["grant_type"] != "refresh_token" or body["client_id"] != NATIVE_CLIENT_ID:
        raise NativeAuthError("invalid_native_refresh")
    token = body["refresh_token"]
    if not isinstance(token, str) or not token or len(token) > 16_384:
        raise NativeAuthError("invalid_native_refresh")
    try:
        matches = secrets.compare_digest(token.encode(), expected_refresh.encode())
    except UnicodeError:
        matches = False
    if not matches:
        raise NativeAuthError("foreign_native_refresh")
    return {key: body[key] for key in ("grant_type", "client_id", "refresh_token")}


def auth_expiry(raw: bytes) -> int:
    """Проверка локального срока, а не криптографическое доказательство доступа."""
    token = json.loads(_auth(raw).raw)["tokens"]["access_token"]
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        payload = base64.b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
        )
        expires = json.loads(payload)["exp"]
        if type(expires) is not int or expires < 0:
            raise ValueError
        return expires
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise NativeAuthError("auth_expiry_unavailable") from None


async def forward_native_refresh(body: dict) -> tuple[int, bytes]:
    """Ровно один POST к закреплённому OAuth endpoint, без redirect/replay."""
    timeout = aiohttp.ClientTimeout(total=30, ceil_threshold=float("inf"))
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.post(
            REFRESH_ENDPOINT, json=body, allow_redirects=False
        ) as response:
            if 300 <= response.status < 400:
                raise NativeAuthError("refresh_redirect_rejected")
            payload = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                if len(payload) + len(chunk) > MAX_NATIVE_BODY:
                    raise NativeAuthError("refresh_response_too_large")
                payload.extend(chunk)
            return response.status, bytes(payload)


async def bounded_native_body(stream: aiohttp.StreamReader) -> bytes:
    payload = bytearray()
    async with asyncio.timeout(30):
        async for chunk in stream.iter_chunked(4096):
            if len(payload) + len(chunk) > MAX_NATIVE_BODY:
                raise NativeAuthError("refresh_request_too_large")
            payload.extend(chunk)
    return bytes(payload)


class NativeAuthReader:
    """Не получает flock владельца и не записывает current/attempt/journal."""

    def __init__(
        self,
        root: Path,
        control_token: str,
        *,
        clock: Callable[[], float] = time.time,
        refresh_transport: Callable[
            [dict], Awaitable[tuple[int, bytes]]
        ] = forward_native_refresh,
    ) -> None:
        if len(control_token) < 24:
            raise ValueError("invalid_proxy_token")
        self.root = root
        self._control_token = control_token
        self.clock = clock
        self._transport = refresh_transport
        self._lock = asyncio.Lock()
        self._forwarded: set[str] = set()

    def journal(self) -> dict:
        _directory(self.root)
        _directory(self.root / "attempts")
        try:
            value = json.loads(_read(self.root / "journal.json"))
        except (ValueError, UnicodeError):
            raise NativeAuthError("invalid_auth_journal") from None
        if not isinstance(value, dict) or value.get("version") != "v0":
            raise NativeAuthError("invalid_auth_journal")
        return value

    def _current(self, *, allow_expired: bool = False) -> bytes:
        if self.journal().get("state") != "idle":
            raise NativeAuthError("auth_preparation_in_progress")
        raw = _auth(_read(self.root / "current.json")).raw
        if not allow_expired and auth_expiry(raw) <= self.clock() + 30:
            raise NativeAuthError("auth_refresh_required")
        return raw

    def status(self) -> dict:
        try:
            raw = self._current(allow_expired=True)
            expires = auth_expiry(raw)
            ready = expires > self.clock() + 30
            return {
                "ready": ready,
                "auth_ready": ready,
                "expires_at": expires,
                "reason": None if ready else "auth_refresh_required",
            }
        except (CredentialError, NativeAuthError) as error:
            return {
                "ready": False,
                "auth_ready": False,
                "expires_at": None,
                "reason": str(error),
            }
        except OSError:
            return {
                "ready": False,
                "auth_ready": False,
                "expires_at": None,
                "reason": "auth_storage_unavailable",
            }

    def inference_headers(self) -> dict[str, str]:
        """Только для внутреннего HTTP-клиента прокси; не возвращать наружу."""
        tokens = json.loads(self._current())["tokens"]
        return {
            "Authorization": "Bearer " + tokens["access_token"],
            "ChatGPT-Account-ID": tokens["account_id"],
        }

    def _validate_active(self, run_id: str, body: object) -> dict:
        if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
            raise NativeAuthError("unknown_auth_attempt")
        journal = self.journal()
        if journal.get("state") != "refreshing" or journal.get("run_id") != run_id:
            raise NativeAuthError("unknown_auth_attempt")
        home = self.root / "attempts" / run_id
        _directory(home)
        auth = _auth(_read(home / "auth.json"))
        if auth.digest != journal.get("before_sha") or auth.identity != journal.get(
            "identity"
        ):
            raise NativeAuthError("auth_attempt_changed")
        return validate_native_grant(
            body, json.loads(auth.raw)["tokens"]["refresh_token"]
        )

    async def refresh(self, run_id: str, body: object) -> tuple[int, bytes]:
        async with self._lock:
            request = self._validate_active(run_id, body)
            if run_id in self._forwarded:
                raise NativeAuthError("refresh_replay_rejected")
            self._forwarded.add(run_id)
            # Статус/тело остаются нативными. Только владелец публикует токены.
            status, response = await self._transport(request)
            if 300 <= status < 400:
                raise NativeAuthError("refresh_redirect_rejected")
            if not isinstance(response, bytes) or len(response) > MAX_NATIVE_BODY:
                raise NativeAuthError("refresh_response_too_large")
            return status, response

    async def refresh_handler(self, request: web.Request) -> web.Response:
        if not valid_bearer(
            request.headers.get("Authorization", ""), self._control_token
        ):
            raise web.HTTPUnauthorized()
        try:
            raw = await bounded_native_body(request.content)
            body = json.loads(raw)
            status, response = await self.refresh(
                request.headers.get("X-Native-Auth-Run", ""), body
            )
            if len(response) > MAX_NATIVE_BODY:
                raise NativeAuthError("refresh_response_too_large")
            return web.Response(
                status=status, body=response, content_type="application/json"
            )
        except (
            CredentialError,
            NativeAuthError,
            ValueError,
            UnicodeError,
            RecursionError,
        ):
            return web.json_response({"error": "native_refresh_rejected"}, status=403)
        except (aiohttp.ClientError, OSError, TimeoutError):
            return web.json_response(
                {"error": "native_refresh_unavailable"}, status=502
            )
