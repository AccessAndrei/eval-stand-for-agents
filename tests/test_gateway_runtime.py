import asyncio
import base64
import json
from pathlib import Path

import pytest

from contracts import validate
from services.common.storage import StorageBudgetExceeded
from services.gateway.runtime import Runtime
from services.gateway.store import RunStore

PROVENANCE = {
    "codex_version": None,
    "commit": None,
    "origin": "unavailable",
    "image_digest": None,
    "architecture": None,
}
TOKENS = {
    "limit": 30000,
    "actual": 10,
    "unknown_reason": None,
    "reserved": 0,
    "input": 8,
    "output": 2,
    "cached": 0,
}


class FakeClient:
    url = "http://proxy"

    def __init__(
        self,
        kind: str,
        *,
        lost_provision: bool = False,
        collection_failed: bool = False,
        cleanup_failed: bool = False,
    ) -> None:
        self.kind = kind
        self.calls: list[tuple] = []
        self.lost_provision = lost_provision
        self.collection_failed = collection_failed
        self.cleanup_failed = cleanup_failed

    async def request(
        self, method: str, path: str, body: dict | None = None, timeout: float = 30
    ) -> dict:
        self.calls.append((method, path))
        if self.kind == "proxy":
            if path.endswith("/close"):
                return {"tokens": TOKENS, "error": None}
            return {"model": "mock-model", "inference_token": "fake-test-value"}
        if self.kind == "harness":
            return {
                "done": True,
                "released": True,
                "execution_status": "timeout",
                "events": [],
            }
        if path.endswith("/collect"):
            if self.collection_failed:
                raise OSError("broken collection")
            return {
                "status": "complete",
                "files": [
                    {
                        "path": "answer.txt",
                        "content_base64": base64.b64encode(b"8080").decode(),
                    }
                ],
                "input_manifest": {},
            }
        if path.endswith("/release"):
            return {"cleanup_status": "failed" if self.cleanup_failed else "success"}
        if method == "POST" and self.lost_provision:
            raise OSError("lost response after creation")
        return {"handle": "sandbox-handle"}


def request() -> dict:
    return {
        "version": "v0",
        "run_id": "run-one",
        "task_id": "B1.1",
        "provider_profile": "mock",
        "prompt": "read",
        "files": [],
        "timeout_s": 10,
    }


@pytest.mark.parametrize(
    "lost,collection_failed,cleanup_failed",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_stages_preserve_primary_error(
    tmp_path: Path, lost: bool, collection_failed: bool, cleanup_failed: bool
) -> None:
    store = RunStore(tmp_path, 30_000_000)
    manager = FakeClient(
        "manager",
        lost_provision=lost,
        collection_failed=collection_failed,
        cleanup_failed=cleanup_failed,
    )
    harness = FakeClient("harness")
    runtime = Runtime(
        store, manager, harness, FakeClient("proxy"), "test-project", PROVENANCE
    )
    launch = request()
    store.accept(launch)
    asyncio.run(runtime.execute(launch))
    state = store.get(launch["run_id"])
    validate("status", state)
    result = state["result"]
    assert result["execution_status"] == ("sandbox_error" if lost else "timeout")
    assert result["collection_status"] == (
        "incomplete" if collection_failed else "complete"
    )
    assert result["cleanup_status"] == ("failed" if cleanup_failed else "success")
    assert manager.calls.count(("POST", "/v0/environments")) == 1
    assert not lost or ("GET", "/v0/environments/by-run/run-one") in manager.calls
    if lost:
        assert harness.calls == []
    assert store.finish(result) == state
    persisted = json.loads((tmp_path / "run-one/result.json").read_text())
    assert persisted == result


def test_journal_full_still_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path, 30_000_000)
    manager = FakeClient("manager")
    runtime = Runtime(
        store,
        manager,
        FakeClient("harness"),
        FakeClient("proxy"),
        "project",
        PROVENANCE,
    )
    store.accept(request())
    original = store.update

    def fail_late(run_id: str, phase: str) -> dict:
        if phase in {"collecting", "cleaning"}:
            raise OSError("no space")
        return original(run_id, phase)

    monkeypatch.setattr(store, "update", fail_late)
    asyncio.run(runtime.execute(request()))
    assert ("POST", "/v0/environments/sandbox-handle/release") in manager.calls
    result = store.get("run-one")["result"]
    assert result["execution_status"] == "timeout"
    assert any(error["code"] == "storage_budget_exceeded" for error in result["errors"])


def test_lost_proxy_ledger_holds_conservative_reserve(tmp_path: Path) -> None:
    class LostLedger(FakeClient):
        async def request(
            self, method: str, path: str, body: dict | None = None, timeout: float = 30
        ) -> dict:
            if path.endswith("/close") or method == "GET":
                raise OSError("connection lost")
            return await super().request(method, path, body, timeout)

    store = RunStore(tmp_path, 30_000_000)
    runtime = Runtime(
        store,
        FakeClient("manager"),
        FakeClient("harness"),
        LostLedger("proxy"),
        "project",
        PROVENANCE,
    )
    store.accept(request())
    asyncio.run(runtime.execute(request()))
    result = store.get("run-one")["result"]
    assert result["tokens"]["reserved"] == 30000
    assert result["tokens"]["unknown_reason"] == "token_accounting_unavailable"
    assert result["execution_status"] == "timeout"


@pytest.mark.parametrize("name", ["bad\\name", "bad\nname"])
def test_invalid_agent_filename_finishes_incomplete_and_releases(
    tmp_path: Path,
    name: str,
) -> None:
    class InvalidFilename(FakeClient):
        async def request(
            self, method: str, path: str, body: dict | None = None, timeout: float = 30
        ) -> dict:
            reply = await super().request(method, path, body, timeout)
            if path.endswith("/collect"):
                reply["files"][0]["path"] = name
            return reply

    store = RunStore(tmp_path, 30_000_000)
    manager = InvalidFilename("manager")
    runtime = Runtime(
        store,
        manager,
        FakeClient("harness"),
        FakeClient("proxy"),
        "project",
        PROVENANCE,
    )
    store.accept(request())
    asyncio.run(runtime.execute(request()))
    status = store.get("run-one")
    validate("status", status)
    assert status["state"] == "terminal"
    assert status["result"]["collection_status"] == "incomplete"
    assert status["result"]["artifacts"] == []
    assert status["result"]["cleanup_status"] == "success"
    assert not store.busy()


@pytest.mark.parametrize(
    "execution", ["success", "cancelled", "timeout", "interrupted"]
)
def test_model_completion_cannot_hide_executor_transport_failure(
    tmp_path: Path,
    execution: str,
) -> None:
    class Client(FakeClient):
        async def request(
            self, method: str, path: str, body: dict | None = None, timeout: float = 30
        ) -> dict:
            reply = await super().request(method, path, body, timeout)
            if self.kind == "harness":
                reply["execution_status"] = execution
            if self.kind == "manager" and path.endswith("/release"):
                reply["errors"] = [
                    {
                        "stage": "execution",
                        "code": "transport_error",
                        "message": "exec-server exited137",
                    }
                ]
            return reply

    store = RunStore(tmp_path, 30_000_000)
    runtime = Runtime(
        store,
        Client("manager"),
        Client("harness"),
        Client("proxy"),
        "project",
        PROVENANCE,
    )
    store.accept(request())
    asyncio.run(runtime.execute(request()))
    result = store.get("run-one")["result"]
    assert result["execution_status"] == (
        "transport_error" if execution == "success" else execution
    )
    assert result["collection_status"] == "complete"
    assert result["cleanup_status"] == "success"


def test_recovery_reads_saved_model_without_launching_inference(tmp_path: Path) -> None:
    snapshot = {
        "name": "mock",
        "model": "historical-model",
        "endpoint": "http://mock-old:8090",
        "protocol": "responses",
        "auth": "none",
        "timeout_s": 30,
        "output_limit": 2048,
        "budget_mechanism": "mock-utf8-quarter-v1",
        "enabled": True,
    }

    class PreviousProfile(FakeClient):
        async def request(
            self, method: str, path: str, body: dict | None = None, timeout: float = 30
        ) -> dict:
            if method == "GET":
                self.calls.append((method, path))
                return {"model": "historical-model", "provider_snapshot": snapshot}
            return await super().request(method, path, body, timeout)

    store = RunStore(tmp_path, 30_000_000)
    proxy = PreviousProfile("proxy")
    harness = FakeClient("harness")
    runtime = Runtime(
        store, FakeClient("manager"), harness, proxy, "project", PROVENANCE
    )
    store.accept(request())
    asyncio.run(runtime.execute(request(), recover=True))
    result = store.get("run-one")["result"]
    assert result["execution_status"] == "interrupted"
    assert result["model"] == "historical-model"
    assert result["provider_snapshot"] == snapshot
    assert ("POST", "/v0/runs/run-one") not in proxy.calls
    assert ("POST", "/v0/runs") not in harness.calls


@pytest.mark.parametrize("failure", [OSError, StorageBudgetExceeded])
def test_recovery_journal_failure_still_reconciles_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    store = RunStore(tmp_path, 30_000_000)
    manager, harness, proxy = (
        FakeClient("manager"),
        FakeClient("harness"),
        FakeClient("proxy"),
    )
    runtime = Runtime(store, manager, harness, proxy, "project", PROVENANCE)
    store.accept(request())
    original = store.update

    def fail_recovery_transition(run_id: str, phase: str) -> dict:
        if phase == "recovering":
            raise failure("no journal space")
        return original(run_id, phase)

    monkeypatch.setattr(store, "update", fail_recovery_transition)
    asyncio.run(runtime.execute(request(), recover=True))
    result = store.get("run-one")["result"]
    assert result["execution_status"] == "interrupted"
    assert result["cleanup_status"] == "success"
    assert any(
        error["stage"] == "recovery" and error["code"] == "storage_budget_exceeded"
        for error in result["errors"]
    )
    assert ("GET", "/v0/environments/by-run/run-one") in manager.calls
    assert ("POST", "/v0/environments/sandbox-handle/release") in manager.calls
    assert ("POST", "/v0/environments") not in manager.calls
    assert ("POST", "/v0/runs") not in harness.calls
    assert ("POST", "/v0/runs/run-one") not in proxy.calls
