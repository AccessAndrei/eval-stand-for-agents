"""Авторизованный Responses-прокси без повторов и обходного маршрута."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from services.common.http import valid_bearer
from services.harness.credentials import CredentialError
from services.llm_proxy.budget import BudgetError, Ledger, run_path
from services.llm_proxy.native_auth import NativeAuthError, NativeAuthReader
from services.llm_proxy.profiles import (
    MOCK_BUDGET,
    Profile,
    ProfileError,
    load_profiles,
    mock_input_tokens,
)
from services.llm_proxy.stream import SSEObserver

AUTH_CALLBACK_URL = "http://harness:8083/v0/auth/prepare"


class Proxy:
    def __init__(
        self,
        profiles: dict[str, Profile],
        state_dir: Path,
        token: str,
        *,
        auth_root: Path | None = None,
        auth_callback_url: str | None = None,
    ) -> None:
        if len(token) < 24:
            raise ValueError("Требуется отдельный управляющий токен")
        self.profiles, self.state_dir, self.token = profiles, state_dir, token
        self.auth = NativeAuthReader(auth_root, token) if auth_root else None
        if auth_root and auth_callback_url != AUTH_CALLBACK_URL:
            raise ValueError("invalid_auth_callback_url")
        self.auth_callback_url = auth_callback_url
        self.ledgers: dict[str, Ledger] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_dir.chmod(0o700)
        for path in state_dir.glob("*.json"):
            data = json.loads(path.read_text())
            ledger = Ledger(path, data["profile"])
            if not ledger.data["closed"]:
                ledger.fail("interrupted")
                ledger.close()
            self.ledgers[path.stem] = ledger

    def authorize(self, request: web.Request, token: str | None = None) -> None:
        if not valid_bearer(
            request.headers.get("Authorization", ""), token or self.token
        ):
            raise web.HTTPUnauthorized()

    def ledger(self, request: web.Request) -> Ledger:
        run_id = request.match_info["run_id"]
        run_path(self.state_dir, run_id)
        if run_id not in self.ledgers:
            raise web.HTTPNotFound()
        return self.ledgers[run_id]

    async def verify(self, name: str) -> Profile:
        profile = self.profiles.get(name)
        if profile is None:
            raise ProfileError("unknown_profile")
        await self.check_profile(profile)
        if profile.budget_mode == "observed_usage":
            return profile
        async with self.session.get(
            profile.endpoint.rstrip("/") + "/budget-capabilities",
            timeout=aiohttp.ClientTimeout(total=min(profile.timeout_s, 10)),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ProfileError("budget_capability_unavailable")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                raw.extend(chunk)
                if len(raw) > 65_536:
                    raise ProfileError("invalid_budget_capability")
            proof = json.loads(raw)
            if (
                proof.get("mechanism") != MOCK_BUDGET
                or proof.get("enforces_max_output_tokens") is not True
                or profile.model not in proof.get("models", [])
            ):
                raise ProfileError("token_bound_unverified")
        return profile

    async def check_profile(self, profile: Profile) -> None:
        try:
            profile.compatible()
        except ProfileError as error:
            if str(error) != "live_authorization_not_ready":
                raise
        await self.ensure_auth(profile)
        profile.compatible(
            live_ready=(
                profile.auth == "chatgpt"
                and self.auth is not None
                and self.auth.status()["ready"]
            )
        )

    async def ensure_auth(self, profile: Profile, *, force: bool = False) -> None:
        if profile.auth != "chatgpt":
            return
        if self.auth is None:
            raise NativeAuthError("live_authorization_not_ready")
        state = self.auth.status()
        if force or state["reason"] == "auth_refresh_required":
            if not self.auth_callback_url:
                raise NativeAuthError("auth_refresh_unavailable")
            async with self.session.post(
                self.auth_callback_url,
                json={"force_refresh": True},
                headers={"Authorization": "Bearer " + self.token},
                timeout=aiohttp.ClientTimeout(total=95),
                allow_redirects=False,
            ) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise NativeAuthError("auth_refresh_unavailable")
                if response.status != 200 or json.loads(raw).get("ready") is not True:
                    raise NativeAuthError("auth_refresh_failed")
            state = self.auth.status()
        if not state["ready"]:
            raise NativeAuthError("live_authorization_not_ready")

    async def health(self, request: web.Request) -> web.Response:
        self.authorize(request)
        return web.json_response(
            {
                "status": "ready",
                "live_auth_ready": self.auth.status()["ready"] if self.auth else False,
            }
        )

    async def preflight(self, request: web.Request) -> web.Response:
        self.authorize(request)
        profile = await self.verify(request.match_info["profile"])
        return web.json_response({"compatible": True, **profile.public()})

    def status(self, run_id: str, ledger: Ledger) -> dict[str, Any]:
        snapshot = ledger.data.get("profile_snapshot")
        return {
            "run_id": run_id,
            "provider_profile": ledger.data["profile"],
            "model": snapshot["model"] if snapshot else None,
            "provider_snapshot": snapshot,
            "profile_unknown_reason": None
            if snapshot
            else "legacy_run_without_profile_snapshot",
            "tokens": ledger.tokens(),
            "closed": ledger.data["closed"],
            "error": ledger.data["error"],
            "requests": ledger.data["requests"],
            "upstream_failure": ledger.data.get("upstream_failure"),
        }

    async def open_run(self, request: web.Request) -> web.Response:
        self.authorize(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"provider_profile"}:
            raise ValueError("invalid_open_request")
        run_id, name = request.match_info["run_id"], body["provider_profile"]
        path = run_path(self.state_dir, run_id)
        async with self.lock:
            ledger = self.ledgers.get(run_id)
            if ledger is not None and ledger.data["profile"] != name:
                raise web.HTTPConflict(text="run_conflict")
            if ledger is None:
                profile = await self.verify(name)
                ledger = Ledger(path, name, profile_snapshot=profile.snapshot())
                self.ledgers[run_id] = ledger
            result = self.status(run_id, ledger)
            result["inference_token"] = ledger.data["token"]
        return web.json_response(result)

    async def get_run(self, request: web.Request) -> web.Response:
        self.authorize(request)
        ledger = self.ledger(request)
        return web.json_response(self.status(request.match_info["run_id"], ledger))

    async def close_run(self, request: web.Request) -> web.Response:
        self.authorize(request)
        ledger = self.ledger(request)
        run_id = request.match_info["run_id"]
        ledger.close()
        if task := self.tasks.get(run_id):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return web.json_response(self.status(run_id, ledger))

    async def responses(self, request: web.Request) -> web.StreamResponse:
        if request.match_info["run_id"] not in self.ledgers:
            raise web.HTTPUnauthorized()
        ledger = self.ledger(request)
        self.authorize(request, ledger.data["token"])
        run_id = request.match_info["run_id"]
        ledger.check_open()
        if run_id in self.tasks:
            raise web.HTTPConflict(text="busy")
        snapshot = ledger.data.get("profile_snapshot")
        if not snapshot:
            ledger.fail("profile_snapshot_missing")
            raise ProfileError("profile_snapshot_missing")
        profile = Profile(**snapshot)
        try:
            await self.check_profile(profile)
        except (NativeAuthError, CredentialError):
            ledger.fail("auth_error")
            raise
        body = await request.json()
        if (
            not isinstance(body, dict)
            or body.get("model") != profile.model
            or body.get("stream") is not True
            or not isinstance(body.get("input"), list)
            or body.get("previous_response_id") is not None
        ):
            ledger.fail(
                "previous_response_id_unsupported"
                if isinstance(body, dict)
                and body.get("previous_response_id") is not None
                else "invalid_responses_request"
            )
            raise ValueError("invalid_responses_request")
        bounded = {**body, "max_output_tokens": profile.output_limit}
        # Auth и чтение тела отдают управление: другой запрос мог занять слот.
        ledger.check_open()
        if run_id in self.tasks:
            raise web.HTTPConflict(text="busy")
        request_id = (
            ledger.reserve_observed(profile.output_limit)
            if profile.budget_mode == "observed_usage"
            else ledger.reserve(mock_input_tokens(bounded), profile.output_limit)
        )
        self.tasks[run_id] = asyncio.current_task()
        try:
            return await self.forward(request, profile, bounded, ledger, request_id)
        finally:
            if self.tasks.get(run_id) is asyncio.current_task():
                self.tasks.pop(run_id, None)

    async def forward(
        self,
        request: web.Request,
        profile: Profile,
        body: dict[str, Any],
        ledger: Ledger,
        request_id: str,
    ) -> web.StreamResponse:
        observer, forwarded, ending = SSEObserver(), 0, bytearray()
        settled = False
        downstream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        try:
            upstream = await self.post_model(
                profile, body, request.match_info["run_id"]
            )
            if upstream.status == 401 and profile.auth == "chatgpt":
                # 401 до успешного ответа не означает начавшуюся генерацию.
                # Единственное повторение — после нативного refresh; не после SSE.
                upstream.release()
                await self.ensure_auth(profile, force=True)
                ledger.data["auth_retries"] = ledger.data.get("auth_retries", 0) + 1
                ledger.save()
                upstream = await self.post_model(
                    profile, body, request.match_info["run_id"]
                )
            async with upstream:
                if upstream.status != 200:
                    await self.record_rejection(upstream, ledger)
                    ledger.fail(
                        "auth_error"
                        if upstream.status in {401, 403}
                        else "provider_error"
                    )
                    return web.json_response({"error": "provider_error"}, status=502)
                await downstream.prepare(request)
                async for chunk in upstream.content.iter_chunked(16_384):
                    forwarded += len(chunk)
                    if forwarded > 8_388_608:
                        raise ValueError("stream_limit_exceeded")
                    observer.feed(chunk)
                    if observer.terminal:
                        ending.extend(chunk)
                    else:
                        await downstream.write(chunk)
                if observer.terminal is None or observer.buffer.strip():
                    raise ValueError("incomplete_response_stream")
                if observer.usage is None:
                    ledger.fail("upstream_usage_unconfirmed")
                else:
                    ledger.settle(request_id, observer.usage)
                    settled = True
                if observer.terminal != "response.completed":
                    ledger.fail("provider_error")
                # Codex может начать следующий запрос сразу после completed;
                # резерв и слот финализируются до отправки этого события.
                self.tasks.pop(request.match_info["run_id"], None)
                await downstream.write(ending)
                await downstream.write_eof()
                return downstream
        except asyncio.CancelledError:
            ledger.fail("cancelled")
            raise
        except (NativeAuthError, CredentialError):
            ledger.fail("auth_error")
            return web.json_response({"error": "auth_error"}, status=401)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ConnectionError,
            ValueError,
            BudgetError,
        ):
            if not settled:
                ledger.fail("transport_error")
            if not downstream.prepared:
                return web.json_response({"error": "transport_error"}, status=502)
            # Резерв сохранён; транспорт обрывается без повторного upstream.
            if request.transport is not None:
                request.transport.close()
            return downstream

    async def record_rejection(
        self, response: aiohttp.ClientResponse, ledger: Ledger
    ) -> None:
        """Сохранять только статус и известные имена параметров, без текста ответа."""
        raw = await response.content.read(65_536)
        text = raw.decode("utf-8", errors="replace").lower()
        parameters = (
            "model",
            "instructions",
            "store",
            "tools",
            "include",
            "reasoning",
            "text",
            "temperature",
            "top_p",
            "service_tier",
            "metadata",
            "max_output_tokens",
            "parallel_tool_calls",
            "prompt_cache_key",
            "safety_identifier",
            "truncation",
        )
        ledger.data["upstream_failure"] = {
            "http_status": response.status,
            "mentioned_parameters": [key for key in parameters if key in text],
        }
        ledger.save()

    async def post_model(
        self, profile: Profile, body: dict, run_id: str
    ) -> aiohttp.ClientResponse:
        headers = {"X-Eval-Run-Id": run_id}
        if profile.auth == "chatgpt":
            if self.auth is None:
                raise NativeAuthError("live_authorization_not_ready")
            headers.update(self.auth.inference_headers())
        return await self.session.post(
            profile.endpoint.rstrip("/") + "/responses",
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=profile.timeout_s),
            allow_redirects=False,
        )


PROXY_KEY = web.AppKey("proxy", Proxy)


@web.middleware
async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except ProfileError as error:
        return web.json_response({"error": str(error)}, status=422)
    except BudgetError as error:
        return web.json_response({"error": str(error)}, status=429)
    except (NativeAuthError, CredentialError):
        return web.json_response({"error": "auth_error"}, status=401)
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "invalid_request"}, status=400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"error": "provider_unavailable"}, status=503)


def create_app(
    profiles_path: Path,
    state_dir: Path,
    token: str,
    *,
    auth_root: Path | None = None,
    auth_callback_url: str | None = None,
) -> web.Application:
    proxy = Proxy(
        load_profiles(profiles_path),
        state_dir,
        token,
        auth_root=auth_root,
        auth_callback_url=auth_callback_url,
    )
    app = web.Application(middlewares=[errors], client_max_size=1_048_576)
    app[PROXY_KEY] = proxy

    async def lifecycle(application: web.Application):
        async with aiohttp.ClientSession(trust_env=False) as session:
            proxy.session = session
            yield
            for task in list(proxy.tasks.values()):
                task.cancel()
            await asyncio.gather(*list(proxy.tasks.values()), return_exceptions=True)
            for ledger in proxy.ledgers.values():
                ledger.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/health", proxy.health)
    app.router.add_get("/v0/profiles/{profile}", proxy.preflight)
    app.router.add_post("/v0/runs/{run_id}", proxy.open_run)
    app.router.add_get("/v0/runs/{run_id}", proxy.get_run)
    app.router.add_post("/v0/runs/{run_id}/close", proxy.close_run)
    app.router.add_post("/v0/runs/{run_id}/responses", proxy.responses)
    if proxy.auth is not None:
        app.router.add_post("/v0/auth/native-refresh", proxy.auth.refresh_handler)
    return app
