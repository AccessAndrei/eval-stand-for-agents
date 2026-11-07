"""Ручной ChatGPT device-login в выделенное хранилище через source-built Codex.

Запускать явно из терминала. Тесты подменяют subprocess и не выполняют login.
После неуспеха --resume импортирует уже сохранённый auth.json без нового входа.
Если auth.json не появился, новый вход запускается только явным --retry-login.
Согласованное хранилище проекта — .auth, полностью исключённое из Git и Docker.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from scripts.build_codex import COMMIT, VERSION
from services.harness.credentials import (
    CredentialError,
    CredentialOwner,
    _atomic,
    _auth,
    _directory,
    _read,
    _sync_directory,
)

REPOSITORY = Path(__file__).resolve().parents[1]
MEMORY_BYTES = "536870912"
STATE_NAME = "auth-setup.json"


class AuthSetupError(RuntimeError):
    """Только безопасный код ошибки, без содержимого auth.json или stderr."""


@contextmanager
def _interrupt_signals() -> Iterator[None]:
    def interrupted(number: int, frame: FrameType | None) -> None:
        raise AuthSetupError("setup_interrupted")

    previous = {
        number: signal.signal(number, interrupted)
        for number in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _bounded_cleanup(state: dict) -> None:
    # Повторный сигнал не должен прервать уже начатый ограниченный cleanup.
    previous = {
        number: signal.signal(number, signal.SIG_IGN)
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    }
    try:
        _stop_owned_container(state)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _docker_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
        if key in os.environ
    }


def _command(args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, check=False, capture_output=True, timeout=timeout, env=_docker_env()
    )


def _json_output(result: subprocess.CompletedProcess) -> object:
    if result.returncode or len(result.stdout) > 1_000_000:
        raise AuthSetupError("docker_inspection_failed")
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise AuthSetupError("docker_inspection_failed") from None


def verify_image(provenance_path: Path) -> tuple[str, str]:
    try:
        with provenance_path.open("rb") as stream:
            raw = stream.read(65_537)
        data = json.loads(raw) if len(raw) <= 65_536 else None
    except (OSError, ValueError, UnicodeError):
        raise AuthSetupError("invalid_source_provenance") from None
    if not isinstance(data, dict) or any(
        data.get(key) != expected
        for key, expected in {
            "origin": "source_build",
            "commit": COMMIT,
            "codex_version": VERSION,
        }.items()
    ):
        raise AuthSetupError("invalid_source_provenance")
    image = data.get("image_digest")
    reported_architecture = data.get("architecture")
    architecture = (
        {"arm64": "arm64", "aarch64": "arm64", "amd64": "amd64", "x86_64": "amd64"}.get(
            reported_architecture
        )
        if isinstance(reported_architecture, str)
        else None
    )
    if (
        not isinstance(image, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
        or architecture is None
    ):
        raise AuthSetupError("invalid_source_provenance")
    inspected = _json_output(_command(["docker", "image", "inspect", image]))
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise AuthSetupError("source_image_mismatch")
    actual = inspected[0]
    if not isinstance(actual, dict) or not isinstance(actual.get("Config"), dict):
        raise AuthSetupError("source_image_mismatch")
    labels = actual["Config"].get("Labels", {})
    expected_labels = {
        "org.opencontainers.image.source": "https://github.com/openai/codex",
        "org.opencontainers.image.revision": COMMIT,
        "org.eval-stand.codex-origin": "source_build",
        "org.eval-stand.codex-version": VERSION,
    }
    if (
        actual.get("Id") != image
        or actual.get("Architecture") != architecture
        or actual.get("Os") != "linux"
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected_labels.items())
    ):
        raise AuthSetupError("source_image_mismatch")
    platform = "linux/" + architecture
    version = _command(_base_run(platform, network="none") + [image, "--version"])
    if version.returncode or version.stdout.strip() != f"codex-cli {VERSION}".encode():
        raise AuthSetupError("source_version_mismatch")
    return image, platform


def _base_run(platform: str, *, network: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--init",
        "--pull=never",
        "--log-driver=none",
        "--platform",
        platform,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--cpus=0.5",
        "--memory=" + MEMORY_BYTES,
        "--memory-swap=" + MEMORY_BYTES,
        "--pids-limit=64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=33554432,mode=1777",
        "--network=" + network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--entrypoint",
        "/usr/local/bin/codex",
    ]


def _write_state(root: Path, state: dict) -> None:
    _atomic(root / STATE_NAME, json.dumps(state, sort_keys=True).encode())


def _read_state(root: Path) -> dict:
    try:
        state = json.loads(_read(root / STATE_NAME))
    except (ValueError, UnicodeError):
        raise AuthSetupError("invalid_setup_state") from None
    if (
        not isinstance(state, dict)
        or state.get("version") != "v0"
        or not re.fullmatch(r"[0-9a-f]{32}", str(state.get("id", "")))
        or state.get("bootstrap") != "bootstrap-" + state["id"]
        or state.get("container") != "eval-stand-auth-" + state["id"]
    ):
        raise AuthSetupError("invalid_setup_state")
    return state


def _stop_owned_container(state: dict) -> None:
    result = _command(["docker", "container", "inspect", state["container"]])
    if result.returncode:
        # Отличить отсутствие контейнера от недоступного Docker без анализа stderr.
        listing = _command(
            [
                "docker",
                "container",
                "ls",
                "-a",
                "--filter",
                "name=^/" + state["container"] + "$",
                "--format",
                "{{.ID}}",
            ]
        )
        if listing.returncode or listing.stdout.strip():
            raise AuthSetupError("setup_cleanup_unconfirmed")
        return
    inspected = _json_output(result)
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise AuthSetupError("setup_cleanup_unconfirmed")
    container = inspected[0]
    if not isinstance(container, dict) or not isinstance(container.get("Config"), dict):
        raise AuthSetupError("setup_cleanup_unconfirmed")
    labels = container["Config"].get("Labels")
    if (
        not isinstance(labels, dict)
        or labels.get("io.eval-stand.auth-setup") != state["id"]
        or container.get("Image") != state["image"]
        or not re.fullmatch(r"[0-9a-f]{64}", str(container.get("Id", "")))
    ):
        raise AuthSetupError("setup_container_not_owned")
    deleted = _command(["docker", "container", "rm", "--force", container["Id"]])
    if deleted.returncode:
        raise AuthSetupError("setup_cleanup_unconfirmed")


def _native_login(bootstrap: Path, state: dict) -> int:
    command = _base_run(state["platform"], network="bridge") + [
        "-it",
        "--name",
        state["container"],
        "--label",
        "io.eval-stand.auth-setup=" + state["id"],
        "--mount",
        f"type=bind,src={bootstrap},dst=/auth",
        "--env",
        "CODEX_HOME=/auth",
        "--env",
        "HOME=/auth",
        "--workdir",
        "/auth",
        state["image"],
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
        "--device-auth",
    ]
    # Нативный device code виден только интерактивному пользователю: потоки не
    # перехватываются, не сохраняются в файлы и не передаются в исключения.
    completed = subprocess.run(command, check=False, timeout=900, env=_docker_env())
    return completed.returncode


def _resume_published(root: Path, bootstrap: Path, state: dict) -> None:
    expected = state.get("auth_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AuthSetupError("published_auth_unconfirmed")
    current = _auth(_read(root / "current.json")).raw
    if not secrets.compare_digest(hashlib.sha256(current).hexdigest(), expected):
        raise AuthSetupError("published_auth_mismatch")
    if bootstrap.exists() or bootstrap.is_symlink():
        _directory(bootstrap)
        copied = bootstrap / "auth.json"
        if copied.exists() or copied.is_symlink():
            if not secrets.compare_digest(current, _read(copied)):
                raise AuthSetupError("published_auth_mismatch")


def _publish_and_cleanup(
    owner: CredentialOwner, bootstrap: Path, state: dict, *, resume: bool
) -> Path:
    current = owner.root / "current.json"
    with _interrupt_signals():
        try:
            if current.exists() or current.is_symlink():
                _resume_published(owner.root, bootstrap, state)
            else:
                _directory(bootstrap)
                if not resume:
                    state = {**state, "status": "login_started"}
                    _write_state(owner.root, state)
                    if _native_login(bootstrap, state) != 0:
                        raise AuthSetupError("native_login_failed")
                raw = _read(bootstrap / "auth.json")
                state = {
                    **state,
                    "status": "publishing",
                    "auth_sha256": hashlib.sha256(raw).hexdigest(),
                }
                _write_state(owner.root, state)
                owner.initialize(raw)
            state = {**state, "status": "published"}
            _write_state(owner.root, state)
            if bootstrap.exists():
                _directory(bootstrap)
                shutil.rmtree(bootstrap)
            _write_state(owner.root, {**state, "status": "complete"})
            return current
        except (
            AuthSetupError,
            CredentialError,
            OSError,
            subprocess.SubprocessError,
            KeyboardInterrupt,
        ):
            try:
                _write_state(owner.root, {**state, "status": "recovery_required"})
            finally:
                # ENOSPC/ошибка fsync не отменяют остановку login-контейнера.
                _bounded_cleanup(state)
            raise


def _discard_failed_bootstrap(root: Path, state: dict) -> None:
    if state.get("status") not in {"prepared", "login_started", "recovery_required"}:
        raise AuthSetupError("setup_retry_not_safe")
    bootstrap = root / state["bootstrap"]
    if bootstrap.exists() or bootstrap.is_symlink():
        _directory(bootstrap)
        native_auth = bootstrap / "auth.json"
        if native_auth.exists() or native_auth.is_symlink():
            raise AuthSetupError("setup_native_auth_requires_resume")
        shutil.rmtree(bootstrap)
    (root / STATE_NAME).unlink()
    _sync_directory(root)


def setup(
    store: Path,
    provenance: Path,
    *,
    resume: bool = False,
    retry_login: bool = False,
    repository_root: Path = REPOSITORY,
) -> Path:
    if resume and retry_login:
        raise AuthSetupError("conflicting_setup_modes")
    if any(character in str(store) for character in (",", "\n", "\r")):
        raise AuthSetupError("unsupported_auth_store_path")
    with CredentialOwner(store, repository_root) as owner:
        if any(character in str(owner.root) for character in (",", "\n", "\r")):
            raise AuthSetupError("unsupported_auth_store_path")
        current = owner.root / "current.json"
        if (current.exists() or current.is_symlink()) and not resume:
            raise AuthSetupError("auth_already_initialized")
        if owner._journal()["state"] != "idle":
            raise AuthSetupError("auth_recovery_required")
        state_path = owner.root / STATE_NAME
        if (state_path.exists() or state_path.is_symlink()) and not (
            resume or retry_login
        ):
            raise AuthSetupError("setup_recovery_required")
        if (resume or retry_login) and not state_path.exists():
            raise AuthSetupError("setup_recovery_missing")
        image, platform = verify_image(provenance)
        if resume or retry_login:
            state = _read_state(owner.root)
            if state.get("image") != image or state.get("platform") != platform:
                raise AuthSetupError("source_image_mismatch")
            _bounded_cleanup(state)
            if retry_login:
                _discard_failed_bootstrap(owner.root, state)
        if not resume:
            identity = uuid.uuid4().hex
            state = {
                "version": "v0",
                "id": identity,
                "bootstrap": "bootstrap-" + identity,
                "container": "eval-stand-auth-" + identity,
                "image": image,
                "platform": platform,
                "status": "prepared",
            }
            (owner.root / state["bootstrap"]).mkdir(mode=0o700)
            _write_state(owner.root, state)
        bootstrap = owner.root / state["bootstrap"]
        return _publish_and_cleanup(owner, bootstrap, state, resume=resume)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=REPOSITORY / ".auth")
    parser.add_argument("--provenance", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--resume", action="store_true")
    modes.add_argument("--retry-login", action="store_true")
    args = parser.parse_args()
    try:
        current = setup(
            args.store,
            args.provenance,
            resume=args.resume,
            retry_login=args.retry_login,
        )
    except (AuthSetupError, CredentialError) as error:
        print("Настройка авторизации не завершена: " + str(error))
        print(
            "Сохранённая авторизация не заменяется. --resume импортирует готовый auth.json; --retry-login явно повторяет вход только при его отсутствии."
        )
        return 2
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
        print(
            "Настройка прервана; изолированный bootstrap сохранён для ручного восстановления."
        )
        return 2
    print(f"Выделенная ChatGPT-авторизация сохранена: {current}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
