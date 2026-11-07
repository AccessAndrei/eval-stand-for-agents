import json
from pathlib import Path

import pytest

from services.common.storage import Storage, StorageBudgetExceeded


def test_reserved_finalization_and_history(tmp_path: Path) -> None:
    store = Storage(tmp_path, 1024)
    reserved = store.reserve("run-1", 1024)
    assert store.reserve("run-1", 1024) == reserved
    with pytest.raises(StorageBudgetExceeded):
        store.reserve("run-2", 1)
    result = tmp_path / "result.json"
    store.finalize(reserved, result, {"execution_status": "timeout"})
    assert json.loads(result.read_text())["execution_status"] == "timeout"
    with pytest.raises(FileExistsError):
        store.finalize(reserved, result, {})
    assert result.exists()


def test_oversize_preserves_reserve(tmp_path: Path) -> None:
    store = Storage(tmp_path, 12)
    reserved = store.reserve("run", 12)
    with pytest.raises(StorageBudgetExceeded):
        store.finalize(reserved, tmp_path / "result.json", {"too_large": "x" * 20})
    assert reserved.stat().st_size == 12


def test_writes_consume_reserved_capacity(tmp_path: Path) -> None:
    store = Storage(tmp_path, 1024)
    reserve = store.reserve("run", 1024)
    for index in range(4):
        store.write_reserved(reserve, tmp_path / "state.json", {"index": index})
        assert store.used() <= 1024
    with pytest.raises(StorageBudgetExceeded):
        store.write_reserved(reserve, tmp_path / "huge.json", {"payload": "x" * 1024})


def test_crash_before_commit_keeps_full_reserve_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    store = Storage(tmp_path, 2048)
    reserve = store.reserve("run", 2048)
    replace = os.replace

    def interrupted(*args: object) -> None:
        raise OSError("interrupted before rename")

    monkeypatch.setattr("services.common.storage.os.replace", interrupted)
    with pytest.raises(OSError):
        store.finalize(
            reserve, tmp_path / "result.json", {"execution_status": "success"}
        )
    assert reserve.stat().st_size == 2048
    assert json.loads(reserve.read_text()) == {"execution_status": "success"}
    monkeypatch.setattr("services.common.storage.os.replace", replace)
    recovered = {
        "execution_status": "interrupted",
        "errors": ["a longer recovery result"],
    }
    store.finalize(reserve, tmp_path / "result.json", recovered)
    assert json.loads((tmp_path / "result.json").read_text()) == recovered
