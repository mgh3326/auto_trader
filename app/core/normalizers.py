"""Shared value-normalization helpers used across service modules.

Each function is a pure utility with no side effects. Only helpers with
identical implementations in 2+ modules are extracted here.
"""

from __future__ import annotations

from typing import Any


def to_float(value: Any, *, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* on None, empty-string, or error.

    The keyword-only *default* parameter prevents accidental positional misuse.

    >>> to_float("3.14")
    3.14
    >>> to_float(None)
    0.0
    >>> to_float("", default=-1.0)
    -1.0
    >>> to_float("bad")
    0.0
    """
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
