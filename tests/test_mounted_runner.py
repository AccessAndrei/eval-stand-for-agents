"""Локальные фикстуры и дерево результатов смонтированной попытки."""

import errno
import os
from pathlib import Path

import pytest
import yaml

from baf.runner.catalog import CatalogError, load_dataset
from baf.runner.mounted_files import (
    WorkspaceTreeError,
    inspect_worktree,
    prepare_fixture,
)
from services.common.storage import Storage, StorageBudgetExceeded


def task_files(root: Path, task_id: str, files: dict[str, bytes]) -> dict[str, str]:
    directory = root / task_id
    for name, content in files.items():
        path = directory / "fixture" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (directory / "check.py").write_text(
        'print("{\\"pass\\":true,\\"reason\\":\\"ok\\"}")\n', encoding="utf-8"
    )
    (directory / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": task_id,
                "baf": "B2",
                "messages": ["Найди нужный файл"],
                "timeout_s": 120,
                "answer_file": "answer.txt",
                "fixture_files": list(files),
            }
        ),
        encoding="utf-8",
    )
    return {"id": task_id, "directory": task_id}


def manifest(root: Path, entries: list[dict[str, str]]) -> Path:
    path = root / "dataset.yaml"
    path.write_text(yaml.safe_dump({"version": 0, "tasks": entries}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "ids",
    [
        ("own-search.1",),
        ("B4.1", "own-search.1", "B1.3", "B8.1", "B7.1", "B6.1"),
    ],
)
def test_mounted_dataset_preserves_manifest_order(
    tmp_path: Path, ids: tuple[str, ...]
) -> None:
    entries = [task_files(tmp_path, task_id, {"data.txt": b"text"}) for task_id in ids]
    tasks = load_dataset(manifest(tmp_path, entries), mounted=True)
    assert tuple(task.id for task in tasks) == ids


def test_mounted_dataset_checks_every_task_before_use(tmp_path: Path) -> None:
    good = task_files(tmp_path, "B2.1", {"text.txt": b"text"})
    broken = task_files(tmp_path, "own-search.1", {"text.txt": b"text"})
    (tmp_path / broken["directory"] / "check.py").unlink()
    with pytest.raises(CatalogError, match="check.py"):
        load_dataset(manifest(tmp_path, [good, broken]), mounted=True)


def test_repeated_task_alias_is_rejected_before_loading_fixtures(
    tmp_path: Path,
) -> None:
    entry = task_files(tmp_path, "B2.1", {"text.txt": b"text"})
    (tmp_path / entry["directory"] / "fixture" / "text.txt").unlink()
    with pytest.raises(CatalogError, match="повторяются"):
        load_dataset(manifest(tmp_path, [entry] * 100), mounted=True)


def test_dataset_rejects_unrepresentable_empty_fixture_directory(
    tmp_path: Path,
) -> None:
    entry = task_files(tmp_path, "B2.1", {"text.txt": b"text"})
    (tmp_path / entry["directory"] / "fixture" / "windows\\backup").mkdir()
    with pytest.raises(CatalogError, match="Недопустимый путь фикстуры"):
        load_dataset(manifest(tmp_path, [entry]), mounted=True)


def test_mounted_dataset_rejects_answer_outside_export(tmp_path: Path) -> None:
    entry = task_files(tmp_path, "B2.1", {"text.txt": b"text"})
    path = tmp_path / entry["directory"] / "task.yaml"
    description = yaml.safe_load(path.read_text(encoding="utf-8"))
    description["answer_file"] = ".git/answer.txt"
    path.write_text(yaml.safe_dump(description), encoding="utf-8")
    with pytest.raises(CatalogError, match="недоступный путь ответа"):
        load_dataset(manifest(tmp_path, [entry]), mounted=True)


def test_fresh_local_fixture_preserves_binary_bytes_and_nested_tree(
    tmp_path: Path,
) -> None:
    csv = "город;число\r\nТула;2\r\n".encode("cp1251")
    binary_files = {"nested/input.csv": csv, "nested/binary.bin": b"\xff\x00\x80"}
    files = {**binary_files, **{f"src/{i}.txt": b"x" for i in range(200)}}
    entry = task_files(tmp_path, "own-search.1", files)
    empty = tmp_path / entry["directory"] / "fixture" / "empty"
    empty.mkdir()
    (tmp_path / entry["directory"] / "expected.txt").write_bytes(b"outside export")
    (task,) = load_dataset(manifest(tmp_path, [entry]), mounted=True)
    (empty.parent / "nested/input.csv").write_bytes(b"source changed after preflight")

    storage = Storage(tmp_path / "results", 8192)
    copied_bytes = sum(map(len, files.values()))
    for number in (1, 2):
        attempt = storage.root / str(number)
        attempt.mkdir()
        reserve = storage.reserve(f"run-{number}", 1024)
        workdir = prepare_fixture(
            task, attempt / "work", storage=storage, reserve=reserve
        )
        tree = inspect_worktree(workdir)
        assert tree.total_bytes == copied_bytes
        assert dict(tree.files) == {
            name: len(content) for name, content in files.items()
        }
        assert "empty" in tree.directories and (workdir / "empty").is_dir()
        assert task.answer_file == "answer.txt"
        assert not (workdir / task.answer_file).exists()
        assert all(
            (workdir / name).read_bytes() == data for name, data in binary_files.items()
        )
        assert not (workdir / "check.py").exists()
        assert not (workdir / "expected.txt").exists()
        assert reserve.stat().st_size == 1024 - copied_bytes
        (workdir / task.answer_file).write_text("ответ попытки", encoding="utf-8")


def test_fixture_file_limit_applies_to_real_tree(tmp_path: Path) -> None:
    files = {f"src/{i}.txt": b"x" for i in range(257)}
    entry = task_files(tmp_path, "own-search.1", files)
    with pytest.raises(CatalogError, match="ограничение объёма"):
        load_dataset(manifest(tmp_path, [entry]), mounted=True)


def test_fixture_cannot_consume_more_than_attempt_reserve(tmp_path: Path) -> None:
    entry = task_files(tmp_path, "B2.1", {"nested/file": b"1234"})
    (task,) = load_dataset(manifest(tmp_path, [entry]), mounted=True)
    storage = Storage(tmp_path / "results", 32)
    reserve = storage.reserve("run-1", 3)
    destination = storage.root / "work"
    with pytest.raises(StorageBudgetExceeded):
        prepare_fixture(task, destination, storage=storage, reserve=reserve)
    assert not destination.exists()
    assert reserve.stat().st_size == 3


def test_interrupted_fixture_copy_removes_partial_tree_and_restores_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = task_files(tmp_path, "B2.1", {"nested/file": b"1234"})
    (task,) = load_dataset(manifest(tmp_path, [entry]), mounted=True)
    storage = Storage(tmp_path / "results", 32)
    reserve = storage.reserve("run-1", 12)
    destination = storage.root / "work"
    fsync = os.fsync
    calls = 0

    def abort_fixture_write_once(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "test out of space")
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", abort_fixture_write_once)
    with pytest.raises(StorageBudgetExceeded):
        prepare_fixture(task, destination, storage=storage, reserve=reserve)
    assert not destination.exists()
    assert reserve.read_bytes() == bytes(12)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "hidden"])
def test_untrusted_output_tree_cannot_supply_unsafe_entries(
    tmp_path: Path, kind: str
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("canary", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    if kind == "symlink":
        (workdir / "link").symlink_to(outside)
    elif kind == "hardlink":
        (workdir / "link").hardlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(workdir / "pipe")
    else:
        (workdir / ".GiT").mkdir()
    with pytest.raises(WorkspaceTreeError):
        inspect_worktree(workdir)


def test_workspace_limits_count_file_bytes_and_allow_absent_answer(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "work"
    (workdir / "nested").mkdir(parents=True)
    (workdir / "nested/source").write_bytes(b"123")
    assert inspect_worktree(workdir, max_bytes=3, max_files=1).files == (
        ("nested/source", 3),
    )
    with pytest.raises(WorkspaceTreeError, match="ограничение объёма"):
        inspect_worktree(workdir, max_bytes=2, max_files=1)
    (workdir / "second").touch()
    with pytest.raises(WorkspaceTreeError, match="ограничение объёма"):
        inspect_worktree(workdir, max_bytes=3, max_files=1)
