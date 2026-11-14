"""Добавление нового протокола требует модуля адаптера и записи в этом реестре."""

from services.llm_proxy.providers.base import (
    ProviderAdapter,
    ProviderAuthError,
    api_key,
)
from services.llm_proxy.providers.responses import ResponsesAdapter

ADAPTERS: dict[str, ProviderAdapter] = {"responses": ResponsesAdapter()}

__all__ = ["ADAPTERS", "ProviderAuthError", "api_key"]
