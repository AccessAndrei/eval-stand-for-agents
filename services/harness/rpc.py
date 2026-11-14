"""Один JSON-RPC канал Codex без повторной отправки запросов."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_event: Callable[[dict], Awaitable[None]],
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.on_event = on_event
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0

    async def send(self, message: dict) -> None:
        self.writer.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        await self.writer.drain()

    async def call(self, method: str, params: dict, timeout: float = 30) -> Any:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def read(self) -> None:
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if "method" in message:
                    if "id" in message:
                        # Нет интерактивных разрешений или динамических инструментов.
                        await self.send(
                            {
                                "id": message["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Unsupported request",
                                },
                            }
                        )
                    else:
                        await self.on_event(message)
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RpcError("Codex отклонил запрос"))
                        else:
                            future.set_result(message.get("result"))
            raise RpcError("Канал Codex закрыт")
        except (OSError, ValueError, RpcError) as error:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RpcError("Канал Codex потерян"))
            raise RpcError("Канал Codex потерян") from error
