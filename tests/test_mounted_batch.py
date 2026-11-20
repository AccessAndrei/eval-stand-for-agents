"""Отдельные попытки БАФ и барьер перед доверенным локальным чеком."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from baf.runner import mounted_batch
from baf.runner.batch import report_reserve_bytes
from baf.runner.catalog import FixtureFile, Task, load_dataset
from baf.runner.mounted_batch import RUN_RESERVE_BYTES, run_mounted_batch
from client.session import ClientError, CloseReport
from contracts.tool_evidence import B5_COMMAND, B5_COMMAND_KIND

FIXTURE = b"\xff\x00"
CONTROL_DATASET = Path(__file__).resolve().parents[1] / "baf" / "control.yaml"
CHECKER = """
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
assert (work.parent / "closed").exists(), "запущен до закрытия"
Path("checker-ran").touch()
assert (work / "nested" / "bytes.bin").read_bytes() == b"\\xff\\x00"
answer = work / "answer.txt"
passed = answer.is_file() and answer.read_text() == "ok"
print(json.dumps({"pass": passed, "reason": "ok" if passed else "incorrect"}))
"""


def task(root: Path, task_id: str) -> Task:
    directory = root / task_id
    directory.mkdir(parents=True)
    (directory / "check.py").write_text(CHECKER, encoding="utf-8")
    return Task(
        id=task_id,
        baf=task_id.split(".")[0],
        directory=directory,
        messages=("создай answer.txt",),
        timeout_s=5,
        fixture_files=(
            FixtureFile("nested/bytes.bin", FIXTURE, sha256(FIXTURE).hexdigest()),
        ),
        fixture_directories=("nested", "empty"),
        answer_file="answer.txt",
    )


def b5_task() -> Task:
    return next(
        task
        for task in load_dataset(CONTROL_DATASET, mounted=True)
        if task.id == "B5.1"
    )


def b5_evidence(
    session_id: str,
    run_id: str,
    *,
    completed: bool = True,
    exit_code: int = 0,
    marked: bool = True,
    session_mismatch: bool = False,
) -> dict:
    identity = {"thread_id": "thread-1", "turn_id": "turn-1"}
    command = {
        **identity,
        "item_id": "item-1",
        "type": "commandExecution",
        "source": "agent",
        "command": B5_COMMAND,
        "raw_output": "вывод команды из транспортного ответа",
    }
    if marked:
        command["command_kind"] = B5_COMMAND_KIND
    events = [{**command, "phase": "started"}]
    if completed:
        events.append({**command, "phase": "completed", "exit_code": exit_code})
    return {
        **identity,
        "version": "v1",
        "session_id": "f" * 32 if session_mismatch else session_id,
        "run_id": run_id,
        "codex_version": "0.159.3",
        "events": events,
    }


class Session:
    def __init__(
        self, workspace: Path, number: int, options: dict[str, Any], plan: dict
    ) -> None:
        self.session_id = f"{number:032x}"
        self.workspace = workspace
        self.options = options
        self.plan = plan
        self.calls: list[str] = []
        self.run_id: str | None = None
        self.task_id: str | None = None

    async def mount(self) -> dict:
        self.calls.append("mount")
        if self.options["protected_paths"]:
            assert self.options["protected_paths"] == ("input",)
            assert {path.name for path in (self.workspace / "input").iterdir()} == {
                "stats.py",
                "data.csv",
            }
        else:
            assert (self.workspace / "empty").is_dir()
            assert (self.workspace / "nested/bytes.bin").read_bytes() == FIXTURE
            assert set(path.name for path in self.workspace.iterdir()) == {
                "nested",
                "empty",
            }
        return {"state": "ready"}

    async def start_run(
        self, prompt: str, *, timeout_s: int, task_id: str, run_id: str
    ) -> dict:
        assert self.calls == ["mount"]
        assert prompt and timeout_s > 0
        self.calls.append("start")
        self.run_id, self.task_id = run_id, task_id
        if self.plan.get("unsafe"):
            (self.workspace / "intruder").symlink_to(self.workspace.parent)
        if self.plan.get("change_input") == "bytes":
            (self.workspace / "input/data.csv").write_bytes(b"another input")
        if self.plan.get("change_input") == "layout":
            (self.workspace / "input/child").mkdir()
        if self.plan.get("answer_directory"):
            (self.workspace / "answer.txt").mkdir()
        elif self.plan.get("answer", "ok") is not None:
            (self.workspace / "answer.txt").write_text(
                self.plan.get("answer", "ok"), encoding="utf-8"
            )
        return {"run_id": run_id}

    async def events(self, run_id: str, *, timeout: float) -> AsyncIterator[dict]:
        assert self.calls == ["mount", "start"]
        assert run_id == self.run_id and timeout > 0
        self.calls.append("events")
        yield {"seq": 1, "type": "message", "text": "сырой текст чата"}
        yield {
            "seq": 2,
            "type": "tool",
            "tool": {
                "phase": "completed",
                "exit_code": 0,
                "raw_output": "вывод команды",
            },
        }
        if self.plan.get("stream_failed"):
            raise ClientError("events", "stream_incomplete")
        proof = self.plan.get("proof")
        evidence = (
            {
                "evidence_status": "complete",
                "evidence": b5_evidence(self.session_id, run_id, **proof),
            }
            if proof is not None
            else {}
        )
        yield {
            "seq": 3,
            "type": "result",
            "result": {
                "version": "v1",
                "session_id": self.session_id,
                "run_id": run_id,
                "task_id": self.task_id,
                "provider_profile": self.options["profile"],
                "execution_status": "success",
                "collection_status": "complete",
                "writers_stopped": True,
                "evidence_status": "not_required",
                **evidence,
                "errors": [],
                "tokens": {"status": "known", "usage": 123},
                "provenance": {"codex_version": "0.159.3"},
                **self.plan.get("result_overrides", {}),
            },
        }

    async def close(self) -> CloseReport:
        self.calls.append("close")
        (self.workspace.parent / "closed").touch()
        return self.plan.get(
            "close", CloseReport({"cleanup_status": "success"}, None, True, True)
        )


class Client:
    def __init__(self, *plans: dict) -> None:
        self.plans = plans
        self.opened = 0
        self.sessions: list[Session] = []

    async def __aenter__(self) -> Client:
        self.opened += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        assert all(session.calls[-1] == "close" for session in self.sessions)

    def session(self, directory: Path, **options: Any) -> Session:
        number = len(self.sessions)
        plan = self.plans[number] if number < len(self.plans) else {}
        assert options["mode"] == "rw"
        assert options["write_limit"] > len(FIXTURE)
        session = Session(directory, number + 1, options, plan)
        self.sessions.append(session)
        return session


def run(
    client: Client, results: Path, *tasks: Task, smoke: bool = False, **limits: int
) -> tuple[Path, dict]:
    return asyncio.run(
        run_mounted_batch(
            client, profile="mock", results=results, tasks=tasks, smoke=smoke, **limits
        )
    )


def test_mounted_batch_uses_one_client_and_fresh_worktrees(tmp_path: Path) -> None:
    first = task(tmp_path, "B1.9")
    second = task(tmp_path, "B2.7")
    client = Client({"answer": None})
    path, report = run(client, tmp_path / "results", first, second)
    assert client.opened == 1 and len(client.sessions) == 2
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert report["version"] == "v1"
    rows = report["tasks"]
    assert "mode" not in report and all("smoke_status" not in row for row in rows)
    assert [row["task_id"] for row in rows] == ["B1.9", "B2.7"]
    assert [row["checker_status"] for row in rows] == ["failed", "passed"]
    assert len({row["session_id"] for row in rows}) == 2
    assert len({row["run_id"] for row in rows}) == 2
    assert report["evaluated"] == 2 and report["wrong_answers"] == 1
    assert report["technical_skips"] == 0 and not report["success"]
    for row in rows:
        workdir = path.parent / row["artifact_path"]
        assert (workdir / "nested/bytes.bin").read_bytes() == FIXTURE
        trace = (path.parent / row["trace_path"]).read_text(encoding="utf-8")
        assert "сырой текст чата" not in trace and "вывод команды" not in trace
        assert json.loads(trace)["events"][1]["tool"]["exit_code"] == 0


def test_independent_server_cleanup_error_keeps_checker_verdict(tmp_path: Path) -> None:
    first = task(tmp_path, "B1.9")
    second = task(tmp_path, "B2.7")
    close = CloseReport(
        {"cleanup_status": "failed"},
        ClientError("cleanup", "cleanup_unconfirmed"),
        True,
        True,
    )
    client = Client({"close": close})
    _, report = run(client, tmp_path / "results", first, second)
    assert len(client.sessions) == 1
    completed, pending = report["tasks"]
    assert completed["cleanup_status"] == "failed"
    assert completed["files_status"] == "confirmed"
    assert completed["checker_status"] == "passed"
    assert (first.directory / "checker-ran").exists()
    assert pending["run_id"] is None and "cleanup_failed" in pending["skip_reason"]
    assert not report["success"]


@pytest.mark.parametrize(
    ("plan", "files_status"),
    [
        ({"result_overrides": {"writers_stopped": False}}, "unconfirmed"),
        ({"result_overrides": {"collection_status": "incomplete"}}, "unconfirmed"),
        (
            {"close": CloseReport({"cleanup_status": "success"}, None, False, True)},
            "unconfirmed",
        ),
        (
            {"close": CloseReport({"cleanup_status": "success"}, None, True, False)},
            "unconfirmed",
        ),
        ({"unsafe": True}, "unsafe"),
        ({"stream_failed": True}, "unconfirmed"),
        ({"result_overrides": {"session_id": "f" * 32}}, "unconfirmed"),
        ({"result_overrides": {"run_id": "f" * 32}}, "unconfirmed"),
        ({"result_overrides": {"task_id": "B1.0"}}, "unconfirmed"),
    ],
)
def test_unproven_worktree_does_not_reach_trusted_checker(
    tmp_path: Path, plan: dict, files_status: str
) -> None:
    first = task(tmp_path, "B1.9")
    _, report = run(Client(plan), tmp_path / "results", first)
    assert not (first.directory / "checker-ran").exists()
    row = report["tasks"][0]
    assert row["files_status"] == files_status
    assert row["checker_status"] == "not_run_collection_failed"
    assert report["technical_skips"] == 1 and report["wrong_answers"] == 0


def test_b5_protects_input_and_skips_without_attestation(tmp_path: Path) -> None:
    first = b5_task()
    client = Client({"result_overrides": {"evidence_status": "incomplete"}})
    _, report = run(client, tmp_path / "results", first)
    assert client.sessions[0].options["protected_paths"] == ("input",)
    row = report["tasks"][0]
    assert row["files_status"] == "confirmed"
    assert row["evidence_status"] == "incomplete"
    assert row["checker_status"] == "not_run_evidence_incomplete"


@pytest.mark.parametrize(
    ("proof", "change_input", "checker_status"),
    [
        ({}, None, "passed"),
        ({"exit_code": 1}, None, "failed"),
        ({"marked": False}, None, "failed"),
        ({"completed": False}, None, "not_run_evidence_incomplete"),
        ({"session_mismatch": True}, None, "not_run_evidence_incomplete"),
        ({}, "bytes", "not_run_evidence_incomplete"),
        ({}, "layout", "not_run_evidence_incomplete"),
    ],
)
def test_b5_combines_trusted_command_proof_and_local_protection(
    tmp_path: Path, proof: dict, change_input: str | None, checker_status: str
) -> None:
    client = Client({"proof": proof, "change_input": change_input, "answer": "11"})
    path, report = run(client, tmp_path / "results", b5_task())
    row = report["tasks"][0]
    assert row["files_status"] == "confirmed"
    assert row["checker_status"] == checker_status
    assert report["technical_skips"] == (
        checker_status == "not_run_evidence_incomplete"
    )
    assert report["wrong_answers"] == (checker_status == "failed")
    assert report["success"] == (checker_status == "passed")
    assert "raw_output" not in path.read_text(encoding="utf-8")
    assert B5_COMMAND not in path.read_text(encoding="utf-8")


def test_b5_rejects_a_bad_manifest_before_opening_a_session(tmp_path: Path) -> None:
    first = b5_task()
    file, *following = first.fixture_files
    broken = replace(first, fixture_files=(replace(file, sha256="f" * 64), *following))
    client = Client()
    _, report = run(client, tmp_path / "results", broken)
    assert client.sessions == []
    assert report["tasks"][0]["checker_status"] == "not_run_evidence_incomplete"


def test_not_enough_space_for_attempt_preserves_all_report_rows(tmp_path: Path) -> None:
    results = tmp_path / "results"
    first = task(tmp_path, "B1.9")
    second = task(tmp_path, "B2.7")
    client = Client()
    limit = report_reserve_bytes(2) + RUN_RESERVE_BYTES - 1
    path, report = run(client, results, first, second, storage_limit=limit)
    assert path.is_file() and len(report["tasks"]) == 2
    assert client.sessions == []
    for row in report["tasks"]:
        assert row["run_id"] is None
        assert "storage_budget_exceeded" in row["skip_reason"]
    assert not list(results.glob("*.reserve"))


def test_next_attempt_cannot_displace_saved_results(tmp_path: Path) -> None:
    results = tmp_path / "results"
    first = task(tmp_path, "B1.9")
    second = task(tmp_path, "B2.7")
    client = Client()
    limit = report_reserve_bytes(2) + RUN_RESERVE_BYTES
    path, report = run(client, results, first, second, storage_limit=limit)
    completed, pending = report["tasks"]
    assert len(client.sessions) == 1 and completed["checker_status"] == "passed"
    assert (
        pending["run_id"] is None
        and "storage_budget_exceeded" in pending["skip_reason"]
    )
    first_report = path.read_bytes()
    fresh_path, fresh = run(Client(), results, first)
    assert fresh["success"] and fresh_path != path
    assert path.read_bytes() == first_report


def test_smoke_does_not_submit_b6_to_checker_or_require_b5_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden_checker(*_: object, **__: object) -> None:
        pytest.fail("Smoke запустил чекер качества")

    monkeypatch.setattr(mounted_batch, "run_checker", forbidden_checker)
    first = b5_task()
    second = task(tmp_path, "B6.1")
    client = Client({"result_overrides": {"evidence_status": "incomplete"}})
    path, report = run(client, tmp_path / "results", first, second, smoke=True)
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert report["mode"] == "smoke" and report["success"]
    assert (
        report["evaluated"] == report["wrong_answers"] == report["technical_skips"] == 0
    )
    assert [row["smoke_status"] for row in report["tasks"]] == ["passed", "passed"]
    assert all(row["checker_status"] == "not_run" for row in report["tasks"])
    assert report["tasks"][0]["evidence_status"] == "incomplete"
    assert client.sessions[0].options["protected_paths"] == ("input",)


@pytest.mark.parametrize("answer", [{"answer": None}, {"answer_directory": True}])
def test_smoke_requires_a_regular_answer_file_and_continues_the_set(
    tmp_path: Path, answer: dict
) -> None:
    first, second = task(tmp_path, "B1.9"), task(tmp_path, "B2.9")
    client = Client(answer)
    _, report = run(client, tmp_path / "results", first, second, smoke=True)
    assert [row["smoke_status"] for row in report["tasks"]] == ["failed", "passed"]
    assert not report["success"]
    assert report["wrong_answers"] == report["technical_skips"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        {"result_overrides": {"writers_stopped": False}},
        {"result_overrides": {"errors": [{"code": "token_accounting_unavailable"}]}},
        {"close": CloseReport({"cleanup_status": "failed"}, None, True, True)},
        {"unsafe": True},
    ],
)
def test_smoke_reports_infrastructure_failure_even_when_answer_exists(
    tmp_path: Path, failure: dict
) -> None:
    first, second = task(tmp_path, "B1.9"), task(tmp_path, "B2.9")
    client = Client(failure)
    _, report = run(client, tmp_path / "results", first, second, smoke=True)
    assert len(client.sessions) == 1
    assert [row["smoke_status"] for row in report["tasks"]] == [
        "technical_skip",
        "not_run",
    ]
    assert report["technical_skips"] == 2 and not report["success"]


def test_smoke_requires_successful_turn_even_if_answer_exists(tmp_path: Path) -> None:
    first = task(tmp_path, "B1.9")
    client = Client({"result_overrides": {"execution_status": "budget_exceeded"}})
    _, report = run(client, tmp_path / "results", first, smoke=True)
    assert report["tasks"][0]["smoke_status"] == "failed"
    assert report["technical_skips"] == 0 and not report["success"]
