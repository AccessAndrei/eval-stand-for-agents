"""Сверка на синтетическом /proc: настоящим процессам сигналы не посылаются."""

import asyncio
import json
import os
import shutil
import signal
import time
from pathlib import Path

import pytest

from services.harness.recovery import (
    OWNER_ENV,
    HarnessRecovery,
    ProcessIdentity,
    RecoveryError,
)

RecoveryFixture = tuple[HarnessRecovery, Path, list[tuple[int, int]]]

PROGRAMS = {
    "/trusted/codex": ("app-server", "--listen", "stdio://"),
    "/trusted/python": ("-m", "services.harness.relay"),
}


def configured_home(state: Path, run_id: str) -> Path:
    home = state / run_id
    home.mkdir(mode=0o700)
    for name in ("config.toml", "environments.toml"):
        path = home / name
        path.write_text("token = 'never-store-this-config-secret'\n")
        path.chmod(0o600)
    return home


def process(
    proc: Path,
    pid: int,
    sid: int,
    home: Path,
    nonce: str,
    *,
    program: str = "/trusted/codex",
    started: int = 11,
    arguments: tuple[str, ...] | None = None,
    uid: int | None = None,
) -> None:
    directory = proc / str(pid)
    directory.mkdir()
    fields = ["S", "0", str(pid), str(sid), *(["0"] * 15), str(started)]
    (directory / "stat").write_text(f"{pid} (fake process) " + " ".join(fields))
    process_uid = os.getuid() if uid is None else uid
    (directory / "status").write_text(
        "Uid:\t" + "\t".join([str(process_uid)] * 4) + "\n"
    )
    (directory / "exe").symlink_to(program)
    argv = (
        program,
        *(PROGRAMS.get(program, ("other",)) if arguments is None else arguments),
    )
    (directory / "cmdline").write_bytes(
        b"\0".join(os.fsencode(arg) for arg in argv) + b"\0"
    )
    (directory / "environ").write_bytes(
        b"MANAGER_TOKEN=never-store-this-environment-secret\0"
        + b"CODEX_HOME="
        + os.fsencode(home)
        + b"\0"
        + OWNER_ENV.encode()
        + b"="
        + nonce.encode()
        + b"\0"
    )


@pytest.fixture
def recovery(tmp_path: Path) -> tuple[HarnessRecovery, Path, list[tuple[int, int]]]:
    proc = tmp_path / "proc"
    proc.mkdir()
    signals = []

    def stop(identity: ProcessIdentity, number: int) -> None:
        signals.append((identity.pid, number))
        shutil.rmtree(proc / str(identity.pid))

    owner = HarnessRecovery(
        tmp_path / "state",
        proc_root=proc,
        allowed_programs=PROGRAMS,
        send_signal=stop,
        per_attempt_s=0.2,
        startup_s=0.5,
    )
    return owner, proc, signals


def test_registry_precedes_spawn_and_unknown_is_not_released(
    recovery: RecoveryFixture,
) -> None:
    owner, _, _ = recovery
    home = configured_home(owner.state, "run-1")
    env = owner.begin_attempt("run-1", home)
    assert len(env[OWNER_ENV]) == 64
    assert owner.status("unknown") is None
    assert owner.status("run-1")["released"] is False
    assert owner.status("run-1")["done"] is False
    with pytest.raises(RecoveryError, match="attempt_already_registered"):
        owner.begin_attempt("run-1", home)
    with pytest.raises(RecoveryError, match="invalid_run_id"):
        owner.status("../foreign")


def test_no_processes_after_container_restart_proves_cleanup(
    recovery: RecoveryFixture,
) -> None:
    owner, _, signals = recovery
    home = configured_home(owner.state, "run-1")
    owner.begin_attempt("run-1")
    result = asyncio.run(owner.reconcile())["run-1"]
    assert result["done"] and result["released"]
    assert result["execution_status"] == "interrupted"
    assert result["evidence_complete"] is False
    assert not home.exists()
    assert signals == []
    assert asyncio.run(owner.reconcile())["run-1"] == result
    saved = (owner.receipts / "run-1.json").read_text()
    assert "never-store-this" not in saved


def test_app_server_relay_and_owned_session_descendants_are_stopped(
    recovery: RecoveryFixture,
) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500001, 500001, home, nonce)
    process(proc, 500002, 500001, home, nonce, program="/trusted/python")
    process(proc, 500003, 500001, home, nonce, program="/other/owned-child")
    process(proc, 500010, 500010, owner.state / "foreign", "other")
    result = asyncio.run(owner.reconcile())["run-1"]
    assert result["released"]
    assert {pid for pid, _ in signals} == {500001, 500002, 500003}
    assert (proc / "500010").exists()


def test_orphan_relay_with_separate_group_is_still_owned(
    recovery: RecoveryFixture,
) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500002, 500001, home, nonce, program="/trusted/python")
    assert asyncio.run(owner.reconcile())["run-1"]["released"]
    assert signals == [(500002, signal.SIGTERM)]


@pytest.mark.parametrize(
    "kind",
    ["wrong_nonce", "unknown_program", "mixed_session", "wrong_session", "wrong_argv"],
)
def test_unconfirmed_ownership_preserves_processes_and_home(
    recovery: RecoveryFixture, kind: str
) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(
        proc,
        500001,
        500002 if kind == "wrong_session" else 500001,
        home,
        "different" if kind == "wrong_nonce" else nonce,
        program="/other/foreign" if kind == "unknown_program" else "/trusted/codex",
        arguments=("exec",) if kind == "wrong_argv" else None,
    )
    if kind == "mixed_session":
        process(
            proc,
            500002,
            500001,
            owner.state / "foreign",
            "different",
            program="/other/foreign",
        )
    result = asyncio.run(owner.reconcile())["run-1"]
    assert result["released"] is False
    assert result["cleanup_error"] == "harness_cleanup_unconfirmed"
    assert home.exists()
    assert signals == []
    assert (proc / "500001").exists()


def test_pid_reuse_between_scan_and_signal_never_signals_new_process(
    recovery: RecoveryFixture,
) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500001, 500001, home, nonce)
    before = owner._process(500001, home, nonce)
    shutil.rmtree(proc / "500001")
    process(proc, 500001, 500001, owner.state / "foreign", "other", started=12)
    owner._signal(before, signal.SIGKILL, home, nonce)
    assert signals == []
    assert (proc / "500001").exists()


def test_ignored_first_signal_escalates_within_deadline(
    recovery: RecoveryFixture,
) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500001, 500001, home, nonce)

    def ignore_term(identity: ProcessIdentity, number: int) -> None:
        signals.append((identity.pid, number))
        if number == signal.SIGKILL:
            shutil.rmtree(proc / str(identity.pid))

    owner.send_signal = ignore_term
    assert asyncio.run(owner.reconcile())["run-1"]["released"]
    assert signal.SIGTERM in [number for _, number in signals]
    assert signal.SIGKILL in [number for _, number in signals]


def test_unstoppable_process_blocks_next_run_boundedly(
    recovery: RecoveryFixture,
) -> None:
    owner, proc, _ = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500001, 500001, home, nonce)
    owner.send_signal = lambda identity, number: None
    started = time.monotonic()
    result = asyncio.run(owner.reconcile())["run-1"]
    assert time.monotonic() - started < 0.5
    assert result["released"] is False
    assert home.exists()


def native_snapshot() -> dict:
    return {
        "execution_status": "success",
        "released": True,
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "events": [{"item_id": "item-1"}],
        "traces": [{"output": "normal output"}],
        "trace_truncated": False,
        "evidence_complete": True,
        "codex_version": "0.159.3",
    }


def test_normal_completion_receipt_preserves_native_evidence(
    recovery: RecoveryFixture,
) -> None:
    owner, _, _ = recovery
    home = configured_home(owner.state, "run-1")
    owner.begin_attempt("run-1")
    result = asyncio.run(owner.complete("run-1", native_snapshot()))
    assert result["released"] and result["done"]
    assert result["execution_status"] == "success"
    assert result["evidence_complete"] is True
    assert result["events"] == native_snapshot()["events"]
    assert result["traces"] == native_snapshot()["traces"]
    assert not home.exists()
    assert asyncio.run(owner.reconcile())["run-1"] == result


def test_crash_after_home_deletion_does_not_lose_native_result(
    recovery: RecoveryFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, _ = recovery
    home = configured_home(owner.state, "run-1")
    owner.begin_attempt("run-1")
    original = owner._write

    def crash_on_final(record: dict) -> None:
        if record["phase"] == "released":
            raise RecoveryError("simulated_crash")
        original(record)

    monkeypatch.setattr(owner, "_write", crash_on_final)
    failed = asyncio.run(owner.complete("run-1", native_snapshot()))
    assert failed["released"] is False
    assert failed["cleanup_error"] == "harness_receipt_write_failed"
    assert not home.exists()
    assert (
        json.loads((owner.receipts / "run-1.json").read_text())["phase"] == "releasing"
    )
    monkeypatch.setattr(owner, "_write", original)
    result = asyncio.run(owner.reconcile())["run-1"]
    assert result["released"] and result["execution_status"] == "success"
    assert result["events"] == native_snapshot()["events"]


def test_unregistered_legacy_home_is_not_claimed_or_deleted(
    recovery: RecoveryFixture,
) -> None:
    owner, _, signals = recovery
    home = configured_home(owner.state, "legacy")
    with pytest.raises(RecoveryError, match="unregistered_attempt_home"):
        asyncio.run(owner.reconcile())
    assert home.exists()
    assert owner.status("legacy") is None
    assert signals == []


def test_home_symlink_never_deletes_foreign_directory(
    recovery: RecoveryFixture, tmp_path: Path
) -> None:
    owner, _, _ = recovery
    home = configured_home(owner.state, "run-1")
    owner.begin_attempt("run-1")
    shutil.rmtree(home)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("preserve")
    home.symlink_to(foreign, target_is_directory=True)
    assert asyncio.run(owner.reconcile())["run-1"]["released"] is False
    assert (foreign / "keep").read_text() == "preserve"


def test_snapshot_bounds_mark_evidence_and_trace_truncation(
    recovery: RecoveryFixture,
) -> None:
    owner, _, _ = recovery
    configured_home(owner.state, "run-1")
    owner.begin_attempt("run-1")
    snapshot = native_snapshot()
    snapshot["events"] = [{"value": "x" * 200_000}] * 6
    snapshot["traces"] = [{"output": "x" * 200_000}] * 6
    result = asyncio.run(owner.complete("run-1", snapshot))
    assert result["released"] is True
    assert result["evidence_complete"] is False
    assert result["trace_truncated"] is True
    assert (owner.receipts / "run-1.json").stat().st_size <= 1_048_576


def test_compact_startup_keeps_full_evidence_only_in_receipt(
    recovery: RecoveryFixture,
) -> None:
    owner, _, _ = recovery
    configured_home(owner.state, "run-1")
    owner.begin_attempt("run-1")
    asyncio.run(owner.complete("run-1", native_snapshot()))
    controls = asyncio.run(owner.reconcile(controls_only=True))["run-1"]
    assert set(controls) == {"done", "released", "execution_status", "cleanup_error"}
    assert owner.status("run-1")["events"] == native_snapshot()["events"]


def test_foreign_uid_environment_is_not_read(recovery: RecoveryFixture) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    owner.begin_attempt("run-1")
    process(proc, 500001, 500001, home, "other", uid=os.getuid() + 1)
    (proc / "500001/environ").unlink()
    assert asyncio.run(owner.reconcile())["run-1"]["released"]
    assert signals == []
    assert (proc / "500001").exists()


def test_unreadable_own_process_marker_cannot_prove_absence(
    recovery: RecoveryFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.harness import recovery as module

    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500001, 500001, home, nonce)

    def denied(*args: object) -> None:
        raise PermissionError("simulated process hardening")

    monkeypatch.setattr(module, "_markers", denied)
    assert asyncio.run(owner.reconcile())["run-1"]["released"] is False
    assert signals == []
    assert home.exists()


def test_initial_receipt_write_failure_still_stops_processes_and_retains_home(
    recovery: RecoveryFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500001, 500001, home, nonce)

    def disk_full(record: dict) -> None:
        raise RecoveryError("recovery_storage_failed")

    monkeypatch.setattr(owner, "_write", disk_full)
    failed = asyncio.run(owner.complete("run-1", native_snapshot()))
    assert signals == [(500001, signal.SIGTERM)]
    assert not (proc / "500001").exists()
    assert home.exists()
    assert failed["released"] is False
    assert failed["execution_status"] == "success"
    assert failed["cleanup_error"] == "harness_receipt_write_failed"
    assert (
        json.loads((owner.receipts / "run-1.json").read_text())["phase"] == "registered"
    )


def test_owned_version_probe_is_a_trusted_separate_session(
    recovery: RecoveryFixture,
) -> None:
    owner, proc, signals = recovery
    home = configured_home(owner.state, "run-1")
    nonce = owner.begin_attempt("run-1")[OWNER_ENV]
    process(proc, 500001, 500001, home, nonce, arguments=("--version",))
    assert asyncio.run(owner.reconcile())["run-1"]["released"]
    assert signals == [(500001, signal.SIGTERM)]
