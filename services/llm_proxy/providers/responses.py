"""OpenAI-compatible Responses endpoint; другие протоколы добавляются модулями."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import aiohttp

from services.llm_proxy.providers.base import UpstreamRequest

if TYPE_CHECKING:
    from services.llm_proxy.profiles import Profile


class ResponsesAdapter:
    def prepare(
        self, profile: Profile, body: dict[str, Any], run_id: str, key: str | None
    ) -> UpstreamRequest:
        headers = {"X-Eval-Run-Id": run_id}
        if key is not None:
            headers["Authorization"] = "Bearer " + key
        return UpstreamRequest(
            url=profile.endpoint.rstrip("/") + "/responses",
            headers=headers,
            body=body,
        )

    async def events(self, response: aiohttp.ClientResponse) -> AsyncIterator[bytes]:
        # Будущий адаптер преобразует свой upstream в эти canonical Responses events.
        async for chunk in response.content.iter_chunked(16_384):
            yield chunk
