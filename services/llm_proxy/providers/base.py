"""Явный реестр адаптеров: на входе и выходе — протокол харнесса Responses."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import aiohttp

if TYPE_CHECKING:
    from services.llm_proxy.profiles import Profile


class ProviderAuthError(RuntimeError):
    """Безопасный код ошибки без значения API-ключа."""


@dataclass(frozen=True)
class UpstreamRequest:
    url: str
    headers: dict[str, str] = field(repr=False)
    body: dict[str, Any]


class ProviderAdapter(Protocol):
    def prepare(
        self, profile: Profile, body: dict[str, Any], run_id: str, key: str | None
    ) -> UpstreamRequest: ...

    def events(self, response: aiohttp.ClientResponse) -> AsyncIterator[bytes]: ...


def api_key(profile: Profile, environ: Mapping[str, str]) -> str | None:
    if profile.auth == "none":
        return None
    name = profile.api_key_env
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,127}", name
    ):
        raise ProviderAuthError("invalid_api_key_env")
    key = environ.get(name, "")
    if not key:
        raise ProviderAuthError("api_key_missing")
    if len(key) > 16_384 or any(not 33 <= ord(c) <= 126 for c in key):
        raise ProviderAuthError("invalid_api_key")
    return key
