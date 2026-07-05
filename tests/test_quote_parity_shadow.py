from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.toss.dto import TossPrice
from app.services.quote_parity_shadow import (
    CoverageReport,
    CurrencyReport,
    DivergenceStats,
    GoNoGoDecision,
    LatencyStats,
    _percentile,
    check_currency,
    classify_coverage,
    evaluate_go_no_go,
    krx_tick_size,
    run_quote_parity_probe,
    summarize_divergence,
    summarize_latency,
)

pytestmark = pytest.mark.unit


class TestKrxTickSize:
    @pytest.mark.parametrize(
        "price,tick",
        [
            ("500", "1"),  # <2,000
            ("1999", "1"),
            ("2000", "5"),  # boundary is inclusive-low
            ("4999", "5"),
            ("5000", "10"),
            ("19999", "10"),
            ("20000", "50"),
            ("49999", "50"),
            ("50000", "100"),
            ("199999", "100"),
            ("200000", "500"),
            ("499999", "500"),
            ("500000", "1000"),
            ("1250000", "1000"),
        ],
    )
    def test_bands(self, price, tick):
        assert krx_tick_size(Decimal(price)) == Decimal(tick)

    def test_nonpositive_price_returns_min_tick(self):
        assert krx_tick_size(Decimal("0")) == Decimal("1")


class TestPercentile:
    def test_empty_is_none(self):
        assert _percentile([], 99) is None

    def test_nearest_rank_p99(self):
        # 100 values 1..100; nearest-rank p99 -> ceil(0.99*100)=99th -> 99.
        assert _percentile([float(i) for i in range(1, 101)], 99) == 99.0

    def test_p50_and_p100(self):
        vals = [10.0, 20.0, 30.0, 40.0]
        assert _percentile(vals, 50) == 20.0  # ceil(0.5*4)=2nd
        assert _percentile(vals, 100) == 40.0


class TestCoverage:
    def test_dotted_us_symbol_matches_when_echoed_verbatim(self):
        rep = classify_coverage(["AAPL", "BRK.B", "MSFT"], ["aapl", "BRK.B", "MSFT"])
        assert rep.matched == ["AAPL", "BRK.B", "MSFT"]  # case-insensitive echo
        assert rep.silent_drops == []
        assert rep.coverage_ratio == 1.0

    def test_dropped_dotted_symbol_is_a_silent_drop(self):
        # Toss silently omits BRK.B (or returns a de-dotted "BRK" that won't match).
        rep = classify_coverage(["AAPL", "BRK.B"], ["AAPL", "BRK"])
        assert rep.silent_drops == ["BRK.B"]
        assert "BRK" in rep.unexpected_echoes
        assert rep.coverage_ratio == 0.5

    def test_allowlisted_miss_is_not_a_silent_drop(self):
        rep = classify_coverage(
            ["AAPL", "DELISTED1"], ["AAPL"], allowlist=frozenset({"DELISTED1"})
        )
        assert rep.silent_drops == []
        assert rep.allowlisted_misses == ["DELISTED1"]
        assert rep.coverage_ratio == 0.5  # coverage still counts it missing

    def test_empty_request_is_full_coverage(self):
        rep = classify_coverage([], [])
        assert rep.coverage_ratio == 1.0
        assert isinstance(rep, CoverageReport)


class TestCurrency:
    def test_all_correct_zero_miskeys(self):
        rep = check_currency(
            [("005930", "KR", "KRW"), ("AAPL", "US", "USD"), ("BRK.B", "US", "usd")]
        )
        assert rep.miskey_count == 0
        assert rep.checked_count == 3

    def test_kr_tagged_usd_is_a_miskey(self):
        rep = check_currency([("005930", "KR", "USD"), ("AAPL", "US", "USD")])
        assert rep.miskey_count == 1
        assert rep.miskeys[0] == {
            "symbol": "005930",
            "market": "KR",
            "expected": "KRW",
            "got": "USD",
        }

    def test_unknown_market_is_not_checked(self):
        rep = check_currency([("BTC", "CRYPTO", "KRW")])
        assert rep.miskey_count == 0
        assert rep.checked_count == 0  # unknown markets are skipped, not failed
        assert isinstance(rep, CurrencyReport)


class TestDivergence:
    def test_us_bps_no_ticks(self):
        # 100.00 vs 100.10 -> 10 bps.
        stats = summarize_divergence(
            [("AAPL", Decimal("100.10"), Decimal("100.00"))], market="US"
        )
        assert stats.market == "US"
        assert stats.count == 1
        assert round(stats.p99_bps, 4) == 10.0
        assert stats.p99_ticks is None  # US never tick-normalized
        assert stats.worst[0]["symbol"] == "AAPL"

    def test_kr_tick_normalization(self):
        # price 30,000 -> tick 50; toss 30,050 vs kis 30,000 -> 1 tick, ~16.67 bps.
        stats = summarize_divergence(
            [("005930", Decimal("30050"), Decimal("30000"))], market="KR"
        )
        assert stats.p99_ticks == 1.0
        assert round(stats.p99_bps, 2) == 16.67

    def test_skips_nonpositive_kis(self):
        stats = summarize_divergence(
            [("BAD", Decimal("10"), Decimal("0"))], market="US"
        )
        assert stats.count == 0
        assert stats.p99_bps is None


class TestLatency:
    def test_percentiles_and_error_rate(self):
        stats = summarize_latency(
            "toss", [10.0, 20.0, 30.0, 40.0], error_count=1, total_wall_ms=105.0
        )
        assert stats.call_count == 5  # 4 samples + 1 error
        assert stats.error_rate == 0.2
        assert stats.p50_ms == 20.0
        assert stats.total_wall_ms == 105.0

    def test_empty_samples(self):
        stats = summarize_latency("kis", [], error_count=0, total_wall_ms=0.0)
        assert stats.call_count == 0
        assert stats.error_rate == 0.0
        assert stats.p95_ms is None
        assert isinstance(stats, LatencyStats)


def _cov(ratio=1.0, drops=None):
    return (
        classify_coverage(["AAPL"], ["AAPL"])
        if ratio == 1.0
        else CoverageReport(
            requested_count=2,
            echoed_count=1,
            matched=["AAPL"],
            silent_drops=list(drops or []),
            allowlisted_misses=[],
            unexpected_echoes=[],
            coverage_ratio=ratio,
        )
    )


def _div(market, p99_bps=1.0, p99_ticks=None):
    return DivergenceStats(
        market=market,
        count=1,
        median_bps=p99_bps,
        p99_bps=p99_bps,
        median_ticks=p99_ticks,
        p99_ticks=p99_ticks,
        worst=[],
    )


def _lat(label, wall, err_rate=0.0):
    # call_count derived; craft via summarize_latency for realism.
    n = 10
    return summarize_latency(label, [wall / n] * n, error_count=0, total_wall_ms=wall)


class TestGoNoGo:
    def test_all_pass_when_precondition_met(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_bps=5.0, p99_ticks=1.0),
            us_div=_div("US", p99_bps=8.0),
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert isinstance(d, GoNoGoDecision)
        assert d.decision == "go"
        assert {b.status for b in d.bars} == {"pass"}

    def test_blocked_when_us_precondition_unmet(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=999.0),  # huge, but not evaluated
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=False,  # ROB-708 not landed
        )
        assert d.decision == "blocked"
        us_bar = next(b for b in d.bars if b.name == "us_divergence")
        assert us_bar.status == "not_evaluable"
        assert "ROB-708" in us_bar.detail

    def test_no_go_on_silent_drop(self):
        d = evaluate_go_no_go(
            coverage=_cov(0.9, drops=["BRK.B"]),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=1.0),
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert d.decision == "no_go"
        assert any(b.name == "silent_drops" and b.status == "fail" for b in d.bars)
        assert any(b.name == "coverage" and b.status == "fail" for b in d.bars)

    def test_no_go_on_currency_miskey(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=1.0),
            currency=check_currency([("005930", "KR", "USD")]),  # mis-key
            toss_latency=_lat("toss", 100.0),
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert d.decision == "no_go"
        assert any(b.name == "currency" and b.status == "fail" for b in d.bars)

    def test_no_go_when_toss_slower_than_kis(self):
        d = evaluate_go_no_go(
            coverage=_cov(1.0),
            kr_div=_div("KR", p99_ticks=1.0),
            us_div=_div("US", p99_bps=1.0),
            currency=check_currency([("AAPL", "US", "USD")]),
            toss_latency=_lat("toss", 500.0),  # slower than kis
            kis_latency=_lat("kis", 300.0),
            us_kis_live_last=True,
        )
        assert d.decision == "no_go"
        assert any(b.name == "latency_wall" and b.status == "fail" for b in d.bars)


class _FakeClock:
    """Monotonic-ish clock: each call advances by a fixed step for deterministic ms."""

    def __init__(self, start=1000.0, step=0.01):
        self.t = start
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


@pytest.mark.asyncio
async def test_orchestrator_blocked_until_rob708(monkeypatch):
    async def toss_prices(batch):
        return [
            TossPrice(
                symbol=s,
                timestamp="2026-07-05T12:00:00Z",
                last_price=Decimal("100.10") if s == "AAPL" else Decimal("30050"),
                currency="USD" if s == "AAPL" else "KRW",
            )
            for s in batch
        ]

    async def kis_kr(symbols):
        return {"005930": 30000.0}

    async def kis_us(symbols):
        return {"AAPL": 100.00}

    report = await run_quote_parity_probe(
        kr_symbols=["005930"],
        us_symbols=["AAPL"],
        toss_prices_fn=toss_prices,
        kis_kr_fetch=kis_kr,
        kis_us_fetch=kis_us,
        clock=_FakeClock(),
        us_kis_live_last=False,  # ROB-708 not landed
    )
    assert report["go_no_go"]["decision"] == "blocked"
    assert report["coverage"]["combined"]["silent_drops"] == []
    assert report["currency"]["miskey_count"] == 0
    # US off-hours capture is recorded verbatim.
    assert report["off_hours"]["us"]["AAPL"]["timestamp"] == "2026-07-05T12:00:00Z"


@pytest.mark.asyncio
async def test_orchestrator_go_when_precondition_met_and_bars_pass():
    async def toss_prices(batch):
        return [
            TossPrice(
                symbol=s, timestamp="t", last_price=Decimal("100.05"), currency="USD"
            )
            for s in batch
        ]

    async def kis_kr(symbols):
        return {}

    async def kis_us(symbols):
        return {"AAPL": 100.00}  # 5 bps < 10

    report = await run_quote_parity_probe(
        kr_symbols=[],
        us_symbols=["AAPL"],
        toss_prices_fn=toss_prices,
        kis_kr_fetch=kis_kr,
        kis_us_fetch=kis_us,
        # Constant clock (step=0) => every measured duration is 0ms, so the
        # latency wall-bar is a deterministic 0 <= 0 pass. A stepping clock would
        # make Toss look slower purely because the probe calls clock() more times
        # on the Toss side than the KIS side — an artifact of the fake, not real
        # latency. Real timing comes from the monotonic clock in the live script.
        clock=_FakeClock(step=0.0),
        us_kis_live_last=True,
    )
    assert report["go_no_go"]["decision"] == "go"


@pytest.mark.asyncio
async def test_orchestrator_counts_toss_batch_error_fail_open():
    calls = {"n": 0}

    async def toss_prices(batch):
        calls["n"] += 1
        raise RuntimeError("toss 500")

    async def kis_us(symbols):
        return {"AAPL": 100.0}

    report = await run_quote_parity_probe(
        kr_symbols=[],
        us_symbols=["AAPL"],
        toss_prices_fn=toss_prices,
        kis_kr_fetch=lambda s: _empty(),
        kis_us_fetch=kis_us,
        clock=_FakeClock(),
        us_kis_live_last=True,
    )
    assert report["latency"]["toss"]["error_count"] == 1
    # A failed Toss batch => everything is a silent drop => no_go, never a crash.
    assert report["go_no_go"]["decision"] == "no_go"


async def _empty():
    return {}
