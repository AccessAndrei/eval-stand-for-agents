"""Только вымышленные credentials, fake native RPC и локальные HTTP-серверы."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from services.harness import native_auth as harness_auth
from services.harness.credentials import CredentialError, CredentialOwner, _atomic
from services.harness.native_auth import NativeAuthController
from services.harness.recovery import HarnessRecovery, RecoveryError
from services.llm_proxy import native_auth as proxy_auth
from services.llm_proxy.native_auth import (
    NATIVE_CLIENT_ID,
    NativeAuthError,
    NativeAuthReader,
)

CONTROL = "offline-only-control-token-000000000000"


def fake_auth(generation: int = 1, *, expires: int | None = None) -> bytes:
    expiry = int(time.time()) + 3600 if expires is None else expires
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
        .decode()
        .rstrip("=")
    )
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": "offline-id",
                "access_token": f"e30.{payload}.offline-signature-{generation}",
                "refresh_token": f"offline-refresh-{generation}",
                "account_id": "offline-account",
            },
            "last_refresh": "2026-11-06T12:00:00Z",
        }
    ).encode()


@pytest.fixture
def stores(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    root = tmp_path / "auth"
    with CredentialOwner(root, repository) as owner:
        owner.initialize(fake_auth())
    return root, repository


class FakeGuard(HarnessRecovery):
    stopped: list[str]

    def __init__(self, state: Path) -> None:
        super().__init__(state)
        self.stopped = []

    async def _stop(self, home: Path, nonce: str, deadline: float) -> None:
        assert self._load(home.name)["owner_nonce"] == nonce
        self.stopped.append(home.name)


def grant(raw: bytes) -> dict:
    return {
        "grant_type": "refresh_token",
        "client_id": NATIVE_CLIENT_ID,
        "refresh_token": json.loads(raw)["tokens"]["refresh_token"],
    }


def account_status() -> dict:
    return {"authMethod": "chatgpt", "authToken": None, "requiresOpenaiAuth": True}


def test_read_only_status_and_headers_never_publish_secrets(
    stores: tuple[Path, Path],
) -> None:
    root, _ = stores
    reader = NativeAuthReader(root, CONTROL)
    status = reader.status()
    assert status["ready"] and status["auth_ready"]
    assert status["expires_at"] > time.time()
    assert "offline-" not in json.dumps(status)
    headers = reader.inference_headers()
    assert headers["Authorization"].startswith("Bearer e30.")
    assert headers["ChatGPT-Account-ID"] == "offline-account"
    _atomic(root / "current.json", fake_auth(expires=1))
    assert reader.status()["reason"] == "auth_refresh_required"
    with pytest.raises(NativeAuthError, match="auth_refresh_required"):
        reader.inference_headers()


def test_proxy_refresh_checks_active_attempt_and_does_not_write_auth(
    stores: tuple[Path, Path],
) -> None:
    root, repository = stores

    async def scenario() -> None:
        calls = []

        async def fake_upstream(body: dict) -> tuple[int, bytes]:
            calls.append(body)
            return 200, b'{"access_token":"offline-new"}'

        reader = NativeAuthReader(root, CONTROL, refresh_transport=fake_upstream)
        with CredentialOwner(root, repository) as owner:
            home = owner.prepare_attempt("auth-one")
            body = grant((home / "auth.json").read_bytes())
            with pytest.raises(NativeAuthError, match="unknown_auth_attempt"):
                await reader.refresh("auth-one", body)
            owner.begin_refresh("auth-one")
            before = {
                path: path.read_bytes()
                for path in (
                    home / "auth.json",
                    root / "current.json",
                    root / "journal.json",
                )
            }
            for bad in (
                {**body, "client_id": "foreign"},
                {**body, "refresh_token": "foreign"},
                {**body, "upstream": "https://foreign.test"},
            ):
                with pytest.raises(NativeAuthError):
                    await reader.refresh("auth-one", bad)
            assert calls == []
            assert await reader.refresh("auth-one", body) == (
                200,
                b'{"access_token":"offline-new"}',
            )
            with pytest.raises(NativeAuthError, match="refresh_replay_rejected"):
                await reader.refresh("auth-one", body)
            assert len(calls) == 1
            assert all(path.read_bytes() == value for path, value in before.items())

    asyncio.run(scenario())


async def prepared_scenario(
    root: Path,
    repository: Path,
    *,
    response_status: int = 200,
    write_native: bool = True,
    force: bool = True,
) -> tuple[dict, list, list, bytes]:
    calls, native_calls = [], []
    rotated = fake_auth(2)

    async def fake_upstream(body: dict) -> tuple[int, bytes]:
        assert json.loads((root / "journal.json").read_bytes())["state"] == "refreshing"
        calls.append(body)
        if response_status != 200:
            return response_status, b'{"error":"invalid_grant"}'
        return 200, json.dumps(json.loads(rotated)["tokens"]).encode()

    reader = NativeAuthReader(root, CONTROL, refresh_transport=fake_upstream)
    app = web.Application(client_max_size=65_536)
    app.router.add_post("/v0/auth/native-refresh", reader.refresh_handler)

    async def fake_native(home: Path, env: dict[str, str]) -> dict:
        native_calls.append(home)
        assert "OPENAI_API_KEY" not in env
        assert (home / "auth.json").exists()
        assert "include_local = false" in (home / "environments.toml").read_text()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                env["CODEX_REFRESH_TOKEN_URL_OVERRIDE"],
                json=grant((home / "auth.json").read_bytes()),
            ) as response:
                await response.read()
                if response.status == 200 and write_native:
                    _atomic(home / "auth.json", rotated)
        # Нативный RPC может вернуть старый метод после неуспешного refresh.
        return account_status()

    async with TestServer(app) as server:
        controller = NativeAuthController(
            root,
            str(server.make_url("")),
            CONTROL,
            repository,
            bridge_port=0,
            native_runner=fake_native,
            guard_factory=FakeGuard,
        )
        await controller.start()
        try:
            result = await controller.prepare(force_refresh=force)
            assert controller.guard.stopped or not native_calls
            assert await controller.wait_idle()
            if not result["ready"]:
                repeated = await controller.prepare()
                assert not repeated["ready"]
                assert len(native_calls) == 1
        finally:
            await controller.close()
    return result, calls, native_calls, rotated


def test_native_rotation_is_published_before_cleanup(stores: tuple[Path, Path]) -> None:
    root, repository = stores
    result, calls, native_calls, rotated = asyncio.run(
        prepared_scenario(root, repository)
    )
    assert result["ready"] and result["refreshed"]
    assert len(calls) == len(native_calls) == 1
    assert (root / "current.json").read_bytes() == rotated
    assert not native_calls[0].exists()
    assert json.loads((root / "journal.json").read_bytes())["state"] == "idle"


def test_fresh_access_fast_path_does_not_spawn_native_or_refresh(
    stores: tuple[Path, Path],
) -> None:
    result, calls, native_calls, _ = asyncio.run(
        prepared_scenario(*stores, force=False)
    )
    assert result["ready"] and result["refreshed"] is False
    assert calls == native_calls == []


def test_expired_access_refreshes_instead_of_permanent_rejection(
    stores: tuple[Path, Path],
) -> None:
    root, repository = stores
    _atomic(root / "current.json", fake_auth(expires=1))
    result, calls, _, rotated = asyncio.run(
        prepared_scenario(root, repository, force=False)
    )
    assert result["ready"] and result["refreshed"]
    assert len(calls) == 1
    assert (root / "current.json").read_bytes() == rotated


@pytest.mark.parametrize("status,write", [(400, True), (200, False)])
def test_refresh_failure_or_missing_native_save_blocks_without_rollback(
    stores: tuple[Path, Path],
    status: int,
    write: bool,
) -> None:
    root, repository = stores
    old = (root / "current.json").read_bytes()
    result, calls, native_calls, _ = asyncio.run(
        prepared_scenario(root, repository, response_status=status, write_native=write)
    )
    assert not result["ready"]
    assert result["reason"] == "auth_recovery_required"
    assert len(calls) == len(native_calls) == 1
    assert (root / "current.json").read_bytes() == old
    assert native_calls[0].exists()


def test_proxy_route_rejects_wrong_control_token_before_body(
    stores: tuple[Path, Path],
) -> None:
    async def scenario() -> None:
        async def forbidden(body: dict) -> tuple[int, bytes]:
            pytest.fail("Неавторизованный запрос не должен доходить до upstream")

        app = web.Application()
        reader = NativeAuthReader(stores[0], CONTROL, refresh_transport=forbidden)
        app.router.add_post("/v0/auth/native-refresh", reader.refresh_handler)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            async with session.post(
                server.make_url("/v0/auth/native-refresh"),
                json={"refresh_token": "foreign"},
            ) as response:
                assert response.status == 401

    asyncio.run(scenario())


def test_native_rpc_never_starts_turn_or_exports_token(
    stores: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        methods, commands = [], []

        class FakeProcess:
            def __init__(self, version: bool) -> None:
                self.stdout, self.stderr = (
                    asyncio.StreamReader(),
                    asyncio.StreamReader(),
                )
                self.stdin = object()
                self.returncode = 0
                if version:
                    self.stdout.feed_data(b"codex-cli 0.159.3\n")
                    self.stdout.feed_eof()
                self.stderr.feed_eof()

            async def wait(self) -> int:
                return 0

        async def spawn(*args: str, **kwargs: object) -> FakeProcess:
            commands.append((args, kwargs))
            return FakeProcess(args[-1] == "--version")

        async def stop_known(process: FakeProcess, timeout: float = 5) -> bool:
            return True

        class FakeRpc:
            def __init__(self, *args: object) -> None:
                pass

            async def read(self) -> None:
                await asyncio.Event().wait()

            async def call(self, method: str, params: dict) -> dict:
                methods.append((method, params))
                return account_status() if method == "getAuthStatus" else {}

            async def send(self, message: dict) -> None:
                methods.append((message["method"], message["params"]))

        monkeypatch.setattr(harness_auth.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(harness_auth, "stop_session", stop_known)
        monkeypatch.setattr(harness_auth, "Rpc", FakeRpc)
        controller = NativeAuthController(
            stores[0],
            "http://llm-proxy:8000",
            CONTROL,
            stores[1],
            bridge_port=0,
            guard_factory=FakeGuard,
        )
        await controller.start()
        try:
            result = await controller.prepare(force_refresh=True)
            assert result["ready"] and not result["refreshed"]
        finally:
            await controller.close()
        assert [method for method, _ in methods] == [
            "initialize",
            "initialized",
            "getAuthStatus",
        ]
        assert methods[-1][1] == {"includeToken": False, "refreshToken": True}
        assert commands[0][0][-1] == "--version"
        assert commands[1][0][1:] == ("app-server", "--listen", "stdio://")
        assert all(kwargs["start_new_session"] for _, kwargs in commands)

    asyncio.run(scenario())


def test_recovery_publishes_saved_rotation_after_process_proof(
    stores: tuple[Path, Path],
) -> None:
    root, repository = stores
    with CredentialOwner(root, repository) as owner:
        home = owner.prepare_attempt("auth-crash")
        for name in ("config.toml", "environments.toml"):
            _atomic(home / name, b"# test\n")
        guard = FakeGuard(root / "attempts")
        guard.begin_attempt("auth-crash", home)
        owner.begin_refresh("auth-crash")
        _atomic(home / "auth.json", fake_auth(2))

    async def scenario() -> None:
        controller = NativeAuthController(
            root,
            "http://llm-proxy:8000",
            CONTROL,
            repository,
            bridge_port=0,
            guard_factory=FakeGuard,
        )
        await controller.start()
        try:
            assert controller.guard.stopped == ["auth-crash"]
            assert controller.status()["ready"]
            assert not home.exists()
            assert (
                json.loads((root / "current.json").read_bytes())["tokens"][
                    "refresh_token"
                ]
                == "offline-refresh-2"
            )
        finally:
            await controller.close()

    asyncio.run(scenario())


def test_unconfirmed_native_stop_never_publishes_or_deletes(
    stores: tuple[Path, Path],
) -> None:
    root, repository = stores
    with CredentialOwner(root, repository) as owner:
        home = owner.prepare_attempt("auth-crash")
        for name in ("config.toml", "environments.toml"):
            _atomic(home / name, b"# test\n")
        guard = FakeGuard(root / "attempts")
        guard.begin_attempt("auth-crash", home)
        owner.begin_refresh("auth-crash")
        _atomic(home / "auth.json", fake_auth(2))

    class FailedGuard(FakeGuard):
        async def _stop(self, home: Path, nonce: str, deadline: float) -> None:
            raise RecoveryError("not_stopped")

    async def scenario() -> None:
        controller = NativeAuthController(
            root,
            "http://llm-proxy:8000",
            CONTROL,
            repository,
            bridge_port=0,
            guard_factory=FailedGuard,
        )
        await controller.start()
        try:
            assert not controller.status()["ready"]
            assert not await controller.wait_idle()
            assert home.exists()
            assert (
                json.loads((root / "current.json").read_bytes())["tokens"][
                    "refresh_token"
                ]
                == "offline-refresh-1"
            )
        finally:
            await controller.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["success", "redirect", "oversize", "timeout"])
def test_native_transport_has_one_fixed_bounded_nonredirecting_request(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    calls = []
    raw = b'{"access_token":"offline-returned"}'

    class Content:
        async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
            yield b"x" * 65_537 if mode == "oversize" else raw

    class Response:
        status = 302 if mode == "redirect" else 200
        content = Content()

        async def __aenter__(self) -> "Response":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    class Session:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["timeout"].total == 30
            assert kwargs["trust_env"] is False

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def post(self, url: str, **kwargs: object) -> Response:
            calls.append((url, kwargs))
            assert url == "https://auth.openai.com/oauth/token"
            assert kwargs["allow_redirects"] is False
            if mode == "timeout":
                raise TimeoutError
            return Response()

    monkeypatch.setattr(proxy_auth.aiohttp, "ClientSession", Session)
    if mode == "success":
        assert asyncio.run(proxy_auth.forward_native_refresh(grant(fake_auth()))) == (
            200,
            raw,
        )
    else:
        with pytest.raises((NativeAuthError, TimeoutError)):
            asyncio.run(proxy_auth.forward_native_refresh(grant(fake_auth())))
    assert len(calls) == 1


def test_only_one_native_controller_writes_but_proxy_can_read(
    stores: tuple[Path, Path],
) -> None:
    async def scenario() -> None:
        controller = NativeAuthController(
            stores[0],
            "http://llm-proxy:8000",
            CONTROL,
            stores[1],
            bridge_port=0,
            guard_factory=FakeGuard,
        )
        await controller.start()
        try:
            with pytest.raises(CredentialError, match="auth_owner_busy"):
                with CredentialOwner(*stores):
                    pytest.fail("Только один Python-владелец может писать авторизацию")
            assert NativeAuthReader(stores[0], CONTROL).status()["ready"]
        finally:
            await controller.close()
        with CredentialOwner(*stores):
            pass

    asyncio.run(scenario())


def test_wait_idle_waits_for_native_preparation_without_cancelling_it(
    stores: tuple[Path, Path],
) -> None:
    async def scenario() -> None:
        started, finish = asyncio.Event(), asyncio.Event()

        async def native(home: Path, env: dict[str, str]) -> dict:
            started.set()
            await finish.wait()
            return account_status()

        controller = NativeAuthController(
            stores[0],
            "http://llm-proxy:8000",
            CONTROL,
            stores[1],
            bridge_port=0,
            native_runner=native,
            guard_factory=FakeGuard,
        )
        await controller.start()
        task = asyncio.create_task(controller.prepare(force_refresh=True))
        try:
            await started.wait()
            assert controller.is_busy
            assert not await controller.wait_idle(timeout_s=0.01)
            assert not task.done()
            finish.set()
            assert (await task)["ready"]
            assert await controller.wait_idle()
            assert not controller.is_busy
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
            await controller.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "known_stopped,guard_confirmed", [(True, True), (False, True), (True, False)]
)
def test_known_native_session_stops_before_guard_inspects_hardened_process(
    stores: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    known_stopped: bool,
    guard_confirmed: bool,
) -> None:
    root, repository = stores
    order = []
    original = (root / "current.json").read_bytes()

    class NativeProcess:
        async def wait(self) -> int:
            order.append("reap")
            return 0

    native_process = NativeProcess()

    async def stop_known(process: NativeProcess, timeout: float = 5) -> bool:
        assert process is native_process
        assert timeout == 5
        order.append("known_session_stop")
        return known_stopped

    class HardenedGuard(FakeGuard):
        async def _stop(self, home: Path, nonce: str, deadline: float) -> None:
            # При старом порядке попытка прочитать environ живого процесса
            # опережала остановку по известному Process/session handle.
            assert order == ["known_session_stop"]
            order.append("nonce_guard")
            if not guard_confirmed:
                raise RecoveryError("process_inspection_failed")

    monkeypatch.setattr(harness_auth, "stop_session", stop_known)
    with CredentialOwner(root, repository) as owner:
        home = owner.prepare_attempt("auth-known-stop")
        for name in ("config.toml", "environments.toml"):
            _atomic(home / name, b"# offline test\n")
        guard = HardenedGuard(root / "attempts")
        guard.begin_attempt("auth-known-stop", home)
        owner.begin_refresh("auth-known-stop")
        _atomic(home / "auth.json", fake_auth(2))
        controller = NativeAuthController(
            root, "http://llm-proxy:8000", CONTROL, repository
        )
        controller.owner, controller.guard = owner, guard
        controller._active = "auth-known-stop"
        controller._process = native_process
        result = asyncio.run(controller._stop_active())
        assert result is (known_stopped and guard_confirmed)
        assert controller._resource_idle is result
        assert order[:2] == ["known_session_stop", "nonce_guard"]
        assert home.exists()
        assert (root / "current.json").read_bytes() == original
        assert (controller._process is None) is result
