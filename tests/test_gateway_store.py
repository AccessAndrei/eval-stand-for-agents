from pathlib import Path

import pytest

from services.gateway.store import Busy, Conflict, RunStore


def launch(run_id: str = "one") -> dict:
    return {
        "version": "v0",
        "run_id": run_id,
        "task_id": "B1.1",
        "provider_profile": "mock",
        "prompt": "Прочитай",
        "files": [],
        "timeout_s": 10,
    }


def test_repeated_accept_and_restart(tmp_path: Path) -> None:
    store = RunStore(tmp_path, 40_000_000)
    first, created = store.accept(launch())
    assert created
    used = store.disk.used()
    repeated, created = RunStore(tmp_path, 40_000_000).accept(launch())
    assert repeated == first and not created
    assert store.disk.used() == used
    with pytest.raises(Conflict):
        store.accept({**launch(), "prompt": "Другое"})
    with pytest.raises(Busy):
        store.accept(launch("two"))


def test_rejected_reservation_is_retryable(tmp_path: Path) -> None:
    from services.common.storage import StorageBudgetExceeded
    from services.gateway.store import RESERVE_BYTES

    store = RunStore(tmp_path, RESERVE_BYTES + 100)
    for _ in range(2):
        with pytest.raises(StorageBudgetExceeded):
            store.accept(launch())
        assert store.records() == []
        assert store.disk.used() == 0


def test_acceptance_does_not_exceed_reserved_limit(tmp_path: Path) -> None:
    from services.gateway.store import FINAL_BYTES, RESERVE_BYTES

    store = RunStore(tmp_path, RESERVE_BYTES + FINAL_BYTES)
    store.accept(launch())
    assert store.disk.used() <= store.disk.limit
