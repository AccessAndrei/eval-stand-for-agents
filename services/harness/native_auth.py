"""Один Python-владелец и auth-only Codex 0.159.3 без диалога и генерации.

Закреплённый getAuthStatus(includeToken=false, refreshToken=true) выполняет
нативный refresh без accounts/check discovery у account/read. RPC устаревший,
но присутствует в закреплённой версии; обновлять вместе с версией Codex.
"""

import asyncio
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from services.common.processes import read_bounded, stop_session
from services.harness.credentials import CredentialError, CredentialOwner, _atomic
from services.harness.recovery import HarnessRecovery, RecoveryError
from services.harness.rpc import Rpc, RpcError
from services.harness.settings import VERSION
from services.llm_proxy.native_auth import (
    MAX_NATIVE_BODY,
    NativeAuthError,
    NativeAuthReader,
    bounded_native_body,
    validate_native_grant,
)

NativeRunner = Callable[[Path, dict[str, str]], Awaitable[dict]]


class NativeAuthController:
    def __init__(
        self,
        root: Path,
        proxy_url: str,
        proxy_token: str,
        repository_root: Path,
        *,
        binary: str = "/usr/local/bin/codex",
        bridge_port: int = 8091,
        native_runner: NativeRunner | None = None,
        guard_factory: Callable[[Path], HarnessRecovery] = HarnessRecovery,
    ) -> None:
        url = urlsplit(proxy_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"llm-proxy", "127.0.0.1", "localhost", "::1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("invalid_internal_auth_proxy")
        if len(proxy_token) < 24:
            raise ValueError("invalid_proxy_token")
        self.root, self.repository_root = root, repository_root
        self.proxy_url, self._proxy_token = proxy_url.rstrip("/"), proxy_token
        self.binary, self.bridge_port = binary, bridge_port
        self._native_runner = native_runner or self._native_account
        self._guard_factory = guard_factory
        self.owner: CredentialOwner | None = None
        self.guard: HarnessRecovery | None = None
        self.reader = NativeAuthReader(root, proxy_token)
        self._session: aiohttp.ClientSession | None = None
        self._web_runner: web.AppRunner | None = None
        self._lock, self._refresh_lock = asyncio.Lock(), asyncio.Lock()
        self._active: str | None = None
        self._sent, self._refresh_ok = False, False
        self._bridge_error: str | None = None
        self._blocked: str | None = None
        self._resource_idle = True
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self.bridge_url: str | None = None
        self.bridge_app = web.Application(client_max_size=MAX_NATIVE_BODY)
        self.bridge_app.router.add_post("/native-refresh", self._native_refresh)

    async def start(self) -> None:
        if self.owner is not None:
            raise NativeAuthError("auth_controller_already_started")
        self.owner = CredentialOwner(self.root, self.repository_root).__enter__()
        try:
            self.root = self.owner.root
            self.reader.root = self.root
            self.guard = self._guard_factory(self.root / "attempts")
            await self._recover()
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, ceil_threshold=float("inf")),
                trust_env=False,
            )
            self._web_runner = web.AppRunner(
                self.bridge_app, access_log=None, shutdown_timeout=5
            )
            await self._web_runner.setup()
            site = web.TCPSite(self._web_runner, "127.0.0.1", self.bridge_port)
            await site.start()
            assert site._server is not None
            port = site._server.sockets[0].getsockname()[1]
            self.bridge_url = f"http://127.0.0.1:{port}/native-refresh"
        except BaseException:
            await self.close()
            raise

    async def _recover(self) -> None:
        assert self.owner is not None and self.guard is not None
        journal = self.owner._journal()
        if journal["state"] == "idle":
            return
        self._active = journal["run_id"]
        self._resource_idle = False
        try:
            record = self.guard._load(self._active)
            if record is None and journal["state"] not in {
                "preparing",
                "prepared",
                "cleaning",
            }:
                raise NativeAuthError("auth_process_ownership_unconfirmed")
            if record is not None:
                await self.guard._stop(
                    self.root / "attempts" / self._active,
                    record["owner_nonce"],
                    time.monotonic() + 15,
                )
            self._resource_idle = True
            # Только CredentialOwner решает, можно ли публиковать auth и удалять home.
            self.owner.recover(native_stopped=True)
        except (CredentialError, RecoveryError, NativeAuthError, OSError):
            self._blocked = "auth_recovery_required"
        finally:
            if self._resource_idle:
                self._active = None

    @property
    def is_busy(self) -> bool:
        return (
            self._lock.locked()
            or self._refresh_lock.locked()
            or not self._resource_idle
        )

    async def wait_idle(self, timeout_s: float = 30) -> bool:
        """Подтвердить отсутствие auth-worker отдельно от доступности credentials."""
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("invalid_auth_idle_timeout")
        try:
            async with asyncio.timeout(timeout_s):
                async with self._lock:
                    async with self._refresh_lock:
                        return self._resource_idle
        except TimeoutError:
            return False

    def status(self) -> dict:
        if self.owner is None:
            return {
                "ready": False,
                "auth_ready": False,
                "reason": "auth_controller_not_started",
            }
        if self._blocked:
            return {"ready": False, "auth_ready": False, "reason": self._blocked}
        return self.reader.status()

    async def prepare(self, *, force_refresh: bool = False) -> dict:
        async with self._lock:
            if self.owner is None or self.guard is None or self._session is None:
                raise NativeAuthError("auth_controller_not_started")
            if self._blocked:
                return {
                    "ready": False,
                    "auth_ready": False,
                    "refreshed": False,
                    "reason": self._blocked,
                }
            previous = self.reader.status()
            if not force_refresh and previous["ready"]:
                return {**previous, "refreshed": False}
            self._active = "auth-" + uuid.uuid4().hex
            self._resource_idle = False
            self._sent = self._refresh_ok = False
            self._bridge_error = None
            refreshed = False
            error = None
            try:
                home = self.owner.prepare_attempt(self._active)
                env = self._configure(home)
                env.update(self.guard.begin_attempt(self._active, home))
                async with asyncio.timeout(60):
                    account = await self._native_runner(home, env)
                if (
                    not isinstance(account, dict)
                    or account.get("authMethod") != "chatgpt"
                    or account.get("authToken") is not None
                    or account.get("requiresOpenaiAuth") is not True
                ):
                    raise NativeAuthError("native_chatgpt_auth_unconfirmed")
            except (
                CredentialError,
                NativeAuthError,
                RecoveryError,
                RpcError,
                OSError,
                ValueError,
                TimeoutError,
            ):
                error = "native_auth_preparation_failed"
            except asyncio.CancelledError:
                error = "native_auth_preparation_cancelled"
            finally:
                stopped = await self._stop_active()
            if not stopped:
                self._blocked = "native_auth_cleanup_unconfirmed"
                return {
                    "ready": False,
                    "auth_ready": False,
                    "refreshed": False,
                    "reason": self._blocked,
                }
            if self.owner._journal().get("run_id") != self._active:
                self._active = None
                return {
                    "ready": False,
                    "auth_ready": False,
                    "refreshed": False,
                    "reason": error or "native_auth_preparation_failed",
                }
            try:
                if self._sent:
                    if not self._refresh_ok or self._bridge_error:
                        raise NativeAuthError("native_refresh_unconfirmed")
                    self.owner.commit_rotation(self._active)
                    refreshed = True
                elif self._bridge_error:
                    raise NativeAuthError("native_refresh_unconfirmed")
                self.owner.cleanup_attempt(self._active, native_stopped=True)
            except (CredentialError, NativeAuthError, OSError):
                # Незавершённый home сохраняется; следующий старт попытается
                # восстановить только подтверждённую локальную ротацию.
                self._blocked = "auth_recovery_required"
            self._active = None
            reason = self._blocked or error
            if reason:
                return {
                    "ready": False,
                    "auth_ready": False,
                    "refreshed": refreshed,
                    "reason": reason,
                }
            return {**self.reader.status(), "refreshed": refreshed}

    def _configure(self, home: Path) -> dict[str, str]:
        assert self.bridge_url is not None
        origin = self.bridge_url.removesuffix("/native-refresh")
        quote = json.dumps
        config = [
            'model = "gpt-6-luna"',
            'model_provider = "auth-control"',
            'cli_auth_credentials_store = "file"',
            'approval_policy = "never"',
            f"model_catalog_json = {quote(str(home / 'models.json'))}",
            f"chatgpt_base_url = {quote(origin + '/disabled-backend')}",
            "[model_providers.auth-control]",
            'name = "auth-control"',
            f"base_url = {quote(origin + '/disabled-inference')}",
            'wire_api = "responses"',
            "requires_openai_auth = true",
            "supports_websockets = false",
            "request_max_retries = 0",
            "stream_max_retries = 0",
            "[features]",
            "hooks = false",
            "multi_agent = false",
        ]
        _atomic(home / "config.toml", ("\n".join(config) + "\n").encode())
        # Пустой локальный каталог подавляет discovery; он не заявляет
        # возможностей Luna, поскольку здесь не создаётся ни один turn.
        _atomic(home / "models.json", b'{"models":[]}')
        _atomic(home / "environments.toml", b"include_local = false\n")
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "CODEX_HOME": str(home),
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE": self.bridge_url,
            "RUST_LOG": "off",
            "OTEL_SDK_DISABLED": "true",
        }

    async def _native_refresh(self, request: web.Request) -> web.Response:
        async with self._refresh_lock:
            if self.owner is None or self._active is None or self._session is None:
                return web.json_response({"error": "invalid_grant"}, status=403)
            try:
                body = json.loads(await bounded_native_body(request.content))
                journal = self.owner._for_run(self._active)
                auth = self.owner._attempt_auth(journal)
                body = validate_native_grant(
                    body, json.loads(auth.raw)["tokens"]["refresh_token"]
                )
                if self._sent:
                    raise NativeAuthError("refresh_replay_rejected")
                self.owner.begin_refresh(
                    self._active
                )  # До любого внешнего побочного эффекта.
                self._sent = True
                async with self._session.post(
                    self.proxy_url + "/v0/auth/native-refresh",
                    json=body,
                    headers={
                        "Authorization": "Bearer " + self._proxy_token,
                        "X-Native-Auth-Run": self._active,
                    },
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400:
                        raise NativeAuthError("refresh_redirect_rejected")
                    raw = await bounded_native_body(response.content)
                    self._refresh_ok = response.status == 200
                    return web.Response(
                        status=response.status,
                        body=raw,
                        content_type="application/json",
                    )
            except (
                CredentialError,
                NativeAuthError,
                ValueError,
                UnicodeError,
                RecursionError,
            ):
                self._bridge_error = "native_refresh_rejected"
                return web.json_response({"error": "invalid_grant"}, status=403)
            except (aiohttp.ClientError, OSError, TimeoutError):
                self._bridge_error = "native_refresh_unavailable"
                return web.json_response(
                    {"error": "temporarily_unavailable"}, status=502
                )

    async def _native_account(self, home: Path, env: dict[str, str]) -> dict:
        await self._version(home, env)
        self._process = await asyncio.create_subprocess_exec(
            self.binary,
            "app-server",
            "--listen",
            "stdio://",
            cwd=home,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=MAX_NATIVE_BODY,
        )
        assert self._process.stdout and self._process.stdin and self._process.stderr
        rpc = Rpc(self._process.stdout, self._process.stdin, self._ignore_event)
        self._tasks = [
            asyncio.create_task(rpc.read()),
            asyncio.create_task(self._discard(self._process.stderr)),
        ]
        await rpc.call(
            "initialize",
            {"clientInfo": {"name": "eval-stand-auth", "version": "0.1.0"}},
        )
        await rpc.send({"method": "initialized", "params": {}})
        return await rpc.call(
            "getAuthStatus", {"includeToken": False, "refreshToken": True}
        )

    async def _version(self, home: Path, env: dict[str, str]) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self.binary,
            "--version",
            cwd=home,
            env=env,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._process.stdout
        reader = asyncio.create_task(read_bounded(self._process.stdout, 1024))
        self._tasks.append(reader)
        async with asyncio.timeout(10):
            await self._process.wait()
            output, truncated = await reader
        if (
            self._process.returncode != 0
            or truncated
            or output.strip() != f"codex-cli {VERSION}".encode()
        ):
            raise NativeAuthError("native_auth_version_mismatch")

    @staticmethod
    async def _ignore_event(event: dict) -> None:
        pass

    @staticmethod
    async def _discard(reader: asyncio.StreamReader) -> None:
        while await reader.read(4096):
            pass

    async def _stop_active(self) -> bool:
        stopped = False
        try:
            known_stopped = True
            if self._process is not None:
                # Процесс был запущен этим владельцем с новой сессией. Сначала
                # останавливаем её по сохранённому handle: живой Codex может
                # запретить чтение environ. Затем независимо сверяем nonce.
                known_stopped = await stop_session(self._process, timeout=5)
            if self.guard is not None and self._active is not None:
                record = self.guard._load(self._active)
                if record is not None:
                    await self.guard._stop(
                        self.root / "attempts" / self._active,
                        record["owner_nonce"],
                        time.monotonic() + 15,
                    )
                elif self._process is not None or (
                    self.owner is not None
                    and self.owner._journal().get("state")
                    not in {"idle", "preparing", "prepared", "cleaning"}
                ):
                    raise NativeAuthError("auth_process_ownership_unconfirmed")
            if self._process is not None:
                await asyncio.wait_for(self._process.wait(), 3)
            stopped = known_stopped
        except (RecoveryError, NativeAuthError, OSError, TimeoutError):
            pass
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            if stopped:
                self._process = None
            self._resource_idle = stopped
        return stopped

    async def close(self) -> None:
        async with self._lock:
            await self._stop_active()
            if self._web_runner is not None:
                await self._web_runner.cleanup()
                self._web_runner = None
            if self._session is not None:
                await self._session.close()
                self._session = None
            if self.owner is not None:
                self.owner.close()
                self.owner = None
