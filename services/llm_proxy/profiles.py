"""Именованные профили: строгий mock и наблюдаемый целевой бюджет API-провайдера."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.llm_proxy.providers import ADAPTERS

MOCK_BUDGET = "mock-utf8-quarter-v1"
OBSERVED_BUDGET = "observed-usage-v1"


class ProfileError(ValueError):
    """Профиль не позволяет безопасно начать попытку."""


def mock_input_tokens(body: dict[str, Any]) -> int:
    """Точное определение искусственных токенов mock; не токенизатор реальной модели."""
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
    api_key_env: str | None = None

    def compatible(self) -> None:
        """Проверить статическую конфигурацию; ключ читает только proxy."""
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
        if len(self.endpoint) > 2048 or any(
            c.isspace() or ord(c) < 32 for c in self.endpoint
        ):
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
        if self.auth == "api_key" and url.scheme != "https":
            try:
                loopback = ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = url.hostname == "localhost"
            if not loopback:
                raise ProfileError("https_required_for_api_key")
        if self.protocol not in ADAPTERS:
            raise ProfileError("unsupported_protocol")
        if (
            type(self.timeout_s) not in {int, float}
            or not math.isfinite(self.timeout_s)
            or not 0 < self.timeout_s <= 180
        ):
            raise ProfileError("invalid_timeout")
        if type(self.output_limit) is not int or not 1 <= self.output_limit <= 30_000:
            raise ProfileError("invalid_output_limit")
        if self.budget_mode == "strict":
            if self.budget_mechanism != MOCK_BUDGET or self.auth != "none":
                raise ProfileError("token_bound_unverified")
        elif self.budget_mode == "observed_usage":
            if self.auth != "api_key" or self.budget_mechanism != OBSERVED_BUDGET:
                raise ProfileError("observed_profile_unverified")
            if not isinstance(self.api_key_env, str) or not self.api_key_env:
                raise ProfileError("api_key_env_required")
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
            "api_key_required": self.auth == "api_key",
            "budget_mechanism": self.budget_mechanism,
        }

    def snapshot(self) -> dict[str, Any]:
        """Только неизменяемые параметры; credentials в Profile не хранятся."""
        snapshot = asdict(self)
        snapshot.pop("output_limit_proof")
        return snapshot


def load_profiles(
    path: Path, environ: Mapping[str, str] | None = None
) -> dict[str, Profile]:
    environ = os.environ if environ is None else environ
    values = json.loads(path.read_text(encoding="utf-8"))
    profiles = {}
    for name, config in values.items():
        value = dict(config)
        for field in ("model", "endpoint"):
            variable = value.pop(field + "_env", None)
            if variable is not None:
                value[field] = environ.get(variable) or value.get(field)
        profiles[name] = Profile(name=name, **value)
    return profiles
