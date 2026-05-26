"""Naver finance per-symbol quote: URL, in-page extraction JS, and parser.

The JS returns a JSON string (``{code, name, price_text}``); all parsing
(comma-strip, int coercion, shape validation) happens here in Python so it is
unit-testable without a browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NaverQuote:
    code: str
    name: str | None
    price: float | None


def naver_url(code: str) -> str:
    return f"https://finance.naver.com/item/main.naver?code={code}"


# Returns a JSON string read back via Runtime.evaluate(returnByValue=true).
# Selectors: current price lives in ``.no_today .blind``; company name in
# ``.wrap_company h2``. Both are stable on the item/main page.
NAVER_EXTRACT_JS: str = (
    "(function(){"
    "function t(s){var e=document.querySelector(s);"
    "return e?e.textContent.trim():null;}"
    "return JSON.stringify({"
    "code:(new URLSearchParams(location.search)).get('code'),"
    "name:t('.wrap_company h2'),"
    "price_text:t('.no_today .blind')"
    "});"
    "})()"
)


def _to_price(price_text: Any) -> float | None:
    if not isinstance(price_text, str):
        return None
    cleaned = price_text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_naver_quote(raw: Any) -> NaverQuote | None:
    """Parse the JS result (JSON string or dict) into a ``NaverQuote``.

    Returns ``None`` for unusable input (not a JSON object / missing code).
    """
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if not isinstance(code, str) or not code:
        return None
    name = data.get("name")
    return NaverQuote(
        code=code,
        name=name if isinstance(name, str) else None,
        price=_to_price(data.get("price_text")),
    )
