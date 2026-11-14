"""Авторизованный Responses-прокси без повторов и обходного маршрута."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from services.common.http import valid_bearer
from services.llm_proxy.budget import BudgetError, Ledger, run_path
from services.llm_proxy.profiles import (
    MOCK_BUDGET,
    Profile,
    ProfileError,
    load_profiles,
    mock_input_tokens,
)
from services.llm_proxy.providers import ADAPTERS, ProviderAuthError, api_key
from services.llm_proxy.stream import SSEObserver


class Proxy:
    def __init__(
        self,
        profiles: dict[str, Profile],
        state_dir: Path,
        token: str,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if len(token) < 24:
            raise ValueError("Требуется отдельный управляющий токен")
        self.profiles, self.state_dir, self.token = profiles, state_dir, token
        self.environ = os.environ if environ is None else environ
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
        profile.compatible()
        api_key(profile, self.environ)

    async def health(self, request: web.Request) -> web.Response:
        self.authorize(request)
        return web.json_response({"status": "ready"})

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
        except ProviderAuthError:
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
                async for chunk in ADAPTERS[profile.protocol].events(upstream):
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
        except ProviderAuthError:
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
        outgoing = ADAPTERS[profile.protocol].prepare(
            profile, body, run_id, api_key(profile, self.environ)
        )
        return await self.session.post(
            outgoing.url,
            json=outgoing.body,
            headers=outgoing.headers,
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
    except ProviderAuthError:
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
    environ: Mapping[str, str] | None = None,
) -> web.Application:
    proxy = Proxy(
        load_profiles(profiles_path, environ),
        state_dir,
        token,
        environ=environ,
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
    return app
