"""Устойчивые исходы харнесса и сверка оставшихся процессов перед приёмом задач.

Реализация исполнения — Linux /proc + pidfd. Тесты могут подставлять дерево
proc и отправку сигнала; неизвестные процессы и старые каталоги без nonce
не объявляются освобождёнными и не удаляются.
"""

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jsonschema import Draft202012Validator

from contracts import schema
from services.harness.credentials import CredentialError, _atomic, _sync_directory
from services.harness.settings import VERSION

OWNER_ENV = "EVAL_STAND_ATTEMPT_OWNER"
MAX_RECEIPT = 1_048_576
_ID = Draft202012Validator(schema("launch")["$defs"]["id"])


class RecoveryError(RuntimeError):
    """Код без argv, окружения и содержимого настроек."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    sid: int
    started: int
    uid: int
    matching: bool
    foreign_home: bool
    anchor: bool
    zombie: bool


def _valid_id(run_id: str) -> None:
    if not _ID.is_valid(run_id):
        raise RecoveryError("invalid_run_id")


def _private(path: Path, uid: int, directory: bool) -> None:
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (
        not expected(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise RecoveryError("unsafe_recovery_path")


def _read_small(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise RecoveryError("process_metadata_too_large")
    return data


def _markers(path: Path, home: bytes, nonce: bytes) -> tuple[bool, bool]:
    """Потоково сравнить только две записи; не строить словарь окружения."""
    home_matches = nonce_matches = False
    found: set[bytes] = set()
    pending = b""
    consumed = 0
    with path.open("rb") as stream:
        while chunk := stream.read(4096):
            consumed += len(chunk)
            if consumed > 1_048_576:
                raise RecoveryError("process_metadata_too_large")
            records = (pending + chunk).split(b"\0")
            pending = records.pop()
            for record in records:
                key, separator, value = record.partition(b"=")
                if key not in {b"CODEX_HOME", OWNER_ENV.encode()}:
                    continue
                if key in found or not separator:
                    raise RecoveryError("ambiguous_process_marker")
                found.add(key)
                if key == b"CODEX_HOME":
                    home_matches = value == home
                else:
                    nonce_matches = secrets.compare_digest(value, nonce)
            if len(found) == 2:
                break
    return home_matches and nonce_matches, home_matches and not nonce_matches


def _process_uid(path: Path, expected_uid: int) -> int:
    # Владелец /proc/<pid> может стать root при недоступном ptrace. Проверяем
    # UID из данных ядра, чтобы такой процесс не исчезал из сверки молча.
    consumed = 0
    with path.open("rb") as stream:
        while line := stream.readline(1024):
            consumed += len(line)
            if consumed > 16_384:
                break
            if line.startswith(b"Uid:"):
                values = [int(value) for value in line.split()[1:]]
                if len(values) == 4:
                    if len(set(values)) == 1 or expected_uid not in values:
                        return values[1]
                raise RecoveryError("mixed_process_credentials")
    raise RecoveryError("process_uid_unconfirmed")


class HarnessRecovery:
    def __init__(
        self,
        state: Path,
        *,
        uid: int | None = None,
        proc_root: Path = Path("/proc"),
        allowed_programs: dict[str, tuple[str, ...]] | None = None,
        send_signal: Callable[[ProcessIdentity, int], None] | None = None,
        per_attempt_s: float = 30,
        startup_s: float = 90,
    ) -> None:
        self.uid = os.getuid() if uid is None else uid
        self.state = state.absolute()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != self.uid
            or info.st_mode & 0o022
        ):
            raise RecoveryError("unsafe_recovery_path")
        self.receipts = self.state / ".harness-recovery"
        self.receipts.mkdir(mode=0o700, exist_ok=True)
        _private(self.receipts, self.uid, True)
        if not 0 < per_attempt_s <= 30 or not 0 < startup_s <= 90:
            raise RecoveryError("invalid_recovery_timeout")
        self.proc_root = proc_root
        self.allowed_programs = allowed_programs or {
            str(Path("/usr/local/bin/codex").resolve()): (
                "app-server",
                "--listen",
                "stdio://",
            ),
            str(Path(sys.executable).resolve()): ("-m", "services.harness.relay"),
        }
        self.send_signal = send_signal
        self.per_attempt_s = per_attempt_s
        self.startup_s = startup_s

    def _path(self, run_id: str) -> Path:
        _valid_id(run_id)
        return self.receipts / (run_id + ".json")

    def _load(self, run_id: str) -> dict | None:
        path = self._path(run_id)
        if not path.exists() and not path.is_symlink():
            return None
        _private(path, self.uid, False)
        try:
            record = json.loads(_read_small(path, MAX_RECEIPT))
        except (ValueError, UnicodeError):
            raise RecoveryError("invalid_recovery_receipt") from None
        if (
            not isinstance(record, dict)
            or record.get("version") != "v0"
            or record.get("run_id") != run_id
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("owner_nonce", "")))
            or record.get("phase")
            not in {"registered", "releasing", "released", "failed"}
            or not (
                record.get("snapshot") is None
                or isinstance(record.get("snapshot"), dict)
            )
        ):
            raise RecoveryError("invalid_recovery_receipt")
        return record

    def _write(self, record: dict) -> None:
        raw = json.dumps(record, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > MAX_RECEIPT:
            raise RecoveryError("recovery_receipt_too_large")
        try:
            _atomic(self._path(record["run_id"]), raw)
        except CredentialError:
            raise RecoveryError("recovery_storage_failed") from None

    def _home(self, run_id: str) -> Path:
        _valid_id(run_id)
        return self.state / run_id

    def begin_attempt(self, run_id: str, home: Path | None = None) -> dict[str, str]:
        expected = self._home(run_id)
        if home is not None and home.absolute() != expected:
            raise RecoveryError("unexpected_attempt_home")
        if self._load(run_id) is not None:
            raise RecoveryError("attempt_already_registered")
        _private(expected, self.uid, True)
        for name in ("config.toml", "environments.toml"):
            _private(expected / name, self.uid, False)
        nonce = secrets.token_hex(32)
        self._write(
            {
                "version": "v0",
                "run_id": run_id,
                "owner_nonce": nonce,
                "phase": "registered",
                "snapshot": None,
                "cleanup_error": None,
            }
        )
        return {OWNER_ENV: nonce}

    def register(self, run_id: str) -> dict[str, str]:
        return self.begin_attempt(run_id)

    @staticmethod
    def _interrupted() -> dict:
        return {
            "execution_status": "interrupted",
            "released": False,
            "thread_id": None,
            "turn_id": None,
            "events": [],
            "traces": [],
            "trace_truncated": False,
            "evidence_complete": False,
            "codex_version": VERSION,
        }

    def status(self, run_id: str) -> dict | None:
        record = self._load(run_id)
        if record is None:
            return None
        snapshot = record["snapshot"] or self._interrupted()
        return {
            **snapshot,
            "done": record["phase"] != "registered",
            "released": record["phase"] == "released",
            "cleanup_error": record["cleanup_error"],
        }

    def _process(self, pid: int, home: Path, nonce: str) -> ProcessIdentity | None:
        directory = self.proc_root / str(pid)
        try:
            raw = _read_small(directory / "stat", 4096)
            fields = raw[raw.rfind(b")") + 2 :].split()
            sid, started, zombie = int(fields[3]), int(fields[19]), fields[0] == b"Z"
            uid = _process_uid(directory / "status", self.uid)
            matching = foreign = anchor = False
            if uid == self.uid and not zombie:
                matching, foreign = _markers(
                    directory / "environ", os.fsencode(home), nonce.encode()
                )
                if matching:
                    executable = os.readlink(directory / "exe")
                    argv = (
                        _read_small(directory / "cmdline", 16_384)
                        .rstrip(b"\0")
                        .split(b"\0")
                    )
                    tail = tuple(os.fsdecode(arg) for arg in argv[1:])
                    expected = self.allowed_programs.get(executable)
                    anchor = expected is not None and (
                        tail == expected
                        or (expected[0] == "app-server" and tail == ("--version",))
                    )
                    if anchor and tail[0] in {"app-server", "--version"} and pid != sid:
                        anchor = False
            return ProcessIdentity(
                pid, sid, started, uid, matching, foreign, anchor, zombie
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError):
            raise RecoveryError("process_inspection_failed") from None

    def _scan(self, home: Path, nonce: str, deadline: float) -> list[ProcessIdentity]:
        if not self.proc_root.is_dir():
            raise RecoveryError("linux_proc_required")
        processes = []
        for entry in self.proc_root.iterdir():
            if time.monotonic() >= deadline or len(processes) >= 4096:
                raise RecoveryError("recovery_timeout")
            if not entry.name.isdecimal():
                continue
            process = self._process(int(entry.name), home, nonce)
            if process is not None and not process.zombie:
                processes.append(process)
        return processes

    def _signal(
        self, process: ProcessIdentity, sig: int, home: Path, nonce: str
    ) -> None:
        if self._process(process.pid, home, nonce) != process:
            return
        if self.send_signal is not None:
            self.send_signal(process, sig)
            return
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RecoveryError("linux_pidfd_required")
        try:
            fd = os.pidfd_open(process.pid)
        except ProcessLookupError:
            return
        try:
            if self._process(process.pid, home, nonce) == process:
                signal.pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)

    async def _stop(self, home: Path, nonce: str, deadline: float) -> None:
        sessions: set[int] = set()
        started = time.monotonic()
        term_until = started + min(0.5, max(0, deadline - started) / 3)
        while time.monotonic() < deadline:
            processes = self._scan(home, nonce, deadline)
            owned = [process for process in processes if process.matching]
            if any(process.foreign_home for process in processes):
                raise RecoveryError("foreign_process_uses_attempt_home")
            sessions.update(process.sid for process in owned if process.anchor)
            if any(
                process.sid in sessions and not process.matching
                for process in processes
            ):
                raise RecoveryError("mixed_process_session")
            if not owned:
                return
            if any(
                process.sid not in sessions
                or process.sid == os.getsid(0)
                or process.pid == os.getpid()
                for process in owned
            ):
                raise RecoveryError("unconfirmed_process_ownership")
            sig = signal.SIGTERM if time.monotonic() < term_until else signal.SIGKILL
            for process in owned:
                self._signal(process, sig, home, nonce)
            await asyncio.sleep(min(0.03, max(0, deadline - time.monotonic())))
        raise RecoveryError("recovery_timeout")

    def _capture(self, snapshot: dict) -> dict:
        if not isinstance(snapshot, dict) or any(
            not isinstance(snapshot.get(key, []), list) for key in ("events", "traces")
        ):
            raise RecoveryError("invalid_native_snapshot")
        result = {
            **self._interrupted(),
            **{key: snapshot[key] for key in self._interrupted() if key in snapshot},
        }
        result["released"] = False
        for key, budget in (("events", 450_000), ("traces", 450_000)):
            captured, total = [], 0
            for entry in result[key][:512]:
                size = len(
                    json.dumps(entry, ensure_ascii=False, allow_nan=False).encode()
                )
                if total + size > budget:
                    break
                captured.append(entry)
                total += size
            if len(captured) != len(result[key]):
                result[
                    "evidence_complete" if key == "events" else "trace_truncated"
                ] = key != "events"
            result[key] = captured
        return result

    async def _finish(self, record: dict, deadline: float) -> dict:
        home = self._home(record["run_id"])
        record = {
            **record,
            "phase": "releasing",
            "cleanup_error": None,
            "snapshot": record["snapshot"] or self._interrupted(),
        }
        try:
            self._write(record)  # Владение переживает удаление home и аварию до итога.
            snapshot_persisted = True
        except (RecoveryError, OSError):
            snapshot_persisted = False
        try:
            # Остановка не зависит от свободного места для нового receipt:
            # nonce был устойчиво зарегистрирован до запуска процесса.
            await self._stop(home, record["owner_nonce"], deadline)
            if not snapshot_persisted:
                return self._unconfirmed(record, "harness_receipt_write_failed")
            if home.exists() or home.is_symlink():
                _private(home, self.uid, True)
                shutil.rmtree(home)
            _sync_directory(self.state)
            if any(
                process.matching or process.foreign_home
                for process in self._scan(home, record["owner_nonce"], deadline)
            ):
                raise RecoveryError("process_reappeared")
            record = {**record, "phase": "released"}
        except (RecoveryError, OSError):
            record = {
                **record,
                "phase": "failed",
                "cleanup_error": "harness_cleanup_unconfirmed",
            }
        if not snapshot_persisted:
            return self._unconfirmed(record, "harness_receipt_write_failed")
        try:
            self._write(record)
        except (RecoveryError, OSError):
            return self._unconfirmed(record, "harness_receipt_write_failed")
        return self.status(record["run_id"])

    def _unconfirmed(self, record: dict, code: str) -> dict:
        return {
            **(record["snapshot"] or self._interrupted()),
            "done": True,
            "released": False,
            "cleanup_error": code,
        }

    async def complete(self, run_id: str, snapshot: dict) -> dict:
        record = self._load(run_id)
        if record is None:
            raise RecoveryError("unknown_attempt")
        if record["phase"] == "released":
            return self.status(run_id)
        record = {**record, "snapshot": self._capture(snapshot)}
        return await self._finish(record, time.monotonic() + self.per_attempt_s)

    async def reconcile(self, *, controls_only: bool = False) -> dict[str, dict]:
        deadline = time.monotonic() + self.startup_s
        results = {}
        for home in self.state.iterdir():
            if time.monotonic() >= deadline:
                raise RecoveryError("recovery_timeout")
            if (
                _ID.is_valid(home.name)
                and home.is_dir()
                and self._load(home.name) is None
            ):
                raise RecoveryError("unregistered_attempt_home")
        for path in self.receipts.glob("*.json"):
            if time.monotonic() >= deadline:
                raise RecoveryError("recovery_timeout")
            run_id = path.stem
            record = self._load(run_id)
            if record["phase"] == "released":
                status = self.status(run_id)
            else:
                until = min(deadline, time.monotonic() + self.per_attempt_s)
                status = await self._finish(record, until)
            results[run_id] = (
                {
                    key: status[key]
                    for key in ("done", "released", "execution_status", "cleanup_error")
                }
                if controls_only
                else status
            )
        return results
