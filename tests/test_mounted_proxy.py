"""Привязка полномочий, бюджет и завершение запуска mounted-прокси."""

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, TestServer

from services.llm_proxy.app import Proxy, create_app
from services.llm_proxy.budget import BudgetError, Ledger, RunBinding
from services.llm_proxy.mock import MOCK_KEY, create_mock_app, encode_event
from services.llm_proxy.profiles import load_profiles

TOKEN = "mounted-proxy-test-management-token"
UPSTREAM_KEY = "mounted-proxy-fixture-only-key"
AUTH = {"Authorization": "Bearer " + TOKEN}
SESSION_A, SESSION_B = "a" * 32, "b" * 32
RUN_A, RUN_B = "1" * 32, "2" * 32


def _binding(
    session_id: str = SESSION_A, run_id: str = RUN_A, profile: str = "mock"
) -> dict[str, str]:
    return {
        "version": "v1",
        "session_id": session_id,
        "run_id": run_id,
        "provider_profile": profile,
    }


def _profiles(path: Path, endpoint: str) -> Path:
    profiles = json.loads(Path("config/profiles.json").read_text())
    profiles["mock"]["endpoint"] = endpoint
    profiles["api"].update(endpoint=endpoint, model="api-test-model", output_limit=256)
    path.write_text(json.dumps(profiles))
    return path


async def _open(client: TestClient, ref: str, body: dict[str, str]) -> dict[str, Any]:
    response = await client.post(f"/v1/runs/{ref}", headers=AUTH, json=body)
    assert response.status == 200, await response.text()
    result = await response.json()
    assert all(result[key] == value for key, value in body.items())
    return result


async def _infer(
    client: TestClient,
    ref: str,
    grant: str,
    *,
    model: str = "mock-codex",
    text: str = "",
    version: str = "v1",
) -> ClientResponse:
    return await client.post(
        f"/{version}/runs/{ref}/responses",
        headers={"Authorization": "Bearer " + grant},
        json={
            "model": model,
            "stream": True,
            "input": [{"role": "user", "content": text}],
        },
    )


def test_reference_and_binding_are_single_owner_across_versions_and_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        upstream = create_mock_app()
        async with TestServer(upstream) as origin:
            path = _profiles(tmp_path / "profiles.json", str(origin.make_url("")))
            state = tmp_path / "proxy"
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                grant = await _open(client, "opaque-a", _binding())
                old = grant["inference_token"]
                assert (await _open(client, "opaque-a", _binding()))[
                    "inference_token"
                ] == old
                persisted = (state / "opaque-a.json").read_text()
                assert old not in persisted
                ledger = json.loads(persisted)
                assert "token" not in ledger
                assert ledger["token_sha256"] == sha256(old.encode()).hexdigest()
                assert ledger["binding"] == {
                    "version": "v1",
                    "session_id": SESSION_A,
                    "run_id": RUN_A,
                }
                for route, body in (
                    ("/v1/runs/opaque-a", _binding(session_id=SESSION_B)),
                    ("/v1/runs/opaque-a", _binding(run_id=RUN_B)),
                    ("/v1/runs/opaque-a", _binding(profile="mock-alt")),
                    ("/v1/runs/opaque-unbound", _binding()),
                    ("/v0/runs/opaque-a", {"provider_profile": "mock"}),
                ):
                    assert (
                        await client.post(route, headers=AUTH, json=body)
                    ).status == 409
                assert (await client.get("/v1/runs/opaque-a")).status == 401
                assert (
                    await client.get("/v0/runs/opaque-a", headers=AUTH)
                ).status == 404
                assert (
                    await _infer(client, "opaque-a", old, version="v0")
                ).status == 401
                legacy_path = "/v0/runs/legacy-ref"
                assert (
                    await client.post(
                        legacy_path, headers=AUTH, json={"provider_profile": "mock"}
                    )
                ).status == 200
                assert (
                    await client.post(
                        "/v1/runs/legacy-ref", headers=AUTH, json=_binding(run_id=RUN_B)
                    )
                ).status == 409
                foreign_close = await client.post(
                    "/v1/runs/legacy-ref/close", headers=AUTH
                )
                assert (
                    foreign_close.status == 404
                    and foreign_close.content_type != "application/json"
                )
                legacy = await client.get(legacy_path, headers=AUTH)
                assert (await legacy.json())["closed"] is False
                observed = await client.get("/v1/runs/opaque-a", headers=AUTH)
                assert "inference_token" not in await observed.json()
                reply = await _infer(client, "opaque-a", old)
                assert reply.status == 200
                await reply.read()
                assert set(upstream[MOCK_KEY].received) == {"opaque-a"}
                closed = await client.post("/v1/runs/opaque-a/close", headers=AUTH)
                terminal = await closed.json()
                assert terminal["closed"] is True
                assert (
                    terminal["tokens"]["input"] > 0 and terminal["tokens"]["output"] > 0
                )
            interrupted = Ledger(
                state / "crashed.json",
                "mock",
                profile_snapshot=grant["provider_snapshot"],
                binding=RunBinding(SESSION_A, RUN_B),
            )
            old_crashed_grant = interrupted.inference_token
            assert old_crashed_grant is not None and not interrupted.data["closed"]
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                reopened = await _open(client, "opaque-a", _binding())
                assert reopened == terminal
                assert (await _infer(client, "opaque-a", old)).status == 429
                fenced = await _open(client, "crashed", _binding(run_id=RUN_B))
                assert fenced["closed"] is True and fenced["error"] == "interrupted"
                assert "inference_token" not in fenced
                assert (
                    await _infer(client, "crashed", old_crashed_grant)
                ).status == 429
                assert set(upstream[MOCK_KEY].received) == {"opaque-a"}

    asyncio.run(asyncio.wait_for(scenario(), 8))


def test_new_turn_counts_inputs_and_outputs_independently_and_denies_stale_grants(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        received: list[tuple[str, dict[str, Any], str]] = []

        async def provider(request: web.Request) -> web.Response:
            ref = request.headers["X-Eval-Run-Id"]
            received.append(
                (ref, await request.json(), request.headers["Authorization"])
            )
            detail = (
                {"input_tokens": 29_995, "output_tokens": 25}
                if ref == "opaque-a"
                else None
            )
            return web.Response(
                body=encode_event(
                    {"type": "response.completed", "response": {"usage": detail}}
                ),
                content_type="text/event-stream",
            )

        upstream = web.Application()
        upstream.router.add_post("/responses", provider)
        async with TestServer(upstream) as origin:
            path = _profiles(tmp_path / "profiles.json", str(origin.make_url("")))
            app = create_app(
                path, tmp_path / "proxy", TOKEN, environ={"MODEL_API_KEY": UPSTREAM_KEY}
            )
            async with TestClient(TestServer(app)) as client:
                first = await _open(client, "opaque-a", _binding(profile="api"))
                grant_a = first["inference_token"]
                success = await _infer(
                    client, "opaque-a", grant_a, model="api-test-model"
                )
                assert success.status == 200
                await success.read()
                status = await (
                    await client.get("/v1/runs/opaque-a", headers=AUTH)
                ).json()
                assert (
                    status["error"] is None and status["tokens"]["over_target"] is True
                )
                assert status["tokens"]["actual"] == 30_020
                assert status["tokens"]["input"] == 29_995
                assert status["tokens"]["output"] == 25
                overflow = await _infer(
                    client, "opaque-a", grant_a, model="api-test-model"
                )
                assert overflow.status == 429
                assert await overflow.json() == {"error": "budget_exceeded"}
                old = await (
                    await client.post("/v1/runs/opaque-a/close", headers=AUTH)
                ).json()
                second = await _open(
                    client, "opaque-b", _binding(run_id=RUN_B, profile="api")
                )
                grant_b = second["inference_token"]
                assert grant_a != grant_b and second["tokens"]["actual"] == 0
                other = await _open(
                    client, "opaque-c", _binding(session_id=SESSION_B, profile="api")
                )
                for ref, token in (
                    ("opaque-b", grant_a),
                    ("opaque-a", grant_b),
                    ("opaque-c", grant_a),
                ):
                    assert (
                        await _infer(client, ref, token, model="api-test-model")
                    ).status == 401
                assert other["tokens"]["actual"] == 0
                unknown = await _infer(
                    client, "opaque-b", grant_b, model="api-test-model"
                )
                assert unknown.status == 200
                await unknown.read()
                denied = await _infer(
                    client, "opaque-b", grant_b, model="api-test-model"
                )
                assert denied.status == 429
                assert await denied.json() == {"error": "upstream_usage_unconfirmed"}
                status = await (
                    await client.get("/v1/runs/opaque-b", headers=AUTH)
                ).json()
                assert status["tokens"]["actual"] is None
                assert (
                    status["tokens"]["unknown_reason"] == "upstream_usage_unconfirmed"
                )
                assert status["tokens"]["reserved"] == 256
                assert (
                    old
                    == await (
                        await client.get("/v1/runs/opaque-a", headers=AUTH)
                    ).json()
                )
                assert [ref for ref, _, _ in received] == ["opaque-a", "opaque-b"]
                for _, payload, upstream_auth in received:
                    assert upstream_auth == "Bearer " + UPSTREAM_KEY
                    assert payload["max_output_tokens"] == 256
                    assert not any(
                        secret in json.dumps(payload)
                        for secret in (
                            SESSION_A,
                            SESSION_B,
                            RUN_A,
                            RUN_B,
                            grant_a,
                            grant_b,
                            UPSTREAM_KEY,
                        )
                    )

    asyncio.run(asyncio.wait_for(scenario(), 8))


def test_close_revokes_active_upstream_and_prevents_delayed_open(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        verifying, release_verify = asyncio.Event(), asyncio.Event()
        entered, disconnected = asyncio.Event(), asyncio.Event()
        count = 0
        mock = create_mock_app()[MOCK_KEY]

        async def capabilities(request: web.Request) -> web.Response:
            if not release_verify.is_set():
                verifying.set()
                await release_verify.wait()
            return await mock.capabilities(request)

        async def provider(request: web.Request) -> web.StreamResponse:
            nonlocal count
            count += 1
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                encode_event({"type": "response.created", "response": {}})
            )
            entered.set()
            try:
                while (
                    request.transport is not None and not request.transport.is_closing()
                ):
                    await asyncio.sleep(0.01)
            finally:
                disconnected.set()
            return response

        upstream = web.Application()
        upstream.router.add_get("/budget-capabilities", capabilities)
        upstream.router.add_post("/responses", provider)
        async with TestServer(upstream) as origin:
            path = _profiles(tmp_path / "profiles.json", str(origin.make_url("")))
            state = tmp_path / "proxy"
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                opening = asyncio.create_task(
                    client.post("/v1/runs/delayed", headers=AUTH, json=_binding())
                )
                await asyncio.wait_for(verifying.wait(), 2)
                absent = await client.post("/v1/runs/delayed/close", headers=AUTH)
                assert absent.status == 404
                assert await absent.json() == {"error": "proxy_ref_fenced"}
                repeated = await client.post("/v1/runs/delayed/close", headers=AUTH)
                assert await repeated.json() == {"error": "proxy_ref_fenced"}
                release_verify.set()
                assert (await opening).status == 409
                large = await _open(client, "large", _binding(session_id=SESSION_B))
                denial = await _infer(
                    client, "large", large["inference_token"], text="x" * 120_000
                )
                assert denial.status == 429 and await denial.json() == {
                    "error": "budget_exceeded"
                }
                assert count == 0
                allowed = await _open(client, "active", _binding())
                grant = allowed["inference_token"]
                response = await _infer(client, "active", grant)
                await asyncio.wait_for(entered.wait(), 2)
                closed = await client.post("/v1/runs/active/close", headers=AUTH)
                state_after = await closed.json()
                assert (
                    state_after["closed"] is True
                    and state_after["error"] == "cancelled"
                )
                assert state_after["tokens"]["actual"] is None
                await asyncio.wait_for(disconnected.wait(), 2)
                assert (await _infer(client, "active", grant)).status == 429
                assert count == 1
                assert (
                    state_after
                    == await (await client.get("/v1/runs/active", headers=AUTH)).json()
                )
                response.close()
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                for route, body in (
                    ("/v1/runs/delayed", _binding(run_id=RUN_B)),
                    ("/v0/runs/delayed", {"provider_profile": "mock"}),
                ):
                    assert (
                        await client.post(route, headers=AUTH, json=body)
                    ).status == 409

    asyncio.run(asyncio.wait_for(scenario(), 8))


def test_body_arriving_after_close_cannot_mutate_terminal_ledger(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        proxy = Proxy(load_profiles(Path("config/profiles.json")), tmp_path, TOKEN)
        profile = proxy.profiles["mock"]
        ledger = Ledger(
            tmp_path / "opaque.json",
            "mock",
            binding=RunBinding(SESSION_A, RUN_A),
            profile_snapshot=profile.snapshot(),
        )
        proxy.ledgers["opaque"] = ledger
        grant = ledger.inference_token
        assert grant is not None
        waiting = asyncio.Event()
        release = asyncio.Event()

        class Request:
            match_info = {"proxy_ref": "opaque"}
            headers = {"Authorization": "Bearer " + grant}

            async def json(self) -> dict[str, str]:
                waiting.set()
                await release.wait()
                return {"model": "invalid"}

        late = asyncio.create_task(proxy.responses(Request()))
        await waiting.wait()
        ledger.close()
        release.set()
        with pytest.raises(BudgetError, match="run_closed"):
            await late
        assert ledger.data["requests"] == 0 and ledger.data["error"] is None

    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_ambiguous_ledger_write_cannot_issue_a_second_grant_or_change_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        original = Ledger.save
        injected = False

        def fail_after_publish(ledger: Ledger) -> None:
            nonlocal injected
            original(ledger)
            if not injected and ledger.path.stem == "partial":
                injected = True
                raise OSError("Ошибка подтверждения записи после её публикации")

        monkeypatch.setattr(Ledger, "save", fail_after_publish)
        async with TestServer(create_mock_app()) as origin:
            path = _profiles(tmp_path / "profiles.json", str(origin.make_url("")))
            state = tmp_path / "proxy"
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                for url, body in (
                    ("/v1/runs/partial", _binding()),
                    ("/v0/runs/partial", {"provider_profile": "mock"}),
                    ("/v1/runs/duplicate", _binding()),
                ):
                    result = await client.post(url, headers=AUTH, json=body)
                    assert result.status == 503, await result.text()
                assert set(item.name for item in state.glob("*.json")) == {
                    "partial.json"
                }
                assert (await client.get("/health", headers=AUTH)).status == 503
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                recovered = await _open(client, "partial", _binding())
                assert (
                    recovered["closed"] is True and recovered["error"] == "interrupted"
                )
                assert "inference_token" not in recovered
                for url, body in (
                    ("/v0/runs/partial", {"provider_profile": "mock"}),
                    ("/v1/runs/duplicate", _binding()),
                ):
                    assert (
                        await client.post(url, headers=AUTH, json=body)
                    ).status == 409
                assert (await _infer(client, "partial", TOKEN)).status == 401

    asyncio.run(asyncio.wait_for(scenario(), 8))
