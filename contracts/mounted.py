"""Проверка сообщений версионированного контракта смонтированной среды."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .validation import ContractError


@lru_cache(maxsize=16)
def mounted_validator(kind: str) -> Draft202012Validator:
    document = json.loads(Path(__file__).with_name("mounted.schema.json").read_text())
    if kind not in document["$defs"]:
        raise ContractError("Неизвестный вид сообщения v1")
    return Draft202012Validator({**document, "$ref": f"#/$defs/{kind}"})


def validate_mounted(kind: str, data: Any) -> None:
    error = next(mounted_validator(kind).iter_errors(data), None)
    if error is not None:
        raise ContractError(f"Нарушение схемы v1: {error.validator}")
