"""Проверка формата сохранённого отчёта смонтированных попыток v1."""

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .validation import ContractError, _validate_tokens

_CHECKED = {"passed", "failed", "checker_error"}
_EVALUATED = {"passed", "failed"}


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    report, *references = (
        json.loads(Path(__file__).with_name(name).read_text(encoding="utf-8"))
        for name in (
            "mounted_report.schema.json",
            "mounted.schema.json",
            "v0.schema.json",
        )
    )
    Draft202012Validator.check_schema(report)
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in references
    )
    return Draft202012Validator(
        report, registry=registry, format_checker=FormatChecker()
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _timestamp(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("Ожидалось время с часовым поясом")
        return moment
    except ValueError as error:
        raise ContractError("Недопустимое время в отчёте v1") from error


def _check_times(record: dict[str, Any]) -> None:
    _require(
        _timestamp(record["finished_at"]) >= _timestamp(record["started_at"]),
        "Завершение предшествует началу",
    )


def _check_result(row: dict[str, Any], profile: str) -> None:
    result = row["result"]
    _require(
        all(
            result[field] == row[field] for field in ("session_id", "run_id", "task_id")
        ),
        "Итог относится к другой попытке",
    )
    _require(result["provider_profile"] == profile, "Итог содержит другой профиль")
    _check_times(result)
    _validate_tokens(result["tokens"])
    provenance = result["provenance"]
    if provenance["origin"] != "unavailable":
        _require(
            all(value is not None for value in provenance.values()),
            "Происхождение бинарника требует полной идентификации",
        )
    snapshot = result["provider_snapshot"]
    if result["execution_status"] == "success":
        _require(
            snapshot is not None and result["model"] is not None,
            "Успешный итог требует конфигурации модели",
        )
    if snapshot is not None:
        _require(
            snapshot["name"] == profile and snapshot["model"] == result["model"],
            "Снимок относится к другой конфигурации провайдера",
        )
    if row["evidence_status"] == "complete":
        evidence = result["evidence"]
        _require(
            result["evidence_status"] == "complete"
            and evidence is not None
            and all(
                evidence.get(field) == result[field]
                for field in ("session_id", "run_id")
            ),
            "Доказательства не подтверждены для этой попытки",
        )


def _check_row(row: dict[str, Any], profile: str) -> None:
    run_id, result = row["run_id"], row["result"]
    checked = row["checker_status"] in _CHECKED
    if run_id is None:
        _require(
            all(
                row[field] is None
                for field in (
                    "session_id",
                    "result",
                    "started_at",
                    "finished_at",
                    "duration_s",
                    "trace_path",
                    "artifact_path",
                )
            )
            and all(
                row[field] == "not_run"
                for field in (
                    "files_status",
                    "cleanup_status",
                    "evidence_status",
                    "checker_status",
                )
            ),
            "Незапущенная задача содержит данные попытки",
        )
    else:
        _require(
            all(
                row[field] is not None
                for field in ("started_at", "finished_at", "duration_s")
            ),
            "В отчёте отсутствует интервал попытки",
        )
        _check_times(row)
        _require(
            row["trace_path"] in (None, f"{run_id}/trace.json")
            and row["artifact_path"] in (None, f"{run_id}/work"),
            "Ссылка относится к другой попытке",
        )
    if result is None:
        _require(
            not checked and row["skip_reason"] is not None,
            "Нет подтверждённого итога для чекера или причины пропуска",
        )
    else:
        _check_result(row, profile)
    cleanup = row["cleanup"]
    if cleanup is None:
        _require(row["cleanup_status"] == "not_run", "Исход очистки без сведений")
    else:
        confirmed = (
            cleanup["server_status"] in (None, "success")
            and cleanup["error"] is None
            and cleanup["export_drained"]
            and cleanup["tunnel_closed"]
        )
        _require(
            row["session_id"] is not None
            and row["cleanup_status"] == ("success" if confirmed else "failed"),
            "Неверный итог очистки",
        )
    if row["files_status"] == "confirmed":
        _require(
            result is not None
            and result["writers_stopped"]
            and result["collection_status"] == "complete"
            and cleanup is not None
            and cleanup["export_drained"]
            and cleanup["tunnel_closed"]
            and cleanup["server_status"] in {"success", "failed"}
            and row["artifact_path"] is not None,
            "Рабочая папка подтверждена без барьера записи",
        )
    if checked:
        _require(
            row["files_status"] == "confirmed"
            and row["evidence_status"] in {"complete", "not_required"}
            and row["checker_reason"] is not None,
            "Проверка указана без подтверждённой рабочей папки или доказательств",
        )


def _succeeded(row: dict[str, Any]) -> bool:
    result = row["result"]
    return bool(
        result is not None
        and result["execution_status"] == "success"
        and row["files_status"] == "confirmed"
        and row["cleanup_status"] == "success"
        and row["evidence_status"] != "incomplete"
        and row["checker_status"] == "passed"
        and not row["errors"]
        and not result["errors"]
    )


def validate_mounted_report(report: Any) -> None:
    """Проверить JSON и согласованность попыток с итоговыми счётчиками."""
    try:
        json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ContractError("Ожидался корректный отчёт JSON v1") from error
    error = next(_validator().iter_errors(report), None)
    if error is not None:
        raise ContractError(f"Нарушение схемы отчёта v1 ({error.validator})")
    _check_times(report)
    rows = report["tasks"]
    _require(
        len({row["task_id"] for row in rows}) == len(rows),
        "Повторяющиеся задачи в отчёте",
    )
    for field in ("session_id", "run_id"):
        values = [row[field] for row in rows if row[field] is not None]
        _require(
            len(set(values)) == len(values), "Повторяющиеся идентификаторы попыток"
        )
    cleanup_failed = False
    for row in rows:
        _require(
            not cleanup_failed or row["run_id"] is None,
            "Новая попытка запущена после ошибки очистки",
        )
        _check_row(row, report["provider_profile"])
        cleanup_failed |= row["cleanup_status"] == "failed"
    evaluated = sum(row["checker_status"] in _EVALUATED for row in rows)
    _require(report["evaluated"] == evaluated, "Неверное число оценённых ответов")
    _require(
        report["wrong_answers"]
        == sum(row["checker_status"] == "failed" for row in rows),
        "Неверное число неправильных ответов",
    )
    _require(
        report["technical_skips"] == len(rows) - evaluated, "Неверное число пропусков"
    )
    _require(report["success"] == all(map(_succeeded, rows)), "Неверный итог набора")
    model = report["course_model_status"]
    if model["checked_at"] is not None:
        _timestamp(model["checked_at"])
    if model["status"] == "confirmed":
        _require(
            all(
                model[field] is not None
                for field in ("model_version", "intelligence_index", "checked_at")
            ),
            "Допуск модели не подтверждён данными",
        )
    else:
        _require(model["reason"] is not None, "Неподтверждённый допуск требует причины")
