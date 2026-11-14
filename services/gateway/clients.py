"""Внутренние операции имеют сроки; командные POST не повторяются."""

import json

import aiohttp


class RemoteError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class Client:
    def __init__(self, url: str, token: str, project_id: str) -> None:
        self.url = url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token, "X-Project-ID": project_id}

    async def request(
        self, method: str, path: str, body: dict | None = None, timeout: float = 30
    ) -> dict:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.request(
                method, self.url + path, json=body, headers=self.headers
            ) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 3_000_000:
                        raise RemoteError(502, "response_too_large")
                if response.status >= 400:
                    # Не включать произвольное тело upstream в ошибку/журнал.
                    code = "remote_error"
                    try:
                        value = json.loads(raw)
                        candidate = value.get(
                            "error", value.get("code", value.get("reason"))
                        )
                        if candidate in {
                            "budget_exceeded",
                            "storage_budget_exceeded",
                            "busy",
                            "auth_error",
                        }:
                            code = candidate
                    except (ValueError, AttributeError):
                        pass
                    raise RemoteError(response.status, code)
                return json.loads(raw)
