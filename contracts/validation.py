"""Структурные и межполевые ограничения контрактов первого чекпоинта."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

MAX_FILES_BYTES = 1_048_576
TASK_IDS = ("B1.1", "B2.1", "B3.1", "B4.1", "B5.1")
MESSAGE_KINDS = frozenset(
    (
        "launch",
        "status",
        "event",
        "result",
        "report",
        "provision",
        "lookup",
        "collect",
        "release",
        "cancel",
        "manager_cancel",
        "connect",
        "manager_status",
        "evidence",
    )
)


class ContractError(ValueError):
    """Сообщение не соответствует контракту; текст не раскрывает его содержимое."""


def schema(kind: str) -> dict[str, Any]:
    """Вернуть автономную схему сообщения, включая локальные определения."""
    if kind not in MESSAGE_KINDS:
        raise ContractError("Неизвестный вид сообщения")
    document = json.loads(Path(__file__).with_name("v0.schema.json").read_text())
    return {**document, "$ref": f"#/$defs/{kind}"}


@lru_cache(maxsize=len(MESSAGE_KINDS))
def _validator(kind: str) -> Draft202012Validator:
    document = schema(kind)
    Draft202012Validator.check_schema(document)
    return Draft202012Validator(document, format_checker=FormatChecker())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def validate(kind: str, payload: Any) -> None:
    """Проверить JSON-схему и семантику, не изменяя исходное сообщение."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError("Ожидалось корректное UTF-8 JSON-сообщение") from exc
    _require(len(encoded) <= 2_500_000, "Сообщение превышает предел размера")
    error = next(_validator(kind).iter_errors(payload), None)
    if error is not None:
        # ValidationError.message может содержать секрет из отклонённого поля.
        raise ContractError(f"Нарушено ограничение схемы: {error.validator}")
    if kind in {"launch", "provision"}:
        _validate_files(payload)
    elif kind == "result":
        _validate_result(payload)
    elif kind == "status":
        _validate_status(payload)
    elif kind == "report":
        _validate_report(payload)
    elif kind == "manager_status":
        if payload["state"] == "released":
            _require(payload["cleanup_status"] == "success", "Release не подтверждён")
    elif kind == "evidence":
        _validate_evidence(payload)


def fingerprint(launch: dict[str, Any]) -> str:
    """Канонический fingerprint валидного запроса; порядок файлов незначим."""
    validate("launch", launch)
    canonical = {
        **launch,
        "files": sorted(launch["files"], key=lambda item: item["path"]),
        "read_only_paths": sorted(launch.get("read_only_paths", [])),
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_files(payload: dict[str, Any]) -> None:
    paths = [item["path"] for item in payload["files"]]
    _require(len(paths) == len(set(paths)), "Повторяющиеся пути файлов")
    _require(
        sum(len(item["content"].encode("utf-8")) for item in payload["files"])
        <= MAX_FILES_BYTES,
        "Превышен суммарный объём исходных файлов",
    )
    _require(
        all(not any(q.startswith(p + "/") for q in paths) for p in paths),
        "Файл одновременно является каталогом",
    )
    read_only = payload.get("read_only_paths", [])
    _require(len(read_only) == len(set(read_only)), "Повторяющаяся защищённая область")
    for path in read_only:
        _require(
            any(p == path or p.startswith(path + "/") for p in paths),
            "Защищённая область отсутствует в исходных файлах",
        )


def _validate_tokens(tokens: dict[str, Any]) -> None:
    actual = tokens["actual"]
    _require(
        (actual is None) == (tokens["unknown_reason"] is not None),
        "Неизвестный расход требует причины; известный её исключает",
    )
    mode = tokens.get("mode", "strict")
    if mode == "strict":
        known = (
            actual
            if actual is not None
            else (tokens["input"] or 0) + (tokens["output"] or 0)
        )
        _require(
            known + tokens["reserved"] <= tokens["limit"],
            "Расход и удержанный резерв превышают токенный предел",
        )
    else:
        _require(
            tokens["reserved"] <= tokens["output_limit"],
            "Наблюдаемый режим резервирует только выход одного запроса",
        )
    if "over_target" in tokens:
        _require(
            tokens["over_target"]
            == (None if actual is None else actual > tokens["limit"]),
            "Неверная отметка превышения целевого расхода",
        )
    if actual is not None:
        _require(
            tokens["input"] is not None and tokens["output"] is not None,
            "Известный расход требует входные и выходные токены",
        )
        _require(tokens["input"] + tokens["output"] == actual, "Неверная сумма токенов")
    if tokens["input"] is not None and tokens["cached"] is not None:
        _require(tokens["cached"] <= tokens["input"], "Кэш превышает входные токены")


def _validate_times(payload: dict[str, Any]) -> None:
    start = datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
    finish = datetime.fromisoformat(payload["finished_at"].replace("Z", "+00:00"))
    _require(finish >= start, "Завершение предшествует началу")


def _validate_result(result: dict[str, Any]) -> None:
    _validate_tokens(result["tokens"])
    _validate_times(result)
    snapshot = result.get("provider_snapshot")
    if snapshot is not None:
        _require(
            snapshot["name"] == result["provider_profile"]
            and snapshot["model"] == result["model"],
            "Снимок профиля относится к другой конфигурации",
        )
        _require(
            snapshot.get("budget_mode", "strict")
            == result["tokens"].get("mode", "strict"),
            "Режим бюджета не совпадает со снимком профиля",
        )
        if "output_limit" in result["tokens"]:
            _require(
                snapshot["output_limit"] == result["tokens"]["output_limit"],
                "Лимит выхода не совпадает со снимком профиля",
            )
    if result["tokens"].get("mode") == "observed_usage":
        _require(
            result["provider_profile"] == "luna"
            and result["model"] == "gpt-6-luna"
            and snapshot is not None,
            "Наблюдаемый бюджет разрешён только для согласованного профиля Luna",
        )
    _require(math.isfinite(result["duration_s"]), "Длительность должна быть конечной")
    artifacts = result["artifacts"]
    _require(
        len({item["path"] for item in artifacts}) == len(artifacts),
        "Повторяющиеся артефакты",
    )
    if result["collection_status"] == "not_started":
        _require(not artifacts, "Артефакты невозможны до сбора")
    if result["cleanup_status"] == "failed":
        _require(
            any(error["stage"] == "cleanup" for error in result["errors"]),
            "Ошибка очистки требует отдельного описания",
        )
    if result["collection_status"] == "incomplete":
        _require(
            any(error["stage"] == "collection" for error in result["errors"]),
            "Неполный сбор требует отдельного описания",
        )
    if result["task_id"] == "B5.1":
        _require(
            result["evidence_status"] != "not_required",
            "B5.1 требует структурированных доказательств",
        )
    provenance = result["provenance"]
    if provenance["origin"] != "unavailable":
        _require(
            all(value is not None for value in provenance.values()),
            "Происхождение бинарника требует полной идентификации",
        )


def _validate_status(status: dict[str, Any]) -> None:
    result = status["result"]
    _require(
        (status["state"] == "terminal") == (result is not None),
        "Только терминальное состояние содержит результат",
    )
    if result is not None:
        _validate_result(result)
        _require(
            all(status[key] == result[key] for key in ("run_id", "task_id")),
            "Результат относится к другой попытке",
        )


def _validate_evidence(evidence: dict[str, Any]) -> None:
    started: set[str] = set()
    completed: set[str] = set()
    commands: dict[str, tuple[str, str]] = {}
    for event in evidence["events"]:
        _require(
            all(
                event[key] == evidence[key]
                for key in ("run_id", "thread_id", "turn_id")
            ),
            "Доказательство относится к другому вызову",
        )
        item_id = event["item_id"]
        command = (event["command"], event["cwd"])
        if event["phase"] == "started":
            _require(item_id not in started, "Повторное начало вызова")
            started.add(item_id)
            commands[item_id] = command
        else:
            _require(item_id not in completed, "Повторное завершение вызова")
            completed.add(item_id)
            _require(
                item_id in started and commands[item_id] == command,
                "Завершение не соответствует началу вызова",
            )
    if evidence["complete"]:
        _require(started == completed, "В полном доказательстве потеряно завершение")


def _validate_task_report(row: dict[str, Any]) -> None:
    result = row["result"]
    if result is None:
        _require(row["checker_status"] == "not_run", "Нет результата для чекера")
        _require(row["skip_reason"] is not None, "Пропуск требует причины")
        return
    _validate_result(result)
    _require(
        all(row[key] == result[key] for key in ("run_id", "task_id")),
        "Отчёт относится к другой попытке",
    )
    transfer_failed = any(
        (error["stage"], error["code"])
        in {
            ("collection", "artifact_transfer_failed"),
            ("storage", "storage_budget_exceeded"),
        }
        for error in row.get("errors", [])
    )
    if result["collection_status"] != "complete" or transfer_failed:
        _require(
            row["checker_status"] == "not_run_collection_failed",
            "Чекер запрещён на неполной копии",
        )
    elif row["task_id"] == "B5.1" and result["evidence_status"] != "complete":
        _require(
            row["checker_status"] == "not_run_evidence_incomplete",
            "Чекер B5.1 запрещён без полного доказательства",
        )


def _task_success(row: dict[str, Any]) -> bool:
    result = row["result"]
    return bool(
        result is not None
        and result["execution_status"] == "success"
        and result["collection_status"] == "complete"
        and result["cleanup_status"] == "success"
        and result["evidence_status"] != "incomplete"
        and row["checker_status"] == "passed"
        and not result["errors"]
        and not row.get("errors", [])
    )


def _validate_report(report: dict[str, Any]) -> None:
    _validate_times(report)
    rows = report["tasks"]
    _require(tuple(row["task_id"] for row in rows) == TASK_IDS, "Нужны ровно B1.1–B5.1")
    for row in rows:
        _validate_task_report(row)
        if row["result"] is not None:
            _require(
                row["result"]["provider_profile"] == report["provider_profile"],
                "Профиль нельзя менять внутри набора",
            )
    evaluated = sum(row["checker_status"] in {"passed", "failed"} for row in rows)
    wrong = sum(row["checker_status"] == "failed" for row in rows)
    _require(report["evaluated"] == evaluated, "Неверное число оценённых ответов")
    _require(report["wrong_answers"] == wrong, "Неверное число неправильных ответов")
    _require(report["technical_skips"] == 5 - evaluated, "Неверное число пропусков")
    _require(report["success"] == all(map(_task_success, rows)), "Неверный итог набора")
    model = report["course_model_status"]
    if model["status"] == "confirmed":
        _require(
            all(
                model[key] is not None
                for key in ("model_version", "intelligence_index", "checked_at")
            ),
            "Допуск модели не подтверждён данными",
        )
    else:
        _require(model["reason"] is not None, "Неподтверждённый допуск требует причины")
