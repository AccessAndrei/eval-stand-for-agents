"""HTTP-связка receipt/слота; Linux-доказательства проверяются отдельно."""

import asyncio
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.harness import server

TOKEN = "management-test-token-not-secret-0000"
AUTH = {"Authorization": "Bearer " + TOKEN}


class FakeRecovery:
    records: dict[str, dict] = {}

    def __init__(self, state: Path) -> None:
        self.state = state

    async def reconcile(self, controls_only: bool = False) -> dict:
        return self.records.copy()

    def status(self, run_id: str) -> dict | None:
        return self.records.get(run_id)

    def begin_attempt(self, run_id: str, home: Path) -> dict:
        self.records[run_id] = {"done": False, "released": False}
        return {"EVAL_STAND_ATTEMPT_OWNER": "private-attempt-owner"}

    async def complete(self, run_id: str, snapshot: dict) -> dict:
        shutil.rmtree(self.state / run_id)
        self.records[run_id] = {**snapshot, "done": True, "released": True}
        return self.records[run_id]


class FakeAdapter:
    calls = 0
    owner_seen = False

    def __init__(self, binary: str, secrets: tuple[str, ...]) -> None:
        self.released = False
        self.secrets = secrets

    def snapshot(self) -> dict:
        return {
            "execution_status": "success",
            "released": self.released,
            "events": [],
            "traces": [],
            "evidence_complete": True,
            "trace_truncated": False,
        }

    async def run(
        self, prompt: str, model: str, home: Path, env: dict, timeout: int
    ) -> dict:
        type(self).calls += 1
        type(self).owner_seen = env["EVAL_STAND_ATTEMPT_OWNER"] in self.secrets
        await asyncio.sleep(0)
        self.released = True
        return self.snapshot()

    async def cancel(self) -> None:
        self.released = True


def launch() -> dict:
    return {
        "request": {
            "version": "v0",
            "run_id": "run-1",
            "task_id": "B1.1",
            "provider_profile": "mock",
            "prompt": "task",
            "files": [],
            "timeout_s": 10,
        },
        "project_id": "test",
        "handle": "test-handle",
        "model": "mock-codex",
        "proxy_url": "http://proxy",
        "inference_token": "fake-inference-token",
    }


def test_completed_receipt_survives_server_restart_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "HarnessRecovery", FakeRecovery)
    monkeypatch.setattr(server, "Adapter", FakeAdapter)
    FakeRecovery.records = {}
    FakeAdapter.calls = 0

    async def scenario() -> None:
        async with TestClient(
            TestServer(server.create_app(TOKEN, tmp_path, "http://manager", TOKEN))
        ) as client:
            assert (
                await client.post("/v0/runs", json=launch(), headers=AUTH)
            ).status == 202
            for _ in range(10):
                result = await (await client.get("/v0/runs/run-1", headers=AUTH)).json()
                if result["done"]:
                    break
                await asyncio.sleep(0.01)
            assert result["released"] and result["execution_status"] == "success"
            assert not (tmp_path / "run-1").exists()
            assert FakeAdapter.owner_seen
        async with TestClient(
            TestServer(server.create_app(TOKEN, tmp_path, "http://manager", TOKEN))
        ) as client:
            assert (
                await client.post("/v0/runs", json=launch(), headers=AUTH)
            ).status == 200
            recovered = await (
                await client.post("/v0/runs/run-1/cancel", headers=AUTH)
            ).json()
            assert recovered == result
            assert FakeAdapter.calls == 1
            assert (await client.get("/v0/runs/unknown", headers=AUTH)).status == 404

    asyncio.run(scenario())


def test_unconfirmed_receipt_blocks_new_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "HarnessRecovery", FakeRecovery)
    monkeypatch.setattr(server, "Adapter", FakeAdapter)
    FakeRecovery.records = {"old": {"done": True, "released": False}}

    async def scenario() -> None:
        async with TestClient(
            TestServer(server.create_app(TOKEN, tmp_path, "http://manager", TOKEN))
        ) as client:
            assert (await client.get("/health", headers=AUTH)).status == 503
            assert (
                await client.post("/v0/runs", json=launch(), headers=AUTH)
            ).status == 409

    asyncio.run(scenario())


def test_returned_persistence_failure_is_terminal_but_does_not_free_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRecovery(FakeRecovery):
        async def complete(self, run_id: str, snapshot: dict) -> dict:
            return {
                **snapshot,
                "done": True,
                "released": False,
                "cleanup_error": "harness_receipt_write_failed",
            }

    monkeypatch.setattr(server, "HarnessRecovery", FailingRecovery)
    monkeypatch.setattr(server, "Adapter", FakeAdapter)
    FailingRecovery.records = {}

    async def scenario() -> None:
        async with TestClient(
            TestServer(server.create_app(TOKEN, tmp_path, "http://manager", TOKEN))
        ) as client:
            await client.post("/v0/runs", json=launch(), headers=AUTH)
            for _ in range(10):
                result = await (await client.get("/v0/runs/run-1", headers=AUTH)).json()
                if result["done"]:
                    break
                await asyncio.sleep(0.01)
            assert result["done"] and not result["released"]
            assert result["cleanup_error"] == "harness_receipt_write_failed"
            another = launch()
            another["request"]["run_id"] = "run-2"
            assert (
                await client.post("/v0/runs", json=another, headers=AUTH)
            ).status == 409

    asyncio.run(scenario())
