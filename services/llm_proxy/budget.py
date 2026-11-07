"""Строгий резерв mock и согласованная остановка Luna между запросами."""

from __future__ import annotations

import json
import re
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Any

from services.common.storage import atomic_json


class BudgetError(RuntimeError):
    """Превышение бюджета либо неопределённый расход."""


class Ledger:
    """Один последовательный владелец; незавершённый резерв не освобождается."""

    def __init__(
        self,
        path: Path,
        profile: str,
        token: str | None = None,
        profile_snapshot: dict[str, Any] | None = None,
        *,
        mode: str | None = None,
        output_limit: int | None = None,
    ) -> None:
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if self.data["profile"] != profile:
                raise ValueError("run_conflict")
            self.data.setdefault("mode", "strict")
            previous = self.data.get("profile_snapshot") or {}
            self.data.setdefault("output_limit", previous.get("output_limit", 30_000))
            if (mode is not None and mode != self.data["mode"]) or (
                output_limit is not None and output_limit != self.data["output_limit"]
            ):
                raise ValueError("run_conflict")
        else:
            snapshot = profile_snapshot or {}
            mode = snapshot.get("budget_mode", "strict") if mode is None else mode
            output_limit = (
                output_limit
                if output_limit is not None
                else snapshot.get(
                    "output_limit", 2048 if mode == "observed_usage" else 30_000
                )
            )
            if not isinstance(mode, str) or mode not in {"strict", "observed_usage"}:
                raise ValueError("invalid_budget_mode")
            if type(output_limit) is not int or not 1 <= output_limit <= 30_000:
                raise ValueError("invalid_output_limit")
            self.data = {
                "profile": profile,
                "mode": mode,
                "output_limit": output_limit,
                "token": token or secrets.token_urlsafe(32),
                "input": 0,
                "output": 0,
                "cached": 0,
                "reservations": {},
                "closed": False,
                "error": None,
                "requests": 0,
                "profile_snapshot": deepcopy(profile_snapshot),
            }
            self.save()

    def save(self) -> None:
        atomic_json(self.path, self.data)
        self.path.chmod(0o600)

    def reserve(self, input_bound: int, output_bound: int) -> str:
        if self.data["mode"] != "strict":
            raise ValueError("observed_usage_requires_reserve_observed")
        if type(input_bound) is not int or input_bound < 0:
            raise ValueError("invalid_input_bound")
        self._check_output(output_bound)
        self.check_open()
        held = self._held()
        spent = self.data["input"] + self.data["output"]
        if input_bound + output_bound + held + spent > 30_000:
            self.fail("budget_exceeded")
            raise BudgetError("budget_exceeded")
        return self._reserve(input_bound, output_bound)

    def check_open(self) -> None:
        """Отклонить завершённую попытку до любых внешних действий."""
        if self.data["closed"] or self.data["error"]:
            raise BudgetError(self.data["error"] or "run_closed")

    def _check_output(self, bound: int) -> None:
        if type(bound) is not int or not 1 <= bound <= self.data["output_limit"]:
            raise ValueError("invalid_output_bound")

    def reserve_observed(self, output_bound: int | None = None) -> str:
        """Разрешить следующий запрос по уже известному usage, не оценивать вход.

        None во входной позиции резерва означает отсутствие верхней границы.
        Последний запрос может превысить целевые 30 000; следующий не уйдёт.
        """
        if self.data["mode"] != "observed_usage":
            raise ValueError("strict_budget_requires_input_bound")
        self.check_open()
        output_bound = (
            self.data["output_limit"] if output_bound is None else output_bound
        )
        self._check_output(output_bound)
        if self.data["reservations"]:
            self.fail("upstream_usage_unconfirmed")
            raise BudgetError("upstream_usage_unconfirmed")
        if self.data["input"] + self.data["output"] >= 30_000:
            self.fail("budget_exceeded")
            raise BudgetError("budget_exceeded")
        return self._reserve(None, output_bound)

    def _reserve(self, input_bound: int | None, output_bound: int) -> str:
        request_id = secrets.token_hex(12)
        self.data["reservations"][request_id] = [input_bound, output_bound]
        self.data["requests"] += 1
        self.save()
        return request_id

    def settle(self, request_id: str, usage: dict[str, Any]) -> None:
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        details = usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            self.fail("provider_budget_violation")
            raise BudgetError("provider_budget_violation")
        cached = details.get("cached_tokens", 0)
        bounds = self.data["reservations"][request_id]
        if (
            any(type(x) is not int or x < 0 for x in (incoming, outgoing, cached))
            or cached > incoming
        ):
            self.fail("provider_budget_violation")
            raise BudgetError("provider_budget_violation")
        violation = outgoing > bounds[1]
        if self.data["mode"] == "strict" and (incoming > bounds[0] or violation):
            self.fail("provider_budget_violation")
            raise BudgetError("provider_budget_violation")
        self.data["input"] += incoming
        self.data["output"] += outgoing
        self.data["cached"] += cached
        del self.data["reservations"][request_id]
        self.save()
        if violation:
            # Известный расход сохраняется даже при нарушенном провайдером cap.
            self.fail("provider_budget_violation")
            raise BudgetError("provider_budget_violation")

    def fail(self, code: str) -> None:
        self.data["error"] = self.data["error"] or code
        self.save()

    def close(self) -> None:
        self.data["closed"] = True
        self.save()

    def tokens(self) -> dict[str, Any]:
        held = self._held()
        unknown = bool(self.data["reservations"])
        actual = None if unknown else self.data["input"] + self.data["output"]
        return {
            "limit": 30_000,
            "mode": self.data["mode"],
            "output_limit": self.data["output_limit"],
            "over_target": None if actual is None else actual > 30_000,
            "actual": actual,
            "input": self.data["input"],
            "output": self.data["output"],
            "cached": self.data["cached"],
            "reserved": held,
            "unknown_reason": "upstream_usage_unconfirmed" if unknown else None,
        }

    def _held(self) -> int:
        # В observed_usage это лишь pending output cap, не общий резерв токенов.
        return sum(
            sum(bound or 0 for bound in bounds)
            for bounds in self.data["reservations"].values()
        )


def run_path(state_dir: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid_run_id")
    return state_dir / f"{run_id}.json"
