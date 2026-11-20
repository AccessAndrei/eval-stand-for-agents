"""Подготовка и проверка локального дерева одной попытки БАФ."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from unicodedata import normalize

from services.common.storage import Storage, StorageBudgetExceeded

if TYPE_CHECKING:
    from .catalog import Task

MAX_WORKTREE_BYTES = 1_048_576
MAX_WORKTREE_FILES = 256
MAX_WORKTREE_ENTRIES = 4096
_MAX_DEPTH = 64
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW


class WorkspaceTreeError(ValueError):
    """Дерево не удовлетворяет границам и лимитам локальной проверки."""


@dataclass(frozen=True)
class WorkspaceTree:
    files: tuple[tuple[str, int], ...]
    directories: tuple[str, ...]
    total_bytes: int


def _file_bytes(parent: int, name: str, previous: os.stat_result) -> bytes:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        current = os.fstat(descriptor)
        if (
            not os.path.samestat(current, previous)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_size != previous.st_size
        ):
            raise WorkspaceTreeError("Файл изменился при чтении")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(previous.st_size + 1)
        if len(content) != previous.st_size:
            raise WorkspaceTreeError("Размер файла изменился при чтении")
        return content
    finally:
        os.close(descriptor)


def _scan(
    workdir: Path, *, max_bytes: int, max_files: int, read_files: bool
) -> tuple[WorkspaceTree, dict[str, bytes]]:
    files: list[tuple[str, int]] = []
    directories: list[str] = []
    contents: dict[str, bytes] = {}
    total_bytes = 0

    def walk(parent: int, prefix: tuple[str, ...], device: int) -> None:
        nonlocal total_bytes
        with os.scandir(parent) as entries:
            for entry in entries:
                parts = (*prefix, entry.name)
                name = "/".join(parts)
                if len(files) + len(directories) >= MAX_WORKTREE_ENTRIES:
                    raise WorkspaceTreeError("Дерево превышает лимит объектов")
                if len(parts) > _MAX_DEPTH:
                    raise WorkspaceTreeError("Дерево превышает лимит глубины")
                if normalize("NFC", entry.name).casefold() == ".git":
                    raise WorkspaceTreeError("Дерево содержит запрещённый путь .git")
                if read_files and "\\" in entry.name:
                    raise WorkspaceTreeError("Недопустимый путь фикстуры")
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device:
                    raise WorkspaceTreeError("Объект на другой файловой системе")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, _DIR_FLAGS, dir_fd=parent)
                    try:
                        if not os.path.samestat(info, os.fstat(child)):
                            raise WorkspaceTreeError("Каталог изменился при проверке")
                        directories.append(name)
                        walk(child, parts, device)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspaceTreeError("Дерево содержит ссылку или спецфайл")
                else:
                    total_bytes += info.st_size
                    if len(files) >= max_files or total_bytes > max_bytes:
                        raise WorkspaceTreeError("Дерево превышает ограничение объёма")
                    files.append((name, info.st_size))
                    if read_files:
                        contents[name] = _file_bytes(parent, entry.name, info)

    try:
        descriptor = os.open(workdir, _DIR_FLAGS)
        try:
            walk(descriptor, (), os.fstat(descriptor).st_dev)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise WorkspaceTreeError("Не удалось проверить локальное дерево") from error
    tree = WorkspaceTree(tuple(sorted(files)), tuple(sorted(directories)), total_bytes)
    return tree, contents


def read_fixture_tree(
    root: Path, *, max_bytes: int, max_files: int
) -> tuple[WorkspaceTree, dict[str, bytes]]:
    """Прочитать байты фикстуры через те же границы, что и у итогового дерева."""
    return _scan(root, max_bytes=max_bytes, max_files=max_files, read_files=True)


def inspect_worktree(
    workdir: Path,
    *,
    max_bytes: int = MAX_WORKTREE_BYTES,
    max_files: int = MAX_WORKTREE_FILES,
) -> WorkspaceTree:
    """Проверить дерево после подтверждённой остановки писателей и экспорта."""
    if max_bytes < 0 or max_files < 0:
        raise ValueError("Лимиты локального дерева должны быть неотрицательными")
    tree, _ = _scan(workdir, max_bytes=max_bytes, max_files=max_files, read_files=False)
    return tree


def export_path(name: str) -> PurePosixPath:
    """Проверить относительный путь в дереве, которое получает агент."""

    parts = name.split("/")
    if (
        len(parts) > _MAX_DEPTH
        or any(
            part in {"", ".", ".."} or normalize("NFC", part).casefold() == ".git"
            for part in parts
        )
        or "\\" in name
        or "\x00" in name
    ):
        raise WorkspaceTreeError("Недопустимый путь фикстуры")
    return PurePosixPath(name)


def prepare_fixture(
    task: Task, workdir: Path, *, storage: Storage, reserve: Path
) -> Path:
    """Создать новый workdir из проверенной фикстуры и расходовать резерв попытки."""
    root = storage.root.resolve(strict=True)
    parent = workdir.parent.resolve(strict=True)
    if not parent.is_relative_to(root) or reserve.parent.resolve(strict=True) != root:
        raise ValueError("Рабочая папка и резерв должны принадлежать хранилищу")
    workdir = parent / workdir.name
    files = [(export_path(file.path), file.content) for file in task.fixture_files]
    size = sum(len(content) for _, content in files)
    directories = {export_path(name) for name in task.fixture_directories}
    directories.update(
        parent for file, _ in files for parent in file.parents if parent.parts
    )
    with storage.locked(), ExitStack() as opened:
        descriptor = os.open(reserve, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceTreeError("Недопустимый резерв попытки")
        reservation = opened.enter_context(os.fdopen(descriptor, "r+b", closefd=False))
        remaining = info.st_size - size
        if remaining < 0:
            raise StorageBudgetExceeded("storage_budget_exceeded")
        workdir.mkdir(mode=0o700)
        try:
            reservation.truncate(remaining)
            reservation.flush()
            os.fsync(reservation.fileno())
            for directory in sorted(directories):
                (workdir / directory).mkdir(mode=0o700)
            for file, content in files:
                with (workdir / file).open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError as error:
            shutil.rmtree(workdir)
            try:
                reservation.seek(remaining)
                reservation.write(bytes(size))
                reservation.flush()
                os.fsync(reservation.fileno())
            except OSError as reserve_error:
                raise StorageBudgetExceeded(
                    "storage_budget_exceeded"
                ) from reserve_error
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise StorageBudgetExceeded("storage_budget_exceeded") from error
            raise
    return workdir
