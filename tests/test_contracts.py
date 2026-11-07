"""Контракты отвергают опасные входы и ложные успешные итоги."""

from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from contracts import ContractError, fingerprint, schema, validate
from contracts.validation import MESSAGE_KINDS
from services.llm_proxy.profiles import load_profiles


@pytest.fixture
def launch() -> dict:
    return {
        "version": "v0",
        "run_id": "run-1",
        "task_id": "B1.1",
        "provider_profile": "mock",
        "prompt": "Прочитайте порт.",
        "files": [{"path": "config/app.yaml", "content": "port: 8080\n"}],
        "timeout_s": 180,
    }


@pytest.fixture
def result() -> dict:
    return {
        "version": "v0",
        "run_id": "run-1",
        "trace_id": "trace-1",
        "task_id": "B1.1",
        "provider_profile": "mock",
        "model": "mock-model",
        "execution_status": "success",
        "collection_status": "complete",
        "cleanup_status": "success",
        "evidence_status": "not_required",
        "errors": [],
        "artifacts": [],
        "tokens": {
            "limit": 30000,
            "actual": 20,
            "unknown_reason": None,
            "reserved": 0,
            "input": 12,
            "output": 8,
            "cached": 0,
        },
        "started_at": "2026-11-06T12:00:00Z",
        "finished_at": "2026-11-06T12:00:01Z",
        "duration_s": 1,
        "provenance": {
            "codex_version": "0.159.3",
            "commit": "a" * 40,
            "origin": "source_build",
            "image_digest": "sha256:" + "b" * 64,
            "architecture": "aarch64",
        },
    }


def test_all_schemas_are_valid_and_self_contained() -> None:
    for kind in MESSAGE_KINDS:
        Draft202012Validator.check_schema(schema(kind))


def test_valid_launch_and_fingerprint_canonicalization(launch: dict) -> None:
    validate("launch", launch)
    original = deepcopy(launch)
    reordered = dict(reversed(list(launch.items())))
    reordered["read_only_paths"] = []
    assert fingerprint(launch) == fingerprint(reordered)
    assert launch == original
    reordered["prompt"] += " Ещё раз."
    assert fingerprint(launch) != fingerprint(reordered)


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        r"a\b",
        "C:/windows",
        "a\x00b",
        "a\nb",
        "%2e%2e/secret",
        ".",
        "..",
    ],
)
def test_reject_unsafe_paths(launch: dict, path: str) -> None:
    launch["files"][0]["path"] = path
    with pytest.raises(ContractError):
        validate("launch", launch)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "v1"),
        ("provider_profile", "unknown"),
        ("run_id", "../run"),
        ("timeout_s", 0),
        ("timeout_s", True),
        ("endpoint", "https://example.test"),
        ("api_key", "secret-canary"),
    ],
)
def test_reject_invalid_launch_without_echoing_secret(
    launch: dict,
    field: str,
    value: object,
) -> None:
    launch[field] = value
    with pytest.raises(ContractError) as error:
        validate("launch", launch)
    assert "secret-canary" not in str(error.value)


def test_duplicate_and_parent_file_conflicts(launch: dict) -> None:
    launch["files"].append(dict(launch["files"][0]))
    with pytest.raises(ContractError, match="Повторяющиеся"):
        validate("launch", launch)
    launch["files"][1]["path"] = "config"
    with pytest.raises(ContractError, match="каталогом"):
        validate("launch", launch)


def test_byte_budget_not_character_budget(launch: dict) -> None:
    launch["files"][0]["content"] = "я" * 600_000
    with pytest.raises(ContractError, match="объём"):
        validate("launch", launch)


def test_read_only_paths_must_exist(launch: dict) -> None:
    launch["read_only_paths"] = ["input"]
    with pytest.raises(ContractError, match="отсутствует"):
        validate("launch", launch)
    launch["read_only_paths"] = ["config"]
    validate("launch", launch)


def test_timeout_collection_failure_keep_independent_outcomes(result: dict) -> None:
    result.update(execution_status="timeout", collection_status="incomplete")
    result["errors"] = [
        {"stage": "execution", "code": "timeout", "message": "Срок истёк"},
        {"stage": "collection", "code": "copy_failed", "message": "Нет копии"},
    ]
    validate("result", result)
    assert result["cleanup_status"] == "success"


def test_actual_unknown_requires_reason_and_keeps_reservation(result: dict) -> None:
    result["tokens"].update(actual=None, input=None, output=None, reserved=2000)
    with pytest.raises(ContractError, match="причины"):
        validate("result", result)
    result["tokens"]["unknown_reason"] = "Поток оборван до usage"
    validate("result", result)


@pytest.mark.parametrize(
    "mutation",
    [
        {"reserved": 29981},
        {"actual": 19},
        {"cached": 13},
        {"limit": 30001},
    ],
)
def test_invalid_token_accounting(result: dict, mutation: dict) -> None:
    result["tokens"].update(mutation)
    with pytest.raises(ContractError):
        validate("result", result)


def test_failed_cleanup_requires_error_but_does_not_erase_answer(result: dict) -> None:
    result["cleanup_status"] = "failed"
    with pytest.raises(ContractError, match="очистки"):
        validate("result", result)
    result["errors"] = [{"stage": "cleanup", "code": "timeout", "message": "Не удалён"}]
    validate("result", result)


def test_status_cannot_publish_result_early(result: dict, launch: dict) -> None:
    status = {
        "version": "v0",
        "run_id": "run-1",
        "task_id": "B1.1",
        "fingerprint": fingerprint(launch),
        "state": "running",
        "updated_at": result["finished_at"],
        "result": result,
    }
    with pytest.raises(ContractError, match="терминальное"):
        validate("status", status)
    status["state"] = "terminal"
    validate("status", status)
    status["result"]["run_id"] = "other"
    with pytest.raises(ContractError, match="другой"):
        validate("status", status)


def make_report(result: dict) -> dict:
    rows = []
    for index in range(1, 6):
        item = deepcopy(result)
        item.update(task_id=f"B{index}.1", run_id=f"run-{index}")
        if index == 5:
            item["evidence_status"] = "complete"
        rows.append(
            {
                "task_id": item["task_id"],
                "run_id": item["run_id"],
                "result": item,
                "checker_status": "passed",
                "checker_reason": None,
                "skip_reason": None,
                "trace_path": f"{index}/events.jsonl",
                "artifact_path": f"{index}/artifacts",
            }
        )
    return {
        "version": "v0",
        "batch_id": "batch-1",
        "provider_profile": "mock",
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
        "tasks": rows,
        "evaluated": 5,
        "wrong_answers": 0,
        "technical_skips": 0,
        "success": True,
        "course_model_status": {
            "status": "unconfirmed",
            "model_version": None,
            "intelligence_index": None,
            "checked_at": None,
            "reason": "Фиктивная модель",
        },
    }


def test_full_report_success_is_derived_from_every_outcome(result: dict) -> None:
    report = make_report(result)
    validate("report", report)
    report["tasks"][0]["checker_status"] = "failed"
    report["wrong_answers"] = 1
    with pytest.raises(ContractError, match="итог"):
        validate("report", report)
    report["success"] = False
    validate("report", report)


def test_report_rejects_duplicate_tasks_and_false_model_admission(result: dict) -> None:
    report = make_report(result)
    report["tasks"][1] = deepcopy(report["tasks"][0])
    with pytest.raises(ContractError, match="ровно"):
        validate("report", report)
    report = make_report(result)
    report["course_model_status"]["status"] = "confirmed"
    with pytest.raises(ContractError, match="Допуск"):
        validate("report", report)


def test_no_checker_on_incomplete_evidence_or_collection(result: dict) -> None:
    report = make_report(result)
    report["tasks"][4]["result"]["evidence_status"] = "incomplete"
    with pytest.raises(ContractError, match="доказательства"):
        validate("report", report)
    report["tasks"][4]["checker_status"] = "not_run_evidence_incomplete"
    report.update(evaluated=4, technical_skips=1, success=False)
    validate("report", report)
    report["tasks"][0]["result"].update(collection_status="not_started")
    with pytest.raises(ContractError, match="неполной"):
        validate("report", report)


@pytest.mark.parametrize(
    "kind", ["lookup", "collect", "release", "manager_cancel", "connect"]
)
def test_manager_control_cannot_choose_container_or_command(kind: str) -> None:
    message = {"version": "v0", "run_id": "run-1", "project_id": "project-1"}
    if kind != "lookup":
        message["handle"] = "opaque-handle"
    validate(kind, message)
    message["command"] = "docker rm unrelated"
    with pytest.raises(ContractError):
        validate(kind, message)


def test_evidence_binds_native_events_and_keeps_stdout_independent() -> None:
    event = {
        "type": "commandExecution",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "item_id": "item-1",
        "command": "echo stats.py",
        "cwd": "/tmp",
        "source": "agent",
    }
    evidence = {
        "version": "v0",
        "codex_version": "0.159.3",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "complete": True,
        "protected_inputs": True,
        "stdout_truncated": True,
        "input_manifest": {"input/stats.py": "a" * 64, "input/data.csv": "b" * 64},
        "events": [
            {**event, "phase": "started"},
            {**event, "phase": "completed", "exit_code": 0},
        ],
    }
    # Контракт сохраняет полное отрицательное доказательство; оценка — у чекера.
    validate("evidence", evidence)
    evidence["events"][1]["run_id"] = "other"
    with pytest.raises(ContractError, match="другому"):
        validate("evidence", evidence)
    evidence["events"].pop()
    with pytest.raises(ContractError, match="завершение"):
        validate("evidence", evidence)
    evidence["complete"] = False
    validate("evidence", evidence)


def observed_result(result: dict) -> dict:
    profile = load_profiles(Path("config/profiles.json"))["luna"]
    result.update(
        provider_profile="luna",
        model="gpt-6-luna",
        provider_snapshot=profile.snapshot(),
    )
    result["tokens"].update(
        mode="observed_usage",
        output_limit=2048,
        over_target=True,
        input=31_000,
        output=100,
        actual=31_100,
    )
    return result


def test_observed_overshoot_is_reported_without_forcing_failed_execution(
    result: dict,
) -> None:
    result = observed_result(result)
    validate("result", result)
    assert result["execution_status"] == "success"
    report = make_report(result)
    report["provider_profile"] = "luna"
    validate("report", report)
    assert report["success"] is True


@pytest.mark.parametrize(
    "change", [{"over_target": False}, {"mode": "strict"}, {"output_limit": 4096}]
)
def test_observed_budget_metadata_cannot_hide_or_relabel_overshoot(
    result: dict, change: dict
) -> None:
    result = observed_result(result)
    result["tokens"].update(change)
    with pytest.raises(ContractError):
        validate("result", result)


def test_observed_unknown_usage_is_not_claimed_as_zero_or_upper_bound(
    result: dict,
) -> None:
    result = observed_result(result)
    result["tokens"].update(
        actual=None,
        over_target=None,
        unknown_reason="upstream_usage_unconfirmed",
        reserved=2048,
    )
    validate("result", result)
    result["tokens"]["over_target"] = False
    with pytest.raises(ContractError, match="превышения"):
        validate("result", result)
