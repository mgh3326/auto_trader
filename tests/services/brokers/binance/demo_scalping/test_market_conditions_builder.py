"""ROB-841 — tests for the server-derived MarketConditions builder.

``build_market_conditions`` reuses the read-only Demo-host bookTicker +
latest 1m kline to construct a :class:`MarketConditions` snapshot whose
spread / data-age fields are ALWAYS server-measured (never caller-supplied).
It fails closed via :class:`MarketConditionsUnavailable` on any provider
failure, empty/malformed kline, missing timestamp, or invalid quote — the
caller must then reject the order without touching broker or ledger.

The valid snapshot feeds the *existing* ``evaluate_risk`` gates, so the
``stale_data`` / ``spread_too_wide`` / valid-boundary cases are exercised
end-to-end here (reason codes unchanged).
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import (
    ReasonCode,
    ScalpingRiskLimits,
    evaluate_risk,
)
from app.services.brokers.binance.demo_scalping.market_data import (
    BookTicker,
    DemoScalpingMarketData,
    MarketConditionsUnavailable,
    build_market_conditions,
)
from app.services.brokers.binance.demo_scalping.signal import Candle

# open_time anchor; now_ms is offset from it to drive data-age.
_OPEN_MS = 1_779_608_160_000


def _candle(open_time_ms: int = _OPEN_MS) -> Candle:
    return Candle(
        open_time_ms=open_time_ms,
        open=Decimal("1.36"),
        high=Decimal("1.37"),
        low=Decimal("1.35"),
        close=Decimal("1.36"),
        close_time_ms=open_time_ms + 59_999,
    )


def _candle_ohlc(
    o: str,
    h: str,
    low: str,
    c: str,
    *,
    open_time_ms: int = _OPEN_MS,
    close_time_ms: int | None = None,
) -> Candle:
    return Candle(
        open_time_ms=open_time_ms,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        close_time_ms=(
            close_time_ms if close_time_ms is not None else open_time_ms + 59_999
        ),
    )


class _FakeMD:
    """Scripted Demo-host reader. Either value may be an Exception to raise."""

    def __init__(self, *, book=None, klines=None):
        self._book = book
        self._klines = klines
        self.book_calls = 0
        self.kline_calls = 0

    async def fetch_book_ticker(self, product, symbol):
        self.book_calls += 1
        if isinstance(self._book, Exception):
            raise self._book
        return self._book

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        self.kline_calls += 1
        if isinstance(self._klines, Exception):
            raise self._klines
        return self._klines


async def _build(md, *, now_ms):
    # now_ms drives the injectable clock; the builder samples it post-fetch.
    return await build_market_conditions(
        md, product="usdm_futures", symbol="XRPUSDT", clock_ms=lambda: now_ms
    )


class _LatencyMD:
    """Advances a shared wall-clock during each fetch to model HTTP latency."""

    def __init__(self, *, book, candle, clock_holder, per_fetch_ms):
        self._book = book
        self._candle = candle
        self._clock = clock_holder  # list[int]
        self._per = per_fetch_ms

    async def fetch_book_ticker(self, product, symbol):
        self._clock[0] += self._per
        return self._book

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        self._clock[0] += self._per
        return [self._candle]


@pytest.mark.asyncio
async def test_valid_snapshot_measures_server_spread_and_age() -> None:
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")), klines=[_candle()]
    )
    market = await _build(md, now_ms=_OPEN_MS + 30_000)  # 30s old
    # spread = 0.05 / 100.025 * 1e4 ≈ 4.9975 bps
    assert market.spread_bps == pytest.approx(Decimal("4.9975"), rel=Decimal("0.001"))
    assert market.data_age_seconds == 30.0
    # A fresh, tight book passes every market gate.
    decision = evaluate_risk(
        product="usdm_futures",
        symbol="XRPUSDT",
        side="BUY",
        target_notional_usdt=Decimal("10"),
        limits=ScalpingRiskLimits(allowlist=frozenset({"XRPUSDT"})),
        ledger=_empty_ledger(),
        market=market,
    )
    assert decision.allowed


@pytest.mark.asyncio
async def test_provider_failure_fails_closed() -> None:
    md = _FakeMD(book=RuntimeError("bookTicker HTTP 503"), klines=[_candle()])
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_empty_kline_fails_closed() -> None:
    md = _FakeMD(book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")), klines=[])
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_kline_provider_error_fails_closed() -> None:
    # A malformed raw kline surfaces as a parse error from the reader.
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        klines=ValueError("could not parse kline row"),
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_missing_timestamp_fails_closed() -> None:
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        klines=[_candle(open_time_ms=0)],
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_crossed_quote_fails_closed() -> None:
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100.10"), ask=Decimal("100.00")),  # ask < bid
        klines=[_candle()],
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_zero_quote_fails_closed() -> None:
    md = _FakeMD(
        book=BookTicker(bid=Decimal("0"), ask=Decimal("0")),
        klines=[_candle()],
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_wide_spread_maps_to_existing_spread_too_wide() -> None:
    # bid=100 ask=101 → ~99.5 bps ≫ 20 bps cap.
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("101")), klines=[_candle()]
    )
    market = await _build(md, now_ms=_OPEN_MS)
    decision = evaluate_risk(
        product="usdm_futures",
        symbol="XRPUSDT",
        side="BUY",
        target_notional_usdt=Decimal("10"),
        limits=ScalpingRiskLimits(allowlist=frozenset({"XRPUSDT"})),
        ledger=_empty_ledger(),
        market=market,
    )
    assert ReasonCode.SPREAD_TOO_WIDE in decision.reason_codes


@pytest.mark.asyncio
async def test_stale_kline_maps_to_existing_stale_data() -> None:
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")), klines=[_candle()]
    )
    market = await _build(md, now_ms=_OPEN_MS + 200_000)  # 200s > 120s cap
    decision = evaluate_risk(
        product="usdm_futures",
        symbol="XRPUSDT",
        side="BUY",
        target_notional_usdt=Decimal("10"),
        limits=ScalpingRiskLimits(allowlist=frozenset({"XRPUSDT"})),
        ledger=_empty_ledger(),
        market=market,
    )
    assert ReasonCode.STALE_DATA in decision.reason_codes


@pytest.mark.asyncio
async def test_data_age_boundary_is_allowed() -> None:
    # age == max_data_age_seconds (120.0): the gate uses strict '>', so the
    # boundary is allowed — proves the builder feeds an exact server value.
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")), klines=[_candle()]
    )
    market = await _build(md, now_ms=_OPEN_MS + 120_000)
    assert market.data_age_seconds == 120.0
    decision = evaluate_risk(
        product="usdm_futures",
        symbol="XRPUSDT",
        side="BUY",
        target_notional_usdt=Decimal("10"),
        limits=ScalpingRiskLimits(allowlist=frozenset({"XRPUSDT"})),
        ledger=_empty_ledger(),
        market=market,
    )
    assert decision.allowed


@pytest.mark.asyncio
async def test_malformed_raw_kline_from_reader_fails_closed(httpx_mock) -> None:
    # End-to-end with the REAL Demo-host reader: a bookTicker OK but a kline
    # row missing fields makes the parser raise → builder fails closed.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"^https://demo-fapi\.binance\.com/fapi/v1/ticker/bookTicker\?.*$"
        ),
        json={"symbol": "XRPUSDT", "bidPrice": "1.35950000", "askPrice": "1.35960000"},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/klines\?.*$"),
        json=[["only-one-field"]],  # malformed: index errors during parse
    )
    md = DemoScalpingMarketData()
    try:
        with pytest.raises(MarketConditionsUnavailable):
            await build_market_conditions(
                md,
                product="usdm_futures",
                symbol="XRPUSDT",
                clock_ms=lambda: _OPEN_MS,
            )
    finally:
        await md.aclose()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_non_finite_quote_fails_closed(bad: str) -> None:
    # ROB-841 review: NaN / ±Infinity must normalize to
    # MarketConditionsUnavailable, not leak as a generic error (NaN comparison
    # raises InvalidOperation; +Inf silently poisons spread_bps). Both legs.
    bid_bad = _FakeMD(
        book=BookTicker(bid=Decimal(bad), ask=Decimal("100")), klines=[_candle()]
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(bid_bad, now_ms=_OPEN_MS)
    ask_bad = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal(bad)), klines=[_candle()]
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(ask_bad, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_fetch_latency_pushes_age_over_stale_boundary() -> None:
    # ROB-841 review: the candle is 115s old when fetching STARTS (< 120 cap),
    # but 6s of bookTicker+kline fetch latency pushes the observed age to 121s.
    # Sampling the clock AFTER both observations trips stale_data; a pre-fetch
    # clock (115.0s) would have wrongly allowed it.
    start = _OPEN_MS
    clock = [start]
    candle = _candle(open_time_ms=start - 115_000)
    md = _LatencyMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        candle=candle,
        clock_holder=clock,
        per_fetch_ms=3_000,  # 2 fetches × 3s = 6s latency
    )
    market = await build_market_conditions(
        md, product="usdm_futures", symbol="XRPUSDT", clock_ms=lambda: clock[0]
    )
    assert market.data_age_seconds == 121.0  # 115 + 6, not the pre-fetch 115
    decision = evaluate_risk(
        product="usdm_futures",
        symbol="XRPUSDT",
        side="BUY",
        target_notional_usdt=Decimal("10"),
        limits=ScalpingRiskLimits(allowlist=frozenset({"XRPUSDT"})),
        ledger=_empty_ledger(),
        market=market,
    )
    assert ReasonCode.STALE_DATA in decision.reason_codes


@pytest.mark.asyncio
async def test_full_shape_nan_ohlc_kline_from_reader_fails_closed(httpx_mock) -> None:
    # ROB-841 review: a kline row with the CORRECT number of fields but a NaN
    # OHLC value parses cleanly (Decimal('NaN')), so open_time-only validation
    # let it through as a healthy snapshot. Semantic validation must reject it.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"^https://demo-fapi\.binance\.com/fapi/v1/ticker/bookTicker\?.*$"
        ),
        json={"symbol": "XRPUSDT", "bidPrice": "1.35950000", "askPrice": "1.35960000"},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/klines\?.*$"),
        json=[
            [
                _OPEN_MS,  # open_time (valid, > 0)
                "1.35960000",  # open
                "1.35980000",  # high
                "1.35950000",  # low
                "NaN",  # close — full shape, but not finite
                "6136.00000000",  # volume
                _OPEN_MS + 59_999,  # close_time
                "8342.58965000",
                46,
                "2795.80000000",
                "3801.21357000",
                "0",
            ]
        ],
    )
    md = DemoScalpingMarketData()
    try:
        with pytest.raises(MarketConditionsUnavailable):
            await build_market_conditions(
                md,
                product="usdm_futures",
                symbol="XRPUSDT",
                clock_ms=lambda: _OPEN_MS,
            )
    finally:
        await md.aclose()


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_non_finite_ohlc_fails_closed(field: str, bad: str) -> None:
    ohlc = {"open": "1.36", "high": "1.37", "low": "1.35", "close": "1.36"}
    ohlc[field] = bad
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        klines=[_candle_ohlc(ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"])],
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad", ["0", "-1"])
@pytest.mark.asyncio
async def test_non_positive_ohlc_fails_closed(field: str, bad: str) -> None:
    ohlc = {"open": "1.36", "high": "1.37", "low": "1.35", "close": "1.36"}
    ohlc[field] = bad
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        klines=[_candle_ohlc(ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"])],
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.parametrize(
    "o,h,low,c",
    [
        ("1.36", "1.34", "1.35", "1.36"),  # high < low
        ("1.36", "1.355", "1.35", "1.36"),  # high < max(open, close)
        ("1.36", "1.37", "1.365", "1.36"),  # low > min(open, close)
    ],
)
@pytest.mark.asyncio
async def test_inconsistent_ohlc_fails_closed(o, h, low, c) -> None:
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        klines=[_candle_ohlc(o, h, low, c)],
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_close_before_open_time_fails_closed() -> None:
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        klines=[
            _candle_ohlc(
                "1.36",
                "1.37",
                "1.35",
                "1.36",
                open_time_ms=_OPEN_MS,
                close_time_ms=_OPEN_MS - 1,  # close_time before open_time
            )
        ],
    )
    with pytest.raises(MarketConditionsUnavailable):
        await _build(md, now_ms=_OPEN_MS)


@pytest.mark.asyncio
async def test_semantically_valid_candle_is_allowed() -> None:
    # Boundary-valid OHLC (high == max, low == min, close_time == open_time)
    # must pass so the tightened validation does not over-reject.
    md = _FakeMD(
        book=BookTicker(bid=Decimal("100"), ask=Decimal("100.05")),
        klines=[
            _candle_ohlc(
                "1.36",
                "1.36",  # high == open == close
                "1.36",  # low == open == close
                "1.36",
                open_time_ms=_OPEN_MS,
                close_time_ms=_OPEN_MS,  # close_time == open_time
            )
        ],
    )
    market = await _build(md, now_ms=_OPEN_MS)
    assert isinstance(market.data_age_seconds, float)


def _empty_ledger():
    from app.services.brokers.binance.demo_scalping.contract import LedgerSnapshot

    return LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=0,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )
