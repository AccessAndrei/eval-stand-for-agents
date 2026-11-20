"""Узкий CONNECT-туннель фиксированного exec-server, stdout и stderr раздельно."""

import asyncio
import logging
import secrets

from contracts import validate
from contracts.mounted import validate_mounted
from services.common.processes import read_bounded, stop_group

from .docker import DockerError
from .manager import Manager, ManagerError, MountedExecutor

LOG = logging.getLogger(__name__)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def _request(
    reader: asyncio.StreamReader, token: str
) -> tuple[str, str, str] | tuple[str, str, str, str]:
    async with asyncio.timeout(5):
        header = await reader.readuntil(b"\r\n\r\n")
    lines = header.decode("ascii").split("\r\n")
    method, path, protocol = lines[0].split(" ")
    if method != "CONNECT" or protocol != "HTTP/1.1":
        raise ManagerError("invalid_connect", 400)
    prefix = next(
        (
            item
            for item in ("/v0/environments/", "/v1/environments/")
            if path.startswith(item)
        ),
        None,
    )
    if prefix is None:
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
    if prefix == "/v1/environments/":
        session_id = headers.get("x-session-id", "")
        validate_mounted(
            "manager_turn",
            {
                "version": "v1",
                "run_id": run_id,
                "project_id": project,
                "handle": handle,
                "session_id": session_id,
            },
        )
        return handle, run_id, project, session_id
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
    link: MountedExecutor | None = None
    bridge: asyncio.Future | None = None
    tasks: list[asyncio.Task] = []
    connected = False
    try:
        handle, run_id, project, *session = await _request(reader, token)
        owner_id = session[0] if session else run_id
        if session:
            link = await manager.connect_mounted(handle, run_id, project, owner_id)
            process, deadline = link.process, link.timeout_s
        else:
            process = await manager.connect(handle, run_id, project)
            deadline = manager._load(run_id)["request"]["timeout_s"]
        assert process.stdin and process.stdout and process.stderr
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        connected = True

        async def monitor_executor() -> None:
            returncode = await process.wait()
            if link is None:
                await manager.executor_exited(owner_id, returncode)
            elif returncode != 0:
                await manager.mounted_transport_failure(link, returncode=returncode)

        tasks = [
            asyncio.create_task(_pipe(reader, process.stdin)),
            asyncio.create_task(_pipe(process.stdout, writer)),
            asyncio.create_task(read_bounded(process.stderr, 16384)),
            asyncio.create_task(monitor_executor()),
        ]
        async with asyncio.timeout(deadline):
            bridge = asyncio.gather(*tasks)
            if link is not None:
                stop = asyncio.create_task(link.stop_requested.wait())
                tasks.append(stop)
                done, _ = await asyncio.wait(
                    {bridge, stop}, return_when=asyncio.FIRST_COMPLETED
                )
                if bridge not in done:
                    return
            await bridge
            await process.wait()
        stderr, truncated = tasks[2].result()
        # Этот поток не содержит управляющее окружение и не попадает в протокол.
        if session:
            if stderr or truncated:
                LOG.warning(
                    "exec_server_stderr session_id=%s run_id=%s captured_bytes=%d truncated=%s",
                    owner_id,
                    run_id,
                    len(stderr),
                    truncated,
                )
        else:
            (manager.root / f"{run_id}.stderr").write_bytes(stderr)
            if truncated:
                (manager.root / f"{run_id}.stderr-truncated").write_text("true\n")
    except Exception as error:
        if connected and link is not None:
            timed_out = isinstance(error, TimeoutError)
            if timed_out or link.process.returncode != 0:
                await manager.mounted_transport_failure(link, timed_out=timed_out)
        elif not connected:
            if isinstance(error, ManagerError):
                status = error.http_status
            else:
                status = 502 if isinstance(error, DockerError) else 400
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
        if bridge is not None:
            await asyncio.gather(bridge, return_exceptions=True)
        if link is not None:
            writer.close()
            try:
                await manager.close_mounted_executor(link)
            except DockerError:
                LOG.warning(
                    "exec_server_stop_unconfirmed session_id=%s run_id=%s",
                    link.session_id,
                    link.run_id,
                )
        elif process:
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
