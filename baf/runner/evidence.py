"""Проверка отдельного доверенного доказательства B5.1, не файлов агента.

Envelope v0 создаёт адаптер: version, codex_version, run_id, thread_id,
turn_id, complete, protected_inputs, input_manifest, events. events содержит
нормализованные нативные commandExecution с phase started/completed, теми же
run/thread/turn id, item_id, command, cwd, source; completed добавляет exit_code.
Поле complete выставляет сборщик доказательства, независимо от stdout.
"""

from dataclasses import dataclass

from contracts.tool_evidence import (
    B5_COMMAND,
    B5_COMMAND_KIND,
    B5_CWD,
    B5_SOURCES,
    is_b5_command,
)

MAX_EVENTS = 256
TOOL_FIELDS = ("type", "phase", "thread_id", "turn_id", "item_id")
RUN_IDENTITY_FIELDS = ("version", "session_id", "run_id")


@dataclass(frozen=True)
class EvidenceResult:
    complete: bool
    passed: bool
    reason: str


def _pairs(events: list[dict], evidence: dict) -> EvidenceResult:
    started: dict[str, dict] = {}
    finished: set[str] = set()
    succeeded = False
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "commandExecution":
            return EvidenceResult(False, False, "Повреждено событие инструмента")
        if (
            not isinstance(event.get("command"), str)
            or not event["command"]
            or not isinstance(event.get("cwd"), str)
            or not event["cwd"]
            or event.get("source")
            not in {
                "agent",
                "userShell",
                "unifiedExecStartup",
                "unifiedExecInteraction",
            }
        ):
            return EvidenceResult(False, False, "Потеряны поля нативного вызова")
        if any(
            event.get(key) != evidence[key]
            for key in ("run_id", "thread_id", "turn_id")
        ):
            return EvidenceResult(True, False, "Событие относится к другой попытке")
        item = event.get("item_id")
        if not isinstance(item, str) or not item:
            return EvidenceResult(False, False, "Потерян идентификатор вызова")
        phase = event.get("phase")
        if phase == "started" and item not in started:
            started[item] = event
        elif phase == "completed" and item in started and item not in finished:
            original = started[item]
            if any(
                event.get(key) != original.get(key)
                for key in ("command", "cwd", "source")
            ):
                return EvidenceResult(
                    False, False, "Начало и конец вызова не согласованы"
                )
            if type(event.get("exit_code")) is not int:
                return EvidenceResult(False, False, "Потерян код завершения")
            finished.add(item)
            succeeded |= (
                is_b5_command(event.get("command"))
                and event.get("cwd") == "/work"
                and event.get("source") in ("agent", "unifiedExecStartup")
                and event["exit_code"] == 0
            )
        else:
            return EvidenceResult(False, False, "Нарушен порядок событий вызова")
    if set(started) != finished:
        return EvidenceResult(False, False, "Потеряно завершение вызова")
    return EvidenceResult(
        True, succeeded, "ok" if succeeded else "Нет разрешённого успешного запуска"
    )


def validate_evidence(
    evidence: object, *, run_id: str, input_manifest: dict[str, str]
) -> EvidenceResult:
    """Отличить утрату доказательства от полного отрицательного результата."""
    if not isinstance(evidence, dict) or evidence.get("complete") is not True:
        return EvidenceResult(False, False, "Доказательство неполно")
    required = ("thread_id", "turn_id", "run_id")
    if any(
        not isinstance(evidence.get(key), str) or not evidence[key] for key in required
    ):
        return EvidenceResult(False, False, "Потеряны идентификаторы попытки")
    if evidence["run_id"] != run_id:
        return EvidenceResult(True, False, "Доказательство относится к другой попытке")
    if evidence.get("version") != "v0":
        return EvidenceResult(False, False, "Неверная версия доказательства")
    if evidence.get("codex_version") != "0.159.3":
        return EvidenceResult(False, False, "Неподдерживаемая версия нативных событий")
    if evidence.get("protected_inputs") is not True:
        return EvidenceResult(False, False, "Не подтверждена защита исходных файлов")
    if evidence.get("input_manifest") != input_manifest:
        return EvidenceResult(True, False, "Манифест исходных файлов не совпадает")
    events = evidence.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        return EvidenceResult(False, False, "Список событий повреждён или усечён")
    return _pairs(events, evidence)


def mounted_evidence_metadata(value: object) -> dict | None:
    """Оставить только ограниченные метаданные смонтированного доказательства."""
    if not isinstance(value, dict):
        return None
    events = value.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        return None
    if any(not isinstance(event, dict) for event in events):
        return None
    top_fields = (*RUN_IDENTITY_FIELDS, "codex_version", "thread_id", "turn_id")
    fields = (*TOOL_FIELDS, "session_id", "run_id", "command_kind", "source")

    def identifiers(source: dict, keys: tuple[str, ...]) -> dict:
        return {
            key: text
            for key in keys
            if isinstance(text := source.get(key), str)
            and 0 < len(text) <= 128
            and text.isascii()
            and text.replace("-", "").replace("_", "").replace(".", "").isalnum()
        }

    metadata = []
    for event in events:
        item = identifiers(event, fields)
        # Пустое или непонятное значение маркера не равно отсутствию команды.
        if any(
            field in event and field not in item
            for field in ("command_kind", "session_id", "run_id")
        ):
            return None
        exit_code = event.get("exit_code")
        if type(exit_code) is int and -(2**31) <= exit_code < 2**31:
            item["exit_code"] = exit_code
        metadata.append(item)
    return {**identifiers(value, top_fields), "events": metadata}


def mounted_b5_evidence(result: dict, input_manifest: dict[str, str]) -> dict | None:
    """Нормализовать события после сверки локальной защиты входов вызывающим кодом."""
    value = mounted_evidence_metadata(result.get("evidence"))
    if (
        result.get("evidence_status") != "complete"
        or value is None
        or result.get("version") != "v1"
        or any(
            not isinstance(result.get(field), str) or value.get(field) != result[field]
            for field in RUN_IDENTITY_FIELDS
        )
    ):
        return None
    thread_id, turn_id = value.get("thread_id"), value.get("turn_id")
    if thread_id is None or turn_id is None:
        return None
    events = []
    for item in value["events"]:
        if "command_kind" not in item:
            continue
        if (
            item["command_kind"] != B5_COMMAND_KIND
            or item.get("type") != "commandExecution"
            or item.get("thread_id") != thread_id
            or item.get("turn_id") != turn_id
            or item.get("source") not in B5_SOURCES
            or any(
                item.get(field, value[field]) != value[field]
                for field in ("session_id", "run_id")
            )
        ):
            return None
        fields = (*TOOL_FIELDS, "source", "exit_code")
        events.append(
            {
                **{field: item[field] for field in fields if field in item},
                "command": B5_COMMAND,
                "cwd": B5_CWD,
                "run_id": value["run_id"],
            }
        )
    return {
        "version": "v0",
        "codex_version": value.get("codex_version"),
        "run_id": value["run_id"],
        "thread_id": thread_id,
        "turn_id": turn_id,
        "complete": True,
        "protected_inputs": True,
        "input_manifest": input_manifest,
        "events": events,
    }
