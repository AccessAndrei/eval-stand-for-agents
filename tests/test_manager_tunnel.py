"""CONNECT-парсер и передача байтов тестируются без Docker и live-провайдера."""

import asyncio

import pytest

from contracts import ContractError
from services.sandbox_manager.manager import ManagerError
from services.sandbox_manager.tunnel import _pipe, _request

TOKEN = "t" * 32


def parse(header: bytes) -> tuple[str, str, str]:
    async def check() -> tuple[str, str, str]:
        reader = asyncio.StreamReader(limit=16384)
        reader.feed_data(header)
        reader.feed_eof()
        return await _request(reader, TOKEN)

    return asyncio.run(check())


def test_connect_is_authenticated_and_bound_to_run() -> None:
    header = (
        "CONNECT /v0/environments/handle-1 HTTP/1.1\r\n"
        f"Authorization: Bearer {TOKEN}\r\nX-Project-ID: project\r\n"
        "X-Run-ID: run-1\r\n\r\n"
    ).encode()
    assert parse(header) == ("handle-1", "run-1", "project")
    with pytest.raises(ManagerError, match="unauthorized"):
        parse(header.replace(TOKEN.encode(), b"wrong"))
    with pytest.raises(ContractError):
        parse(header.replace(b"X-Run-ID: run-1\r\n", b""))
    with pytest.raises(ContractError):
        parse(header.replace(b"handle-1", b"../other"))


def test_connect_rejects_duplicate_auth_and_arbitrary_docker_route() -> None:
    header = (
        "CONNECT /v0/environments/h HTTP/1.1\r\n"
        f"Authorization: Bearer {TOKEN}\r\nX-Project-ID: project\r\n"
        "X-Run-ID: run-1\r\n\r\n"
    ).encode()
    with pytest.raises(ManagerError, match="invalid_connect"):
        parse(header.replace(b"/v0/environments/h", b"/containers/json"))
    with pytest.raises(ManagerError, match="duplicate_header"):
        parse(header.replace(b"X-Run-ID:", b"Authorization: duplicate\r\nX-Run-ID:"))


def test_pipe_preserves_utf8_split_bytes_and_applies_backpressure() -> None:
    class Writer:
        def __init__(self) -> None:
            self.output = bytearray()
            self.drains = 0
            self.eof = False

        def write(self, chunk: bytes) -> None:
            self.output.extend(chunk)

        async def drain(self) -> None:
            self.drains += 1

        def can_write_eof(self) -> bool:
            return True

        def write_eof(self) -> None:
            self.eof = True

    async def scenario() -> None:
        reader = asyncio.StreamReader()
        writer = Writer()
        task = asyncio.create_task(_pipe(reader, writer))
        data = "абв".encode()
        reader.feed_data(data[:1])
        await asyncio.sleep(0)
        reader.feed_data(data[1:])
        reader.feed_eof()
        await task
        assert bytes(writer.output) == data
        assert writer.drains == 2
        assert writer.eof

    asyncio.run(scenario())
