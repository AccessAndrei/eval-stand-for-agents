"""Именованные профили: строгий mock и наблюдаемый целевой бюджет Luna."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MOCK_BUDGET = "mock-utf8-quarter-v1"
OBSERVED_BUDGET = "observed-usage-v1"
LUNA_ENDPOINT = "https://chatgpt.com/backend-api/codex"


class ProfileError(ValueError):
    """Профиль не позволяет безопасно начать попытку."""


def mock_input_tokens(body: dict[str, Any]) -> int:
    """Точное определение искусственных токенов mock; не токенизатор Luna."""
    payload = {key: value for key, value in body.items() if key != "max_output_tokens"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return (len(encoded) + 3) // 4


@dataclass(frozen=True)
class Profile:
    name: str
    model: str | None
    endpoint: str | None
    protocol: str
    auth: str
    timeout_s: float
    output_limit: int
    budget_mechanism: str | None
    enabled: bool = True
    budget_mode: str = "strict"
    # Чтение старых сохранённых снимков; новые профили не зависят от отчётов.
    output_limit_proof: str | None = None

    def compatible(self, *, live_ready: bool = False) -> None:
        """Проверить режим; live_ready подтверждает отдельно подключённую auth-границу."""
        if (
            type(self.enabled) is not bool
            or not self.enabled
            or not isinstance(self.model, str)
            or not self.model
            or len(self.model) > 128
            or not self.endpoint
        ):
            raise ProfileError("profile_incomplete")
        if not isinstance(self.endpoint, str):
            raise ProfileError("invalid_endpoint")
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ProfileError("invalid_endpoint")
        if self.protocol != "responses":
            raise ProfileError("unsupported_protocol")
        if (
            type(self.timeout_s) not in {int, float}
            or not math.isfinite(self.timeout_s)
            or not 0 < self.timeout_s <= 180
        ):
            raise ProfileError("invalid_timeout")
        if type(self.output_limit) is not int or not 1 <= self.output_limit <= 30_000:
            raise ProfileError("invalid_output_limit")
        if self.name == "luna" and self.budget_mode != "observed_usage":
            raise ProfileError("observed_profile_unverified")
        if self.budget_mode == "strict":
            if self.budget_mechanism != MOCK_BUDGET or self.auth != "none":
                raise ProfileError("token_bound_unverified")
        elif self.budget_mode == "observed_usage":
            if (
                self.name != "luna"
                or self.model != "gpt-6-luna"
                or self.endpoint != LUNA_ENDPOINT
                or self.auth != "chatgpt"
                or self.output_limit != 2048
                or self.budget_mechanism != OBSERVED_BUDGET
            ):
                raise ProfileError("observed_profile_unverified")
            if live_ready is not True:
                raise ProfileError("live_authorization_not_ready")
        else:
            raise ProfileError("invalid_budget_mode")

    def public(self) -> dict[str, Any]:
        return {
            "provider_profile": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "protocol": self.protocol,
            "auth": self.auth,
            "timeout_s": self.timeout_s,
            "token_limit": 30_000,
            "budget_mode": self.budget_mode,
            "output_limit": self.output_limit,
            "live_authorization_required": self.auth == "chatgpt",
            "budget_mechanism": self.budget_mechanism,
        }

    def snapshot(self) -> dict[str, Any]:
        """Только неизменяемые параметры; credentials в Profile не хранятся."""
        snapshot = asdict(self)
        snapshot.pop("output_limit_proof")
        return snapshot


def load_profiles(path: Path) -> dict[str, Profile]:
    values = json.loads(path.read_text(encoding="utf-8"))
    return {name: Profile(name=name, **value) for name, value in values.items()}
