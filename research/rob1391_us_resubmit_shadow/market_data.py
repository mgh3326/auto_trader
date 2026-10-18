"""Public market data (Alpaca ``data.alpaca.markets`` only, GET only).

The host gate is the one the US intraday corpus already uses
(``research.us_intraday_corpus.alpaca_data.assert_data_host``): it runs before
every request, rejects the trading/broker hosts by name and anything that is
not the pinned data host. There is no order, account or position endpoint here.

Every accessor returns an explicit status. ``unknown`` means "the data needed
for this decision is missing or truncated" and is never turned into zero, an
empty fill or a pass by any caller.

🔴 Feed: the feed (``sip`` or ``iex``) is sealed into the manifest. IEX quotes
are a single venue's book, not the NBBO, and IEX minute bars carry only IEX
volume; an IEX epoch therefore understates both ask validity and the 1 %
volume cap. SIP is the sealed default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from research.us_intraday_corpus.alpaca_data import (
    ForbiddenHostError,
    assert_data_host,
    load_credentials,
)

DATA_HOST = "data.alpaca.markets"
BARS_URL = f"https://{DATA_HOST}/v2/stocks/bars"
QUOTES_URL = f"https://{DATA_HOST}/v2/stocks/quotes"
FEEDS = ("sip", "iex")
MAX_PAGES_PER_WINDOW = 5
PAGE_LIMIT = 10_000

OK = "ok"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Bar:
    start_utc: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class Quote:
    at_utc: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal

    @property
    def spread_bp(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * Decimal(10_000)


@dataclass(frozen=True, slots=True)
class BarsResult:
    status: str
    bars: tuple[Bar, ...] = ()
    reason: str | None = None
    digests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuoteResult:
    status: str
    quote: Quote | None = None  # None with status ok == none observed (complete)
    reason: str | None = None
    digests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AskEvidence:
    """Whether a valid ask <= limit was observed inside a window.

    ``status == ok`` with ``found is False`` is positive evidence that the full
    (non-truncated) quote stream in the window never offered at/below limit.
    """

    status: str
    found: bool = False
    at_utc: datetime | None = None
    ask: Decimal | None = None
    reason: str | None = None
    digests: tuple[str, ...] = ()


class MarketData(Protocol):
    feed: str

    def daily_bars(
        self, symbol: str, start: date, end: date, *, adjustment: str
    ) -> BarsResult: ...

    def minute_bars(
        self, symbol: str, start_utc: datetime, end_utc: datetime
    ) -> BarsResult: ...

    def first_valid_quote(
        self, symbol: str, start_utc: datetime, end_utc: datetime
    ) -> QuoteResult: ...

    def ask_at_or_below(
        self, symbol: str, start_utc: datetime, end_utc: datetime, limit: Decimal
    ) -> AskEvidence: ...


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except Exception:
        return None
    return out if out.is_finite() else None


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        out = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return out.astimezone(UTC) if out.tzinfo else None


def parse_bar(raw: dict[str, Any]) -> Bar | None:
    start = _parse_ts(raw.get("t"))
    if start is None:
        return None
    return Bar(
        start_utc=start,
        open=_d(raw.get("o")),
        high=_d(raw.get("h")),
        low=_d(raw.get("l")),
        close=_d(raw.get("c")),
        volume=_d(raw.get("v")),
        vwap=_d(raw.get("vw")),
    )


def parse_valid_quote(raw: dict[str, Any]) -> Quote | None:
    """A quote is valid iff timestamped, both sides positive and not crossed."""

    at = _parse_ts(raw.get("t"))
    bid, ask = _d(raw.get("bp")), _d(raw.get("ap"))
    bid_size, ask_size = _d(raw.get("bs")), _d(raw.get("as"))
    if at is None or bid is None or ask is None or bid_size is None or ask_size is None:
        return None
    if bid <= 0 or ask <= 0 or bid_size <= 0 or ask_size <= 0 or bid > ask:
        return None
    return Quote(at_utc=at, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


ResponseSink = Callable[[str, dict[str, Any], dict[str, Any]], str]


class AlpacaMarketData:
    """GET-only client. ``sink`` persists every raw page and returns its digest."""

    def __init__(
        self,
        *,
        feed: str,
        sink: ResponseSink,
        session: Any | None = None,
        min_interval_sec: float = 0.35,
    ) -> None:
        if feed not in FEEDS:
            raise ValueError(f"feed must be one of {FEEDS}")
        self.feed = feed
        self._sink = sink
        if session is None:
            import requests

            key, secret = load_credentials()
            session = requests.Session()
            session.headers.update(
                {
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                    "Accept": "application/json",
                }
            )
        self._session = session
        self._min_interval = min_interval_sec
        self._last = 0.0
        self.contacted_hosts: set[str] = set()

    # -------------------------------------------------------------- transport
    def _get(
        self, url: str, params: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        import time

        host = assert_data_host(url)  # raises ForbiddenHostError before any I/O
        self.contacted_hosts.add(host)
        backoff = 1.0
        for _ in range(6):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                response = self._session.get(url, params=params, timeout=30)
            except ForbiddenHostError:
                raise
            except Exception as exc:  # network error -> unknown, never empty
                return None, None, f"transport_error:{type(exc).__name__}"
            if response.status_code == 429:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if response.status_code != 200:
                return None, None, f"http_{response.status_code}"
            try:
                payload = response.json()
            except ValueError:
                return None, None, "non_json_response"
            if not isinstance(payload, dict):
                return None, None, "unexpected_payload_shape"
            digest = self._sink(url.rsplit("/", 1)[-1], params, payload)
            return payload, digest, None
        return None, None, "rate_limited"

    def _pages(
        self, url: str, params: dict[str, Any], key: str, symbol: str
    ) -> tuple[list[dict[str, Any]] | None, tuple[str, ...], str | None, bool]:
        """Yield rows for one symbol across pages, bounded. Returns
        (rows, digests, error, complete)."""

        rows: list[dict[str, Any]] = []
        digests: list[str] = []
        token: str | None = None
        for _ in range(MAX_PAGES_PER_WINDOW):
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            payload, digest, error = self._get(url, page_params)
            if error:
                return None, tuple(digests), error, False
            assert payload is not None and digest is not None
            digests.append(digest)
            block = payload.get(key) or {}
            if not isinstance(block, dict):
                return None, tuple(digests), "unexpected_payload_shape", False
            got = block.get(symbol) or []
            if not isinstance(got, list):
                return None, tuple(digests), "unexpected_payload_shape", False
            rows.extend(r for r in got if isinstance(r, dict))
            token = payload.get("next_page_token")
            if not token:
                return rows, tuple(digests), None, True
        return rows, tuple(digests), None, False

    # -------------------------------------------------------------- accessors
    def _bars(
        self, symbol: str, timeframe: str, start: str, end: str, adjustment: str
    ) -> BarsResult:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": PAGE_LIMIT,
            "adjustment": adjustment,
            "feed": self.feed,
            "sort": "asc",
        }
        rows, digests, error, complete = self._pages(BARS_URL, params, "bars", symbol)
        if error or rows is None:
            return BarsResult(UNKNOWN, reason=error or "no_rows", digests=digests)
        if not complete:
            return BarsResult(UNKNOWN, reason="pagination_cap", digests=digests)
        bars = [b for b in (parse_bar(r) for r in rows) if b is not None]
        if len(bars) != len(rows):
            return BarsResult(UNKNOWN, reason="unparseable_bar", digests=digests)
        return BarsResult(OK, bars=tuple(bars), digests=digests)

    def daily_bars(
        self, symbol: str, start: date, end: date, *, adjustment: str = "raw"
    ) -> BarsResult:
        return self._bars(
            symbol,
            "1Day",
            f"{start.isoformat()}T00:00:00Z",
            f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
            adjustment,
        )

    def minute_bars(
        self, symbol: str, start_utc: datetime, end_utc: datetime
    ) -> BarsResult:
        return self._bars(symbol, "1Min", _iso(start_utc), _iso(end_utc), "raw")

    def _quotes(
        self, symbol: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[list[dict[str, Any]] | None, tuple[str, ...], str | None, bool]:
        params = {
            "symbols": symbol,
            "start": _iso(start_utc),
            "end": _iso(end_utc),
            "limit": PAGE_LIMIT,
            "feed": self.feed,
            "sort": "asc",
        }
        return self._pages(QUOTES_URL, params, "quotes", symbol)

    def first_valid_quote(
        self, symbol: str, start_utc: datetime, end_utc: datetime
    ) -> QuoteResult:
        rows, digests, error, complete = self._quotes(symbol, start_utc, end_utc)
        if error or rows is None:
            return QuoteResult(UNKNOWN, reason=error or "no_rows", digests=digests)
        for raw in rows:
            quote = parse_valid_quote(raw)
            if quote is not None and start_utc <= quote.at_utc < end_utc:
                return QuoteResult(OK, quote=quote, digests=digests)
        if not complete:
            return QuoteResult(UNKNOWN, reason="pagination_cap", digests=digests)
        return QuoteResult(OK, quote=None, reason="no_valid_quote", digests=digests)

    def ask_at_or_below(
        self, symbol: str, start_utc: datetime, end_utc: datetime, limit: Decimal
    ) -> AskEvidence:
        rows, digests, error, complete = self._quotes(symbol, start_utc, end_utc)
        if error or rows is None:
            return AskEvidence(UNKNOWN, reason=error or "no_rows", digests=digests)
        for raw in rows:
            quote = parse_valid_quote(raw)
            if quote is None or not (start_utc <= quote.at_utc < end_utc):
                continue
            if quote.ask <= limit:
                return AskEvidence(
                    OK, found=True, at_utc=quote.at_utc, ask=quote.ask, digests=digests
                )
        if not complete:
            return AskEvidence(UNKNOWN, reason="pagination_cap", digests=digests)
        return AskEvidence(OK, found=False, digests=digests)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")
