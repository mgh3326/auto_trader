"""Canonical validation shared by Kiwoom mock request boundaries."""

from __future__ import annotations

import re
from typing import Any

_KRX_SYMBOL_RE = re.compile(r"[0-9]{6}", re.ASCII)


def normalize_krx_symbol(value: Any) -> str:
    """Return a canonical six-digit KRX symbol or fail closed."""

    candidate = str(value).strip()
    if _KRX_SYMBOL_RE.fullmatch(candidate) is None:
        raise ValueError(
            f"Kiwoom KRX symbol must be exactly 6 ASCII digits; got {value!r}"
        )
    return candidate
