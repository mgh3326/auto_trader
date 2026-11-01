"""Parser for the public NASDAQ Trader symbol directory files.

Two files make up the US exchange-listed universe:

* ``nasdaqlisted.txt`` — every Nasdaq-listed security;
* ``otherlisted.txt``  — every security listed on NYSE, NYSE American,
  NYSE Arca, Cboe BZX and IEX.

Their column sets changed over the years (``nasdaqlisted`` gained ``ETF`` and
``NextShares``; ``otherlisted`` renamed ``Symbol`` to ``ACT Symbol`` and gained
``NASDAQ Symbol``), so parsing is driven by the header row, never by position.

A file is accepted only when it is *provably complete*: a known header, the
``File Creation Time`` footer, and at least ``MIN_ROWS`` data rows. A truncated
or error-page body would otherwise read as "thousands of symbols delisted on
this date", which is exactly the kind of fabricated evidence this package
exists to prevent. Rejection raises ``SymbolDirectoryError``; callers record it
as a coverage gap.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from datetime import date, datetime

from app.core.symbol import to_db_symbol

SERIES_NASDAQ = "nasdaqlisted"
SERIES_OTHER = "otherlisted"
SERIES = (SERIES_NASDAQ, SERIES_OTHER)

SOURCE_URLS = {
    SERIES_NASDAQ: "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    SERIES_OTHER: "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}

# The smallest archived file (2008) still carries ~2,500 rows per series; a
# body under this is a partial or placeholder response, not a market event.
MIN_ROWS = 1000

_FOOTER_RE = re.compile(r"^File Creation Time: (\d{2})(\d{2})(\d{4})(\d{2}):(\d{2})")

# otherlisted ``Exchange`` codes (NASDAQ Trader symbol directory definitions).
OTHER_EXCHANGE_CODES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}

# Common-stock heuristic, kept byte-identical to
# ``app.services.us_common_stock_classifier`` (a test pins the equality) so the
# "likely common stock" subset here means the same thing as the screener's
# ``is_common_stock`` flag. Duplicated rather than imported because that module
# imports the database layer at import time.
EXCLUDED_SYMBOL_MARKERS = ("$", "+", "=", "^", "/", " ")
EXCLUDED_NAME_MARKERS = (
    " ETF",
    " ETN",
    " FUND",
    " TRUST",
    " WARRANT",
    " WTS",
    " RIGHT",
    " UNIT",
    " PREFERRED",
    " PREFERENCE",
    " PFD",
    " ADR",
    " DEPOSITARY",
    " DEPOSITORY",
    " NOTES DUE",
    " BOND",
)


class SymbolDirectoryError(ValueError):
    """The body is not a complete, recognisable symbol directory file."""


@dataclass(frozen=True)
class ListingRow:
    symbol: str  # DB dot-format (app.core.symbol.to_db_symbol)
    raw_symbol: str
    security_name: str
    exchange: str
    etf: bool | None  # None: column absent in this file vintage
    likely_common_stock: bool


@dataclass(frozen=True)
class Observation:
    """One complete directory file: the listed set of one series on one date."""

    series: str
    as_of: date  # from the file's own ``File Creation Time`` footer
    created_at: str  # footer timestamp, ISO ``YYYY-MM-DDTHH:MM``
    rows: tuple[ListingRow, ...]


def decode_body(raw: bytes) -> str:
    """Decode a fetched body. Some archived captures are stored gzip-encoded."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="strict")


def _flag(value: str) -> bool | None:
    v = value.strip().upper()
    if v == "Y":
        return True
    if v == "N":
        return False
    return None


def looks_like_common_stock(
    raw_symbol: str, name: str, *, etf: bool | None, test_issue: bool
) -> bool:
    normalized = (raw_symbol or "").strip().upper().replace(".", "-")
    upper_name = f" {(name or '').upper()} "
    if not normalized or etf is True or test_issue:
        return False
    if any(marker in normalized for marker in EXCLUDED_SYMBOL_MARKERS):
        return False
    if any(marker in upper_name for marker in EXCLUDED_NAME_MARKERS):
        return False
    return True


def _parse_footer(line: str) -> tuple[date, str]:
    match = _FOOTER_RE.match(line)
    if match is None:
        raise SymbolDirectoryError("missing File Creation Time footer (truncated?)")
    mm, dd, yyyy, hh, mi = match.groups()
    try:
        created = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi))
    except ValueError as exc:
        raise SymbolDirectoryError(f"unparseable footer timestamp: {line!r}") from exc
    return created.date(), created.strftime("%Y-%m-%dT%H:%M")


def parse_symbol_directory(series: str, text: str) -> Observation:
    """Parse one ``nasdaqlisted``/``otherlisted`` body into an Observation."""
    if series not in SERIES:
        raise ValueError(f"unknown series {series!r}")
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        raise SymbolDirectoryError("body too short")

    header = lines[0].split("|")
    as_of, created_at = _parse_footer(lines[-1])
    col = {name.strip(): i for i, name in enumerate(header)}

    if series == SERIES_NASDAQ:
        symbol_col = "Symbol"
        required = ("Symbol", "Security Name", "Market Category", "Test Issue")
    else:
        symbol_col = "ACT Symbol" if "ACT Symbol" in col else "Symbol"
        required = (symbol_col, "Security Name", "Exchange", "ETF", "Test Issue")
    missing = [c for c in required if c not in col]
    if missing:
        raise SymbolDirectoryError(f"{series}: header lacks {missing}: {lines[0]!r}")

    rows: dict[str, ListingRow] = {}
    seen_lines: dict[str, str] = {}
    for line in lines[1:-1]:
        parts = line.split("|")
        if len(parts) < len(header):
            raise SymbolDirectoryError(f"{series}: short row {line!r}")
        raw_symbol = parts[col[symbol_col]].strip().upper()
        if not raw_symbol:
            continue
        test_issue = _flag(parts[col["Test Issue"]])
        if test_issue is None:
            # Uncertain evidence is not guessed: reject the file, record a gap.
            raise SymbolDirectoryError(f"{series}: Test Issue not Y/N in {line!r}")
        if test_issue:
            continue  # NASDAQ test symbols (ZVZZT, ...) are never tradable listings
        name = parts[col["Security Name"]].strip()
        etf = _flag(parts[col["ETF"]]) if "ETF" in col else None
        if series == SERIES_NASDAQ:
            exchange = "NASDAQ"
        else:
            code = parts[col["Exchange"]].strip().upper()
            exchange = OTHER_EXCHANGE_CODES.get(code, f"UNKNOWN:{code}")
        symbol = to_db_symbol(raw_symbol)
        if symbol in seen_lines:
            if seen_lines[symbol] != line:
                raise SymbolDirectoryError(
                    f"{series}: conflicting rows for {symbol!r}: "
                    f"{seen_lines[symbol]!r} vs {line!r}"
                )
            continue  # byte-identical duplicate line carries no new evidence
        seen_lines[symbol] = line
        rows[symbol] = ListingRow(
            symbol=symbol,
            raw_symbol=raw_symbol,
            security_name=name,
            exchange=exchange,
            etf=etf,
            likely_common_stock=looks_like_common_stock(
                raw_symbol, name, etf=etf, test_issue=False
            ),
        )

    if len(rows) < MIN_ROWS:
        raise SymbolDirectoryError(
            f"{series}: only {len(rows)} rows (< {MIN_ROWS}); refusing partial file"
        )
    ordered = tuple(rows[s] for s in sorted(rows))
    return Observation(series=series, as_of=as_of, created_at=created_at, rows=ordered)
