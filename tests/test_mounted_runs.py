import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from contracts import ContractError
from contracts.mounted import validate_mounted
from services.gateway.clients import RemoteError
from services.gateway.mounted_runs import EventLog, MountedRuns
from services.gateway.runtime import Runtime
from services.gateway.store import RunStore

SESSION = "a" * 32
RUN = "b" * 32


class Sessions:
    @asynccontextmanager
    async def locked(self, request):
        yield SimpleNamespace(
            session_id=SESSION,
            manager_handle="1" * 48,
            profile="mock",
            profile_snapshot={},
            state="ready",
        )


class Service:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    async def request(self, method, endpoint, data=None, timeout=None):
        self.calls.append((method, endpoint))
        if self.response is None:
            raise RemoteError(404, "not_found")
        return self.response


def runtime(tmp_path: Path, proxy=None) -> Runtime:
    return Runtime(
        RunStore(tmp_path, 1_000_000),
        Service(),
        Service(),
        proxy or Service(),
        "testing",
        {},
    )


def test_invalid_unicode_and_invalid_id_never_reserve_work(tmp_path: Path) -> None:
    service = runtime(tmp_path)

    async def invalid_json():
        return {"run_id": RUN, "timeout_s": 10, "prompt": chr(0xD800)}

    controller = MountedRuns(service, Sessions())
    with pytest.raises(UnicodeError):
        asyncio.run(controller.start(SimpleNamespace(json=invalid_json)))
    assert service.store.disk.used() == 0
    assert not list(controller.root.iterdir())
    with pytest.raises(ContractError):
        validate_mounted("id", RUN + "\n")


def test_recovery_stores_one_interrupted_result_without_run_replay(
    tmp_path: Path,
) -> None:
    controller = MountedRuns(runtime(tmp_path), Sessions())
    session = SimpleNamespace(
        session_id=SESSION, manager_handle="1" * 48, profile="mock"
    )
    controller._open(session, {"run_id": RUN}, "f" * 64)
    restarted = MountedRuns(controller.runtime, Sessions())
    asyncio.run(restarted.recover(web.Application()))
    first = restarted._receipt(SESSION, RUN)["result"]
    asyncio.run(restarted.recover(web.Application()))
    assert first == restarted._receipt(SESSION, RUN)["result"]
    assert first["execution_status"] == "interrupted"
    assert first["writers_stopped"] is False
    assert [action for action, _ in restarted.runtime.harness.calls] == ["GET"]
    assert all(
        endpoint.endswith(("/collect", "/close"))
        for method, endpoint in restarted.runtime.manager.calls
        + restarted.runtime.proxy.calls
    )


def test_proxy_outcome_cannot_replace_timeout(tmp_path: Path) -> None:
    identity = {"version": "v1", "session_id": SESSION, "run_id": RUN}
    proxy = Service(
        {**identity, "tokens": {"actual": None}, "error": "auth_error", "closed": True}
    )
    controller = MountedRuns(runtime(tmp_path, proxy), Sessions())
    result = {"execution_status": "timeout", "errors": []}
    asyncio.run(controller._close_proxy({**identity, "proxy_ref": "x"}, result))
    assert result["execution_status"] == "timeout"
    assert result["errors"][0]["code"] == "auth_error"
    assert result["proxy_closed"]


@pytest.mark.parametrize("confirmed", [True, False])
def test_next_turn_needs_durable_clearance(tmp_path: Path, confirmed: bool) -> None:
    controller = MountedRuns(runtime(tmp_path), Sessions())
    session = SimpleNamespace(
        session_id=SESSION, manager_handle="1" * 48, profile="mock"
    )
    receipt = controller._open(session, {"run_id": RUN}, "f" * 64)
    with pytest.raises(web.HTTPConflict):
        controller._require_prior_clearance(SESSION)
    result = controller._result(receipt)
    result["continuation_ready"] = confirmed
    controller._finish(receipt, result, 0, EventLog())
    restarted = MountedRuns(controller.runtime, Sessions())
    if confirmed:
        restarted._require_prior_clearance(SESSION)
    else:
        with pytest.raises(web.HTTPConflict):
            restarted._require_prior_clearance(SESSION)
