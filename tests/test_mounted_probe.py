"""Диагностика mounted probe при неполной очистке среды."""

import asyncio
from pathlib import Path

import pytest

from tests.infra import mounted_probe


def test_mount_failure_remains_visible_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class Session:
        session_id = "session-under-test"

        async def mount(self) -> dict[str, str]:
            return {"state": "ready", "mode": "rw"}

        async def close(self) -> None:
            raise OSError("sensitive transport detail")

    class Client:
        def __init__(self, gateway: str, token: str, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *error: object) -> None:
            pass

        def session(self, folder: Path, *, profile: str, mode: str) -> Session:
            assert (
                folder / "container-marker.txt"
            ).read_bytes() == mounted_probe.MARKER
            return Session()

    def broken_inspect(project: str, session_id: str, mode: str) -> str:
        raise mounted_probe.ProbeError("agent_actual_sshfs_mount")

    def remaining(project: str, session_id: str) -> tuple[list[str], list[str]]:
        calls.append((project, session_id))
        return [], ["unreleased-workspace"]

    monkeypatch.setattr(mounted_probe, "MountedClient", Client)
    monkeypatch.setattr(mounted_probe, "_inspect_mount", broken_inspect)
    monkeypatch.setattr(mounted_probe, "_resources", remaining)
    report: dict = {"checks": [], "cleanup": {}}

    with pytest.raises(mounted_probe.ProbeError, match="^agent_actual_sshfs_mount$"):
        asyncio.run(
            mounted_probe._probe(
                "http://127.0.0.1:8080", "test-token", "probe-project", report
            )
        )

    assert calls == [("probe-project", "session-under-test")]
    assert report["cleanup"] == {
        "rw": {
            "confirmed": False,
            "close_error": "OSError",
            "sandbox_absent": True,
            "volume_absent": False,
        }
    }
