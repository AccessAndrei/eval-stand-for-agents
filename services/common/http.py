"""HTTP-граница: токен обязателен, диагностические тела не раскрывают запрос."""

import secrets
from collections.abc import Awaitable, Callable

from aiohttp import web

from contracts import ContractError


def valid_bearer(supplied: str, token: str) -> bool:
    if not token:
        return False
    try:
        return secrets.compare_digest(
            supplied.encode("utf-8"), ("Bearer " + token).encode("utf-8")
        )
    except UnicodeError:
        return False


def authenticated(token: str) -> Callable:
    if not token or len(token) < 24:
        raise ValueError("Требуется отдельный токен длиной не менее 24 символов")

    @web.middleware
    async def middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        supplied = request.headers.get("Authorization", "")
        if not valid_bearer(supplied, token):
            raise web.HTTPUnauthorized()
        try:
            return await handler(request)
        except ContractError as error:
            raise web.HTTPBadRequest(text="Нарушение контракта v0") from error
        except (KeyError, ValueError) as error:
            raise web.HTTPBadRequest(text="Некорректный запрос") from error

    return middleware
