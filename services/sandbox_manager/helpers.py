"""Доверенный код для фиксированного запуска Python внутри среды.

Модуль передаётся через `python -I -S -c` и не загружается из /work.
UID 1001 держит PID1; UID 1000 исполняет агент. Отдельный доверенный
контейнер с UID 0 прекращает агентные процессы и читает рабочую область.
"""

import base64
import hashlib
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path, PurePosixPath


def idle() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0] > 0:
                pass
        except ChildProcessError:
            pass
        time.sleep(0.05)


def agent_processes() -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            status = (entry / "status").read_text()
        except FileNotFoundError:
            continue
        uid = next(line for line in status.splitlines() if line.startswith("Uid:"))
        state = next(line for line in status.splitlines() if line.startswith("State:"))
        if uid.split()[1] == "1000" and "Z (zombie)" not in state:
            found.append(int(entry.name))
    return found


def quiesce() -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        targets = agent_processes()
        if not targets:
            return
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.02)
    raise RuntimeError("agent_processes_remain")


def safe_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if path.is_absolute() or str(path) != raw or ".." in path.parts:
        raise ValueError("invalid_path")
    return path


def prepare(root: Path) -> None:
    payload = json.load(sys.stdin)
    for item in payload["files"]:
        target = root / safe_path(item["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(item["content"].encode("utf-8"))
        if root == Path("/input"):
            target.chmod(0o444)
    if root == Path("/input"):
        for directory in sorted(root.rglob("*"), reverse=True):
            if directory.is_dir():
                directory.chmod(0o555)
        root.chmod(0o555)


def collect(root: Path, limit: int, max_files: int) -> dict:
    """Читает только обычные файлы, отвергает ссылки, FIFO и выход за лимит."""
    files, manifest, used = [], {}, 0

    def unreadable(error: OSError) -> None:
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
        for name in sorted(dirs + names):
            path = Path(directory) / name
            meta = path.lstat()
            if stat.S_ISDIR(meta.st_mode):
                continue
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                raise ValueError("unsafe_file_type")
            used += meta.st_size
            if used > limit or len(files) >= max_files:
                raise ValueError("collection_limit")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                current = os.fstat(stream.fileno())
                if (current.st_dev, current.st_ino) != (meta.st_dev, meta.st_ino):
                    raise ValueError("unstable_file")
                data = stream.read(limit + 1)
            if len(data) != meta.st_size:
                raise ValueError("unstable_file")
            relative = str(path.relative_to(root))
            safe_path(relative)
            files.append(
                {"path": relative, "content_base64": base64.b64encode(data).decode()}
            )
            manifest[relative] = hashlib.sha256(data).hexdigest()
    return {"status": "complete", "files": files, "manifest": manifest}


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "idle":
        idle()
    elif action == "prepare":
        prepare(Path(sys.argv[2]))
    elif action == "collect_trusted":
        quiesce()
        print(
            json.dumps(
                collect(Path("/proc/1/root/work"), int(sys.argv[2]), int(sys.argv[3]))
            )
        )
    elif action == "quiesce":
        quiesce()
    else:
        raise ValueError("unknown_action")
