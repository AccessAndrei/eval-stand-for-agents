"""Проверка разбора сохранённых отчётов БАФ и независимых исходов v1."""

import csv
import json
import sys
from pathlib import Path

import pytest

from baf.runner.catalog import load_dataset
from contracts import ContractError
from contracts.tool_evidence import B5_COMMAND_KIND
from scripts.analyze_baf import main, runs_from_report

DATASET = Path("baf/control.yaml")
STARTED = "2026-11-20T10:00:00+00:00"
FINISHED = "2026-11-20T10:01:00+00:00"
COMMIT = "01fc69f4026735edfdf6789820549727a4867b11"
IMAGE = "sha256:" + "a" * 64


def pending(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "run_id": None,
        "session_id": None,
        "result": None,
        "started_at": None,
        "finished_at": None,
        "duration_s": None,
        "files_status": "not_run",
        "evidence_status": "not_run",
        "cleanup_status": "not_run",
        "cleanup": None,
        "checker_status": "not_run",
        "checker_reason": None,
        "skip_reason": "Запуск остановлен: cleanup_failed",
        "trace_path": None,
        "artifact_path": None,
        "errors": [],
    }


def attempt(task_id: str, ordinal: int, *, cleanup_failed: bool) -> dict:
    run_id = f"{ordinal:032x}"
    session_id = f"{ordinal + 10:032x}"
    status = "failed" if cleanup_failed else "success"
    return {
        **pending(task_id),
        "run_id": run_id,
        "session_id": session_id,
        "started_at": STARTED,
        "finished_at": FINISHED,
        "duration_s": 19.5,
        "files_status": "confirmed",
        "evidence_status": "not_required",
        "cleanup_status": status,
        "cleanup": {
            "server_status": status,
            "export_drained": True,
            "tunnel_closed": True,
            "error": "cleanup_unconfirmed" if cleanup_failed else None,
        },
        "checker_status": "passed" if cleanup_failed else "failed",
        "checker_reason": "Ответ верен" if cleanup_failed else "Неверное значение",
        "skip_reason": None,
        "trace_path": f"{run_id}/trace.json",
        "artifact_path": f"{run_id}/work",
        "errors": [
            {"stage": "cleanup", "code": "cleanup_failed", "message": "Ошибка очистки"}
        ]
        if cleanup_failed
        else [],
        "result": {
            "version": "v1",
            "task_id": task_id,
            "run_id": run_id,
            "session_id": session_id,
            "trace_id": run_id,
            "provider_profile": "mock",
            "model": "mock-codex",
            "provider_snapshot": {
                "name": "mock",
                "model": "mock-codex",
                "endpoint": "http://mock-provider:8090",
                "protocol": "responses",
                "auth": "none",
                "timeout_s": 15,
                "output_limit": 1024,
                "budget_mechanism": "mock-utf8-quarter-v1",
                "budget_mode": "strict",
                "enabled": True,
            },
            "execution_status": "success",
            "collection_status": "complete",
            "writers_stopped": True,
            "evidence_status": "not_required",
            "evidence": None,
            "errors": [],
            "tokens": {
                "limit": 30000,
                "mode": "strict",
                "output_limit": 1024,
                "over_target": None if cleanup_failed else False,
                "reserved": 0,
                "actual": None if cleanup_failed else 73,
                "input": None if cleanup_failed else 48,
                "output": None if cleanup_failed else 25,
                "cached": None if cleanup_failed else 0,
                "unknown_reason": "usage_unavailable" if cleanup_failed else None,
            },
            "provenance": {
                "codex_version": "0.159.3",
                "commit": COMMIT,
                "image_digest": IMAGE,
                "architecture": "arm64",
                "origin": "source_build",
            },
            "started_at": STARTED,
            "finished_at": FINISHED,
            "duration_s": 7.0,
            "last_seq": 3,
        },
    }


@pytest.fixture
def report() -> dict:
    tasks = load_dataset(DATASET, mounted=True)
    return {
        "version": "v1",
        "batch_id": "f" * 32,
        "provider_profile": "mock",
        "started_at": STARTED,
        "finished_at": FINISHED,
        "tasks": [
            attempt(tasks[0].id, 1, cleanup_failed=False),
            attempt(tasks[1].id, 2, cleanup_failed=True),
            *(pending(task.id) for task in tasks[2:]),
        ],
        "evaluated": 2,
        "wrong_answers": 1,
        "technical_skips": 3,
        "success": False,
        "course_model_status": {
            "status": "unconfirmed",
            "model_version": None,
            "intelligence_index": None,
            "checked_at": None,
            "reason": "Нет подтверждения допуска",
        },
    }


@pytest.mark.parametrize("with_continuation", [False, True])
def test_v1_analyzer_preserves_result_and_counters_in_eda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: dict,
    with_continuation: bool,
) -> None:
    if with_continuation:
        report["tasks"][0]["result"].update(
            harness_released=True,
            native_continuation_safe=True,
            proxy_closed=True,
            continuation_ready=True,
        )
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    output = tmp_path / "eda"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "analyze_baf",
            "--dataset",
            str(DATASET),
            "--report",
            str(report_file),
            "--output",
            str(output),
        ],
    )
    main()
    with (output / "runs.csv").open(encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert [row["task_id"] for row in rows] == [
        row["task_id"] for row in report["tasks"]
    ]
    assert rows[0]["checker_reason"] == "Неверное значение"
    assert rows[0]["tokens_total"] == "73" and rows[0]["tokens_cached"] == "0"
    assert rows[0]["duration_s"] == "7.0" and rows[0]["attempt_duration_s"] == "19.5"
    assert rows[0]["source_commit"] == COMMIT and rows[0]["image_digest"] == IMAGE
    assert rows[0]["trace_path"] == report["tasks"][0]["trace_path"]
    assert (
        rows[1]["execution_status"] == "success"
        and rows[1]["checker_status"] == "passed"
    )
    assert (
        rows[1]["files_status"] == "confirmed" and rows[1]["cleanup_status"] == "failed"
    )
    assert (
        rows[1]["tokens_total"] == ""
        and rows[1]["tokens_unknown_reason"] == "usage_unavailable"
    )
    assert (
        rows[2]["session_id"] == ""
        and rows[2]["skip_reason"] == "Запуск остановлен: cleanup_failed"
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert {
        field: summary["results"][field]
        for field in ("evaluated", "wrong_answers", "technical_skips", "success")
    } == {"evaluated": 2, "wrong_answers": 1, "technical_skips": 3, "success": False}


@pytest.mark.parametrize(
    "fault",
    [
        "counter",
        "order",
        "identity",
        "provenance",
        "tokens",
        "writers",
        "continuation",
        "cleanup",
        "execution",
        "model",
        "not_started",
        "version",
    ],
)
def test_v1_analyzer_rejects_inconsistent_report(report: dict, fault: str) -> None:
    row = report["tasks"][0]
    result = row["result"]
    if fault == "counter":
        report["technical_skips"] = 0
    elif fault == "order":
        report["tasks"][0], report["tasks"][1] = report["tasks"][1], report["tasks"][0]
    elif fault == "identity":
        result["session_id"] = "c" * 32
    elif fault == "provenance":
        result["provenance"]["image_digest"] = None
    elif fault == "tokens":
        result["tokens"]["actual"] = 72
    elif fault == "writers":
        result["writers_stopped"] = False
    elif fault == "continuation":
        result["continuation_ready"] = "true"
    elif fault == "cleanup":
        row["cleanup_status"] = "failed"
    elif fault == "execution":
        result["execution_status"] = "transport_error"
    elif fault == "model":
        result.update(model=None, provider_snapshot=None)
    elif fault == "not_started":
        report["tasks"][-1]["evidence_status"] = "complete"
    else:
        report["version"] = "v2"
    with pytest.raises(ValueError):
        runs_from_report(report, load_dataset(DATASET))


def test_v1_requires_evidence_to_accept_b5_checker_verdict(report: dict) -> None:
    task = load_dataset(DATASET, mounted=True)[-1]
    report.update(
        tasks=[attempt("B5.1", 5, cleanup_failed=True)],
        evaluated=1,
        wrong_answers=0,
        technical_skips=0,
    )
    with pytest.raises(ContractError, match="полного доказательства"):
        runs_from_report(report, (task,))


def test_v1_rejects_attempt_after_cleanup_failure(report: dict) -> None:
    tasks = load_dataset(DATASET, mounted=True)
    report["tasks"][2] = attempt(tasks[2].id, 3, cleanup_failed=False)
    report.update(evaluated=3, wrong_answers=2, technical_skips=2)
    with pytest.raises(ContractError, match="после ошибки очистки"):
        runs_from_report(report, tasks)


@pytest.mark.parametrize("successful_command", [True, False])
def test_v1_requires_successful_command_for_passing_b5_verdict(
    report: dict, successful_command: bool
) -> None:
    task = load_dataset(DATASET, mounted=True)[-1]
    row = attempt(task.id, 5, cleanup_failed=True)
    result = row["result"]
    item = {
        "type": "commandExecution",
        "command_kind": B5_COMMAND_KIND,
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "item_id": "item-1",
        "source": "agent",
    }
    result["evidence_status"] = row["evidence_status"] = "complete"
    result["evidence"] = {
        "version": "v1",
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "codex_version": "0.159.3",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "events": [
            {**item, "phase": "started"},
            {**item, "phase": "completed", "exit_code": 0},
        ]
        if successful_command
        else [],
    }
    report.update(tasks=[row], evaluated=1, wrong_answers=0, technical_skips=0)
    if successful_command:
        assert runs_from_report(report, (task,))[0]["checker_status"] == "passed"
    else:
        with pytest.raises(ContractError, match="расходится с доказательством"):
            runs_from_report(report, (task,))
        row["checker_status"] = "failed"
        report["wrong_answers"] = 1
        assert runs_from_report(report, (task,))[0]["checker_status"] == "failed"


def test_historical_v0_report_keeps_original_columns() -> None:
    path = Path("results/d72a5329d7aa4117bb41aa7995be74c9/report.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = runs_from_report(report, load_dataset(DATASET))
    assert [row["task_id"] for row in rows] == [
        row["task_id"] for row in report["tasks"]
    ]
    assert rows[0]["tokens_total"] == report["tasks"][0]["result"]["tokens"]["actual"]
    assert "session_id" not in rows[0]
