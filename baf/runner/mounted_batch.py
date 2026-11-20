"""Последовательная оценка локальных папок через рабочие сессии v1."""

from __future__ import annotations

import os
import time
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from client.session import ClientError, CloseReport, MountedClient, MountedSession
from contracts import ContractError
from contracts.mounted import validate_mounted
from services.common.storage import Storage, StorageBudgetExceeded, encode

from .batch import CONTINUE_EXECUTION, now, report_reserve_bytes
from .catalog import CatalogError, Task
from .checkers import run_checker
from .evidence import (
    TOOL_FIELDS,
    mounted_b5_evidence,
    mounted_evidence_metadata,
    validate_evidence,
)
from .mounted_files import (
    MAX_WORKTREE_BYTES,
    MAX_WORKTREE_FILES,
    WorkspaceTreeError,
    export_path,
    inspect_worktree,
    prepare_fixture,
    read_fixture_tree,
)

TRACE_RESERVE_BYTES = 131_072
MAX_TRACE_EVENTS = 256
RUN_RESERVE_BYTES = MAX_WORKTREE_BYTES + TRACE_RESERVE_BYTES
CONTINUE_ERRORS = {"command_error", "timeout", "budget_exceeded"}
PROTECTED_PATH = "input"
ProtectedTree = tuple[tuple[str, ...], dict[str, str]]


def _row(task: Task, *, smoke: bool) -> dict[str, Any]:
    row = {
        "task_id": task.id,
        "session_id": None,
        "run_id": None,
        "result": None,
        "started_at": None,
        "finished_at": None,
        "duration_s": None,
        "files_status": "not_run",
        "cleanup_status": "not_run",
        "cleanup": None,
        "evidence_status": "not_run",
        "checker_status": "not_run",
        "checker_reason": None,
        "skip_reason": "Набор ещё не дошёл до задачи",
        "trace_path": None,
        "artifact_path": None,
        "errors": [],
    }
    if smoke:
        row["smoke_status"] = "not_run"
    return row


def _error(row: dict, stage: str, code: str, message: str) -> None:
    row["errors"].append({"stage": stage, "code": code, "message": message})


def _reserve_work_capacity(storage: Storage, reserve: Path) -> int:
    # Экспорт расходует выделенную часть квоты через заданный лимит записи.
    # Резерв трассы остаётся материализован до записи и закрытия сессии.
    with storage.locked():
        with reserve.open("r+b") as stream:
            capacity = os.fstat(stream.fileno()).st_size - TRACE_RESERVE_BYTES
            if capacity < 0:
                raise StorageBudgetExceeded("storage_budget_exceeded")
            stream.truncate(TRACE_RESERVE_BYTES)
            stream.flush()
            os.fsync(stream.fileno())
    return capacity


def _trace_event(event: dict) -> dict:
    record = {"seq": event["seq"], "type": event["type"]}
    if event["type"] == "tool":
        tool = event["tool"]
        metadata = {
            field: value
            for field in TOOL_FIELDS
            if isinstance(value := tool.get(field), str)
            and len(value) <= 64
            and value.isascii()
            and value.replace("_", "").replace("-", "").isalnum()
        }
        exit_code = tool.get("exit_code")
        if type(exit_code) is int and -(2**31) <= exit_code < 2**31:
            metadata["exit_code"] = exit_code
        record["tool"] = metadata
    return record


def _protected_tree(workdir: Path) -> ProtectedTree:
    tree, contents = read_fixture_tree(
        workdir, max_bytes=MAX_WORKTREE_BYTES, max_files=MAX_WORKTREE_FILES
    )
    prefix = PROTECTED_PATH + "/"
    return (
        tuple(
            path
            for path in tree.directories
            if path == PROTECTED_PATH or path.startswith(prefix)
        ),
        {
            path: sha256(data).hexdigest()
            for path, data in contents.items()
            if path.startswith(prefix)
        },
    )


async def _receive(
    session: MountedSession, task: Task, run_id: str, profile: str, trace: dict
) -> dict:
    result = None
    async for event in session.events(run_id, timeout=task.timeout_s + 120):
        if result is not None:
            raise ContractError("Поток содержит события после итога")
        if len(trace["events"]) < MAX_TRACE_EVENTS:
            trace["events"].append(_trace_event(event))
        else:
            trace["truncated"] = True
        if event["type"] != "result":
            continue
        result = event["result"]
        validate_mounted("run_result", result)
        if (
            result["session_id"] != session.session_id
            or result["run_id"] != run_id
            or result.get("task_id") != task.id
            or result.get("provider_profile") != profile
        ):
            raise ContractError("Итог относится к другой попытке или профилю")
        if "evidence" in result:
            result = {
                **result,
                "evidence": mounted_evidence_metadata(result["evidence"]),
            }
        try:
            encode(result)
        except (ValueError, RecursionError) as error:
            raise ContractError("Итог невозможно сохранить в отчёте") from error
    if result is None:
        raise ContractError("Итог попытки отсутствует")
    return result


def _record_close(row: dict, report: CloseReport) -> None:
    server = report.server or {}
    server_status = server.get("cleanup_status")
    row["cleanup_status"] = "success" if report.ok else "failed"
    row["cleanup"] = {
        "server_status": server_status,
        "export_drained": report.export_drained,
        "tunnel_closed": report.tunnel_closed,
        "error": report.server_error.code if report.server_error else None,
    }
    if not report.ok:
        _error(row, "cleanup", "cleanup_failed", "Очистка сессии не подтверждена")


def _skip_check(row: dict, status: str, reason: str) -> str:
    row["checker_status"] = status
    row["checker_reason"] = reason
    row["skip_reason"] = reason
    return status


def _skip_evaluation(row: dict, status: str, reason: str, *, smoke: bool) -> str:
    if smoke:
        row.update(smoke_status="technical_skip", skip_reason=reason)
        return status
    return _skip_check(row, status, reason)


def _evidence_skip(row: dict, reason: str) -> str:
    row["evidence_status"] = "incomplete"
    _error(row, "evidence", "evidence_unconfirmed", reason)
    return _skip_check(row, "not_run_evidence_incomplete", reason)


def _run_error(result: dict) -> str | None:
    for error in result["errors"]:
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str):
            return "invalid_result"
        if code not in CONTINUE_ERRORS:
            return code
    status = result["execution_status"]
    return None if status in CONTINUE_EXECUTION else status


def _evaluate(
    task: Task,
    workdir: Path,
    row: dict,
    protected_before: ProtectedTree | None,
    *,
    smoke: bool,
    run_error: str | None,
) -> str | None:
    result = row["result"]
    cleanup = row["cleanup"]
    if (
        result is None
        or not result["writers_stopped"]
        or result["collection_status"] != "complete"
        or cleanup is None
        or not cleanup["export_drained"]
        or not cleanup["tunnel_closed"]
        or cleanup["server_status"] not in {"success", "failed"}
    ):
        row["files_status"] = "unconfirmed"
        if task.evidence_required:
            row["evidence_status"] = "incomplete"
        _error(row, "collection", "worktree_unconfirmed", "Рабочая папка не сверена")
        return _skip_evaluation(
            row,
            "not_run_collection_failed",
            "Состояние рабочей папки не подтверждено",
            smoke=smoke,
        )
    protected_after = None
    tree = None
    try:
        if task.evidence_required and not smoke:
            protected_after = _protected_tree(workdir)
        else:
            tree = inspect_worktree(workdir)
    except WorkspaceTreeError:
        row["files_status"] = "unsafe"
        if task.evidence_required:
            row["evidence_status"] = "incomplete"
        _error(row, "collection", "unsafe_worktree", "Рабочая папка нарушает границы")
        return _skip_evaluation(
            row, "not_run_collection_failed", "Рабочая папка небезопасна", smoke=smoke
        )
    row["files_status"] = "confirmed"
    if smoke:
        if row["cleanup_status"] != "success" or run_error is not None:
            return _skip_evaluation(
                row,
                "not_run_execution_failed",
                "Инфраструктурный исход попытки",
                smoke=True,
            )
        if result["execution_status"] != "success" or result["errors"]:
            row.update(
                smoke_status="failed", skip_reason="Запуск не завершился успешно"
            )
            return None
        assert tree is not None
        if any(name == task.answer_file for name, _ in tree.files):
            row["smoke_status"] = "passed"
        else:
            row.update(smoke_status="failed", skip_reason="Ожидаемый файл отсутствует")
        return None
    if result["execution_status"] not in CONTINUE_EXECUTION:
        return _skip_check(
            row,
            "not_run_execution_failed",
            "Инфраструктурный исход попытки",
        )

    evidence = None
    if task.evidence_required:
        if protected_before is None or protected_before != protected_after:
            return _evidence_skip(row, "Не подтверждена неизменность защищённых входов")
        evidence = mounted_b5_evidence(result, protected_before[1])
        proof = validate_evidence(
            evidence, run_id=row["run_id"], input_manifest=task.input_manifest
        )
        if not proof.complete:
            return _evidence_skip(row, proof.reason)
    check = run_checker(
        task,
        workdir,
        run_id=row["run_id"],
        collection_status="complete",
        evidence=evidence,
    )
    row["checker_status"] = check["checker_status"]
    row["checker_reason"] = check["reason"][:2048]
    if row["checker_status"] == "checker_error":
        _error(row, "checker", "checker_error", "Доверенный чекер завершился ошибкой")
        return "checker_error"
    if row["checker_status"] == "not_run_evidence_incomplete":
        return _evidence_skip(row, row["checker_reason"])
    return None


async def _attempt(
    client: MountedClient,
    storage: Storage,
    directory: Path,
    task: Task,
    profile: str,
    row: dict,
    *,
    smoke: bool,
) -> str | None:
    run_id = uuid4().hex
    reserve = storage.reserve(f"mounted-run-{run_id}", RUN_RESERVE_BYTES)
    started = time.monotonic()
    row.update(run_id=run_id, started_at=now(), skip_reason=None)
    attempt = directory / run_id
    workdir = attempt / "work"
    trace = {
        "version": "v1",
        "session_id": None,
        "run_id": run_id,
        "task_id": task.id,
        "events": [],
        "truncated": False,
    }
    stop_reason = None
    try:
        attempt.mkdir(mode=0o700)
        prepare_fixture(task, workdir, storage=storage, reserve=reserve)
        row["artifact_path"] = f"{run_id}/work"
        protected_before = None
        if task.evidence_required and not smoke:
            try:
                protected_before = _protected_tree(workdir)
            except WorkspaceTreeError:
                return _evidence_skip(row, "Не удалось сверить защищённые входы")
            directories, manifest = protected_before
            if (
                PROTECTED_PATH not in directories
                or not manifest
                or manifest != task.input_manifest
            ):
                return _evidence_skip(row, "Защищённые входы не совпадают с фикстурой")
        capacity = _reserve_work_capacity(storage, reserve)
        session = client.session(
            workdir,
            profile=profile,
            mode="rw",
            protected_paths=(PROTECTED_PATH,) if task.evidence_required else (),
            write_limit=capacity,
            entry_limit=MAX_WORKTREE_FILES,
        )
        row["session_id"] = trace["session_id"] = session.session_id
        try:
            await session.mount()
            await session.start_run(
                "\n".join(task.messages),
                timeout_s=task.timeout_s,
                task_id=task.id,
                run_id=run_id,
            )
            row["result"] = await _receive(session, task, run_id, profile, trace)
            row["evidence_status"] = row["result"]["evidence_status"]
        except (ClientError, ContractError) as error:
            code = error.code if isinstance(error, ClientError) else "invalid_result"
            stage = error.stage if isinstance(error, ClientError) else "execution"
            _error(row, stage, code, "Не удалось получить подтверждённый итог попытки")
            stop_reason = code
        finally:
            _record_close(row, await session.close())
        result = row["result"]
        run_error = _run_error(result) if result is not None else None
        check_reason = _evaluate(
            task, workdir, row, protected_before, smoke=smoke, run_error=run_error
        )
        storage.finalize(reserve, attempt / "trace.json", trace)
        row["trace_path"] = f"{run_id}/trace.json"
        if row["cleanup_status"] != "success":
            return "cleanup_failed"
        if stop_reason is not None:
            return stop_reason
        return run_error if run_error is not None else check_reason
    finally:
        row.update(finished_at=now(), duration_s=time.monotonic() - started)
        # История остаётся на месте, освобождается только остаток резерва.
        reserve.unlink(missing_ok=True)


def _report(
    batch_id: str, profile: str, started: str, rows: list[dict], *, smoke: bool
) -> dict:
    evaluated = sum(row["checker_status"] in {"passed", "failed"} for row in rows)
    success = all(
        (result := row["result"]) is not None
        and result["execution_status"] == "success"
        and row["files_status"] == "confirmed"
        and row["cleanup_status"] == "success"
        and row["evidence_status"] != "incomplete"
        and not result["errors"]
        and not row["errors"]
        and row["checker_status"] == "passed"
        for row in rows
    )
    report = {
        "version": "v1",
        "batch_id": batch_id,
        "provider_profile": profile,
        "started_at": started,
        "finished_at": now(),
        "tasks": rows,
        "evaluated": evaluated,
        "wrong_answers": sum(row["checker_status"] == "failed" for row in rows),
        "technical_skips": len(rows) - evaluated,
        "success": success,
        "course_model_status": {
            "status": "unconfirmed",
            "model_version": None,
            "intelligence_index": None,
            "checked_at": None,
            "reason": "Версия модели, рейтинг и дата проверки допуска не подтверждены",
        },
    }
    if smoke:
        report.update(
            mode="smoke",
            success=all(row["smoke_status"] == "passed" for row in rows),
            technical_skips=sum(
                row["smoke_status"] in {"technical_skip", "not_run"} for row in rows
            ),
        )
    return report


async def run_mounted_batch(
    client: MountedClient,
    *,
    profile: str,
    results: Path,
    tasks: tuple[Task, ...],
    storage_limit: int = 1_000_000_000,
    smoke: bool = False,
) -> tuple[Path, dict]:
    """Зарезервировать отчёт и выполнить датасет через один headless-клиент."""
    if not tasks or len({task.id for task in tasks}) != len(tasks):
        raise CatalogError("Нужен непустой набор задач с уникальными ID")
    try:
        validate_mounted("profile", profile)
        for task in tasks:
            if smoke:
                export_path(task.answer_file)
            validate_mounted(
                "run_start",
                {
                    "run_id": "0" * 32,
                    "task_id": task.id,
                    "prompt": "\n".join(task.messages),
                    "timeout_s": task.timeout_s,
                },
            )
    except (ContractError, WorkspaceTreeError) as error:
        raise CatalogError("Недопустимый профиль или описание попытки v1") from error
    batch_id = uuid4().hex
    storage = Storage(results, storage_limit)
    reserve = storage.reserve(
        f"mounted-batch-{batch_id}", report_reserve_bytes(len(tasks))
    )
    directory = results / batch_id
    started = now()
    rows = [_row(task, smoke=smoke) for task in tasks]
    stop_reason = None
    try:
        directory.mkdir(mode=0o700)
        async with client:
            for task, row in zip(tasks, rows, strict=True):
                if stop_reason is not None:
                    row["skip_reason"] = "Запуск остановлен: " + stop_reason
                    continue
                row["skip_reason"] = None
                try:
                    stop_reason = await _attempt(
                        client, storage, directory, task, profile, row, smoke=smoke
                    )
                except StorageBudgetExceeded:
                    stop_reason = "storage_budget_exceeded"
                    _error(
                        row, "storage", stop_reason, "Недостаточно места для попытки"
                    )
                except (OSError, WorkspaceTreeError):
                    stop_reason = "runner_io_error"
                    _error(
                        row, "runner", stop_reason, "Ошибка локальной папки или отчёта"
                    )
                if smoke and row["run_id"] is not None and row["errors"]:
                    row["smoke_status"] = "technical_skip"
                if row["result"] is None and not row["skip_reason"]:
                    row["skip_reason"] = "Попытка не подтверждена: " + str(stop_reason)
        report = _report(batch_id, profile, started, rows, smoke=smoke)
        path = directory / "report.json"
        storage.finalize(reserve, path, report)
        return path, report
    finally:
        reserve.unlink(missing_ok=True)
