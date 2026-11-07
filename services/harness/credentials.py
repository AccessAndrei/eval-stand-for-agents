"""Единственный владелец выделенной ChatGPT-авторизации.

Разрешено отдельное внешнее хранилище либо согласованный каталог .auth в корне
проекта, если Git игнорирует всю область и в индексе нет её файлов.

Нативный Codex пишет только auth.json своей попытки. Перед любым обменом,
который может обновить токены, адаптер обязан вызвать begin_refresh. После
успешного нативного сохранения — commit_rotation. Этот модуль не выполняет
login, OAuth-запросы и не читает личный каталог Codex.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType

MAX_AUTH_BYTES = 65_536
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class CredentialError(RuntimeError):
    """Безопасный код ошибки: пути и содержимое авторизации не включаются."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Auth:
    raw: bytes = field(repr=False)
    digest: str
    identity: str


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise CredentialError("invalid_auth_json")
        value[key] = item
    return value


def _parse_json(raw: bytes) -> dict:
    if not isinstance(raw, bytes):
        raise CredentialError("invalid_auth_json")
    if len(raw) > MAX_AUTH_BYTES:
        raise CredentialError("auth_too_large")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise CredentialError("invalid_auth_json") from None
    if not isinstance(value, dict):
        raise CredentialError("invalid_auth_json")
    return value


def _auth(raw: bytes) -> _Auth:
    value = _parse_json(raw)
    allowed = {
        "auth_mode",
        "OPENAI_API_KEY",
        "tokens",
        "last_refresh",
        "agent_identity",
        "personal_access_token",
        "bedrock_api_key",
        "bedrock_access_keys",
    }
    forbidden = allowed - {"auth_mode", "tokens", "last_refresh"}
    if (
        set(value) - allowed
        or value.get("auth_mode") not in (None, "chatgpt")
        or any(value.get(key) is not None for key in forbidden)
    ):
        raise CredentialError("chatgpt_auth_required")
    tokens = value.get("tokens")
    if not isinstance(tokens, dict) or set(tokens) - {
        "id_token",
        "access_token",
        "refresh_token",
        "account_id",
    }:
        raise CredentialError("invalid_auth_json")
    for key in ("id_token", "access_token", "refresh_token"):
        token = tokens.get(key)
        if not isinstance(token, str) or not token.strip() or len(token) > 16_384:
            raise CredentialError("invalid_auth_json")
    account = tokens.get("account_id")
    # Нативный формат допускает None, но выделенное хранилище должно связывать
    # ротацию с конкретным аккаунтом. JWT здесь не проверяется: без account_id
    # локальную смену токенов нельзя считать подтверждённой ротацией владельца.
    if not isinstance(account, str) or not account.strip() or len(account) > 512:
        raise CredentialError("auth_account_id_required")
    if value.get("last_refresh") is not None:
        try:
            timestamp = datetime.fromisoformat(
                value["last_refresh"].replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise CredentialError("invalid_auth_json") from None
    digest = hashlib.sha256(json.dumps(tokens, sort_keys=True).encode()).hexdigest()
    identity = hashlib.sha256(json.dumps(account).encode()).hexdigest()
    return _Auth(raw, digest, identity)


def _secure_metadata(info: os.stat_result, *, directory: bool = False) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or (not directory and info.st_nlink != 1)
    ):
        raise CredentialError("unsafe_auth_permissions")


def _directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    _secure_metadata(path.lstat(), directory=True)


def _read(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            _secure_metadata(os.fstat(stream.fileno()))
            raw = stream.read(MAX_AUTH_BYTES + 1)
        if len(raw) > MAX_AUTH_BYTES:
            raise CredentialError("auth_too_large")
        return raw
    except FileNotFoundError:
        raise CredentialError("auth_missing") from None
    except OSError:
        raise CredentialError("auth_io_error") from None


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: Path, raw: bytes) -> None:
    temporary = path.parent / (".pending-" + uuid.uuid4().hex)
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except OSError:
        raise CredentialError("auth_io_error") from None
    finally:
        temporary.unlink(missing_ok=True)


def _require_ignored_auth_store(repository: Path) -> None:
    probes = (
        ".auth/",
        ".auth/current.json",
        ".auth/journal.json",
        ".auth/attempts/guard/auth.json",
        ".auth/bootstrap-guard/auth.json",
        ".auth/auth-setup.json",
        ".auth/arbitrary-future-file",
    )
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"

    def git(
        *arguments: str, input_data: bytes | None = None
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(repository), *arguments],
                input=input_data,
                capture_output=True,
                timeout=5,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            raise CredentialError("auth_store_git_guard_failed") from None

    top = git("rev-parse", "--show-toplevel")
    try:
        matches_repository = (
            Path(os.fsdecode(top.stdout).strip()).resolve() == repository
        )
    except (ValueError, OSError):
        matches_repository = False
    if top.returncode or not matches_repository:
        raise CredentialError("auth_store_git_guard_failed")
    tracked = git("ls-files", "--cached", "-z", "--", ".auth")
    if tracked.returncode:
        raise CredentialError("auth_store_git_guard_failed")
    if tracked.stdout:
        raise CredentialError("auth_store_contains_tracked_files")
    ignored = git(
        "check-ignore",
        "--no-index",
        "--stdin",
        input_data=("\n".join(probes) + "\n").encode(),
    )
    if ignored.returncode or set(ignored.stdout.splitlines()) != {
        path.encode() for path in probes
    }:
        raise CredentialError("auth_store_not_fully_ignored")


class CredentialOwner:
    """Контекст держит flock весь срок жизни Python-владельца, без ожидания.

    root — постоянный каталог вне рабочих сред: внешний либо строго repo/.auth
    после проверки Git. Монтирование отдельно от удаляемых Compose-томов —
    обязанность развёртывания. Очистка попытки не удаляет current.json.
    """

    def __init__(self, root: Path, repository_root: Path) -> None:
        if root.is_symlink():
            raise CredentialError("unsafe_auth_path")
        self.root = root.resolve()
        repository = repository_root.resolve()
        self._repository_guard: Path | None = None
        if self.root.is_relative_to(repository):
            if self.root != repository / ".auth":
                raise CredentialError("auth_store_inside_repository")
            _require_ignored_auth_store(repository)
            self._repository_guard = repository
        self._lock_fd: int | None = None

    def __enter__(self) -> "CredentialOwner":
        if self._lock_fd is not None:
            raise CredentialError("auth_owner_already_open")
        try:
            _directory(self.root, create=True)
            fd = os.open(
                self.root / ".owner.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                _secure_metadata(os.fstat(fd))
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise CredentialError("auth_owner_busy") from None
            except (CredentialError, OSError):
                os.close(fd)
                raise
            self._lock_fd = fd
            _directory(self.root / "attempts", create=True)
            return self
        except CredentialError:
            self.close()
            raise
        except OSError:
            self.close()
            raise CredentialError("auth_io_error") from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _assert_owner(self) -> None:
        if self._lock_fd is None:
            raise CredentialError("auth_owner_not_open")
        _directory(self.root)
        _directory(self.root / "attempts")
        if self._repository_guard is not None:
            _require_ignored_auth_store(self._repository_guard)

    def _journal(self) -> dict:
        self._assert_owner()
        path = self.root / "journal.json"
        if not path.exists() and not path.is_symlink():
            return {"version": "v0", "state": "idle"}
        journal = _parse_json(_read(path))
        if journal.get("version") != "v0" or journal.get("state") not in {
            "idle",
            "preparing",
            "prepared",
            "refreshing",
            "publishing",
            "active",
            "blocked",
            "cleaning",
        }:
            raise CredentialError("invalid_auth_journal")
        if journal["state"] != "idle":
            if (
                not isinstance(journal.get("run_id"), str)
                or not _RUN_ID.fullmatch(journal["run_id"])
                or any(
                    not isinstance(journal.get(key), str)
                    or not _DIGEST.fullmatch(journal[key])
                    for key in ("before_sha", "identity")
                )
            ):
                raise CredentialError("invalid_auth_journal")
        if journal["state"] == "publishing" and not _DIGEST.fullmatch(
            str(journal.get("after_sha", ""))
        ):
            raise CredentialError("invalid_auth_journal")
        return journal

    def _save_journal(self, journal: dict) -> None:
        _atomic(
            self.root / "journal.json", json.dumps(journal, sort_keys=True).encode()
        )

    def initialize(self, raw: bytes) -> None:
        """Установить только явно переданный нативный auth.json, без login."""
        current = self.root / "current.json"
        if (
            self._journal()["state"] != "idle"
            or current.exists()
            or current.is_symlink()
        ):
            raise CredentialError("auth_already_initialized")
        auth = _auth(raw)
        _atomic(self.root / "current.json", auth.raw)
        self._save_journal({"version": "v0", "state": "idle"})

    def prepare_attempt(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise CredentialError("invalid_run_id")
        if self._journal()["state"] != "idle":
            raise CredentialError("auth_recovery_required")
        auth = _auth(_read(self.root / "current.json"))
        home = self.root / "attempts" / run_id
        if home.exists() or home.is_symlink():
            raise CredentialError("auth_attempt_exists")
        journal = {
            "version": "v0",
            "state": "preparing",
            "run_id": run_id,
            "before_sha": auth.digest,
            "identity": auth.identity,
        }
        # Журнал предшествует созданию каталога: авария не теряет владение им.
        self._save_journal(journal)
        home.mkdir(mode=0o700)
        _atomic(home / "auth.json", auth.raw)
        _sync_directory(home.parent)
        self._save_journal({**journal, "state": "prepared"})
        return home

    def _for_run(self, run_id: str) -> dict:
        journal = self._journal()
        if journal.get("run_id") != run_id:
            raise CredentialError("unknown_auth_attempt")
        return journal

    def begin_refresh(self, run_id: str) -> None:
        journal = self._for_run(run_id)
        if journal["state"] not in {"prepared", "active"}:
            raise CredentialError("auth_recovery_required")
        auth = self._attempt_auth(journal)
        current = _auth(_read(self.root / "current.json"))
        if auth.digest != journal["before_sha"] or current.digest != auth.digest:
            self._block(journal)
        self._save_journal({**journal, "state": "refreshing"})

    def _attempt_auth(self, journal: dict) -> _Auth:
        home = self.root / "attempts" / journal["run_id"]
        _directory(home)
        auth = _auth(_read(home / "auth.json"))
        if auth.identity != journal["identity"]:
            raise CredentialError("auth_identity_changed")
        return auth

    def _block(self, journal: dict) -> None:
        self._save_journal({**journal, "state": "blocked"})
        raise CredentialError("auth_rotation_uncertain")

    def _publish(self, journal: dict, auth: _Auth) -> None:
        current = _auth(_read(self.root / "current.json"))
        if (
            current.digest not in {journal["before_sha"], auth.digest}
            or current.identity != journal["identity"]
        ):
            self._block(journal)
        pending = {**journal, "state": "publishing", "after_sha": auth.digest}
        self._save_journal(pending)
        _atomic(self.root / "current.json", auth.raw)
        self._save_journal({**journal, "state": "active", "before_sha": auth.digest})

    def commit_rotation(self, run_id: str) -> None:
        journal = self._for_run(run_id)
        if journal["state"] not in {"refreshing", "publishing"}:
            raise CredentialError("refresh_not_started")
        auth = self._attempt_auth(journal)
        if auth.digest == journal["before_sha"]:
            self._block(journal)
        if journal["state"] == "publishing" and auth.digest != journal["after_sha"]:
            self._block(journal)
        self._publish(journal, auth)

    def recover(self, *, native_stopped: bool = False) -> None:
        """Восстановить только после отдельного подтверждения остановки Codex."""
        journal = self._journal()
        if journal["state"] == "idle":
            return
        if not native_stopped:
            raise CredentialError("native_auth_writer_not_stopped")
        if journal["state"] in {"refreshing", "publishing", "blocked"}:
            try:
                auth = self._attempt_auth(journal)
            except (CredentialError, OSError):
                self._block(journal)
            if auth.digest == journal["before_sha"]:
                self._block(journal)
            if journal["state"] == "publishing" and auth.digest != journal["after_sha"]:
                self._block(journal)
            self._publish(journal, auth)
            journal = self._journal()
        self.cleanup_attempt(journal["run_id"], native_stopped=True)

    def cleanup_attempt(self, run_id: str, *, native_stopped: bool = False) -> None:
        journal = self._for_run(run_id)
        if not native_stopped:
            raise CredentialError("native_auth_writer_not_stopped")
        if journal["state"] not in {"preparing", "prepared", "active", "cleaning"}:
            raise CredentialError("auth_recovery_required")
        current = _auth(_read(self.root / "current.json"))
        if current.digest != journal["before_sha"]:
            self._block(journal)
        home = self.root / "attempts" / run_id
        if home.exists() or home.is_symlink():
            _directory(home)
            if journal["state"] not in {"preparing", "cleaning"}:
                auth = self._attempt_auth(journal)
                if auth.digest != current.digest:
                    self._block(journal)
        self._save_journal({**journal, "state": "cleaning"})
        if home.exists():
            shutil.rmtree(home)
        _sync_directory(home.parent)
        self._save_journal({"version": "v0", "state": "idle"})
