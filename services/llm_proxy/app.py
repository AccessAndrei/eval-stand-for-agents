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
from services.common.storage import atomic_json
from services.llm_proxy.budget import BudgetError, Ledger, RunBinding, run_path
from services.llm_proxy.profiles import (
    MOCK_BUDGET,
    Profile,
    ProfileError,
    load_profiles,
    mock_input_tokens,
)
from services.llm_proxy.providers import ADAPTERS, ProviderAuthError, api_key
from services.llm_proxy.stream import SSEObserver

CLOSE_TIMEOUT = 10


def _reference(request: web.Request) -> str:
    if "proxy_ref" in request.match_info:
        return request.match_info["proxy_ref"]
    return request.match_info["run_id"]


def _belongs(request: web.Request, ledger: Ledger) -> bool:
    return ("proxy_ref" in request.match_info) == (ledger.binding is not None)


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
        self.bindings: dict[RunBinding, str] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.storage_failed = False
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_dir.chmod(0o700)
        for path in state_dir.glob("*.json"):
            data = json.loads(path.read_text())
            ledger = Ledger(path, data["profile"])
            if not ledger.data["closed"]:
                ledger.fail("interrupted")
                ledger.close()
            self._remember(path.stem, ledger)

    def _remember(self, reference: str, ledger: Ledger) -> None:
        if ledger.binding is not None:
            if self.bindings.setdefault(ledger.binding, reference) != reference:
                raise ValueError("duplicate_run_binding")
        self.ledgers[reference] = ledger

    def authorize(self, request: web.Request, token: str | None = None) -> None:
        if not valid_bearer(
            request.headers.get("Authorization", ""), token or self.token
        ):
            raise web.HTTPUnauthorized()

    def ledger(self, request: web.Request) -> Ledger:
        reference = _reference(request)
        run_path(self.state_dir, reference)
        ledger = self.ledgers.get(reference)
        if ledger is None or not _belongs(request, ledger):
            raise web.HTTPNotFound()
        return ledger

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
        if self.storage_failed:
            raise web.HTTPServiceUnavailable(text="proxy_state_unavailable")
        return web.json_response({"status": "ready"})

    async def preflight(self, request: web.Request) -> web.Response:
        self.authorize(request)
        profile = await self.verify(request.match_info["profile"])
        return web.json_response({"compatible": True, **profile.public()})

    def status(self, run_id: str, ledger: Ledger) -> dict[str, Any]:
        snapshot = ledger.data.get("profile_snapshot")
        return {
            **(ledger.binding.public() if ledger.binding else {"run_id": run_id}),
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

    def _existing_run(
        self, path: Path, profile: str, binding: RunBinding | None
    ) -> Ledger | None:
        if self.storage_failed:
            raise web.HTTPServiceUnavailable(text="proxy_state_unavailable")
        if path.with_suffix(".closed").exists():
            raise web.HTTPConflict(text="run_closed")
        ledger = self.ledgers.get(path.stem)
        if ledger is None and path.exists():
            self.storage_failed = True
            raise web.HTTPServiceUnavailable(text="proxy_state_unavailable")
        if ledger is not None and (
            ledger.data["profile"] != profile or ledger.binding != binding
        ):
            raise web.HTTPConflict(text="run_conflict")
        if binding is not None and self.bindings.get(binding, path.stem) != path.stem:
            raise web.HTTPConflict(text="run_conflict")
        return ledger

    async def open_run(self, request: web.Request) -> web.Response:
        self.authorize(request)
        body = await request.json()
        if not isinstance(body, dict) or "provider_profile" not in body:
            raise ValueError("invalid_open_request")
        name = body.pop("provider_profile")
        if "proxy_ref" in request.match_info:
            binding = RunBinding.read(body)
        elif body:
            raise ValueError("invalid_open_request")
        else:
            binding = None
        reference = _reference(request)
        path = run_path(self.state_dir, reference)
        ledger = self._existing_run(path, name, binding)
        if ledger is None:
            profile = await self.verify(name)
            # После проверки профиля запрос мог быть закрыт или уже принят.
            ledger = self._existing_run(path, name, binding)
            if ledger is None:
                try:
                    ledger = Ledger(
                        path, name, profile_snapshot=profile.snapshot(), binding=binding
                    )
                except OSError as error:
                    # После неясного исхода записи только перезапуск сверит журнал.
                    self.storage_failed = True
                    raise web.HTTPServiceUnavailable(
                        text="proxy_state_unavailable"
                    ) from error
                self._remember(reference, ledger)
        result = self.status(reference, ledger)
        if grant := ledger.inference_token:
            result["inference_token"] = grant
        return web.json_response(result)

    async def get_run(self, request: web.Request) -> web.Response:
        self.authorize(request)
        ledger = self.ledger(request)
        return web.json_response(self.status(_reference(request), ledger))

    async def close_run(self, request: web.Request) -> web.Response:
        self.authorize(request)
        reference = _reference(request)
        path = run_path(self.state_dir, reference)
        if "proxy_ref" in request.match_info and reference not in self.ledgers:
            # Даже если открытие ещё проверяет профиль, оно больше не выдаст grant.
            try:
                atomic_json(path.with_suffix(".closed"), {"version": "v1"})
            except OSError as error:
                self.storage_failed = True
                raise web.HTTPServiceUnavailable(
                    text="proxy_state_unavailable"
                ) from error
            return web.json_response({"error": "proxy_ref_fenced"}, status=404)
        ledger = self.ledger(request)
        try:
            ledger.close()
        finally:
            if task := self.tasks.get(reference):
                if not task.cancelling():
                    task.cancel()
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        return web.json_response(self.status(reference, ledger))

    async def responses(self, request: web.Request) -> web.StreamResponse:
        reference = _reference(request)
        ledger = self.ledgers.get(reference)
        if ledger is None or not _belongs(request, ledger):
            raise web.HTTPUnauthorized()
        if ledger.binding is None:
            self.authorize(request, ledger.data["token"])
        elif not ledger.matches_v1_grant(request.headers.get("Authorization", "")):
            raise web.HTTPUnauthorized()
        ledger.check_open()
        if reference in self.tasks:
            raise web.HTTPConflict(text="busy")
        snapshot = ledger.data.get("profile_snapshot")
        if not snapshot:
            ledger.fail("profile_snapshot_missing")
            raise ProfileError("profile_snapshot_missing")
        profile = Profile(**snapshot)
        try:
            await self.check_profile(profile)
        except ProviderAuthError:
            ledger.check_open()
            ledger.fail("auth_error")
            raise
        body = await request.json()
        # После ожидания тела запрос ещё не правомочен менять бюджет или итог.
        ledger.check_open()
        if reference in self.tasks:
            raise web.HTTPConflict(text="busy")
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
        request_id = (
            ledger.reserve_observed(profile.output_limit)
            if profile.budget_mode == "observed_usage"
            else ledger.reserve(mock_input_tokens(bounded), profile.output_limit)
        )
        self.tasks[reference] = asyncio.current_task()
        try:
            return await self.forward(request, profile, bounded, ledger, request_id)
        finally:
            if self.tasks.get(reference) is asyncio.current_task():
                self.tasks.pop(reference, None)

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
        finished = False
        downstream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        try:
            upstream = await self.post_model(profile, body, _reference(request))
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
                finished = True
            # Codex может начать следующий запрос сразу после completed;
            # запрос upstream и резерв финализируются до отправки этого события.
            self.tasks.pop(_reference(request), None)
            await downstream.write(ending)
            await downstream.write_eof()
            return downstream
        except asyncio.CancelledError:
            if not finished:
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
            if not finished and not settled:
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
    app.router.add_post("/v1/runs/{proxy_ref}", proxy.open_run)
    app.router.add_get("/v1/runs/{proxy_ref}", proxy.get_run)
    app.router.add_post("/v1/runs/{proxy_ref}/close", proxy.close_run)
    app.router.add_post("/v1/runs/{proxy_ref}/responses", proxy.responses)
    return app
