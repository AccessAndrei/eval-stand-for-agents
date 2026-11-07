"""Ограниченный HTTP-клиент gateway без повторов и переадресации токена."""

import asyncio
import json
import math
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp


class GatewayError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TransportError(RuntimeError):
    """Ответ не подтверждён; POST автоматически не повторяется."""


class Transport(Protocol):
    def request(self, method: str, path: str, payload: dict | None = None) -> dict: ...


class GatewayClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 10) -> None:
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Нужен адрес gateway без credentials и пути")
        if len(token) < 24 or any(character.isspace() for character in token):
            raise ValueError("CLIENT_TOKEN отсутствует либо недопустим")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Таймаут должен быть конечным положительным числом")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout_s = timeout_s

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Синхронный интерфейс для последовательного CLI-раннера."""
        return asyncio.run(self._request(method, path, payload))

    async def _request(self, method: str, path: str, payload: dict | None) -> dict:
        # total ограничивает весь обмен, включая весь поток тела: приход нового
        # байта не продлевает срок. Округление длинных таймаутов отключено.
        timeout = aiohttp.ClientTimeout(total=self.timeout_s, ceil_threshold=math.inf)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=False
            ) as session:
                async with session.request(
                    method,
                    self.base_url + path,
                    json=payload,
                    headers={"Authorization": "Bearer " + self._token},
                    allow_redirects=False,
                ) as response:
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65_536):
                        if len(body) + len(chunk) > 4_000_000:
                            raise TransportError("response_too_large")
                        body.extend(chunk)
            try:
                value = json.loads(body)
            except ValueError:
                self._check_status(response.status)
                raise
            self._check_status(response.status, value)
            if not isinstance(value, dict):
                raise TransportError("invalid_response")
            return value
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as error:
            raise TransportError("gateway_transport_error") from error

    @staticmethod
    def _check_status(status: int, payload: object = None) -> None:
        if 200 <= status < 300:
            return
        codes = {
            401: "unauthorized",
            404: "not_found",
            409: "busy",
            507: "storage_budget_exceeded",
        }
        if isinstance(payload, dict) and payload.get("error") in {
            "auth_error",
            "profile_incompatible",
            "storage_budget_exceeded",
            "busy",
            "conflict",
        }:
            raise GatewayError(payload["error"])
        raise GatewayError(codes.get(status, "gateway_rejected"))
