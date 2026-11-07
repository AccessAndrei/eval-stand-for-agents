"""Проверки границы провайдера и резерва без live-доступа."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientPayloadError, web
from aiohttp.test_utils import TestClient, TestServer

from services.llm_proxy.app import PROXY_KEY, Proxy, create_app
from services.llm_proxy.budget import BudgetError, Ledger
from services.llm_proxy.mock import MOCK_KEY, create_mock_app, encode_event
from services.llm_proxy.profiles import ProfileError, load_profiles, mock_input_tokens
from services.llm_proxy.stream import SSEObserver

TOKEN = "test-management-token-not-a-secret-000000"
AUTH = {"Authorization": "Bearer " + TOKEN}


def profiles_file(tmp_path: Path, endpoint: str, output_limit: int = 2048) -> Path:
    profiles = json.loads(Path("config/profiles.json").read_text())
    for name in ("mock", "mock-alt"):
        profiles[name]["endpoint"] = endpoint
        profiles[name]["output_limit"] = output_limit
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(profiles))
    return path


async def open_run(
    client: TestClient, name: str = "mock", run_id: str = "run-1"
) -> dict:
    response = await client.post(
        "/v0/runs/" + run_id,
        json={"provider_profile": name},
        headers=AUTH,
    )
    assert response.status == 200, await response.text()
    return await response.json()


def test_profile_rejects_usage_without_bound_and_incomplete_configuration() -> None:
    profiles = load_profiles(Path("config/profiles.json"))
    profiles["mock"].compatible()
    for name, code in (
        ("luna", "live_authorization_not_ready"),
        ("deepseek", "profile_incomplete"),
    ):
        with pytest.raises(ProfileError, match=code):
            profiles[name].compatible()
    with pytest.raises(ProfileError, match="unsupported_protocol"):
        replace(profiles["mock"], protocol="chat_completions").compatible()
    with pytest.raises(ProfileError, match="invalid_endpoint"):
        replace(profiles["mock"], endpoint="http://secret:password@host").compatible()
    for update in (
        {"model": 1},
        {"endpoint": 1},
        {"enabled": "true"},
        {"timeout_s": True},
    ):
        with pytest.raises(ProfileError):
            replace(profiles["mock"], **update).compatible()


def test_ledger_cumulative_cap_and_unknown_usage_survive_reload(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.json", "mock")
    request_id = ledger.reserve(8000, 2000)
    ledger.settle(request_id, {"input_tokens": 7000, "output_tokens": 1000})
    ledger.reserve(9000, 2000)
    reloaded = Ledger(tmp_path / "ledger.json", "mock")
    assert reloaded.tokens() == {
        "limit": 30_000,
        "mode": "strict",
        "output_limit": 30_000,
        "over_target": None,
        "actual": None,
        "input": 7000,
        "output": 1000,
        "cached": 0,
        "reserved": 11_000,
        "unknown_reason": "upstream_usage_unconfirmed",
    }
    with pytest.raises(BudgetError, match="budget_exceeded"):
        reloaded.reserve(10_000, 2000)
    assert reloaded.data["requests"] == 2


def test_provider_cannot_free_reservation_with_false_usage(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.json", "mock")
    request_id = ledger.reserve(10, 10)
    with pytest.raises(BudgetError, match="provider_budget_violation"):
        ledger.settle(request_id, {"input_tokens": 11, "output_tokens": 2})
    assert ledger.tokens()["reserved"] == 20


def test_sse_utf8_packet_boundaries_and_bounded_events() -> None:
    observer = SSEObserver()
    payload = encode_event(
        {
            "type": "response.completed",
            "response": {
                "output": "Медиана",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
    )
    for byte in payload:
        observer.feed(bytes([byte]))
    assert observer.usage == {"input_tokens": 1, "output_tokens": 2}
    with pytest.raises(ValueError, match="sse_event_too_large"):
        SSEObserver(max_event_bytes=5).feed(b"123456")
    with pytest.raises(ValueError, match="duplicate_terminal_event"):
        observer.feed(payload)


def test_responses_complete_native_tool_cycle_and_switch_profiles(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        upstream = create_mock_app()
        async with TestServer(upstream) as server:
            path = profiles_file(tmp_path, str(server.make_url("")))
            async with TestClient(
                TestServer(create_app(path, tmp_path / "state", TOKEN))
            ) as client:
                for index, name in enumerate(("mock", "mock-alt")):
                    run_id = f"run-{index}"
                    opened = await open_run(client, name, run_id)
                    body: dict[str, Any] = {
                        "model": opened["model"],
                        "stream": True,
                        "input": [],
                    }
                    headers = {"Authorization": "Bearer " + opened["inference_token"]}
                    seen = []
                    for expected in (
                        "function_call",
                        "custom_tool_call",
                        "function_call",
                        "message",
                    ):
                        response = await client.post(
                            f"/v0/runs/{run_id}/responses",
                            json=body,
                            headers=headers,
                        )
                        assert response.status == 200, await response.text()
                        events = [
                            json.loads(frame[6:])
                            for frame in (await response.text()).strip().split("\n\n")
                        ]
                        item = events[-1]["response"]["output"][0]
                        usage = events[-1]["response"]["usage"]
                        # Codex 0.159.3 требует total_tokens при разборе completed.
                        assert (
                            usage["total_tokens"]
                            == usage["input_tokens"] + usage["output_tokens"]
                        )
                        assert item["type"] == expected
                        seen.append(item["id"])
                        if expected != "message":
                            body["input"].extend(
                                [
                                    item,
                                    {
                                        "type": expected + "_output",
                                        "call_id": item["call_id"],
                                        "output": "реальный результат инструмента в интеграционном тесте",
                                    },
                                ]
                            )
                    assert len(seen) == len(set(seen))
                    closed = await client.post(f"/v0/runs/{run_id}/close", headers=AUTH)
                    tokens = (await closed.json())["tokens"]
                    assert 0 < tokens["actual"] < 30_000
                    assert tokens["reserved"] == 0
                assert upstream[MOCK_KEY].calls == 8

    asyncio.run(scenario())


def test_auth_preflight_conflict_idempotency_and_budget_before_upstream(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        upstream = create_mock_app()
        async with TestServer(upstream) as server:
            path = profiles_file(tmp_path, str(server.make_url("")))
            async with TestClient(
                TestServer(create_app(path, tmp_path / "state", TOKEN))
            ) as client:
                assert (await client.get("/health")).status == 401
                for name, status in (
                    ("luna", 401),
                    ("deepseek", 422),
                    ("missing", 422),
                ):
                    response = await client.get("/v0/profiles/" + name, headers=AUTH)
                    assert response.status == status
                    if name == "luna":
                        assert (await response.json())["error"] == "auth_error"
                first = await open_run(client)
                assert (await open_run(client))["inference_token"] == first[
                    "inference_token"
                ]
                response = await client.post(
                    "/v0/runs/run-1",
                    headers=AUTH,
                    json={"provider_profile": "mock-alt"},
                )
                assert response.status == 409
                assert (await client.get("/v0/runs/run-1")).status == 401
                auth = {"Authorization": "Bearer " + first["inference_token"]}
                body = {
                    "model": "mock-codex",
                    "stream": True,
                    "input": [{"content": "x" * 120_000}],
                }
                response = await client.post(
                    "/v0/runs/run-1/responses", headers=auth, json=body
                )
                assert response.status == 429
                assert (await response.json())["error"] == "budget_exceeded"
                assert upstream[MOCK_KEY].calls == 0

    asyncio.run(scenario())


def test_mock_rejects_missing_tool_result_and_enforces_output_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TestClient(TestServer(create_mock_app())) as client:
            body = {
                "model": "mock-codex",
                "stream": True,
                "input": [],
                "max_output_tokens": 2048,
            }
            headers = {"X-Eval-Run-Id": "run-one"}
            assert (
                await client.post("/responses", json=body, headers=headers)
            ).status == 200
            assert (
                await client.post("/responses", json=body, headers=headers)
            ).status == 400
            response = await client.post(
                "/responses",
                json={**body, "max_output_tokens": 1},
                headers={"X-Eval-Run-Id": "limited"},
            )
            observer = SSEObserver()
            observer.feed(await response.read())
            assert observer.terminal == "response.incomplete"
            assert observer.usage["output_tokens"] <= 1

    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["missing_usage", "disconnect"])
def test_unknown_usage_is_held_and_transport_never_replayed(
    tmp_path: Path, finish: str
) -> None:
    async def scenario() -> None:
        upstream = create_mock_app()
        calls = []

        async def response(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            calls.append(body)
            result = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await result.prepare(request)
            if finish == "missing_usage":
                await result.write(
                    encode_event({"type": "response.completed", "response": {}})
                )
                await result.write_eof()
            else:
                await result.write(
                    encode_event({"type": "response.created", "response": {}})
                )
                request.transport.close()
            return result

        # Отдельный контролируемый upstream с тем же механизмом бюджета.
        broken = web.Application()
        broken.router.add_get("/budget-capabilities", upstream[MOCK_KEY].capabilities)
        broken.router.add_post("/responses", response)
        async with TestServer(broken) as server:
            path = profiles_file(tmp_path, str(server.make_url("")))
            async with TestClient(
                TestServer(create_app(path, tmp_path / "state", TOKEN))
            ) as client:
                opened = await open_run(client)
                headers = {"Authorization": "Bearer " + opened["inference_token"]}
                body = {"model": "mock-codex", "stream": True, "input": []}
                response = await client.post(
                    "/v0/runs/run-1/responses", json=body, headers=headers
                )
                try:
                    await response.read()
                except ClientPayloadError:
                    assert finish == "disconnect"
                status = await (await client.get("/v0/runs/run-1", headers=AUTH)).json()
                assert status["tokens"]["actual"] is None
                assert status["tokens"]["reserved"] == mock_input_tokens(body) + 2048
                retry = await client.post(
                    "/v0/runs/run-1/responses", json=body, headers=headers
                )
                assert retry.status == 429
                assert len(calls) == 1

    asyncio.run(scenario())


def test_restart_returns_closed_attempt_without_new_reserve(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TestServer(create_mock_app()) as upstream:
            path = profiles_file(tmp_path, str(upstream.make_url("")))
            state = tmp_path / "state"
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                first = await open_run(client)
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                repeated = await open_run(client)
                assert repeated["closed"] is True
                assert repeated["tokens"] == first["tokens"]
                assert repeated["inference_token"] == first["inference_token"]

    asyncio.run(scenario())


def test_profile_snapshot_survives_changed_configuration_after_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with (
            TestServer(create_mock_app()) as old_upstream,
            TestServer(create_mock_app()) as new_upstream,
        ):
            path = profiles_file(tmp_path, str(old_upstream.make_url("")))
            state = tmp_path / "state"
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                first = await open_run(client)
            changed = json.loads(path.read_text())
            changed["mock"].update(
                model="mock-codex-alt",
                endpoint=str(new_upstream.make_url("")),
                timeout_s=17,
                output_limit=1024,
            )
            path.write_text(json.dumps(changed))
            async with TestClient(TestServer(create_app(path, state, TOKEN))) as client:
                previous = await open_run(client)
                assert previous["closed"] is True
                assert previous["model"] == "mock-codex"
                assert previous["provider_snapshot"] == first["provider_snapshot"]
                fresh = await open_run(client, run_id="run-2")
                assert fresh["model"] == "mock-codex-alt"
                assert fresh["provider_snapshot"]["endpoint"] == str(
                    new_upstream.make_url("")
                )
                assert fresh["provider_snapshot"]["output_limit"] == 1024
                assert fresh["provider_snapshot"]["timeout_s"] == 17

    asyncio.run(scenario())


def test_legacy_ledger_does_not_invent_profile_snapshot(tmp_path: Path) -> None:
    Ledger(tmp_path / "legacy.json", "mock")
    proxy = Proxy({}, tmp_path, TOKEN)
    status = proxy.status("legacy", proxy.ledgers["legacy"])
    assert status["model"] is None
    assert status["provider_snapshot"] is None
    assert status["profile_unknown_reason"] == "legacy_run_without_profile_snapshot"


def test_close_cancels_stream_holds_reserve_and_allows_next_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        mock = create_mock_app()[MOCK_KEY]
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def slow(request: web.Request) -> web.StreamResponse:
            calls.append(await request.json())
            result = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await result.prepare(request)
            await result.write(
                encode_event({"type": "response.created", "response": {}})
            )
            started.set()
            await finish.wait()
            return result

        upstream = web.Application()
        upstream.router.add_get("/budget-capabilities", mock.capabilities)
        upstream.router.add_post("/responses", slow)
        async with TestServer(upstream) as server:
            path = profiles_file(tmp_path, str(server.make_url("")))
            async with TestClient(
                TestServer(create_app(path, tmp_path / "state", TOKEN))
            ) as client:
                opened = await open_run(client)
                response = await client.post(
                    "/v0/runs/run-1/responses",
                    headers={"Authorization": "Bearer " + opened["inference_token"]},
                    json={"model": "mock-codex", "stream": True, "input": []},
                )
                await asyncio.wait_for(started.wait(), 2)
                closed = await asyncio.wait_for(
                    client.post("/v0/runs/run-1/close", headers=AUTH),
                    2,
                )
                status = await closed.json()
                assert status["closed"] is True
                assert status["error"] == "cancelled"
                assert status["tokens"]["reserved"] > 0
                assert len(calls) == 1
                next_run = await open_run(client, run_id="run-2")
                assert next_run["closed"] is False
                assert next_run["tokens"]["actual"] == 0
                response.close()
                finish.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("closed", [True, False])
def test_finished_run_rejected_before_auth_or_upstream(
    tmp_path: Path, closed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        app = create_app(Path("config/profiles.json"), tmp_path / "state", TOKEN)
        proxy = app[PROXY_KEY]
        profile = proxy.profiles["luna"]
        ledger = Ledger(
            tmp_path / "state" / "finished.json",
            "luna",
            profile_snapshot=profile.snapshot(),
        )
        if closed:
            ledger.close()
        else:
            ledger.fail("budget_exceeded")
        proxy.ledgers["finished"] = ledger

        async def forbidden(*args: object) -> None:
            pytest.fail("Finished run must not prepare auth or call upstream")

        monkeypatch.setattr(proxy, "check_profile", forbidden)
        monkeypatch.setattr(proxy, "post_model", forbidden)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v0/runs/finished/responses",
                headers={"Authorization": "Bearer " + ledger.data["token"]},
                json={"model": "gpt-6-luna", "stream": True, "input": []},
            )
            assert response.status == 429
            assert (await response.json())["error"] == (
                "run_closed" if closed else "budget_exceeded"
            )
            assert ledger.data["requests"] == 0

    asyncio.run(scenario())


def test_concurrent_requests_cannot_replace_active_upstream_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        proxy = Proxy(load_profiles(Path("config/profiles.json")), tmp_path, TOKEN)
        profile = proxy.profiles["mock"]
        ledger = Ledger(
            tmp_path / "run.json", "mock", profile_snapshot=profile.snapshot()
        )
        proxy.ledgers["run"] = ledger
        both_ready, finish = asyncio.Event(), asyncio.Event()
        arrivals = 0

        async def check_profile(*args: object) -> None:
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_ready.set()
            await both_ready.wait()

        async def forward(*args: object) -> web.Response:
            await finish.wait()
            return web.Response()

        class Request:
            match_info = {"run_id": "run"}
            headers = {"Authorization": "Bearer " + ledger.data["token"]}

            async def json(self) -> dict:
                return {"model": "mock-codex", "stream": True, "input": []}

        monkeypatch.setattr(proxy, "check_profile", check_profile)
        monkeypatch.setattr(proxy, "forward", forward)
        tasks = [asyncio.create_task(proxy.responses(Request())) for _ in range(2)]
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED
            )
            assert len(done) == len(pending) == 1
            assert isinstance(next(iter(done)).exception(), web.HTTPConflict)
            assert ledger.data["requests"] == 1
            assert proxy.tasks["run"] in pending
        finally:
            finish.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_upstream_rejection_keeps_only_safe_diagnostics(tmp_path: Path) -> None:
    from types import SimpleNamespace

    async def scenario() -> None:
        proxy = Proxy(load_profiles(Path("config/profiles.json")), tmp_path, TOKEN)
        ledger = Ledger(tmp_path / "rejected.json", "mock")
        content = asyncio.StreamReader()
        content.feed_data(b'{"error":"Unsupported parameter: store; private-sentinel"}')
        content.feed_eof()
        await proxy.record_rejection(
            SimpleNamespace(status=400, content=content), ledger
        )
        assert ledger.data["upstream_failure"] == {
            "http_status": 400,
            "mentioned_parameters": ["store"],
        }
        assert "private-sentinel" not in ledger.path.read_text()

    asyncio.run(scenario())


def test_valid_sse_is_parsed_without_requiring_provider_media_type(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        mock = create_mock_app()[MOCK_KEY]
        upstream = web.Application()
        upstream.router.add_get("/budget-capabilities", mock.capabilities)

        async def response(request: web.Request) -> web.Response:
            return web.Response(
                body=encode_event(
                    {
                        "type": "response.completed",
                        "response": {
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                            "output": [],
                        },
                    }
                ),
                content_type="application/octet-stream",
            )

        upstream.router.add_post("/responses", response)
        async with TestServer(upstream) as server:
            path = profiles_file(tmp_path, str(server.make_url("")))
            async with TestClient(
                TestServer(create_app(path, tmp_path / "state", TOKEN))
            ) as client:
                opened = await open_run(client)
                reply = await client.post(
                    "/v0/runs/run-1/responses",
                    headers={"Authorization": "Bearer " + opened["inference_token"]},
                    json={"model": "mock-codex", "stream": True, "input": []},
                )
                assert reply.status == 200
                assert "response.completed" in await reply.text()
                status = await client.get("/v0/runs/run-1", headers=AUTH)
                assert (await status.json())["tokens"]["actual"] == 2

    asyncio.run(scenario())
