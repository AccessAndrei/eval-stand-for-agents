"""Точка входа загрузчика."""

from utils.dates import parse_date


def load_date(value: str) -> str:
    return parse_date(value).isoformat()
