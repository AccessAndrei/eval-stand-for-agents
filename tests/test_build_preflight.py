"""Ресурсный preflight и допустимая нормализация lock без внешних обновлений."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import build_codex
from scripts.build_codex import normalize_lock, preflight, validate_version


def test_preflight_accepts_less_than_maximum() -> None:
    info = {
        "MemTotal": 3_000_000_000,
        "Architecture": "aarch64",
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsQuota": True,
    }
    assert preflight(info, 2_000_000_000, 60) == "linux/arm64"
    with pytest.raises(ValueError):
        preflight({**info, "CpuCfsQuota": False}, 2_000_000_000, 60)
    with pytest.raises(ValueError):
        preflight(info, 4_000_000_000, 60)
    with pytest.raises(ValueError):
        preflight(info, 2_000_000_000, 3601)


def test_lock_normalization_changes_only_local_workspace_versions(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text('[workspace.package]\nversion="0.159.3"\n')
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli/Cargo.toml").write_text(
        '[package]\nname="codex-cli"\nversion.workspace=true\n'
    )
    (tmp_path / "Cargo.lock").write_text("""version = 4

[[package]]
name = "codex-cli"
version = "0.0.0"
dependencies = ["serde"]

[[package]]
name = "serde"
version = "1.0.200"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "unchanged"
""")
    report = normalize_lock(tmp_path)
    assert report["workspace_version_changes"] == {"codex-cli": ("0.0.0", "0.159.3")}
    assert report["external_dependencies_unchanged"] is True
    assert 'version = "1.0.200"' in (tmp_path / "Cargo.lock").read_text()
    assert 'checksum = "unchanged"' in (tmp_path / "Cargo.lock").read_text()
    saved = json.loads(
        (tmp_path / ".build-evidence/lock-normalization.json").read_text()
    )
    assert saved["before_sha256"] != saved["after_sha256"]
    assert (
        "-version"
        in (tmp_path / ".build-evidence/lock-normalization.patch").read_text()
    )
    second = normalize_lock(tmp_path)
    assert second["workspace_version_changes"] == {}


def test_exact_binary_version_is_required() -> None:
    validate_version("codex-cli 0.159.3\n")
    with pytest.raises(ValueError):
        validate_version("codex-cli 0.0.0")


def test_expired_deadline_prevents_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_codex, "_DEADLINE", 0)
    with pytest.raises(subprocess.TimeoutExpired):
        build_codex.command(["must-not-execute"])


def test_cleanup_without_owned_id_never_calls_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("Docker не должен вызываться без подтверждённого id")

    monkeypatch.setattr(build_codex, "command", unexpected)
    build_codex.stop_builder(None)


def test_watchdog_stops_only_supplied_id_and_preserves_finished_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"status": "building"}))
    stopped = []
    monkeypatch.setattr(build_codex, "stop_builder", stopped.append)
    build_codex.watchdog(tmp_path, 0, "owned-id")
    assert stopped == ["owned-id"]
    assert json.loads(path.read_text())["status"] == "timeout"
    build_codex.watchdog(tmp_path, 0, "owned-id")
    assert stopped == ["owned-id"]


def test_new_builder_id_is_recovered_when_bootstrap_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = {
        "Id": "own-created-id",
        "Name": "/" + build_codex.CONTAINER,
        "Config": {"Image": build_codex.BUILDER_IMAGE},
    }

    def fake_command(args: list[str], **kwargs: object) -> str:
        if args[1:3] == ["buildx", "ls"]:
            return ""
        if "--bootstrap" in args:
            raise RuntimeError("bootstrap lost response")
        if args[1] == "inspect":
            return json.dumps([container])
        return build_codex.BUILDER

    monkeypatch.setattr(build_codex, "command", fake_command)
    ownership = {}
    with pytest.raises(RuntimeError, match="lost response"):
        build_codex.prepare_builder(7_000_000_000, ownership)
    assert ownership == {"container_id": "own-created-id"}


def test_foreign_existing_builder_is_not_started_or_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = {
        "Id": "foreign-id",
        "Name": "/" + build_codex.CONTAINER,
        "Config": {"Image": build_codex.BUILDER_IMAGE},
        "HostConfig": {
            "Memory": 7_000_000_000,
            "MemorySwap": 7_000_000_000,
            "CpuQuota": 100_000,
            "CpuPeriod": 100_000,
        },
        "State": {"Running": False},
    }
    commands = []

    def fake_command(args: list[str], **kwargs: object) -> str:
        commands.append(args)
        return (
            build_codex.BUILDER
            if args[1:3] == ["buildx", "ls"]
            else json.dumps([container])
        )

    monkeypatch.setattr(build_codex, "command", fake_command)
    monkeypatch.setattr(build_codex, "recorded_builder_ids", lambda: set())
    ownership = {}
    with pytest.raises(ValueError, match="не принадлежит"):
        build_codex.prepare_builder(7_000_000_000, ownership)
    assert ownership == {}
    assert not any("--bootstrap" in args for args in commands)
