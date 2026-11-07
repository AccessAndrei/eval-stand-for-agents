"""Проверка выгруженных файлов и расходование предварительно выделенного места."""

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path

from contracts import validate
from services.common.storage import Storage, StorageBudgetExceeded

MAX_FILES_BYTES = 1_048_576
RUN_RESERVE_BYTES = 6_291_456


class ArtifactError(ValueError):
    """Невозможно подтвердить полную выгруженную копию."""


def decode_files(bundle: dict, result: dict) -> dict[str, bytes]:
    """Сопоставить ровно одно содержимое каждому элементу серверного манифеста."""
    manifest = {item["path"]: item for item in result["artifacts"]}
    entries = bundle.get("files")
    if not isinstance(entries, list) or len(entries) > 256:
        raise ArtifactError("Неверный список файлов")
    files = {}
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "content_base64"}:
            raise ArtifactError("Неверная запись файла")
        name = entry["path"]
        if not isinstance(name, str) or name not in manifest or name in files:
            raise ArtifactError("Путь отсутствует в манифесте или повторён")
        try:
            content = base64.b64decode(entry["content_base64"], validate=True)
        except (binascii.Error, ValueError, TypeError) as error:
            raise ArtifactError("Недопустимая кодировка файла") from error
        total += len(content)
        if total > MAX_FILES_BYTES:
            raise ArtifactError("Превышен размер выгруженных файлов")
        expected = manifest[name]
        if (
            len(content) != expected["size_bytes"]
            or hashlib.sha256(content).hexdigest() != expected["sha256"]
        ):
            raise ArtifactError("Файл не соответствует серверному манифесту")
        files[name] = content
    if set(files) != set(manifest):
        raise ArtifactError("Выгружены не все файлы серверного манифеста")
    return files


def _consume(storage: Storage, reserve: Path, destination: Path, data: bytes) -> None:
    """Уменьшить прикладной резерв перед записью; ошибки диска не скрываются.

    Резерв отчёта остаётся отдельным файлом. Общая квота файловой системы
    обеспечивается развёртыванием, а не этим прикладным учётом.
    """
    with storage.locked():
        remaining = reserve.stat().st_size - len(data)
        if remaining < 0:
            raise StorageBudgetExceeded("storage_budget_exceeded")
        with reserve.open("r+b") as stream:
            stream.truncate(remaining)
            stream.flush()
            os.fsync(stream.fileno())
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with destination.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def save_bundle(
    storage: Storage, reserve: Path, directory: Path, bundle: dict, result: dict
) -> Path:
    validate("result", result)
    files = decode_files(bundle, result)
    events = bundle.get("events")
    if not isinstance(events, list) or len(events) > 2048:
        raise ArtifactError("Недопустимый журнал событий")
    for event in events:
        validate("event", event)
        if event["run_id"] != result["run_id"] or event["task_id"] != result["task_id"]:
            raise ArtifactError("Журнал относится к другой попытке")
    evidence = bundle.get("evidence")
    metadata = {"evidence.json": evidence, "bundle.json": bundle}
    for name, value in metadata.items():
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        _consume(storage, reserve, directory / name, data)
    trace = "".join(
        json.dumps(event, ensure_ascii=False) + "\n" for event in events
    ).encode("utf-8")
    _consume(storage, reserve, directory / "events.jsonl", trace)
    workdir = directory / "work"
    workdir.mkdir(mode=0o700)
    for name, content in files.items():
        _consume(storage, reserve, workdir / name, content)
    reserve.unlink()
    return workdir
