"""Ограниченный вывод и очистка групп дочерних процессов POSIX."""

import asyncio
import os
import signal
import time
from asyncio.subprocess import Process


async def read_bounded(reader: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks = bytearray()
    truncated = False
    while chunk := await reader.read(65536):
        room = max(0, limit - len(chunks))
        chunks.extend(chunk[:room])
        truncated |= len(chunk) > room
    return bytes(chunks), truncated


def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


async def stop_group(process: Process, timeout: float = 5) -> bool:
    """Смерть родителя не заменяет проверку оставшейся группы."""
    pgid = process.pid
    started = time.monotonic()
    for sig, wait in ((signal.SIGTERM, min(timeout / 3, 1)), (signal.SIGKILL, timeout)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            await process.wait()
            return True
        deadline = min(started + timeout, time.monotonic() + wait)
        while time.monotonic() < deadline:
            if not group_exists(pgid):
                await process.wait()
                return True
            await asyncio.sleep(0.03)
    return not group_exists(pgid)


async def session_members(session_id: int) -> list[int]:
    """Relay Codex меняет PGID, но остаётся в сессии app-server."""
    if os.path.isdir("/proc"):
        candidates = [int(name) for name in os.listdir("/proc") if name.isdecimal()]
    else:
        query = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-axo",
            "pid=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(3):
            output, _ = await query.communicate()
        if query.returncode != 0:
            raise OSError("Нельзя проверить процессы сессии")
        candidates = [int(value) for value in output.split() if value.isdigit()]
    members = []
    for pid in candidates:
        try:
            if os.getsid(pid) == session_id:
                members.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
    return members


async def stop_session(process: Process, timeout: float = 5) -> bool:
    """Останавливает и проверяет также отдельную группу штатного relay."""
    session_id = process.pid  # Процесс создан с start_new_session=True.
    deadline = time.monotonic() + timeout
    for sig, grace in (
        (signal.SIGTERM, min(timeout / 3, 1)),
        (signal.SIGKILL, timeout),
    ):
        until = min(deadline, time.monotonic() + grace)
        while time.monotonic() < until:
            try:
                members = await session_members(session_id)
            except (OSError, asyncio.TimeoutError):
                await stop_group(process, max(0.1, deadline - time.monotonic()))
                return False
            if not members:
                await process.wait()
                return True
            for pid in members:
                try:
                    if os.getsid(pid) == session_id:
                        os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            await asyncio.sleep(0.03)
    try:
        return not await session_members(session_id)
    except (OSError, asyncio.TimeoutError):
        return False
