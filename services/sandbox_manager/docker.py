"""Ограниченный запуск Docker CLI; произвольные команды наружу не экспортируются."""

import asyncio
import json
import os
from asyncio.subprocess import Process
from dataclasses import dataclass

from services.common.processes import read_bounded, stop_group


class DockerError(RuntimeError):
    """Операция Docker не подтверждена, включая неоднозначный таймаут."""


@dataclass(frozen=True)
class DockerReply:
    stdout: bytes
    stderr: bytes
    code: int


class Docker:
    def __init__(self, executable: str = "docker", timeout: float = 20) -> None:
        self.executable = executable
        self.timeout = timeout

    async def spawn(self, args: list[str]) -> Process:
        return await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")},
        )

    async def command(
        self,
        args: list[str],
        data: bytes = b"",
        limit: int = 2_000_000,
        timeout: float | None = None,
        *,
        check: bool = True,
    ) -> DockerReply:
        process = await self.spawn(args)
        assert process.stdin and process.stdout and process.stderr
        readers = [
            asyncio.create_task(read_bounded(process.stdout, limit)),
            asyncio.create_task(read_bounded(process.stderr, 16384)),
        ]
        try:
            async with asyncio.timeout(timeout or self.timeout):
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
                stdout, stderr = await asyncio.gather(*readers)
                code = await process.wait()
            if stdout[1] or (check and code != 0):
                raise DockerError(
                    "docker_output_limit" if stdout[1] else "docker_failed"
                )
            return DockerReply(stdout[0], stderr[0], code)
        except (TimeoutError, OSError, BrokenPipeError) as exc:
            raise DockerError("docker_transport_or_timeout") from exc
        finally:
            await stop_group(process, timeout=3)
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    async def inspect(self, kind: str, name: str) -> dict | None:
        reply = await self.command([kind, "inspect", name], check=False)
        if reply.code != 0:
            # Недоступность daemon не равна отсутствию ресурса.
            if any(
                marker in reply.stderr.lower()
                for marker in (
                    b"no such container",
                    b"no such volume",
                    b"no such image",
                )
            ):
                return None
            raise DockerError("docker_inspect_failed")
        parsed = json.loads(reply.stdout)
        if not isinstance(parsed, list) or len(parsed) != 1:
            raise DockerError("docker_inspect_invalid")
        return parsed[0]
