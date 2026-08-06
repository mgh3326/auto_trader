"""Canonical serialization and Decimal helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any

from research.kr_corpus.d3_engine.constants import (
    DECIMAL_PRECISION,
    DECIMAL_ROUNDING,
)


def decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def fixed(value: Decimal, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        return format(value.quantize(quantum), "f")


def plain(value: Decimal) -> str:
    """Emit fixed notation without changing contract-significant trailing zeros."""

    return format(value, "f")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return plain(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    payload = json.dumps(
        _jsonable(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (payload + "\n").encode("utf-8")
