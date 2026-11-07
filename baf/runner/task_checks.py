"""Доверенные проверки содержимого полной стабильной копии."""

import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

from jsonschema import Draft202012Validator

from baf.runner.evidence import validate_evidence


def _text(workdir: Path, name: str) -> str | None:
    path = workdir / name
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65_536:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError:
        return None


def _result(passed: bool, reason: str = "Ответ не соответствует заданию") -> dict:
    return {"pass": passed, "reason": "ok" if passed else reason}


def check_search(workdir: Path) -> dict:
    answer = _text(workdir, "answer.txt")
    return _result(answer is not None and answer.strip() == "src/utils/dates.py")


def check_json(workdir: Path, task_dir: Path) -> dict:
    text = _text(workdir, "config.json")
    if text is None:
        return _result(False, "config.json отсутствует либо недопустим")
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return _result(False, "config.json не является корректным JSON")
    schema = json.loads((task_dir / "schema.json").read_text(encoding="utf-8"))
    return _result(Draft202012Validator(schema).is_valid(value))


def _tree(root: Path) -> dict[str, bytes] | None:
    files = {}
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            return None
        if not path.is_file():
            continue
        total += path.stat().st_size
        if total > 1_000_000 or len(files) >= 64:
            return None
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def check_edit(workdir: Path, task_dir: Path) -> dict:
    expected = _tree(task_dir / "fixture")
    if expected is None:
        raise ValueError("Повреждена доверенная fixture")
    expected["README.md"] = expected["README.md"].replace(
        b"pip install calendar==1.2", b"pip install calendar==1.3"
    )
    return _result(
        _tree(workdir) == expected, "Изменено не только значение версии в Install"
    )


def check_tool(workdir: Path, task_dir: Path, evidence: object, run_id: str) -> dict:
    manifest = {
        name: sha256((task_dir / "fixture" / name).read_bytes()).hexdigest()
        for name in ("input/stats.py", "input/data.csv")
    }
    proof = validate_evidence(evidence, run_id=run_id, input_manifest=manifest)
    if not proof.complete:
        return {
            "pass": None,
            "reason": proof.reason,
            "checker_status": "not_run_evidence_incomplete",
        }
    if not proof.passed:
        return _result(False, proof.reason)
    for name, expected in manifest.items():
        path = workdir / name
        if (
            path.parent.is_symlink()
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 65_536
        ):
            return _result(False, "Защищённые исходные файлы отсутствуют")
        if sha256(path.read_bytes()).hexdigest() != expected:
            return _result(False, "Защищённые исходные файлы изменены")
    answer = _text(workdir, "answer.txt")
    try:
        correct = answer is not None and Decimal(answer.strip()) == Decimal(11)
    except InvalidOperation:
        correct = False
    return _result(correct, "Неверная медиана")
