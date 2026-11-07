"""Проверка отдельного доверенного доказательства B5.1, не файлов агента.

Envelope v0 создаёт адаптер: version, codex_version, run_id, thread_id,
turn_id, complete, protected_inputs, input_manifest, events. events содержит
нормализованные нативные commandExecution с phase started/completed, теми же
run/thread/turn id, item_id, command, cwd, source; completed добавляет exit_code.
Поле complete выставляет сборщик доказательства, независимо от stdout.
"""

import shlex
from dataclasses import dataclass

from baf.runner.catalog import B5_COMMAND

MAX_EVENTS = 256


@dataclass(frozen=True)
class EvidenceResult:
    complete: bool
    passed: bool
    reason: str


def _allowed_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    try:
        # Codex сериализует argv через shlex_join. Внутри разрешённой оболочки
        # принимается только точная строка, без иной shell-грамматики.
        return command == B5_COMMAND or shlex.split(command, posix=True) == [
            "/usr/bin/bash",
            "-lc",
            B5_COMMAND,
        ]
    except ValueError:
        return False


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
                _allowed_command(event.get("command"))
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
