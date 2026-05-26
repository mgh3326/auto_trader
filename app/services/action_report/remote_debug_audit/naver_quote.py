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


# Naver Finance item/main DOM has shifted over time, so the extraction tries a
# small ordered list of selector variants and uses the first one with non-empty
# text. ``.no_today .blind`` / ``.wrap_company h2`` are the long-standing
# selectors and stay first; the rest are fallbacks for layout variants.
NAVER_PRICE_SELECTORS: tuple[str, ...] = (
    ".no_today .blind",
    ".no_today em .blind",
    "#_nowVal",
    "#chart_area .rate_info .no_today .blind",
)
NAVER_NAME_SELECTORS: tuple[str, ...] = (
    ".wrap_company h2 a",
    ".wrap_company h2",
    "#middle .h_company .wrap_company h2",
)

# In-page helper: first selector whose element has non-empty trimmed text.
_PICK_FN: str = (
    "function pick(sels){"
    "for(var i=0;i<sels.length;i++){"
    "var e=document.querySelector(sels[i]);"
    "if(e&&e.textContent&&e.textContent.trim())return e.textContent.trim();"
    "}return null;}"
)


def _selectors_json(selectors: tuple[str, ...]) -> str:
    return json.dumps(list(selectors))


# Returns a JSON string read back via Runtime.evaluate(returnByValue=true).
NAVER_EXTRACT_JS: str = (
    "(function(){"
    + _PICK_FN
    + "return JSON.stringify({"
    + "code:(new URLSearchParams(location.search)).get('code'),"
    + f"name:pick({_selectors_json(NAVER_NAME_SELECTORS)}),"
    + f"price_text:pick({_selectors_json(NAVER_PRICE_SELECTORS)})"
    + "});})()"
)

# Render gate for the CDP poll loop: true once any price selector has text.
NAVER_READY_JS: str = (
    "(function(){"
    + _PICK_FN
    + f"return pick({_selectors_json(NAVER_PRICE_SELECTORS)})!==null;"
    + "})()"
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
