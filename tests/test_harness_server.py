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

    def __init__(self, state: Path, *, mounted_state: Path) -> None:
        self.state = state
        self.mounted_state = mounted_state
        self.closed: set[str] = set()

    def attempt_home(self, run_id: str, *, mounted: bool = False) -> Path:
        root = self.mounted_state if mounted else self.state
        root.mkdir(exist_ok=True)
        return root / run_id

    def session_home(self, session_id: str) -> Path:
        return self.mounted_state / ".sessions" / session_id

    def session_closed(self, session_id: str) -> bool:
        return session_id in self.closed

    def close_session(self, session_id: str) -> None:
        self.closed.add(session_id)
        home = self.session_home(session_id)
        if home.is_dir():
            shutil.rmtree(home)

    async def reconcile(self, controls_only: bool = False) -> dict:
        return self.records.copy()

    def status(self, run_id: str) -> dict | None:
        return self.records.get(run_id)

    def begin_attempt(
        self, run_id: str, home: Path, *, mounted: tuple[str, str] | None = None
    ) -> dict:
        self.records[run_id] = {"done": False, "released": False}
        if mounted:
            session_id, fingerprint = mounted
            self.records[run_id].update(
                version="v1", session_id=session_id, request_fingerprint=fingerprint
            )
        return {"EVAL_STAND_ATTEMPT_OWNER": "private-attempt-owner"}

    async def complete(self, run_id: str, snapshot: dict) -> dict:
        if session_id := self.records[run_id].get("session_id"):
            for name in ("config.toml", "environments.toml", "models.json"):
                (self.session_home(session_id) / name).unlink(missing_ok=True)
        else:
            shutil.rmtree(self.attempt_home(run_id))
        self.records[run_id] = {
            **self.records[run_id],
            **snapshot,
            "done": True,
            "released": True,
        }
        return self.records[run_id]


class FakeAdapter:
    calls = 0
    owner_seen = False
    continuations: list[tuple[Path, str | None, str]] = []

    def __init__(
        self,
        binary: str,
        secrets: tuple[str, ...],
        *,
        mounted: bool = False,
        resume_thread_id: str | None = None,
    ) -> None:
        self.released = False
        self.secrets = secrets
        self.mounted = mounted
        self.resume_thread_id = resume_thread_id

    def snapshot(self) -> dict:
        snapshot = {
            "execution_status": "success",
            "released": self.released,
            "events": [],
            "traces": [],
            "evidence_complete": True,
            "trace_truncated": False,
        }
        if self.mounted:
            snapshot["messages"] = [{"item_id": "greeting", "text": "ответ из /work"}]
            snapshot["thread_id"] = self.resume_thread_id or "native-thread"
            snapshot["continuation_safe"] = self.released
        return snapshot

    async def run(
        self, prompt: str, model: str, home: Path, env: dict, timeout: int
    ) -> dict:
        type(self).calls += 1
        type(self).owner_seen = env["EVAL_STAND_ATTEMPT_OWNER"] in self.secrets
        if self.mounted:
            assert Path(env["CODEX_HOME"]) == home
            history = home / "sessions"
            if self.resume_thread_id is None:
                history.mkdir(mode=0o700)
                (history / "rollout.jsonl").write_text("native-private-output")
            else:
                assert (
                    history / "rollout.jsonl"
                ).read_text() == "native-private-output"
            self.continuations.append(
                (home, self.resume_thread_id, env["STAND_INFERENCE_TOKEN"])
            )
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


def test_mounted_messages_remain_ephemeral_after_completion_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "HarnessRecovery", FakeRecovery)
    monkeypatch.setattr(server, "Adapter", FakeAdapter)
    FakeRecovery.records = {}
    FakeAdapter.calls = 0
    mounted_root = tmp_path / "volatile"
    durable = tmp_path / "durable"
    session_id, run_id = "b" * 32, "c" * 32
    run_path = f"/v1/sessions/{session_id}/runs/{run_id}"
    home = mounted_root / ".sessions" / session_id
    data = {
        **launch(),
        "handle": "a" * 48,
        "request": {
            "version": "v1",
            "session_id": session_id,
            "run_id": run_id,
            "prompt": "прочитай каталог",
            "timeout_s": 10,
        },
    }

    async def scenario() -> None:
        async with TestClient(
            TestServer(
                server.create_app(
                    TOKEN, durable, "http://manager", TOKEN, mounted_state=mounted_root
                )
            )
        ) as client:
            assert (
                await client.post("/v1/runs", json=data, headers=AUTH)
            ).status == 202
            async with asyncio.timeout(2):
                while True:
                    terminal = await (await client.get(run_path, headers=AUTH)).json()
                    if terminal["done"]:
                        break
                    await asyncio.sleep(0.01)
            assert terminal["released"]
            assert terminal["messages"] == [
                {"item_id": "greeting", "text": "ответ из /work"}
            ]
            assert terminal["run_id"] == run_id and terminal["session_id"] == session_id
            assert "messages" not in FakeRecovery.records[run_id]
            assert "request_fingerprint" not in terminal
            assert not (durable / run_id).exists()
            assert (home / "sessions/rollout.jsonl").is_file()
            assert not (home / "environments.toml").exists()
            assert (await client.get(f"/v0/runs/{run_id}", headers=AUTH)).status == 404
            assert (
                await client.post(f"/v0/runs/{run_id}/cancel", headers=AUTH)
            ).status == 404
            changed = {
                **data,
                "request": {**data["request"], "prompt": "другой запрос"},
            }
            assert (
                await client.post("/v1/runs", json=changed, headers=AUTH)
            ).status == 409
        async with TestClient(
            TestServer(
                server.create_app(
                    TOKEN, durable, "http://manager", TOKEN, mounted_state=mounted_root
                )
            )
        ) as client:
            assert (
                await client.post("/v1/runs", json=data, headers=AUTH)
            ).status == 200
            recovered = await (
                await client.post(run_path + "/cancel", headers=AUTH)
            ).json()
            assert recovered == {**terminal, "messages": []}
            assert FakeAdapter.calls == 1
            changed = {**data, "request": {**data["request"], "run_id": "d" * 32}}
            refused = await client.post("/v1/runs", json=changed, headers=AUTH)
            assert refused.status == 409
            for _ in range(2):
                closed = await client.delete(f"/v1/sessions/{session_id}", headers=AUTH)
                assert await closed.json() == {"cleanup_status": "success"}
            assert not home.exists()

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


def test_mounted_turns_share_only_session_home_and_keep_scoped_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "HarnessRecovery", FakeRecovery)
    monkeypatch.setattr(server, "Adapter", FakeAdapter)
    FakeRecovery.records = {}
    FakeAdapter.continuations = []
    session_id = "a" * 32
    path = f"/v1/sessions/{session_id}"
    scratch = tmp_path / "scratch"

    def data(run_id: str, inference: str) -> dict:
        return {
            **launch(),
            "handle": "f" * 48,
            "inference_token": inference,
            "request": {
                "version": "v1",
                "session_id": session_id,
                "run_id": run_id,
                "prompt": "продолжение",
                "timeout_s": 10,
            },
        }

    async def terminal(client: TestClient, endpoint: str) -> dict:
        async with asyncio.timeout(2):
            while True:
                response = await client.get(endpoint, headers=AUTH)
                value = await response.json()
                if value["done"]:
                    return value
                await asyncio.sleep(0.01)

    async def scenario() -> None:
        app = server.create_app(
            TOKEN, tmp_path / "durable", "http://manager", TOKEN, mounted_state=scratch
        )
        async with TestClient(TestServer(app)) as client:
            first, second = "b" * 32, "c" * 32
            assert (
                await client.post("/v1/runs", json=data(first, "lease-a"), headers=AUTH)
            ).status == 202
            old = await terminal(client, f"{path}/runs/{first}")
            assert old["released"] and old["continuation_safe"]
            assert (
                await client.post(
                    "/v1/runs", json=data(second, "lease-b"), headers=AUTH
                )
            ).status == 202
            new = await terminal(client, f"{path}/runs/{second}")
            assert new["released"] and new["thread_id"] == old["thread_id"]
            home = scratch / ".sessions" / session_id
            assert FakeAdapter.continuations == [
                (home, None, "lease-a"),
                (home, old["thread_id"], "lease-b"),
            ]
            assert (
                await (await client.get(f"{path}/runs/{first}", headers=AUTH)).json()
                == old
            )
            wrong = f"/v1/sessions/{'d' * 32}/runs/{first}"
            assert (await client.get(wrong, headers=AUTH)).status == 404
            assert (await client.post(wrong + "/cancel", headers=AUTH)).status == 404
            assert (await client.get(f"/v1/runs/{first}", headers=AUTH)).status == 404
            assert (await client.delete(path, headers=AUTH)).status == 200
            assert not home.exists()
            after_close = await (
                await client.get(f"{path}/runs/{first}", headers=AUTH)
            ).json()
            assert after_close == {**old, "messages": []}
            late = await client.post(
                "/v1/runs", json=data("e" * 32, "lease-c"), headers=AUTH
            )
            assert late.status == 409

    asyncio.run(scenario())


def test_cancel_stops_active_run_even_if_receipt_becomes_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreadableRecovery(FakeRecovery):
        def status(self, run_id: str) -> dict | None:
            record = super().status(run_id)
            if record is not None and not record["done"]:
                raise server.RecoveryError("receipt_unreadable")
            return record

    async def scenario() -> None:
        stopped = asyncio.Event()

        class BlockingAdapter(FakeAdapter):
            async def run(
                self, prompt: str, model: str, home: Path, env: dict, timeout: int
            ) -> dict:
                await stopped.wait()
                self.released = True
                return self.snapshot()

            async def cancel(self) -> None:
                stopped.set()

        UnreadableRecovery.records = {}
        monkeypatch.setattr(server, "HarnessRecovery", UnreadableRecovery)
        monkeypatch.setattr(server, "Adapter", BlockingAdapter)
        async with TestClient(
            TestServer(server.create_app(TOKEN, tmp_path, "http://manager", TOKEN))
        ) as client:
            assert (
                await client.post("/v0/runs", json=launch(), headers=AUTH)
            ).status == 202
            assert (
                await client.post("/v1/runs/run-1/cancel", headers=AUTH)
            ).status == 404
            assert not stopped.is_set()
            assert (
                await client.post("/v0/runs/run-1/cancel", headers=AUTH)
            ).status == 200
            assert stopped.is_set()

    asyncio.run(scenario())


def test_mounted_close_fences_session_even_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailFirstClose(FakeRecovery):
        def close_session(self, session_id: str) -> None:
            if not self.closed:
                self.closed.add("injected-failure")
                raise server.RecoveryError("harness_cleanup_unconfirmed")
            super().close_session(session_id)

    async def scenario() -> None:
        stopped = asyncio.Event()

        class BlockingAdapter(FakeAdapter):
            async def run(
                self, prompt: str, model: str, home: Path, env: dict, timeout: int
            ) -> dict:
                await stopped.wait()
                return await super().run(prompt, model, home, env, timeout)

            async def cancel(self) -> None:
                stopped.set()

        FailFirstClose.records = {}
        monkeypatch.setattr(server, "HarnessRecovery", FailFirstClose)
        monkeypatch.setattr(server, "Adapter", BlockingAdapter)
        session_id, run_id = "a" * 32, "b" * 32
        path = f"/v1/sessions/{session_id}"
        launched = {
            **launch(),
            "handle": "f" * 48,
            "request": {
                "version": "v1",
                "session_id": session_id,
                "run_id": run_id,
                "prompt": "запусти команду",
                "timeout_s": 10,
            },
        }
        app = server.create_app(
            TOKEN,
            tmp_path / "durable",
            "http://manager",
            TOKEN,
            mounted_state=tmp_path / "scratch",
        )
        async with TestClient(TestServer(app)) as client:
            assert (
                await client.post("/v1/runs", json=launched, headers=AUTH)
            ).status == 202
            assert (await client.delete(path, headers=AUTH)).status == 409
            wrong = f"/v1/sessions/{'c' * 32}/runs/{run_id}/cancel"
            assert (await client.post(wrong, headers=AUTH)).status == 404
            assert not stopped.is_set()
            assert (
                await client.post(f"{path}/runs/{run_id}/cancel", headers=AUTH)
            ).status == 200
            async with asyncio.timeout(2):
                while True:
                    response = await client.get(f"{path}/runs/{run_id}", headers=AUTH)
                    if (await response.json())["done"]:
                        break
                    await asyncio.sleep(0.01)
            assert (await client.delete(path, headers=AUTH)).status == 503
            next_run = {
                **launched,
                "request": {**launched["request"], "run_id": "d" * 32},
            }
            assert (
                await client.post("/v1/runs", json=next_run, headers=AUTH)
            ).status == 409
            assert (await client.delete(path, headers=AUTH)).status == 200

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
