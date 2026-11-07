"""Контрактные проверки fixture и чекеров без модели и Docker."""

import copy
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from baf.runner.catalog import (
    B5_COMMAND,
    DEFAULT_CATALOG,
    CatalogError,
    Task,
    load_checkpoint,
)
from baf.runner.checkers import run_checker


@pytest.fixture
def tasks() -> tuple[Task, ...]:
    return load_checkpoint()


def prepare(task: Task, root: Path) -> Path:
    workdir = root / task.id
    workdir.mkdir()
    for file in task.fixture_files:
        path = workdir / file.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.content)
    return workdir


def check(task: Task, workdir: Path, evidence: dict | None = None) -> dict:
    return run_checker(
        task, workdir, run_id="run-1", collection_status="complete", evidence=evidence
    )


def proof(task: Task) -> dict:
    shared = {
        "type": "commandExecution",
        "source": "agent",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "item_id": "item-1",
        "command": B5_COMMAND,
        "cwd": "/work",
    }
    return {
        "version": "v0",
        "codex_version": "0.159.3",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "complete": True,
        "protected_inputs": True,
        "input_manifest": task.input_manifest,
        "events": [
            {**shared, "phase": "started"},
            {**shared, "phase": "completed", "exit_code": 0},
        ],
    }


def test_exactly_five_complete_tasks(tasks: tuple[Task, ...]) -> None:
    assert tuple(task.id for task in tasks) == ("B1.1", "B2.1", "B3.1", "B4.1", "B5.1")
    assert all(task.fixture_files and task.checker_path.is_file() for task in tasks)
    assert all(
        "check.py" not in {file.path for file in task.fixture_files} for task in tasks
    )
    assert len(tasks[-1].input_manifest) == 2


def test_incomplete_fixture_rejected_before_run(tmp_path: Path) -> None:
    shutil.copytree(DEFAULT_CATALOG.parent, tmp_path / "baf")
    (tmp_path / "baf/B1-read/B1.1-service-port/fixture/config/app.yaml").unlink()
    with pytest.raises(CatalogError, match="неполна"):
        load_checkpoint(tmp_path / "baf/checkpoint1.yaml")


@pytest.mark.parametrize("path", ["../elsewhere", "/tmp/escape", "B1-read/../other"])
def test_catalog_rejects_traversal(tmp_path: Path, path: str) -> None:
    content = DEFAULT_CATALOG.read_text().replace("B1-read/B1.1-service-port", path)
    catalog = tmp_path / "checkpoint1.yaml"
    catalog.write_text(content)
    with pytest.raises(CatalogError, match="путь"):
        load_checkpoint(catalog)


def test_catalog_does_not_include_full_catalog(tmp_path: Path) -> None:
    shutil.copytree(DEFAULT_CATALOG.parent, tmp_path / "baf")
    (tmp_path / "baf/catalog.yaml").write_text("невалидный: [")
    assert len(load_checkpoint(tmp_path / "baf/checkpoint1.yaml")) == 5


@pytest.mark.parametrize("index,answer", [(0, "8137"), (1, "src/utils/dates.py")])
def test_text_answers(
    tasks: tuple[Task, ...], tmp_path: Path, index: int, answer: str
) -> None:
    task = tasks[index]
    workdir = prepare(task, tmp_path)
    assert check(task, workdir)["checker_status"] == "failed"
    (workdir / "answer.txt").write_text("wrong")
    assert check(task, workdir)["pass"] is False
    (workdir / "answer.txt").write_text(answer + "\n")
    assert check(task, workdir)["pass"] is True


def test_b1_false_with_zero_exit(tasks: tuple[Task, ...], tmp_path: Path) -> None:
    workdir = prepare(tasks[0], tmp_path)
    result = subprocess.run(
        [sys.executable, str(tasks[0].checker_path), str(workdir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["pass"] is False
    assert check(tasks[0], workdir)["checker_status"] == "failed"


@pytest.mark.parametrize("change", ["correct", "type", "missing", "extra", "invalid"])
def test_json_schema(tasks: tuple[Task, ...], tmp_path: Path, change: str) -> None:
    workdir = prepare(tasks[2], tmp_path)
    content = {
        "service": "calendar",
        "port": 8137,
        "enabled": True,
        "tags": ["local", "test"],
    }
    if change == "type":
        content["port"] = "8137"
    elif change == "missing":
        del content["enabled"]
    elif change == "extra":
        content["unrequested"] = True
    (workdir / "config.json").write_text(
        "{" if change == "invalid" else json.dumps(content)
    )
    assert check(tasks[2], workdir)["pass"] is (change == "correct")


@pytest.mark.parametrize(
    "change", ["correct", "all_versions", "other_file", "extra_file"]
)
def test_only_install_may_change(
    tasks: tuple[Task, ...], tmp_path: Path, change: str
) -> None:
    workdir = prepare(tasks[3], tmp_path)
    readme = workdir / "README.md"
    text = readme.read_text().replace("calendar==1.2", "calendar==1.3")
    if change == "all_versions":
        text = text.replace("1.2", "1.3")
    elif change == "other_file":
        (workdir / "settings.ini").write_text("changed")
    elif change == "extra_file":
        (workdir / "extra.txt").write_text("extra")
    readme.write_text(text)
    assert check(tasks[3], workdir)["pass"] is (change == "correct")


def test_b5_valid_proof_ignores_stdout_truncation(
    tasks: tuple[Task, ...], tmp_path: Path
) -> None:
    task = tasks[4]
    workdir = prepare(task, tmp_path)
    (workdir / "answer.txt").write_text("11\n")
    evidence = {**proof(task), "stdout_truncated": True}
    assert check(task, workdir, evidence)["checker_status"] == "passed"


def test_b5_native_shell_command(tasks: tuple[Task, ...], tmp_path: Path) -> None:
    task = tasks[4]
    workdir = prepare(task, tmp_path)
    (workdir / "answer.txt").write_text("11")
    evidence = proof(task)
    for event in evidence["events"]:
        event["command"] = shlex.join(["/usr/bin/bash", "-lc", B5_COMMAND])
        event["source"] = "unifiedExecStartup"
    assert check(task, workdir, evidence)["pass"] is True


@pytest.mark.parametrize(
    "command",
    [
        B5_COMMAND.replace(" -I", "\n-I"),
        shlex.join(["/usr/bin/bash", "-lc", B5_COMMAND + "; echo 11"]),
        shlex.join(["/usr/bin/bash", "-lc", "echo " + B5_COMMAND]),
    ],
)
def test_b5_shell_grammar_rejected(
    tasks: tuple[Task, ...], tmp_path: Path, command: str
) -> None:
    task = tasks[4]
    workdir = prepare(task, tmp_path)
    (workdir / "answer.txt").write_text("11")
    evidence = proof(task)
    for event in evidence["events"]:
        event["command"] = command
    assert check(task, workdir, evidence)["pass"] is False


@pytest.mark.parametrize(
    "attack",
    [
        "guess",
        "echo",
        "script",
        "data",
        "foreign_run",
        "foreign_event",
        "nonzero",
        "python_env",
        "startup",
        "shell",
        "wrong_cwd",
        "manifest",
        "wrong_answer",
    ],
)
def test_b5_rejects_false_execution(
    tasks: tuple[Task, ...], tmp_path: Path, attack: str
) -> None:
    task = tasks[4]
    workdir = prepare(task, tmp_path)
    (workdir / "answer.txt").write_text("0" if attack == "wrong_answer" else "11")
    evidence = proof(task)
    if attack == "guess":
        evidence["events"] = []
    elif attack in ("script", "data"):
        name = "stats.py" if attack == "script" else "data.csv"
        (workdir / "input" / name).write_text("11")
    elif attack == "foreign_run":
        evidence["run_id"] = "run-2"
    elif attack == "foreign_event":
        evidence["events"][0]["run_id"] = "run-2"
    elif attack == "nonzero":
        evidence["events"][1]["exit_code"] = 1
    elif attack == "manifest":
        evidence["input_manifest"]["input/data.csv"] = "0" * 64
    elif attack in ("echo", "python_env", "startup", "shell", "wrong_cwd"):
        for event in evidence["events"]:
            if attack == "wrong_cwd":
                event["cwd"] = "/tmp"
            else:
                event["command"] = {
                    "echo": "echo stats.py",
                    "python_env": "PYTHONPATH=/work " + B5_COMMAND,
                    "startup": B5_COMMAND.replace(" -I -S", ""),
                    "shell": "sh -c '" + B5_COMMAND + "'",
                }[attack]
    assert check(task, workdir, evidence)["checker_status"] == "failed"


@pytest.mark.parametrize(
    "loss", ["missing", "flag", "end", "protected", "item", "duplicate"]
)
def test_b5_lost_evidence_is_technical_skip(
    tasks: tuple[Task, ...], tmp_path: Path, loss: str
) -> None:
    task = tasks[4]
    workdir = prepare(task, tmp_path)
    (workdir / "answer.txt").write_text("11")
    evidence = proof(task)
    if loss == "missing":
        evidence = None
    elif loss == "flag":
        evidence["complete"] = False
    elif loss == "end":
        evidence["events"].pop()
    elif loss == "protected":
        evidence["protected_inputs"] = False
    elif loss == "item":
        del evidence["events"][0]["item_id"]
    elif loss == "duplicate":
        evidence["events"].append(copy.deepcopy(evidence["events"][-1]))
    result = check(task, workdir, evidence)
    assert result["checker_status"] == "not_run_evidence_incomplete"
    assert result["pass"] is None


def test_checker_does_not_run_on_incomplete_collection(
    tasks: tuple[Task, ...], tmp_path: Path
) -> None:
    task = replace(tasks[0], directory=tmp_path / "does-not-exist")
    result = run_checker(task, tmp_path, run_id="run-1", collection_status="incomplete")
    assert result["checker_status"] == "not_run_collection_failed"


@pytest.mark.parametrize(
    "source",
    ["raise RuntimeError()", "print('invalid')", "import time; time.sleep(10)"],
)
def test_checker_failure_is_not_wrong_answer(
    tasks: tuple[Task, ...], tmp_path: Path, source: str
) -> None:
    (tmp_path / "check.py").write_text(source)
    task = replace(tasks[0], directory=tmp_path)
    result = run_checker(
        task, tmp_path, run_id="run-1", collection_status="complete", timeout_s=0.1
    )
    assert result["checker_status"] == "checker_error"
    assert result["pass"] is None


def test_python_isolation_ignores_startup_and_pythonpath(
    tasks: tuple[Task, ...], tmp_path: Path
) -> None:
    workdir = prepare(tasks[4], tmp_path)
    (workdir / "sitecustomize.py").write_text("raise RuntimeError('injection')")
    (workdir / "statistics.py").write_text("raise RuntimeError('injection')")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(workdir / "input/stats.py"),
            str(workdir / "input/data.csv"),
        ],
        cwd=workdir,
        env={
            "PYTHONPATH": str(workdir),
            "PYTHONSTARTUP": str(workdir / "sitecustomize.py"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "11"
