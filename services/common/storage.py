"""Устойчивые ограниченные записи; общий бюджет задаётся владельцем тома."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class StorageBudgetExceeded(RuntimeError):
    """Недостаточно места для приёма и последующей финализации."""


def encode(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode()


def atomic_json(path: Path, value: object) -> None:
    data = encode(value)
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Storage:
    """Сериализует резервы и записи в выделенной доверенной области.

    Это прикладной лимит области, а не замена квоты файловой системы
    для общего дискового состояния проекта и Docker.
    """

    def __init__(self, root: Path, limit: int) -> None:
        self.root = root
        self.limit = limit
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def locked(self) -> Iterator[None]:
        with (self.root / ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def used(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def reserve(self, name: str, size: int) -> Path:
        """Материализует резерв нулями; sparse truncate не резервирует место."""
        if not name.replace("-", "").replace("_", "").isalnum() or size < 0:
            raise ValueError("Некорректный резерв")
        with self.locked():
            path = self.root / (name + ".reserve")
            if path.exists():
                if path.stat().st_size != size:
                    raise ValueError("Конфликт размера резерва")
                return path
            if self.used() + size > self.limit:
                raise StorageBudgetExceeded("storage_budget_exceeded")
            try:
                with path.open("xb") as stream:
                    remaining = size
                    while remaining:
                        chunk = bytes(min(remaining, 65536))
                        stream.write(chunk)
                        remaining -= len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                path.unlink(missing_ok=True)
                raise StorageBudgetExceeded("storage_budget_exceeded") from error
            return path

    def finalize(self, reserve: Path, destination: Path, value: object) -> None:
        """Пишет в уже выделенные блоки без второго полного файла."""
        data = encode(value)
        with self.locked():
            if destination.exists():
                raise FileExistsError(destination)
            reserved_size = reserve.stat().st_size
            if len(data) > reserved_size:
                raise StorageBudgetExceeded("Итог превышает резерв")
            with reserve.open("r+b") as stream:
                stream.write(data)
                # До атомарного переноса резерв сохраняет исходную ёмкость.
                # Пробелы дают допустимый JSON даже при аварии сразу после rename.
                remaining = reserved_size - len(data)
                while remaining:
                    padding = b" " * min(remaining, 65536)
                    stream.write(padding)
                    remaining -= len(padding)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(reserve, destination)
            for parent in {reserve.parent, destination.parent}:
                directory = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            # Имя результата уже устойчиво: теперь можно освободить хвост.
            with destination.open("r+b") as stream:
                stream.truncate(len(data))
                stream.flush()
                os.fsync(stream.fileno())

    def write_reserved(self, reserve: Path, destination: Path, value: object) -> None:
        """Выделить запись из резерва, включая временную атомарную копию.

        Резерв расходуется монотонно: замена старого файла не возвращает кредит.
        Это даёт запас на ограниченное число переходов состояния и повторное
        сохранение после сбоя, не маскируя потреблённое место.
        """
        size = len(encode(value))
        with self.locked():
            remaining = reserve.stat().st_size
            if size > remaining:
                raise StorageBudgetExceeded("storage_budget_exceeded")
            with reserve.open("r+b") as stream:
                stream.truncate(remaining - size)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_json(destination, value)
