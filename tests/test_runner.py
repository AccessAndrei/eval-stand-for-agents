"""Раннер с тестовыми серверными ответами; это не проверка Docker-маршрута."""

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from baf.runner.batch import REPORT_RESERVE_BYTES, execute, launch_request, run_batch
from baf.runner.catalog import B5_COMMAND, load_checkpoint
from baf.runner.transport import GatewayClient, GatewayError, TransportError
from contracts import fingerprint, validate
from services.common.storage import StorageBudgetExceeded

TIME = "2026-11-06T12:00:00+00:00"


def result_for(request: dict, files: dict[str, str]) -> dict:
    return {
        "version": "v0",
        "run_id": request["run_id"],
        "trace_id": request["run_id"],
        "task_id": request["task_id"],
        "provider_profile": request["provider_profile"],
        "model": "test-model",
        "execution_status": "success",
        "collection_status": "complete",
        "cleanup_status": "success",
        "evidence_status": "complete"
        if request["task_id"] == "B5.1"
        else "not_required",
        "errors": [],
        "artifacts": [
            {
                "path": path,
                "size_bytes": len(content.encode()),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
            for path, content in files.items()
        ],
        "tokens": {
            "limit": 30000,
            "actual": 10,
            "unknown_reason": None,
            "reserved": 0,
            "input": 5,
            "output": 5,
            "cached": 0,
        },
        "started_at": TIME,
        "finished_at": TIME,
        "duration_s": 0,
        "provenance": {
            "codex_version": None,
            "commit": None,
            "origin": "unavailable",
            "image_digest": None,
            "architecture": None,
        },
    }


def evidence_for(request: dict) -> dict:
    shared = {
        "type": "commandExecution",
        "source": "agent",
        "run_id": request["run_id"],
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "item_id": "item-1",
        "command": B5_COMMAND,
        "cwd": "/work",
    }
    return {
        "version": "v0",
        "codex_version": "0.159.3",
        "run_id": request["run_id"],
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "complete": True,
        "protected_inputs": True,
        "input_manifest": {
            item["path"]: hashlib.sha256(item["content"].encode()).hexdigest()
            for item in request["files"]
        },
        "events": [
            {**shared, "phase": "started"},
            {**shared, "phase": "completed", "exit_code": 0},
        ],
    }


class FakeGateway:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.requests: list[dict] = []
        self.states: dict[str, dict] = {}
        self.bundles: dict[str, dict] = {}

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if method == "POST":
            assert path == "/v0/runs", "Нет скрытого replay или неожиданной отмены"
            assert payload is not None
            validate("launch", payload)
            self.requests.append(payload)
            if self.mode == "storage" and len(self.requests) == 2:
                raise GatewayError("storage_budget_exceeded")
            status = self._finish(payload)
            if self.mode == "lost_response":
                raise TransportError("lost_response")
            return status
        run_id = path.split("/")[3]
        if path.endswith("/artifacts"):
            return self.bundles[run_id]
        return self.states[run_id]

    def _finish(self, request: dict) -> dict:
        files = {item["path"]: item["content"] for item in request["files"]}
        task_id = request["task_id"]
        answers = {"B1.1": "8137", "B2.1": "src/utils/dates.py", "B5.1": "11"}
        if task_id in answers:
            files["answer.txt"] = answers[task_id]
        elif task_id == "B3.1":
            files["config.json"] = json.dumps(
                {
                    "service": "calendar",
                    "port": 8137,
                    "enabled": True,
                    "tags": ["local", "test"],
                }
            )
        else:
            files["README.md"] = files["README.md"].replace(
                "calendar==1.2", "calendar==1.3"
            )
        if self.mode == "wrong" and task_id == "B1.1":
            files["answer.txt"] = "wrong"
        result = result_for(request, files)
        if self.mode == "cleanup" and task_id == "B1.1":
            result["cleanup_status"] = "failed"
            result["errors"] = [
                {"stage": "cleanup", "code": "cleanup_failed", "message": "test"}
            ]
        if self.mode == "budget" and task_id == "B1.1":
            result["execution_status"] = "budget_exceeded"
        if self.mode == "incomplete" and task_id == "B1.1":
            result["collection_status"] = "incomplete"
            result["execution_status"] = "timeout"
            result["errors"] = [
                {
                    "stage": "collection",
                    "code": "collection_incomplete",
                    "message": "test",
                }
            ]
        bundle = {
            "files": [
                {
                    "path": path,
                    "content_base64": base64.b64encode(content.encode()).decode(),
                }
                for path, content in files.items()
            ],
            "evidence": evidence_for(request) if task_id == "B5.1" else None,
            "events": [],
        }
        if self.mode == "hash_mismatch":
            bundle["files"][0]["content_base64"] = "d3Jvbmc="
        if self.mode == "traversal":
            bundle["files"][0]["path"] = "../../escape"
        if self.mode == "evidence" and task_id == "B5.1":
            bundle["evidence"]["events"].pop()
        status = {
            "version": "v0",
            "run_id": request["run_id"],
            "task_id": task_id,
            "fingerprint": fingerprint(request),
            "state": "terminal",
            "updated_at": TIME,
            "result": result,
        }
        self.states[request["run_id"]] = status
        self.bundles[request["run_id"]] = bundle
        return status


def test_all_five_and_history(tmp_path: Path) -> None:
    gateway = FakeGateway()
    first_path, report = run_batch(gateway, profile="mock", results=tmp_path)
    assert report["success"] is True
    assert report["evaluated"] == 5
    assert report["technical_skips"] == 0
    assert len(gateway.requests) == 5
    assert len({request["run_id"] for request in gateway.requests}) == 5
    original = first_path.read_bytes()
    second_path, second = run_batch(FakeGateway(), profile="mock", results=tmp_path)
    assert second["success"]
    assert first_path != second_path
    assert first_path.read_bytes() == original
    assert not list(tmp_path.glob("*.reserve"))


@pytest.mark.parametrize("mode", ["wrong", "budget"])
def test_quality_and_budget_do_not_stop_next_task(tmp_path: Path, mode: str) -> None:
    gateway = FakeGateway(mode)
    _, report = run_batch(gateway, profile="mock", results=tmp_path)
    assert len(gateway.requests) == 5
    assert report["success"] is False
    assert report["evaluated"] == 5
    assert report["wrong_answers"] == (1 if mode == "wrong" else 0)


@pytest.mark.parametrize("mode,count", [("cleanup", 1), ("storage", 2)])
def test_infrastructure_failure_stops_and_records_all_ids(
    tmp_path: Path, mode: str, count: int
) -> None:
    gateway = FakeGateway(mode)
    _, report = run_batch(gateway, profile="mock", results=tmp_path)
    assert len(gateway.requests) == count
    assert len(report["tasks"]) == 5
    assert report["success"] is False
    assert all(row["skip_reason"] for row in report["tasks"][count:])
    if mode == "cleanup":
        assert report["tasks"][0]["checker_status"] == "passed"
        assert report["tasks"][0]["result"]["cleanup_status"] == "failed"


def test_lost_accept_response_uses_lookup_without_replay(tmp_path: Path) -> None:
    gateway = FakeGateway("lost_response")
    _, report = run_batch(gateway, profile="mock", results=tmp_path)
    assert report["success"]
    assert len(gateway.requests) == 5


@pytest.mark.parametrize("mode", ["hash_mismatch", "traversal", "incomplete"])
def test_untrusted_or_partial_copy_never_reaches_checker(
    tmp_path: Path, mode: str
) -> None:
    gateway = FakeGateway(mode)
    _, report = run_batch(gateway, profile="mock", results=tmp_path)
    assert report["tasks"][0]["checker_status"] == "not_run_collection_failed"
    assert report["wrong_answers"] == 0
    assert report["technical_skips"] == 5
    assert not (tmp_path.parent / "escape").exists()
    if mode == "incomplete":
        assert report["tasks"][0]["result"]["execution_status"] == "timeout"
        assert report["tasks"][0]["result"]["errors"][0]["stage"] == "collection"


def test_missing_evidence_is_not_wrong_answer(tmp_path: Path) -> None:
    _, report = run_batch(FakeGateway("evidence"), profile="mock", results=tmp_path)
    assert report["tasks"][4]["checker_status"] == "not_run_evidence_incomplete"
    assert report["evaluated"] == 4
    assert report["wrong_answers"] == 0
    assert report["technical_skips"] == 1


def test_report_reserve_survives_no_room_for_run(tmp_path: Path) -> None:
    gateway = FakeGateway()
    _, report = run_batch(
        gateway,
        profile="mock",
        results=tmp_path,
        storage_limit=REPORT_RESERVE_BYTES + 100,
    )
    assert gateway.requests == []
    assert report["technical_skips"] == 5
    assert report["tasks"][0]["errors"][0]["code"] == "storage_budget_exceeded"


def test_no_batch_starts_without_report_reserve(tmp_path: Path) -> None:
    gateway = FakeGateway()
    with pytest.raises(StorageBudgetExceeded):
        run_batch(gateway, profile="mock", results=tmp_path, storage_limit=100)
    assert gateway.requests == []


def test_fixture_input_is_fresh_per_attempt(tmp_path: Path) -> None:
    gateway = FakeGateway()
    run_batch(gateway, profile="mock", results=tmp_path)
    tasks = load_checkpoint()
    for request, task in zip(gateway.requests, tasks, strict=True):
        assert {file["path"] for file in request["files"]} == {
            file.path for file in task.fixture_files
        }
        assert "answer.txt" not in {file["path"] for file in request["files"]}


def test_cancellation_waits_for_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = launch_request(load_checkpoint()[0], "run-cancel", "mock")
    request["timeout_s"] = 1
    gateway = FakeGateway()
    terminal = gateway._finish(request)
    terminal["result"]["execution_status"] = "cancelled"
    active = {**terminal, "state": "running", "result": None}
    calls = []

    class CancellingGateway:
        cancelled = False

        def request(self, method: str, path: str, payload: dict | None = None) -> dict:
            calls.append((method, path))
            if path.endswith("/cancel"):
                self.cancelled = True
                return {**active, "state": "cancelling"}
            return terminal if self.cancelled else active

    ticks = iter([0, 2, 2])
    monkeypatch.setattr("baf.runner.batch.time.monotonic", lambda: next(ticks))
    result = execute(CancellingGateway(), request, poll_s=0, grace_s=0)
    assert result["cleanup_status"] == "success"
    assert result["execution_status"] == "cancelled"
    assert calls == [
        ("POST", "/v0/runs"),
        ("GET", "/v0/runs/run-cancel"),
        ("POST", "/v0/runs/run-cancel/cancel"),
        ("GET", "/v0/runs/run-cancel"),
    ]


def test_artifact_budget_error_uses_report_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exhausted(*args: object, **kwargs: object) -> None:
        raise StorageBudgetExceeded("storage_budget_exceeded")

    monkeypatch.setattr("baf.runner.batch.save_bundle", exhausted)
    gateway = FakeGateway()
    path, report = run_batch(gateway, profile="mock", results=tmp_path)
    assert path.is_file()
    assert len(gateway.requests) == 1
    assert report["tasks"][0]["result"]["execution_status"] == "success"
    assert report["tasks"][0]["checker_status"] == "not_run_collection_failed"
    assert report["tasks"][0]["errors"][0]["stage"] == "storage"


@pytest.mark.parametrize("success,code", [(True, 0), (False, 1)])
def test_cli_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, success: bool, code: int
) -> None:
    from baf.runner import __main__ as cli

    monkeypatch.setattr("sys.argv", ["baf.runner", "--profile", "mock"])
    monkeypatch.setenv("CLIENT_TOKEN", "test-token-" * 4)
    report = {
        "success": success,
        "evaluated": 5,
        "wrong_answers": int(not success),
        "technical_skips": 0,
    }
    monkeypatch.setattr(
        cli, "run_batch", lambda *args, **kwargs: (tmp_path / "report.json", report)
    )
    assert cli.main() == code


@pytest.mark.parametrize("failure", ["transport", "storage"])
def test_b5_missing_copy_precedes_missing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from baf.runner import batch

    class IncompleteB5Gateway(FakeGateway):
        def _finish(self, request: dict) -> dict:
            status = super()._finish(request)
            if request["task_id"] == "B5.1":
                status["result"]["evidence_status"] = "incomplete"
            return status

        def request(self, method: str, path: str, payload: dict | None = None) -> dict:
            if failure == "transport" and path.endswith("/artifacts"):
                run_id = path.split("/")[3]
                if self.states[run_id]["task_id"] == "B5.1":
                    raise TransportError("lost_artifacts")
            return super().request(method, path, payload)

    original = batch.save_bundle

    def save_with_failure(*args: object) -> Path:
        if failure == "storage" and args[-1]["task_id"] == "B5.1":
            raise StorageBudgetExceeded("storage_budget_exceeded")
        return original(*args)

    monkeypatch.setattr(batch, "save_bundle", save_with_failure)
    gateway = IncompleteB5Gateway()
    path, report = run_batch(gateway, profile="mock", results=tmp_path)
    validate("report", report)
    assert path.is_file()
    assert len(report["tasks"]) == 5
    assert len(gateway.requests) == 5
    row = report["tasks"][-1]
    assert row["result"]["collection_status"] == "complete"
    assert row["result"]["evidence_status"] == "incomplete"
    assert row["checker_status"] == "not_run_collection_failed"
    assert row["errors"][0]["stage"] == (
        "collection" if failure == "transport" else "storage"
    )
    assert report["success"] is False
    assert report["technical_skips"] == 1


def test_gateway_slow_drip_obeys_total_deadline() -> None:
    class SlowHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = b'{"value":"' + b"a" * 40 + b'"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = HTTPServer(("127.0.0.1", 0), SlowHandler)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        client = GatewayClient(
            f"http://127.0.0.1:{server.server_port}", "test-token-" * 4, timeout_s=0.15
        )
        started = time.monotonic()
        with pytest.raises(TransportError):
            client.request("GET", "/slow")
        assert time.monotonic() - started < 0.6
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not worker.is_alive()


@pytest.mark.parametrize(
    "code",
    ["storage_budget_exceeded", "event_storage_failed", "token_accounting_unavailable"],
)
def test_server_accounting_failure_stops_even_after_successful_cleanup(
    tmp_path: Path, code: str
) -> None:
    class AccountingFailure(FakeGateway):
        def _finish(self, request: dict) -> dict:
            state = super()._finish(request)
            state["result"]["errors"].append(
                {"stage": "storage", "code": code, "message": "test"}
            )
            return state

    gateway = AccountingFailure()
    report_path, report = run_batch(gateway, profile="mock", results=tmp_path)
    assert report_path.exists()
    assert len(gateway.requests) == 1
    assert report["tasks"][0]["result"]["execution_status"] == "success"
    assert report["tasks"][0]["result"]["cleanup_status"] == "success"
    assert all(row["result"] is None for row in report["tasks"][1:])
    assert all(code in row["skip_reason"] for row in report["tasks"][1:])
    assert not report["success"]


def test_runner_waits_for_auth_provision_and_agent_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    request = launch_request(load_checkpoint()[0], "slow-preparation", "mock")
    terminal = FakeGateway()._finish(request)

    class DelayedPreparation:
        calls = 0

        def request(self, method: str, path: str, payload: dict | None = None) -> dict:
            if method == "POST":
                assert path == "/v0/runs", (
                    "До окончания допустимых стадий отмена не нужна"
                )
                clock["now"] = 10.0
                raise TransportError("response pending")
            self.calls += 1
            clock["now"] += 60
            if self.calls >= 5:
                return terminal
            return {**terminal, "state": "running", "result": None}

    monkeypatch.setattr("baf.runner.batch.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("baf.runner.batch.time.sleep", lambda _: None)
    assert execute(DelayedPreparation(), request)["execution_status"] == "success"
