"""Прозрачный stdio → CONNECT; stdout содержит только байты исполнителя."""

import asyncio
import os
import sys
from urllib.parse import urlsplit


async def relay() -> None:
    address = urlsplit(os.environ["MANAGER_TUNNEL_URL"])
    if address.scheme != "http" or not address.hostname:
        raise ValueError("Требуется внутренний HTTP-адрес менеджера")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(address.hostname, address.port or 8082), 10
    )
    handle = os.environ["SANDBOX_HANDLE"]
    token = os.environ["MANAGER_TOKEN"]
    project = os.environ["PROJECT_ID"]
    run_id = os.environ["RUN_ID"]
    if any("\r" in item or "\n" in item for item in (handle, token, project, run_id)):
        raise ValueError("Недопустимый заголовок")
    writer.write(
        (
            f"CONNECT /v0/environments/{handle} HTTP/1.1\r\n"
            f"Host: {address.hostname}\r\nAuthorization: Bearer {token}\r\n"
            f"X-Project-ID: {project}\r\nX-Run-ID: {run_id}\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    if not header.startswith(b"HTTP/1.1 200 "):
        raise ConnectionError("Менеджер отклонил канал")
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(stdin)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    async def upload() -> None:
        while chunk := await stdin.read(65536):
            writer.write(chunk)
            await writer.drain()
        writer.write_eof()

    async def download() -> None:
        while chunk := await reader.read(65536):
            await asyncio.to_thread(sys.stdout.buffer.write, chunk)
            await asyncio.to_thread(sys.stdout.buffer.flush)

    tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
    try:
        # Ответ EOF может прийти до stdin: не оставлять relay висящим.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if tasks[0] in done:
            await tasks[1]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(relay())
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        print("Канал исполнителя недоступен", file=sys.stderr)
        raise SystemExit(1)
