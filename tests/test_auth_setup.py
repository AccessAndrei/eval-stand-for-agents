"""Ручной setup проверяется только подменой Docker и вымышленным auth.json."""

import json
import os
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from scripts import auth_setup
from services.harness.credentials import CredentialError, CredentialOwner

IMAGE = "sha256:" + "a" * 64
FAKE_AUTH = json.dumps(
    {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "offline-only-id",
            "access_token": "offline-only-access",
            "refresh_token": "offline-only-refresh",
            "account_id": "offline-only-account",
        },
    }
).encode()


@pytest.fixture
def setup_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    provenance = repository / "provenance.runtime.json"
    provenance.write_text(
        json.dumps(
            {
                "origin": "source_build",
                "commit": auth_setup.COMMIT,
                "codex_version": auth_setup.VERSION,
                "image_digest": IMAGE,
                "architecture": "arm64",
            }
        )
    )
    return tmp_path / "dedicated-store", provenance, repository


class FakeDocker:
    def __init__(self, store: Path, repository: Path) -> None:
        self.store = store
        self.repository = repository
        self.calls: list[tuple[list[str], dict]] = []
        self.login_code = 0
        self.version = f"codex-cli {auth_setup.VERSION}\n".encode()
        self.foreign_container = False
        self.actual = {
            "Id": IMAGE,
            "Architecture": "arm64",
            "Os": "linux",
            "Config": {
                "Labels": {
                    "org.opencontainers.image.source": "https://github.com/openai/codex",
                    "org.opencontainers.image.revision": auth_setup.COMMIT,
                    "org.eval-stand.codex-origin": "source_build",
                    "org.eval-stand.codex-version": auth_setup.VERSION,
                }
            },
        }

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        self.calls.append((command, kwargs))
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps([self.actual]).encode(), b""
            )
        if command[:2] == ["docker", "run"] and command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, self.version, b"")
        if command[:2] == ["docker", "run"] and command[-2:] == [
            "login",
            "--device-auth",
        ]:
            assert "capture_output" not in kwargs
            assert "stdout" not in kwargs and "stderr" not in kwargs
            assert kwargs["timeout"] == 900
            with pytest.raises(CredentialError, match="auth_owner_busy"):
                with CredentialOwner(self.store, self.repository):
                    pytest.fail("Setup должен держать единственный lock во время login")
            mount = command[command.index("--mount") + 1]
            bootstrap = Path(
                mount.removeprefix("type=bind,src=").removesuffix(",dst=/auth")
            )
            assert bootstrap.parent == self.store
            assert stat.S_IMODE(bootstrap.stat().st_mode) == 0o700
            assert list(bootstrap.iterdir()) == []
            path = bootstrap / "auth.json"
            path.write_bytes(FAKE_AUTH)
            path.chmod(0o600)
            return subprocess.CompletedProcess(command, self.login_code)
        if command[:3] == ["docker", "container", "inspect"]:
            if self.foreign_container:
                container = {
                    "Id": "b" * 64,
                    "Image": IMAGE,
                    "Config": {"Labels": {"io.eval-stand.auth-setup": "foreign"}},
                }
                return subprocess.CompletedProcess(
                    command, 0, json.dumps([container]).encode(), b""
                )
            return subprocess.CompletedProcess(command, 1, b"", b"not found")
        if command[:3] == ["docker", "container", "ls"]:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError("Неожиданный вызов Docker в изолированном тесте")


def test_manual_setup_uses_only_source_binary_and_isolated_home(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    current = auth_setup.setup(store, provenance, repository_root=repository)
    assert current.read_bytes() == FAKE_AUTH
    assert stat.S_IMODE(current.stat().st_mode) == 0o600
    assert not list(store.glob("bootstrap-*"))
    state = json.loads((store / auth_setup.STATE_NAME).read_bytes())
    assert state["status"] == "complete"
    login = next(
        command for command, _ in docker.calls if command[-1] == "--device-auth"
    )
    assert login.count("--mount") == 1
    assert login[login.index("--platform") + 1] == "linux/arm64"
    assert login[login.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    for flag in (
        "--rm",
        "-it",
        "--pull=never",
        "--log-driver=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--cpus=0.5",
        "--memory=536870912",
        "--memory-swap=536870912",
        "--network=bridge",
    ):
        assert flag in login
    assert login[-5:] == [
        IMAGE,
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
        "--device-auth",
    ]
    assert "CODEX_HOME=/auth" in login and "HOME=/auth" in login
    assert ".codex" not in " ".join(login)
    assert not any("pull" in command[:2] for command, _ in docker.calls)


def test_existing_authorization_is_not_read_or_replaced(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    with CredentialOwner(store, repository) as owner:
        owner.initialize(FAKE_AUTH)
    docker = FakeDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="auth_already_initialized"):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert docker.calls == []
    assert (store / "current.json").read_bytes() == FAKE_AUTH


def test_store_inside_repository_rejected_before_docker(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provenance, repository = setup_paths
    docker = FakeDocker(repository / "auth", repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(CredentialError, match="auth_store_inside_repository"):
        auth_setup.setup(repository / "auth", provenance, repository_root=repository)
    assert docker.calls == []


@pytest.mark.parametrize(
    "mismatch", ["id", "architecture", "os", "revision", "origin", "version"]
)
def test_source_image_verification_prevents_login(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    if mismatch == "id":
        docker.actual["Id"] = "sha256:" + "b" * 64
    elif mismatch == "architecture":
        docker.actual["Architecture"] = "amd64"
    elif mismatch == "os":
        docker.actual["Os"] = "windows"
    elif mismatch == "revision":
        docker.actual["Config"]["Labels"]["org.opencontainers.image.revision"] = (
            "b" * 40
        )
    elif mismatch == "origin":
        docker.actual["Config"]["Labels"]["org.eval-stand.codex-origin"] = (
            "upstream_diagnostic"
        )
    else:
        docker.version = b"codex-cli 0.0.0\n"
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert not any(command[-1] == "--device-auth" for command, _ in docker.calls)
    assert not (store / "current.json").exists()


def test_resume_after_publication_never_reinitializes_current(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    write_state = auth_setup._write_state

    class SimulatedCrash(BaseException):
        pass

    def crash_after_publication(root: Path, state: dict) -> None:
        if state["status"] == "published":
            raise SimulatedCrash
        write_state(root, state)

    monkeypatch.setattr(auth_setup, "_write_state", crash_after_publication)
    with pytest.raises(SimulatedCrash):
        auth_setup.setup(store, provenance, repository_root=repository)
    current = store / "current.json"
    before = current.stat()
    assert current.read_bytes() == FAKE_AUTH
    assert len(list(store.glob("bootstrap-*/auth.json"))) == 1
    assert (
        json.loads((store / auth_setup.STATE_NAME).read_bytes())["status"]
        == "publishing"
    )
    monkeypatch.setattr(auth_setup, "_write_state", write_state)

    def forbidden_initialize(self: CredentialOwner, raw: bytes) -> None:
        pytest.fail("Resume не должен повторно публиковать или заменять current.json")

    monkeypatch.setattr(CredentialOwner, "initialize", forbidden_initialize)
    for _ in range(2):
        assert (
            auth_setup.setup(store, provenance, resume=True, repository_root=repository)
            == current
        )
    assert current.stat().st_ino == before.st_ino
    assert current.stat().st_mtime_ns == before.st_mtime_ns
    assert not list(store.glob("bootstrap-*"))
    assert sum(command[-1] == "--device-auth" for command, _ in docker.calls) == 1


def test_state_write_failure_cannot_skip_owned_cleanup(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    docker.login_code = 1
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    write_state = auth_setup._write_state
    stopped = []

    def disk_full(root: Path, state: dict) -> None:
        if state["status"] == "recovery_required":
            raise OSError("simulated disk full")
        write_state(root, state)

    monkeypatch.setattr(auth_setup, "_write_state", disk_full)
    monkeypatch.setattr(
        auth_setup,
        "_stop_owned_container",
        lambda state: stopped.append(state["container"]),
    )
    with pytest.raises(OSError, match="simulated disk full"):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert len(stopped) == 1
    assert len(list(store.glob("bootstrap-*/auth.json"))) == 1
    assert not (store / "current.json").exists()


def test_resume_never_replaces_a_different_current_authorization(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    current = auth_setup.setup(store, provenance, repository_root=repository)
    changed = json.loads(FAKE_AUTH)
    changed["tokens"]["access_token"] = "offline-only-different-access"
    saved = json.dumps(changed).encode()
    current.write_bytes(saved)
    before = current.stat()
    with pytest.raises(auth_setup.AuthSetupError, match="published_auth_mismatch"):
        auth_setup.setup(store, provenance, resume=True, repository_root=repository)
    assert current.read_bytes() == saved
    assert current.stat().st_ino == before.st_ino
    assert current.stat().st_mtime_ns == before.st_mtime_ns
    assert sum(command[-1] == "--device-auth" for command, _ in docker.calls) == 1


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_signal_stops_owned_login_and_preserves_bootstrap(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    number: int,
) -> None:
    store, provenance, repository = setup_paths

    class InterruptedDocker(FakeDocker):
        def __call__(
            self, command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess:
            result = super().__call__(command, **kwargs)
            if command[-1] == "--device-auth":
                signal.raise_signal(number)
            return result

    docker = InterruptedDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    stopped = []

    def stop_owned(state: dict) -> None:
        signal.raise_signal(number)  # Повторный сигнал не прерывает cleanup.
        with pytest.raises(CredentialError, match="auth_owner_busy"):
            with CredentialOwner(store, repository):
                pytest.fail("Lock должен сохраняться до остановки login")
        stopped.append(state["container"])

    monkeypatch.setattr(auth_setup, "_stop_owned_container", stop_owned)
    previous = {
        sig: signal.getsignal(sig)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    }
    with pytest.raises(auth_setup.AuthSetupError, match="setup_interrupted"):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert len(stopped) == 1
    assert len(list(store.glob("bootstrap-*/auth.json"))) == 1
    assert not (store / "current.json").exists()
    assert previous == {sig: signal.getsignal(sig) for sig in previous}


def test_failed_login_preserves_new_auth_and_resume_does_not_login_again(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    docker.login_code = 1
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="native_login_failed"):
        auth_setup.setup(store, provenance, repository_root=repository)
    (bootstrap,) = store.glob("bootstrap-*")
    assert (bootstrap / "auth.json").read_bytes() == FAKE_AUTH
    assert not (store / "current.json").exists()
    assert (
        json.loads((store / auth_setup.STATE_NAME).read_bytes())["status"]
        == "recovery_required"
    )
    before = len(docker.calls)
    with pytest.raises(auth_setup.AuthSetupError, match="setup_recovery_required"):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert len(docker.calls) == before
    current = auth_setup.setup(
        store, provenance, resume=True, repository_root=repository
    )
    assert current.read_bytes() == FAKE_AUTH
    assert not bootstrap.exists()
    assert sum(command[-1] == "--device-auth" for command, _ in docker.calls) == 1


def test_failure_does_not_delete_foreign_container(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    docker.login_code = 1
    docker.foreign_container = True
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="setup_container_not_owned"):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert not any(
        command[:3] == ["docker", "container", "rm"] for command, _ in docker.calls
    )
    assert len(list(store.glob("bootstrap-*/auth.json"))) == 1


def test_setup_never_prints_authorization(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    monkeypatch.setattr(
        "sys.argv",
        ["auth_setup", "--store", str(store), "--provenance", str(provenance)],
    )
    assert auth_setup.main() == 0
    output = capsys.readouterr()
    assert "offline-only" not in output.out + output.err
    assert "сохранена" in output.out


def test_timeout_stops_only_owned_container_and_keeps_saved_auth(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths

    class TimedOutDocker(FakeDocker):
        def __call__(
            self, command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess:
            if command[:3] == ["docker", "container", "inspect"]:
                self.calls.append((command, kwargs))
                state = json.loads((store / auth_setup.STATE_NAME).read_bytes())
                container = {
                    "Id": "b" * 64,
                    "Image": IMAGE,
                    "Config": {"Labels": {"io.eval-stand.auth-setup": state["id"]}},
                }
                return subprocess.CompletedProcess(
                    command, 0, json.dumps([container]).encode(), b""
                )
            if command[:3] == ["docker", "container", "rm"]:
                self.calls.append((command, kwargs))
                assert command == ["docker", "container", "rm", "--force", "b" * 64]
                return subprocess.CompletedProcess(command, 0, b"", b"")
            result = super().__call__(command, **kwargs)
            if command[-1] == "--device-auth":
                raise subprocess.TimeoutExpired(command, 900)
            return result

    docker = TimedOutDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(subprocess.TimeoutExpired):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert len(list(store.glob("bootstrap-*/auth.json"))) == 1
    assert not (store / "current.json").exists()
    assert (
        sum(command[:3] == ["docker", "container", "rm"] for command, _ in docker.calls)
        == 1
    )


def test_resume_does_not_follow_bootstrap_symlink(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    docker.login_code = 1
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="native_login_failed"):
        auth_setup.setup(store, provenance, repository_root=repository)
    (bootstrap,) = store.glob("bootstrap-*")
    (bootstrap / "auth.json").unlink()
    bootstrap.rmdir()
    bootstrap.symlink_to(repository, target_is_directory=True)
    protected = repository / "auth.json"
    protected.write_bytes(FAKE_AUTH)
    protected.chmod(0o600)
    with pytest.raises(CredentialError, match="unsafe_auth_permissions"):
        auth_setup.setup(store, provenance, resume=True, repository_root=repository)
    assert protected.read_bytes() == FAKE_AUTH
    assert not (store / "current.json").exists()


def test_cli_defaults_to_approved_repository_auth(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provenance, _ = setup_paths
    calls = []

    def setup_only(
        store: Path, source: Path, *, resume: bool = False, retry_login: bool = False
    ) -> Path:
        assert retry_login is False
        calls.append((store, source, resume))
        return store / "current.json"

    monkeypatch.setattr(auth_setup, "setup", setup_only)
    monkeypatch.setattr("sys.argv", ["auth_setup", "--provenance", str(provenance)])
    assert auth_setup.main() == 0
    assert calls == [(auth_setup.REPOSITORY / ".auth", provenance, False)]


def test_setup_accepts_approved_ignored_repository_store_with_mock_login(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provenance, repository = setup_paths
    subprocess.run(
        ["git", "init", "--quiet", str(repository)], check=True, capture_output=True
    )
    (repository / ".gitignore").write_text(".auth/\n")
    (repository / ".dockerignore").write_text(".auth/\n")
    store = repository / ".auth"
    docker = FakeDocker(store, repository)
    original_run = subprocess.run

    def local_git_or_fake_docker(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        if command[0] == "git":
            return original_run(command, **kwargs)
        return docker(command, **kwargs)

    monkeypatch.setattr(auth_setup.subprocess, "run", local_git_or_fake_docker)
    current = auth_setup.setup(store, provenance, repository_root=repository)
    assert current == store / "current.json"
    assert current.read_bytes() == FAKE_AUTH
    assert not list(store.glob("bootstrap-*"))


def test_explicit_retry_after_login_without_auth_uses_fresh_owned_bootstrap(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths

    class NoAuthFirstLogin(FakeDocker):
        login_calls = 0

        def __call__(
            self, command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess:
            result = super().__call__(command, **kwargs)
            if command[-1] == "--device-auth":
                self.login_calls += 1
                if self.login_calls == 1:
                    state = json.loads((store / auth_setup.STATE_NAME).read_bytes())
                    (store / state["bootstrap"] / "auth.json").unlink()
                    return subprocess.CompletedProcess(command, 1)
            return result

    docker = NoAuthFirstLogin(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="native_login_failed"):
        auth_setup.setup(store, provenance, repository_root=repository)
    original = json.loads((store / auth_setup.STATE_NAME).read_bytes())
    assert not (store / original["bootstrap"] / "auth.json").exists()
    with pytest.raises(auth_setup.AuthSetupError, match="setup_recovery_required"):
        auth_setup.setup(store, provenance, repository_root=repository)
    assert docker.login_calls == 1
    current = auth_setup.setup(
        store, provenance, retry_login=True, repository_root=repository
    )
    replacement = json.loads((store / auth_setup.STATE_NAME).read_bytes())
    assert replacement["id"] != original["id"]
    assert replacement["container"] != original["container"]
    assert docker.login_calls == 2
    assert current.read_bytes() == FAKE_AUTH
    assert not (store / original["bootstrap"]).exists()


def test_retry_refuses_saved_native_auth_and_keeps_resume_import_only(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    docker.login_code = 1
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="native_login_failed"):
        auth_setup.setup(store, provenance, repository_root=repository)
    original = (store / auth_setup.STATE_NAME).read_bytes()
    with pytest.raises(
        auth_setup.AuthSetupError, match="setup_native_auth_requires_resume"
    ):
        auth_setup.setup(
            store, provenance, retry_login=True, repository_root=repository
        )
    assert (store / auth_setup.STATE_NAME).read_bytes() == original
    (bootstrap,) = store.glob("bootstrap-*")
    assert (bootstrap / "auth.json").read_bytes() == FAKE_AUTH
    assert sum(command[-1] == "--device-auth" for command, _ in docker.calls) == 1
    auth_setup.setup(store, provenance, resume=True, repository_root=repository)
    assert sum(command[-1] == "--device-auth" for command, _ in docker.calls) == 1


def test_retry_refuses_current_authorization_before_docker(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    with CredentialOwner(store, repository) as owner:
        owner.initialize(FAKE_AUTH)
    docker = FakeDocker(store, repository)
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="auth_already_initialized"):
        auth_setup.setup(
            store, provenance, retry_login=True, repository_root=repository
        )
    assert docker.calls == []
    assert (store / "current.json").read_bytes() == FAKE_AUTH


def test_retry_refuses_foreign_container_without_removing_bootstrap(
    setup_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, provenance, repository = setup_paths
    docker = FakeDocker(store, repository)
    docker.login_code = 1
    monkeypatch.setattr(auth_setup.subprocess, "run", docker)
    with pytest.raises(auth_setup.AuthSetupError, match="native_login_failed"):
        auth_setup.setup(store, provenance, repository_root=repository)
    (bootstrap,) = store.glob("bootstrap-*")
    (bootstrap / "auth.json").unlink()
    docker.foreign_container = True
    with pytest.raises(auth_setup.AuthSetupError, match="setup_container_not_owned"):
        auth_setup.setup(
            store, provenance, retry_login=True, repository_root=repository
        )
    assert bootstrap.exists()
    assert not any(
        command[:3] == ["docker", "container", "rm"] for command, _ in docker.calls
    )
    assert sum(command[-1] == "--device-auth" for command, _ in docker.calls) == 1
