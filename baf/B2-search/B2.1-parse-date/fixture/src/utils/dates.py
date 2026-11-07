"""Разбор календарной даты."""

from datetime import date


def parse_date(value: str) -> date:
    return date.fromisoformat(value)
