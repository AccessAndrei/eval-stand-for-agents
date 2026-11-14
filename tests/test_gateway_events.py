"""HTTP-событие должно приходить до terminal, без повторов при финализации."""

import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from contracts import validate
from services.gateway.runtime import Runtime
from services.gateway.server import create_app
from services.gateway.store import RunStore

TOKEN = "events-management-test-token-00000"
AUTH = {"Authorization": "Bearer " + TOKEN}
PROVENANCE = {
    "codex_version": None,
    "commit": None,
    "origin": "unavailable",
    "image_digest": None,
    "architecture": None,
}


def test_event_arrives_while_run_is_active_and_finalization_does_not_duplicate(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started, finish = asyncio.Event(), asyncio.Event()
        traces = [
            {
                "timestamp": "2026-11-06T12:00:00Z",
                "type": "command_started",
                "output": "Живое событие",
                "output_truncated": False,
                "outcome": "running",
            }
        ]

        class Client:
            url = "http://unused"

            async def request(self, method: str, path: str, *args: object) -> dict:
                return {"traces": list(traces), "evidence_complete": True}

        class ControlledRuntime(Runtime):
            async def execute(self, launch: dict, recover: bool = False) -> None:
                self.store.update(launch["run_id"], "running")
                started.set()
                await finish.wait()
                self.store.write(
                    launch["run_id"],
                    "events.json",
                    self.events(launch, {"traces": traces, "evidence_complete": True}),
                )
                result = self.result(launch)
                result.update(
                    execution_status="success",
                    collection_status="complete",
                    cleanup_status="success",
                )
                self.store.finish(result)

        runtime = ControlledRuntime(
            RunStore(tmp_path, 30_000_000),
            Client(),
            Client(),
            Client(),
            "test",
            PROVENANCE,
        )
        async with TestClient(TestServer(create_app(runtime, TOKEN))) as client:
            launch = {
                "version": "v0",
                "run_id": "events-run",
                "task_id": "B1.1",
                "provider_profile": "mock",
                "prompt": "test",
                "files": [],
                "timeout_s": 5,
            }
            assert (
                await client.post("/v0/runs", json=launch, headers=AUTH)
            ).status == 202
            await asyncio.wait_for(started.wait(), 1)
            assert (await client.get("/v0/runs/events-run/events")).status == 401
            response = await client.get("/v0/runs/events-run/events", headers=AUTH)
            try:
                assert response.status == 200
                first = json.loads(
                    await asyncio.wait_for(response.content.readline(), 1)
                )
                validate("event", first)
                assert first["output"] == "Живое событие"
                assert runtime.store.get("events-run")["state"] == "running"
                assert not finish.is_set()
                traces.append(
                    {
                        **traces[0],
                        "type": "command_completed",
                        "output": "done",
                        "outcome": "0",
                    }
                )
                second = json.loads(
                    await asyncio.wait_for(response.content.readline(), 1)
                )
                assert second["sequence"] == 1
                assert runtime.store.get("events-run")["state"] == "running"
                finish.set()
                assert await asyncio.wait_for(response.read(), 1) == b""
                saved = await (
                    await client.get("/v0/runs/events-run/events", headers=AUTH)
                ).text()
                assert [
                    json.loads(line)["sequence"] for line in saved.splitlines()
                ] == [0, 1]
            finally:
                finish.set()
                response.close()

    asyncio.run(scenario())


def test_busy_rejection_precedes_auth_and_preserves_identical_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started, finish = asyncio.Event(), asyncio.Event()
        preparations: list[str] = []

        class Client:
            async def request(self, *args: object) -> dict:
                raise AssertionError("Неожиданный внешний вызов")

        class ControlledRuntime(Runtime):
            async def prepare_profile(self, name: str) -> None:
                preparations.append(name)

            async def execute(self, launch: dict, recover: bool = False) -> None:
                self.store.update(launch["run_id"], "running")
                started.set()
                await finish.wait()
                result = self.result(launch)
                result.update(
                    execution_status="success",
                    collection_status="complete",
                    cleanup_status="success",
                )
                self.store.finish(result)

        runtime = ControlledRuntime(
            RunStore(tmp_path, 30_000_000),
            Client(),
            Client(),
            Client(),
            "test",
            PROVENANCE,
        )
        launch = {
            "version": "v0",
            "run_id": "active-run",
            "task_id": "B1.1",
            "provider_profile": "mock",
            "prompt": "test",
            "files": [],
            "timeout_s": 5,
        }
        async with TestClient(TestServer(create_app(runtime, TOKEN))) as client:
            try:
                accepted = await client.post("/v0/runs", json=launch, headers=AUTH)
                assert accepted.status == 202
                await asyncio.wait_for(started.wait(), 1)
                used = runtime.store.disk.used()
                rejected = await client.post(
                    "/v0/runs",
                    json={**launch, "run_id": "new-run", "provider_profile": "api"},
                    headers=AUTH,
                )
                assert rejected.status == 409
                assert await rejected.text() == "busy"
                replay = await client.post("/v0/runs", json=launch, headers=AUTH)
                assert replay.status == 200
                assert (await replay.json())["run_id"] == launch["run_id"]
                assert preparations == ["mock"]
                assert runtime.store.disk.used() == used
            finally:
                finish.set()

    asyncio.run(scenario())
