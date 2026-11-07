"""Ограниченное наблюдение SSE без изменения пересылаемых байтов."""

from __future__ import annotations

import json
from typing import Any


class SSEObserver:
    def __init__(self, max_event_bytes: int = 1_048_576) -> None:
        self.buffer = b""
        self.limit = max_event_bytes
        self.terminal: str | None = None
        self.usage: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while True:
            candidates = [
                (self.buffer.find(separator), separator)
                for separator in (b"\n\n", b"\r\n\r\n")
                if separator in self.buffer
            ]
            if not candidates:
                if len(self.buffer) > self.limit:
                    raise ValueError("sse_event_too_large")
                return
            position, separator = min(candidates, key=lambda value: value[0])
            if position > self.limit:
                raise ValueError("sse_event_too_large")
            frame, self.buffer = (
                self.buffer[:position],
                self.buffer[position + len(separator) :],
            )
            self._event(frame)

    def _event(self, frame: bytes) -> None:
        lines = frame.decode("utf-8").splitlines()
        data = "\n".join(
            line[5:].lstrip(" ") for line in lines if line.startswith("data:")
        )
        if not data or data == "[DONE]":
            return
        event = json.loads(data)
        if not isinstance(event, dict):
            raise ValueError("invalid_sse_event")
        kind = event.get("type")
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            if self.terminal is not None:
                raise ValueError("duplicate_terminal_event")
            self.terminal = kind
            response = event.get("response", {})
            usage = response.get("usage") if isinstance(response, dict) else None
            self.usage = usage if isinstance(usage, dict) else None
