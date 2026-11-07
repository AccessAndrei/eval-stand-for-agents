"""Пять последовательных попыток; серверные исходы сохраняются неизменными."""

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from baf.runner.artifacts import RUN_RESERVE_BYTES, ArtifactError, save_bundle
from baf.runner.catalog import TASK_IDS, CatalogError, Task, load_checkpoint
from baf.runner.checkers import run_checker
from baf.runner.transport import GatewayError, Transport, TransportError
from contracts import ContractError, fingerprint, validate
from services.common.storage import Storage, StorageBudgetExceeded

REPORT_RESERVE_BYTES = 1_048_576
CONTINUE_EXECUTION = {"success", "command_error", "timeout", "budget_exceeded"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def launch_request(task: Task, run_id: str, profile: str) -> dict:
    request = {
        "version": "v0",
        "run_id": run_id,
        "task_id": task.id,
        "provider_profile": profile,
        "prompt": "\n".join(task.messages),
        "timeout_s": task.timeout_s,
        "files": [
            {"path": file.path, "content": file.content.decode("utf-8")}
            for file in task.fixture_files
        ],
        "read_only_paths": ["input"] if task.evidence_required else [],
    }
    validate("launch", request)
    return request


def _empty_row(task: Task) -> dict:
    return {
        "task_id": task.id,
        "run_id": None,
        "result": None,
        "checker_status": "not_run",
        "checker_reason": None,
        "skip_reason": "Набор ещё не дошёл до задачи",
        "trace_path": None,
        "artifact_path": None,
        "errors": [],
    }


def _error(row: dict, stage: str, code: str, message: str) -> None:
    row["errors"].append({"stage": stage, "code": code, "message": message})


def _status_result(status: dict, request: dict) -> dict | None:
    validate("status", status)
    if (
        status["run_id"] != request["run_id"]
        or status["task_id"] != request["task_id"]
        or status["fingerprint"] != fingerprint(request)
    ):
        raise ContractError("Статус относится к другому запросу")
    if (
        status["result"] is not None
        and status["result"]["provider_profile"] != request["provider_profile"]
    ):
        raise ContractError("Сервер заменил профиль попытки")
    return status["result"]


def execute(
    transport: Transport,
    request: dict,
    *,
    poll_s: float = 0.2,
    grace_s: float | None = None,
) -> dict:
    """POST один раз; при потере ответа только lookup, затем ограниченная отмена."""
    path = "/v0/runs/" + request["run_id"]
    try:
        status = transport.request("POST", "/v0/runs", request)
        result = _status_result(status, request)
        if result is not None:
            return result
    except TransportError:
        pass
    # Auth95 + provision60 + подготовка/освобождение harness75 + сбор20
    # + release30 + транспорт20. Сам agent timeout задаётся отдельно.
    overhead = 300 if grace_s is None else grace_s
    cancel_grace = 180 if grace_s is None else grace_s
    deadline = time.monotonic() + request["timeout_s"] + overhead
    cancelled = False
    while True:
        try:
            result = _status_result(transport.request("GET", path), request)
            if result is not None:
                return result
        except TransportError:
            pass
        except GatewayError as error:
            if error.code != "not_found":
                raise
        if time.monotonic() >= deadline:
            if cancelled:
                raise TransportError("terminal_result_unavailable")
            cancelled = True
            deadline = time.monotonic() + cancel_grace
            try:
                transport.request(
                    "POST",
                    path + "/cancel",
                    {"version": "v0", "run_id": request["run_id"]},
                )
            except (GatewayError, TransportError):
                pass
        time.sleep(poll_s)


def _evaluate(
    transport: Transport,
    storage: Storage,
    reserve: Path,
    batch_dir: Path,
    task: Task,
    row: dict,
) -> None:
    result = row["result"]
    directory = batch_dir / row["run_id"]
    directory.mkdir(mode=0o700)
    bundle = transport.request("GET", f"/v0/runs/{row['run_id']}/artifacts")
    workdir = save_bundle(storage, reserve, directory, bundle, result)
    row["artifact_path"] = f"{row['run_id']}/work"
    row["trace_path"] = f"{row['run_id']}/events.jsonl"
    evidence = bundle.get("evidence")
    if task.evidence_required and result["evidence_status"] != "complete":
        evidence = None
    check = run_checker(
        task,
        workdir,
        run_id=row["run_id"],
        collection_status=result["collection_status"],
        evidence=evidence,
    )
    row["checker_status"] = check["checker_status"]
    row["checker_reason"] = check["reason"][:2048]
    if check["checker_status"] == "checker_error":
        _error(row, "checker", "checker_error", "Доверенный чекер завершился ошибкой")


def _attempt(
    transport: Transport,
    storage: Storage,
    batch_dir: Path,
    task: Task,
    row: dict,
    profile: str,
) -> str | None:
    run_id = uuid.uuid4().hex
    row["run_id"] = run_id
    reserve = storage.reserve("run-" + run_id, RUN_RESERVE_BYTES)
    try:
        result = execute(transport, launch_request(task, run_id, profile))
        row["result"] = result
        row["skip_reason"] = None
        try:
            _evaluate(transport, storage, reserve, batch_dir, task, row)
        except StorageBudgetExceeded:
            row["checker_status"] = "not_run_collection_failed"
            row["checker_reason"] = "Недостаточно места для полной копии"
            _error(row, "storage", "storage_budget_exceeded", "Исчерпан резерв копии")
            return "storage_budget_exceeded"
        except (
            ArtifactError,
            ContractError,
            GatewayError,
            TransportError,
            OSError,
        ) as error:
            row["checker_status"] = "not_run_collection_failed"
            row["checker_reason"] = "Не подтверждена полная выгруженная копия"
            _error(
                row,
                "collection",
                "artifact_transfer_failed",
                "Не удалось проверить и сохранить копию",
            )
            if isinstance(error, OSError):
                return "storage_budget_exceeded"
        if result["cleanup_status"] != "success":
            return "cleanup_failed"
        for error in result["errors"]:
            if error["code"] in {
                "storage_budget_exceeded",
                "event_storage_failed",
                "token_accounting_unavailable",
            }:
                return error["code"]
        if result["execution_status"] not in CONTINUE_EXECUTION:
            return result["execution_status"]
        if row["checker_status"] in {
            "checker_error",
            "not_run_collection_failed",
            "not_run_evidence_incomplete",
        }:
            return row["checker_status"]
        return None
    finally:
        # Освобождается лишь неиспользованный резерв этой попытки, не история.
        reserve.unlink(missing_ok=True)


def _report(batch_id: str, profile: str, started: str, rows: list[dict]) -> dict:
    evaluated = sum(row["checker_status"] in {"passed", "failed"} for row in rows)
    success = all(
        row["result"] is not None
        and row["result"]["execution_status"] == "success"
        and row["result"]["collection_status"] == "complete"
        and row["result"]["cleanup_status"] == "success"
        and row["result"]["evidence_status"] != "incomplete"
        and not row["result"]["errors"]
        and not row["errors"]
        and row["checker_status"] == "passed"
        for row in rows
    )
    return {
        "version": "v0",
        "batch_id": batch_id,
        "provider_profile": profile,
        "started_at": started,
        "finished_at": now(),
        "tasks": rows,
        "evaluated": evaluated,
        "wrong_answers": sum(row["checker_status"] == "failed" for row in rows),
        "technical_skips": 5 - evaluated,
        "success": success,
        "course_model_status": {
            "status": "unconfirmed",
            "model_version": None,
            "intelligence_index": None,
            "checked_at": None,
            "reason": "Версия модели, рейтинг и дата проверки допуска не подтверждены",
        },
    }


def run_batch(
    transport: Transport,
    *,
    profile: str,
    results: Path,
    storage_limit: int = 1_000_000_000,
    tasks: tuple[Task, ...] | None = None,
) -> tuple[Path, dict]:
    """Сохранить ровно пять исходов, включая незапущенные, без перезаписи истории."""
    tasks = load_checkpoint() if tasks is None else tasks
    if tuple(task.id for task in tasks) != TASK_IDS:
        raise CatalogError("Нужны ровно B1.1–B5.1")
    for task in tasks:
        launch_request(task, "validation", profile)
    batch_id = uuid.uuid4().hex
    started = now()
    storage = Storage(results, storage_limit)
    reserve = storage.reserve("batch-" + batch_id, REPORT_RESERVE_BYTES)
    batch_dir = results / batch_id
    batch_dir.mkdir(mode=0o700)
    rows = [_empty_row(task) for task in tasks]
    stop_reason = None
    for task, row in zip(tasks, rows, strict=True):
        if stop_reason is not None:
            row["skip_reason"] = "Запуск остановлен: " + stop_reason
            continue
        try:
            stop_reason = _attempt(transport, storage, batch_dir, task, row, profile)
        except StorageBudgetExceeded:
            stop_reason = "storage_budget_exceeded"
            _error(
                row, "storage", stop_reason, "Недостаточно места для следующей попытки"
            )
        except (GatewayError, TransportError, ContractError, OSError) as error:
            stop_reason = (
                error.code if isinstance(error, GatewayError) else "runtime_unavailable"
            )
            _error(
                row,
                "execution",
                stop_reason,
                "Не удалось получить подтверждённый исход попытки",
            )
        if row["result"] is None:
            row["skip_reason"] = "Попытка не подтверждена: " + str(stop_reason)
    report = _report(batch_id, profile, started, rows)
    validate("report", report)
    report_path = batch_dir / "report.json"
    storage.finalize(reserve, report_path, report)
    return report_path, report
