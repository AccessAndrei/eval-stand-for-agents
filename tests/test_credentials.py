"""Офлайн-проверки владения авторизацией; все токены вымышленные."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from services.harness.credentials import CredentialError, CredentialOwner


def fake_auth(generation: int = 1, account: str = "offline-account") -> bytes:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": f"offline-id-{generation}",
                "access_token": f"offline-access-{generation}",
                "refresh_token": f"offline-refresh-{generation}",
                "account_id": account,
            },
            "last_refresh": "2026-11-06T12:00:00Z",
        }
    ).encode()


def native_save(home: Path, raw: bytes) -> None:
    """Воспроизвести атомарное локальное сохранение, без нативного процесса."""
    path = home / "native-auth.pending"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(path, home / "auth.json")


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    return tmp_path / "dedicated-auth", repository


def test_isolated_home_and_private_permissions(paths: tuple[Path, Path]) -> None:
    root, repository = paths
    with CredentialOwner(root, repository) as owner:
        owner.initialize(fake_auth())
        (root / "config.toml").write_text("personal = true")
        home = owner.prepare_attempt("run-1")
        assert {path.name for path in home.iterdir()} == {"auth.json"}
        assert (home / "auth.json").read_bytes() == fake_auth()
        for path in (root, root / "attempts", home):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in (
            root / "current.json",
            root / "journal.json",
            root / ".owner.lock",
            home / "auth.json",
        ):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rotation_published_before_cleanup_and_next_attempt(
    paths: tuple[Path, Path],
) -> None:
    root, repository = paths
    with CredentialOwner(root, repository) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        owner.begin_refresh("run-1")
        journal = json.loads((root / "journal.json").read_bytes())
        assert journal["state"] == "refreshing"
        assert b"offline-refresh" not in (root / "journal.json").read_bytes()
        native_save(home, fake_auth(2))
        owner.commit_rotation("run-1")
        assert (root / "current.json").read_bytes() == fake_auth(2)
        assert home.exists()
        owner.cleanup_attempt("run-1", native_stopped=True)
        assert not home.exists()
        assert (root / "current.json").exists()
        next_home = owner.prepare_attempt("run-2")
        assert (next_home / "auth.json").read_bytes() == fake_auth(2)


def test_missing_auth_is_explicit(paths: tuple[Path, Path]) -> None:
    with CredentialOwner(*paths) as owner:
        with pytest.raises(CredentialError, match="auth_missing"):
            owner.prepare_attempt("run-1")


@pytest.mark.parametrize("account", [None, "", " ", "missing"])
def test_authorization_without_account_identity_is_rejected(
    paths: tuple[Path, Path],
    account: str | None,
) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        # Обе потенциальные версии токенов отклоняются ещё при установке:
        # hash(null) не может связать разные аккаунты в одну ротацию.
        for generation in (1, 2):
            value = json.loads(fake_auth(generation))
            if account == "missing":
                del value["tokens"]["account_id"]
            else:
                value["tokens"]["account_id"] = account
            with pytest.raises(CredentialError, match="auth_account_id_required"):
                owner.initialize(json.dumps(value).encode())
        assert not (root / "current.json").exists()


def test_rotation_cannot_drop_account_identity(paths: tuple[Path, Path]) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        owner.begin_refresh("run-1")
        changed = json.loads(fake_auth(2))
        changed["tokens"]["account_id"] = None
        native_save(home, json.dumps(changed).encode())
        with pytest.raises(CredentialError, match="auth_account_id_required"):
            owner.commit_rotation("run-1")
        assert (root / "current.json").read_bytes() == fake_auth()
    with CredentialOwner(*paths) as restarted:
        with pytest.raises(CredentialError, match="auth_rotation_uncertain"):
            restarted.recover(native_stopped=True)


def test_second_python_writer_rejected(paths: tuple[Path, Path]) -> None:
    with CredentialOwner(*paths):
        with pytest.raises(CredentialError, match="auth_owner_busy"):
            with CredentialOwner(*paths):
                pytest.fail("Второй владелец не должен получить lock")
    with CredentialOwner(*paths):
        pass


def test_repository_store_rejected(paths: tuple[Path, Path]) -> None:
    _, repository = paths
    with pytest.raises(CredentialError, match="auth_store_inside_repository"):
        CredentialOwner(repository / "auth", repository)
    assert not (repository / "auth").exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b'{"tokens":',
        b"x" * 65_537,
        b'{"OPENAI_API_KEY":"offline-api-key"}',
        b'{"auth_mode":"chatgpt","auth_mode":"apikey"}',
        b'{"auth_mode":{}}',
        fake_auth().replace(b'"chatgpt"', b'"apikey"'),
        fake_auth().replace(b'"offline-refresh-1"', b'""'),
    ],
)
def test_invalid_authorization_never_written(
    paths: tuple[Path, Path], raw: bytes
) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        with pytest.raises(CredentialError) as caught:
            owner.initialize(raw)
        assert "offline-" not in str(caught.value)
        assert not (root / "current.json").exists()


def test_no_implicit_replacement_of_authorization(paths: tuple[Path, Path]) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        with pytest.raises(CredentialError, match="auth_already_initialized"):
            owner.initialize(fake_auth(2))
        assert (root / "current.json").read_bytes() == fake_auth()


def test_recovery_rolls_forward_local_rotation(paths: tuple[Path, Path]) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        owner.begin_refresh("run-1")
        native_save(home, fake_auth(2))
    with CredentialOwner(*paths) as restarted:
        with pytest.raises(CredentialError, match="native_auth_writer_not_stopped"):
            restarted.recover()
        restarted.recover(native_stopped=True)
        assert (root / "current.json").read_bytes() == fake_auth(2)
        assert not home.exists()
        next_home = restarted.prepare_attempt("run-2")
        assert (next_home / "auth.json").read_bytes() == fake_auth(2)


@pytest.mark.parametrize(
    "failure", ["no_local_save", "missing", "malformed", "metadata_only"]
)
def test_uncertain_rotation_blocks_without_old_token_reuse(
    paths: tuple[Path, Path],
    failure: str,
) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        owner.begin_refresh("run-1")
        if failure == "missing":
            (home / "auth.json").unlink()
        elif failure == "malformed":
            native_save(home, b'{"incomplete":')
        elif failure == "metadata_only":
            native_save(home, fake_auth().replace(b"12:00:00", b"13:00:00"))
    with CredentialOwner(*paths) as restarted:
        with pytest.raises(CredentialError, match="auth_rotation_uncertain"):
            restarted.recover(native_stopped=True)
        with pytest.raises(CredentialError, match="auth_recovery_required"):
            restarted.prepare_attempt("run-2")
        assert (root / "current.json").read_bytes() == fake_auth()
        assert home.exists()
        assert json.loads((root / "journal.json").read_bytes())["state"] == "blocked"


def test_crash_after_current_publish_recovered(
    paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        owner.begin_refresh("run-1")
        native_save(home, fake_auth(2))
        original = owner._save_journal

        def interrupted(journal: dict) -> None:
            if journal["state"] == "active":
                raise CredentialError("simulated_crash")
            original(journal)

        monkeypatch.setattr(owner, "_save_journal", interrupted)
        with pytest.raises(CredentialError, match="simulated_crash"):
            owner.commit_rotation("run-1")
        assert (root / "current.json").read_bytes() == fake_auth(2)
        assert json.loads((root / "journal.json").read_bytes())["state"] == "publishing"
    with CredentialOwner(*paths) as restarted:
        restarted.recover(native_stopped=True)
        assert (root / "current.json").read_bytes() == fake_auth(2)
        assert not home.exists()


def test_recovery_requires_same_account(paths: tuple[Path, Path]) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        owner.begin_refresh("run-1")
        native_save(home, fake_auth(2, account="different-offline-account"))
        with pytest.raises(CredentialError, match="auth_identity_changed"):
            owner.commit_rotation("run-1")
    with CredentialOwner(*paths) as restarted:
        with pytest.raises(CredentialError, match="auth_rotation_uncertain"):
            restarted.recover(native_stopped=True)
        assert (root / "current.json").read_bytes() == fake_auth()


@pytest.mark.parametrize("target", ["current", "attempt", "attempt_directory"])
def test_symlinks_never_read_or_deleted(paths: tuple[Path, Path], target: str) -> None:
    root, repository = paths
    outside = repository / "must-survive"
    outside.write_bytes(fake_auth(99))
    outside.chmod(0o600)
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        if target == "current":
            (root / "current.json").unlink()
            (root / "current.json").symlink_to(outside)
            with pytest.raises(CredentialError):
                owner.prepare_attempt("run-1")
        else:
            home = owner.prepare_attempt("run-1")
            (home / "auth.json").unlink()
            if target == "attempt":
                (home / "auth.json").symlink_to(outside)
            else:
                home.rmdir()
                home.symlink_to(repository, target_is_directory=True)
            with pytest.raises(CredentialError):
                owner.cleanup_attempt("run-1", native_stopped=True)
    assert outside.read_bytes() == fake_auth(99)


def test_public_permissions_rejected(paths: tuple[Path, Path]) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        (root / "current.json").chmod(0o644)
        with pytest.raises(CredentialError, match="unsafe_auth_permissions"):
            owner.prepare_attempt("run-1")


def test_pending_refresh_cannot_be_cleaned(paths: tuple[Path, Path]) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        owner.begin_refresh("run-1")
        with pytest.raises(CredentialError, match="auth_recovery_required"):
            owner.cleanup_attempt("run-1", native_stopped=True)
        assert home.exists()
        assert (root / "current.json").exists()


def test_cleanup_requires_native_stop_and_valid_owner(paths: tuple[Path, Path]) -> None:
    root, _ = paths
    with CredentialOwner(*paths) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        with pytest.raises(CredentialError, match="native_auth_writer_not_stopped"):
            owner.cleanup_attempt("run-1")
        with pytest.raises(CredentialError, match="unknown_auth_attempt"):
            owner.cleanup_attempt("other-run", native_stopped=True)
        owner.cleanup_attempt("run-1", native_stopped=True)
        assert not home.exists()
        assert (root / "current.json").read_bytes() == fake_auth()
    with pytest.raises(CredentialError, match="auth_owner_not_open"):
        owner.prepare_attempt("run-2")


def git_repository(path: Path, ignore: str = ".auth/\n") -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(path)], check=True, capture_output=True
    )
    (path / ".gitignore").write_text(ignore)


def test_approved_repository_auth_folder_is_allowed_only_when_fully_ignored(
    paths: tuple[Path, Path],
) -> None:
    _, repository = paths
    git_repository(repository)
    store = repository / ".auth"
    with CredentialOwner(store, repository) as owner:
        owner.initialize(fake_auth())
        home = owner.prepare_attempt("run-1")
        assert home.is_relative_to(store)
    assert (store / "current.json").read_bytes() == fake_auth()
    ignored = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "--quiet", ".auth/current.json"],
        check=False,
    )
    assert ignored.returncode == 0


@pytest.mark.parametrize("ignore", ["", ".auth/current.json\n", ".auth/*.json\n"])
def test_partial_or_missing_ignore_rejected_before_store_creation(
    paths: tuple[Path, Path],
    ignore: str,
) -> None:
    _, repository = paths
    git_repository(repository, ignore)
    with pytest.raises(CredentialError, match="auth_store_not_fully_ignored"):
        CredentialOwner(repository / ".auth", repository)
    assert not (repository / ".auth").exists()


def test_already_tracked_auth_file_rejected_even_when_ignore_matches(
    paths: tuple[Path, Path],
) -> None:
    _, repository = paths
    git_repository(repository)
    store = repository / ".auth"
    store.mkdir(mode=0o700)
    tracked = store / "older-diagnostic.txt"
    tracked.write_text("offline test fixture")
    subprocess.run(
        ["git", "-C", str(repository), "add", "--force", ".auth/older-diagnostic.txt"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(CredentialError, match="auth_store_contains_tracked_files"):
        CredentialOwner(store, repository)
    assert not (store / "current.json").exists()


def test_ignore_guard_is_rechecked_before_authorization_publication(
    paths: tuple[Path, Path],
) -> None:
    _, repository = paths
    git_repository(repository)
    store = repository / ".auth"
    with CredentialOwner(store, repository) as owner:
        (repository / ".gitignore").write_text("")
        with pytest.raises(CredentialError, match="auth_store_not_fully_ignored"):
            owner.initialize(fake_auth())
    assert not (store / "current.json").exists()


def test_repository_auth_requires_real_repository(paths: tuple[Path, Path]) -> None:
    _, repository = paths
    with pytest.raises(CredentialError, match="auth_store_git_guard_failed"):
        CredentialOwner(repository / ".auth", repository)
