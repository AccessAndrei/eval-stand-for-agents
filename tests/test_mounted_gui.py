import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from client import gui


class _Session:
    def __init__(self) -> None:
        self.after: list[int] = []
        self.started: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.checked: list[str] = []
        self.closed = False
        self.close_ok = True
        self.history_gap = False

    async def mount(self) -> None:
        pass

    async def start_run(
        self, prompt: str, *, timeout_s: int, run_id: str
    ) -> dict[str, str]:
        self.started.append((run_id, prompt))
        return {"run_id": run_id}

    async def events(
        self, run_id: str, *, after: int, timeout: float
    ) -> AsyncIterator[dict[str, object]]:
        assert run_id == self.started[0][0]
        self.after.append(after)
        if after == 0:
            yield {"seq": 1, "type": "message", "text": "Первая часть"}
            raise OSError("private_test_detail")
        if self.history_gap:
            raise _HistoryGap()
        yield {"seq": 2, "type": "message", "text": "Вторая часть"}
        yield {
            "seq": 3,
            "type": "tool",
            "tool": {"type": "commandExecution", "phase": "completed", "exit_code": 0},
        }
        yield {"seq": 4, "type": "result", "result": {"execution_status": "success"}}

    async def run_status(self, run_id: str) -> dict[str, object]:
        self.checked.append(run_id)
        return {"state": "terminal", "result": {"execution_status": "success"}}

    async def cancel_run(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def close(self) -> SimpleNamespace:
        self.closed = True
        return SimpleNamespace(ok=self.close_ok)


class _Client:
    def __init__(self, session: _Session) -> None:
        self._session = session
        self.opened: list[tuple[Path, str, str]] = []
        self.exited = False

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *exception: object) -> None:
        self.exited = True

    def session(self, root: Path, *, profile: str, mode: str) -> _Session:
        self.opened.append((root, profile, mode))
        return self._session


class _HistoryGap(Exception):
    code = "history_unavailable"
    earliest_seq = 4


def test_gui_controller_resumes_same_run_after_last_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _Session()
    client = _Client(session)
    updates = []
    monkeypatch.setattr(gui, "_mounted_client", lambda address, token: client)

    async def scenario() -> None:
        chat = gui._Chat(lambda kind, text: updates.append((kind, text)))
        await chat.connect(
            "http://127.0.0.1:8080", "test-token", tmp_path, "mock", "ro"
        )
        assert updates[-1] == ("ready", "Папка подключена в /work (ro).")
        await chat.send("Прочитай файл")
        assert updates[-1][0] == "paused"
        await chat.cancel()
        await chat.watch()
        assert updates[-1][0] == "ready"
        await chat.disconnect()

    asyncio.run(scenario())
    assert client.opened == [(tmp_path, "mock", "ro")]
    assert [prompt for _, prompt in session.started] == ["Прочитай файл"]
    assert session.cancelled == [session.started[0][0]]
    assert session.after == [0, 1]
    assert "".join(text for kind, text in updates if kind == "chat") == (
        "\nАгент: Первая часть\n\nАгент: Вторая часть\n"
    )
    assert "private_test_detail" not in str(updates)
    assert ("log", "Команда завершилась (код 0).") in updates
    assert updates[-1][0] == "disconnected"
    assert session.closed and client.exited


def test_gui_controller_reports_unconfirmed_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _Session()
    session.close_ok = False
    client = _Client(session)
    updates = []
    monkeypatch.setattr(gui, "_mounted_client", lambda address, token: client)

    async def scenario() -> None:
        chat = gui._Chat(lambda kind, text: updates.append((kind, text)))
        await chat.connect(
            "http://127.0.0.1:8080", "test-token", tmp_path, "mock", "rw"
        )
        await chat.disconnect()

    asyncio.run(scenario())
    assert updates[-1][0] == "cleanup_failed"
    assert session.closed and client.exited


def test_gui_controller_checks_same_run_when_earlier_events_are_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _Session()
    session.history_gap = True
    monkeypatch.setattr(gui, "_mounted_client", lambda address, token: _Client(session))
    updates = []

    async def scenario() -> None:
        chat = gui._Chat(lambda kind, text: updates.append((kind, text)))
        await chat.connect(
            "http://127.0.0.1:8080", "test-token", tmp_path, "mock", "rw"
        )
        await chat.send("Прочитай файл")
        await chat.watch()
        assert updates[-1] == ("ready", "Запуск завершён (успех).")
        await chat.disconnect()

    asyncio.run(scenario())
    assert session.checked == [session.started[0][0]]
    assert any("Первый доступный номер - 4" in text for _, text in updates)
