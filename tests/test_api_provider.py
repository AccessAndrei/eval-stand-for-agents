"""Сменяемый API-провайдер: ключ только upstream, история и ошибки без секретов."""

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, TestServer

from services.llm_proxy.app import create_app
from services.llm_proxy.mock import encode_event
from services.llm_proxy.profiles import Profile, ProfileError, load_profiles
from services.llm_proxy.providers import ADAPTERS
from services.llm_proxy.providers.base import UpstreamRequest
from services.llm_proxy.providers.responses import ResponsesAdapter

TOKEN = "management-test-token-not-secret-00000"
KEY = "test-provider-key-not-a-real-secret"
AUTH = {"Authorization": "Bearer " + TOKEN}


def profile_file(tmp_path: Path, endpoint: str, protocol: str = "responses") -> Path:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "course-provider": {
                    "model": "course-model-id",
                    "endpoint": endpoint,
                    "protocol": protocol,
                    "auth": "api_key",
                    "api_key_env": "COURSE_KEY",
                    "timeout_s": 5,
                    "output_limit": 256,
                    "budget_mode": "observed_usage",
                    "budget_mechanism": "observed-usage-v1",
                }
            }
        )
    )
    return path


def completed() -> bytes:
    return encode_event(
        {
            "type": "response.completed",
            "response": {
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "output": [],
            },
        }
    )


async def run_request(client: TestClient) -> ClientResponse:
    opened = await client.post(
        "/v0/runs/api-run", json={"provider_profile": "course-provider"}, headers=AUTH
    )
    assert opened.status == 200, await opened.text()
    token = (await opened.json())["inference_token"]
    assert token != KEY
    return await client.post(
        "/v0/runs/api-run/responses",
        headers={"Authorization": "Bearer " + token},
        json={"model": "course-model-id", "stream": True, "input": []},
    )


@pytest.mark.parametrize("key", [None, "", "white space", "line\nbreak", "не-ascii"])
def test_missing_or_invalid_api_key_rejected_before_start(
    tmp_path: Path, key: str | None
) -> None:
    async def scenario() -> None:
        path = profile_file(tmp_path, "https://provider.example/v1")
        environ = {} if key is None else {"COURSE_KEY": key}
        app = create_app(path, tmp_path / "state", TOKEN, environ=environ)
        async with TestClient(TestServer(app)) as client:
            reply = await client.post(
                "/v0/runs/api-run",
                headers=AUTH,
                json={"provider_profile": "course-provider"},
            )
            assert reply.status == 401
            assert await reply.json() == {"error": "auth_error"}
            assert not (tmp_path / "state" / "api-run.json").exists()

    asyncio.run(scenario())


def test_env_config_and_legacy_history_have_no_secret_dependency(
    tmp_path: Path,
) -> None:
    profiles = load_profiles(
        Path("config/profiles.json"),
        {
            "MODEL_ID": "future-model",
            "MODEL_ENDPOINT": "https://future.example/api",
            "MODEL_API_KEY": KEY,
        },
    )
    profile = profiles["api"]
    profile.compatible()
    assert profile.model == "future-model"
    assert profile.endpoint == "https://future.example/api"
    assert KEY not in json.dumps(profile.snapshot())
    assert "model_env" not in profile.snapshot()
    old = {
        **profile.snapshot(),
        "name": "luna",
        "model": "gpt-6-luna",
        "auth": "chatgpt",
        "api_key_env": None,
    }
    historical = Profile(**old)
    assert historical.snapshot()["model"] == "gpt-6-luna"
    with pytest.raises(ProfileError):
        historical.compatible()


def test_api_key_requires_https_except_loopback(tmp_path: Path) -> None:
    for endpoint in ("http://provider.example/v1", "https://provider.example/\npath"):
        profile = load_profiles(profile_file(tmp_path, endpoint))["course-provider"]
        with pytest.raises(ProfileError):
            profile.compatible()
    for endpoint in (
        "https://provider.example/v1",
        "http://127.0.0.1:8090",
        "http://[::1]:8090",
    ):
        load_profiles(profile_file(tmp_path, endpoint))["course-provider"].compatible()


def test_bearer_forwarded_once_usage_saved_without_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = []

        async def upstream(request: web.Request) -> web.Response:
            calls.append((dict(request.headers), await request.json()))
            return web.Response(body=completed(), content_type="text/event-stream")

        source = web.Application()
        source.router.add_post("/v1/responses", upstream)
        async with TestServer(source) as server:
            path = profile_file(tmp_path, str(server.make_url("/v1")))
            app = create_app(
                path, tmp_path / "state", TOKEN, environ={"COURSE_KEY": KEY}
            )
            async with TestClient(TestServer(app)) as client:
                response = await run_request(client)
                assert response.status == 200
                assert "response.completed" in await response.text()
                status = await (
                    await client.get("/v0/runs/api-run", headers=AUTH)
                ).json()
                assert status["tokens"]["actual"] == 12
                assert status["tokens"]["output_limit"] == 256
                assert KEY not in json.dumps(status)
                assert KEY not in (tmp_path / "state" / "api-run.json").read_text()
        assert len(calls) == 1
        headers, body = calls[0]
        assert headers["Authorization"] == "Bearer " + KEY
        assert headers["X-Eval-Run-Id"] == "api-run"
        assert body["model"] == "course-model-id"
        assert body["max_output_tokens"] == 256
        assert KEY not in json.dumps(body)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 307])
def test_auth_failure_and_redirect_never_replay_or_leak(
    tmp_path: Path, status: int
) -> None:
    async def scenario() -> None:
        calls = []

        async def upstream(request: web.Request) -> web.Response:
            calls.append(request.path)
            return web.Response(
                status=status, text=KEY, headers={"Location": "/stolen"}
            )

        source = web.Application()
        source.router.add_post("/responses", upstream)
        source.router.add_post("/stolen", upstream)
        async with TestServer(source) as server:
            path = profile_file(tmp_path, str(server.make_url("")))
            app = create_app(
                path, tmp_path / "state", TOKEN, environ={"COURSE_KEY": KEY}
            )
            async with TestClient(TestServer(app)) as client:
                response = await run_request(client)
                assert response.status == 502
                assert KEY not in await response.text()
                saved = (tmp_path / "state" / "api-run.json").read_text()
                assert KEY not in saved
                assert json.loads(saved)["upstream_failure"]["http_status"] == status
        assert calls == ["/responses"]

    asyncio.run(scenario())


def test_protocol_adapter_can_be_added_without_proxy_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OtherAdapter(ResponsesAdapter):
        def prepare(
            self, profile: Profile, body: dict, run_id: str, key: str | None
        ) -> UpstreamRequest:
            standard = super().prepare(profile, body, run_id, key)
            return UpstreamRequest(
                profile.endpoint + "/generate", standard.headers, {"request": body}
            )

    monkeypatch.setitem(ADAPTERS, "course-protocol", OtherAdapter())

    async def scenario() -> None:
        async def upstream(request: web.Request) -> web.Response:
            assert (await request.json())["request"]["model"] == "course-model-id"
            return web.Response(body=completed(), content_type="text/event-stream")

        source = web.Application()
        source.router.add_post("/generate", upstream)
        async with TestServer(source) as server:
            path = profile_file(tmp_path, str(server.make_url("")), "course-protocol")
            app = create_app(
                path, tmp_path / "state", TOKEN, environ={"COURSE_KEY": KEY}
            )
            async with TestClient(TestServer(app)) as client:
                response = await run_request(client)
                assert response.status == 200
                assert "response.completed" in await response.text()

    asyncio.run(scenario())
