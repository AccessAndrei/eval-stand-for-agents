"""Запуск доверенного чекера вне рабочей папки агента с ограничением срока."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from baf.runner.catalog import Task
from baf.runner.evidence import validate_evidence


def _outcome(status: str, reason: str, passed: bool | None = None) -> dict:
    return {"checker_status": status, "pass": passed, "reason": reason}


def _parse_result(stdout: str, returncode: int) -> dict:
    if returncode != 0 or len(stdout.encode("utf-8")) > 65_536:
        return _outcome(
            "checker_error", "Чекер завершился с ошибкой либо превысил объём вывода"
        )
    try:
        result = json.loads(stdout)
    except (ValueError, RecursionError):
        return _outcome("checker_error", "Чекер не вернул допустимый JSON")
    if not isinstance(result, dict) or not isinstance(result.get("reason"), str):
        return _outcome("checker_error", "Неверный формат результата чекера")
    if (
        result.get("checker_status") == "not_run_evidence_incomplete"
        and result.get("pass") is None
    ):
        return _outcome("not_run_evidence_incomplete", result["reason"])
    if type(result.get("pass")) is not bool:
        return _outcome(
            "checker_error", "В результате чекера отсутствует логическое pass"
        )
    return _outcome(
        "passed" if result["pass"] else "failed", result["reason"], result["pass"]
    )


def run_checker(
    task: Task,
    workdir: Path,
    *,
    run_id: str,
    collection_status: str,
    evidence: dict | None = None,
    timeout_s: float = 10,
) -> dict:
    """Проверять лишь полную копию; не путать технический пропуск с неверным ответом."""
    if collection_status != "complete":
        return _outcome("not_run_collection_failed", "Сбор файлов неполон")
    if task.evidence_required:
        proof = validate_evidence(
            evidence, run_id=run_id, input_manifest=task.input_manifest
        )
        if not proof.complete:
            return _outcome("not_run_evidence_incomplete", proof.reason)
    payload = json.dumps(evidence, ensure_ascii=False)
    if len(payload.encode("utf-8")) > 1_000_000:
        return _outcome("not_run_evidence_incomplete", "Превышен объём доказательства")
    command = [sys.executable, "-I", str(task.checker_path), str(workdir.resolve())]
    if task.evidence_required:
        command.append(run_id)
    try:
        with subprocess.Popen(
            command,
            cwd=task.directory,
            env={"PATH": os.defpath},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        ) as process:
            try:
                stdout, _ = process.communicate(payload, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)
                return _outcome("checker_error", "Чекер превысил срок выполнения")
            return _parse_result(stdout, process.returncode)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return _outcome("checker_error", "Не удалось выполнить доверенный чекер")
