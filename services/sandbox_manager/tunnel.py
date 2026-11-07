"""Узкий CONNECT-туннель фиксированного exec-server, stdout и stderr раздельно."""

import asyncio
import secrets

from contracts import validate
from services.common.processes import read_bounded, stop_group

from .manager import Manager, ManagerError


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def _request(reader: asyncio.StreamReader, token: str) -> tuple[str, str, str]:
    async with asyncio.timeout(5):
        header = await reader.readuntil(b"\r\n\r\n")
    lines = header.decode("ascii").split("\r\n")
    method, path, protocol = lines[0].split(" ")
    if method != "CONNECT" or protocol != "HTTP/1.1":
        raise ManagerError("invalid_connect", 400)
    prefix = "/v0/environments/"
    if not path.startswith(prefix):
        raise ManagerError("invalid_connect", 400)
    headers: dict[str, str] = {}
    for line in lines[1:-2]:
        key, value = line.split(":", 1)
        key = key.lower()
        if key in headers:
            raise ManagerError("duplicate_header", 400)
        headers[key] = value.strip()
    if not secrets.compare_digest(headers.get("authorization", ""), "Bearer " + token):
        raise ManagerError("unauthorized", 401)
    handle = path[len(prefix) :]
    run_id, project = headers.get("x-run-id", ""), headers.get("x-project-id", "")
    validate(
        "connect",
        {"version": "v0", "run_id": run_id, "project_id": project, "handle": handle},
    )
    return handle, run_id, project


async def connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    manager: Manager,
    token: str,
) -> None:
    process = None
    tasks: list[asyncio.Task] = []
    connected = False
    try:
        handle, run_id, project = await _request(reader, token)
        process = await manager.connect(handle, run_id, project)
        assert process.stdin and process.stdout and process.stderr
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        connected = True

        async def monitor_executor() -> None:
            await manager.executor_exited(run_id, await process.wait())

        tasks = [
            asyncio.create_task(_pipe(reader, process.stdin)),
            asyncio.create_task(_pipe(process.stdout, writer)),
            asyncio.create_task(read_bounded(process.stderr, 16384)),
            asyncio.create_task(monitor_executor()),
        ]
        deadline = manager._load(run_id)["request"]["timeout_s"]
        async with asyncio.timeout(deadline):
            await asyncio.gather(*tasks)
            await process.wait()
        stderr, truncated = tasks[2].result()
        # Этот поток не содержит управляющее окружение и не попадает в протокол.
        (manager.root / f"{run_id}.stderr").write_bytes(stderr)
        if truncated:
            (manager.root / f"{run_id}.stderr-truncated").write_text("true\n")
    except Exception as error:
        if not connected:
            status = error.http_status if isinstance(error, ManagerError) else 400
            writer.write(
                f"HTTP/1.1 {status} Rejected\r\nConnection: close\r\n\r\n".encode()
            )
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process:
            await stop_group(process, timeout=3)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def serve_tunnel(
    manager: Manager, token: str, host: str, port: int
) -> asyncio.Server:
    if len(token) < 24:
        raise ValueError("manager_token_required")
    return await asyncio.start_server(
        lambda reader, writer: connection(reader, writer, manager, token),
        host,
        port,
        limit=16384,
    )
