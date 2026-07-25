"""ROB-709 — read-only A/B parity shadow: Toss prices() batch vs the raw KIS
batch layer. Pure comparison/stats engine (no I/O) + an injected-fn orchestrator.

Decides whether /invest batch current-price reads can flip to Toss-first
(ROB-710). NO user-facing behavior change: this module never writes, never
mutates a broker/order/watch path, and never edits the production resolver — it
observes the Toss and KIS seams and emits go/no-go metrics.

PRECONDITION ROB-708: the KIS US layer must move to a live-last quote before US
divergence is a valid promotion signal; until then evaluate_go_no_go marks the
US-divergence bar not_evaluable and the decision blocked.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.brokers.toss.dto import TossPrice

PricesFn = Callable[[list[str]], Awaitable[list["TossPrice"]]]
KisFetchFn = Callable[[list[str]], Awaitable[dict[str, float | None]]]

# 2023-01 KRX cash-equity tick ladder (KOSPI/KOSDAQ). NOT the Upbit KRW ladder
# in app/services/paper_fills.py:14-27. First band whose threshold <= price wins.
_KRX_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("500000"), Decimal("1000")),
    (Decimal("200000"), Decimal("500")),
    (Decimal("50000"), Decimal("100")),
    (Decimal("20000"), Decimal("50")),
    (Decimal("5000"), Decimal("10")),
    (Decimal("2000"), Decimal("5")),
    (Decimal("0"), Decimal("1")),
)


def krx_tick_size(price: Decimal) -> Decimal:
    """KRX equity tick for ``price`` (KRW). Non-positive -> smallest tick."""
    if price <= 0:
        return Decimal("1")
    for threshold, unit in _KRX_TICK_BANDS:
        if price >= threshold:
            return unit
    return Decimal("1")  # pragma: no cover - last band threshold is 0


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Deterministic nearest-rank percentile; ``None`` for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((pct / 100.0) * len(ordered))
    idx = min(max(rank, 1), len(ordered)) - 1
    return ordered[idx]


@dataclass(frozen=True)
class CoverageReport:
    requested_count: int
    echoed_count: int
    matched: list[str]
    silent_drops: list[str]
    allowlisted_misses: list[str]
    unexpected_echoes: list[str]
    coverage_ratio: float


def classify_coverage(
    requested: Sequence[str],
    echoed_symbols: Iterable[str],
    *,
    allowlist: frozenset[str] = frozenset(),
) -> CoverageReport:
    # SAME echo-match as fetch_toss_batch_prices (invest_price_fallback.py:105-112).
    by_upper = {s.upper(): s for s in requested}
    allow_upper = {a.upper() for a in allowlist}
    echoed = [str(e) for e in echoed_symbols]
    echoed_upper = {e.upper() for e in echoed}

    matched = [by_upper[u] for u in by_upper if u in echoed_upper]
    missing = [by_upper[u] for u in by_upper if u not in echoed_upper]
    allowlisted_misses = [s for s in missing if s.upper() in allow_upper]
    silent_drops = [s for s in missing if s.upper() not in allow_upper]
    unexpected_echoes = [e for e in echoed if e.upper() not in by_upper]

    req_n = len(by_upper)
    return CoverageReport(
        requested_count=req_n,
        echoed_count=len(echoed),
        matched=matched,
        silent_drops=silent_drops,
        allowlisted_misses=allowlisted_misses,
        unexpected_echoes=unexpected_echoes,
        coverage_ratio=(len(matched) / req_n) if req_n else 1.0,
    )


_EXPECTED_CURRENCY = {"KR": "KRW", "US": "USD"}


@dataclass(frozen=True)
class CurrencyReport:
    checked_count: int
    miskeys: list[dict[str, str]]
    miskey_count: int


def check_currency(rows: Sequence[tuple[str, str, str]]) -> CurrencyReport:
    miskeys: list[dict[str, str]] = []
    checked = 0
    for symbol, market, currency in rows:
        expected = _EXPECTED_CURRENCY.get(str(market).upper())
        if expected is None:
            continue  # unknown market: not our jurisdiction, not a failure
        checked += 1
        if str(currency).upper() != expected:
            miskeys.append(
                {
                    "symbol": str(symbol),
                    "market": str(market).upper(),
                    "expected": expected,
                    "got": str(currency).upper(),
                }
            )
    return CurrencyReport(
        checked_count=checked, miskeys=miskeys, miskey_count=len(miskeys)
    )


@dataclass(frozen=True)
class DivergenceStats:
    market: str
    count: int
    median_bps: float | None
    p99_bps: float | None
    median_ticks: float | None
    p99_ticks: float | None
    worst: list[dict]


def summarize_divergence(
    pairs: Sequence[tuple[str, Decimal, Decimal]],
    *,
    market: str,
    top_n: int = 20,
) -> DivergenceStats:
    is_kr = str(market).upper() == "KR"
    rows: list[dict] = []
    for symbol, toss, kis in pairs:
        if kis <= 0:
            continue
        bps = abs(float(toss) - float(kis)) / float(kis) * 10000.0
        ticks = (
            abs(float(toss) - float(kis)) / float(krx_tick_size(kis)) if is_kr else None
        )
        rows.append(
            {
                "symbol": symbol,
                "toss": float(toss),
                "kis": float(kis),
                "bps": bps,
                "ticks": ticks,
            }
        )
    bps_vals = [r["bps"] for r in rows]
    tick_vals = [r["ticks"] for r in rows if r["ticks"] is not None]
    rows.sort(key=lambda r: r["bps"], reverse=True)
    return DivergenceStats(
        market=str(market).upper(),
        count=len(rows),
        median_bps=statistics.median(bps_vals) if bps_vals else None,
        p99_bps=_percentile(bps_vals, 99),
        median_ticks=statistics.median(tick_vals) if tick_vals else None,
        p99_ticks=_percentile(tick_vals, 99),
        worst=rows[:top_n],
    )


@dataclass(frozen=True)
class LatencyStats:
    label: str
    call_count: int
    error_count: int
    error_rate: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    total_wall_ms: float


def summarize_latency(
    label: str,
    samples_ms: Sequence[float],
    *,
    error_count: int,
    total_wall_ms: float,
) -> LatencyStats:
    call_count = len(samples_ms) + error_count
    return LatencyStats(
        label=label,
        call_count=call_count,
        error_count=error_count,
        error_rate=(error_count / call_count) if call_count else 0.0,
        p50_ms=_percentile(samples_ms, 50),
        p95_ms=_percentile(samples_ms, 95),
        p99_ms=_percentile(samples_ms, 99),
        total_wall_ms=total_wall_ms,
    )


@dataclass(frozen=True)
class GoBars:
    coverage_min: float = 0.995
    max_silent_drops: int = 0
    kr_p99_max_ticks: float = 1.0
    us_p99_max_bps: float = 10.0
    max_currency_miskeys: int = 0
    require_toss_wall_le_kis: bool = True
    require_toss_error_rate_le_kis: bool = True


@dataclass(frozen=True)
class BarResult:
    name: str
    status: str  # "pass" | "fail" | "not_evaluable"
    detail: str


@dataclass(frozen=True)
class GoNoGoDecision:
    decision: str  # "go" | "no_go" | "blocked"
    bars: list[BarResult]


def _bar(name: str, ok: bool, detail: str) -> BarResult:
    return BarResult(name=name, status="pass" if ok else "fail", detail=detail)


def evaluate_go_no_go(
    *,
    coverage: CoverageReport,
    kr_div: DivergenceStats,
    us_div: DivergenceStats,
    currency: CurrencyReport,
    toss_latency: LatencyStats,
    kis_latency: LatencyStats,
    us_kis_live_last: bool,
    bars: GoBars = GoBars(),
) -> GoNoGoDecision:
    results: list[BarResult] = []

    results.append(
        _bar(
            "coverage",
            coverage.coverage_ratio >= bars.coverage_min,
            f"coverage_ratio={coverage.coverage_ratio:.4f} min={bars.coverage_min}",
        )
    )
    results.append(
        _bar(
            "silent_drops",
            len(coverage.silent_drops) <= bars.max_silent_drops,
            f"silent_drops={len(coverage.silent_drops)} max={bars.max_silent_drops}",
        )
    )
    kr_ok = kr_div.p99_ticks is None or kr_div.p99_ticks <= bars.kr_p99_max_ticks
    results.append(
        _bar(
            "kr_divergence",
            kr_ok,
            f"kr_p99_ticks={kr_div.p99_ticks} max={bars.kr_p99_max_ticks}",
        )
    )

    # ROB-708 precondition: US divergence is a daily-close-vs-live artifact until
    # _kis_fetch_us moves to a live-last quote. Do NOT pass/fail it — mark it
    # not_evaluable so the operator cannot mistake a blocked run for a go.
    if not us_kis_live_last:
        results.append(
            BarResult(
                name="us_divergence",
                status="not_evaluable",
                detail=(
                    "blocked on ROB-708 — KIS US layer is daily-close (period=D), "
                    "not live-last; US divergence is not a valid promotion signal"
                ),
            )
        )
    else:
        us_ok = us_div.p99_bps is None or us_div.p99_bps <= bars.us_p99_max_bps
        results.append(
            _bar(
                "us_divergence",
                us_ok,
                f"us_p99_bps={us_div.p99_bps} max={bars.us_p99_max_bps}",
            )
        )

    results.append(
        _bar(
            "currency",
            currency.miskey_count <= bars.max_currency_miskeys,
            f"miskeys={currency.miskey_count} max={bars.max_currency_miskeys}",
        )
    )
    if bars.require_toss_wall_le_kis:
        results.append(
            _bar(
                "latency_wall",
                toss_latency.total_wall_ms <= kis_latency.total_wall_ms,
                f"toss_wall_ms={toss_latency.total_wall_ms} "
                f"kis_wall_ms={kis_latency.total_wall_ms}",
            )
        )
    if bars.require_toss_error_rate_le_kis:
        results.append(
            _bar(
                "error_rate",
                toss_latency.error_rate <= kis_latency.error_rate,
                f"toss_err={toss_latency.error_rate} kis_err={kis_latency.error_rate}",
            )
        )

    if any(b.status == "not_evaluable" for b in results):
        decision = "blocked"
    elif any(b.status == "fail" for b in results):
        decision = "no_go"
    else:
        decision = "go"
    return GoNoGoDecision(decision=decision, bars=results)


def _chunk(symbols: list[str], size: int) -> list[list[str]]:
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


async def _fetch_toss_side(
    symbols: list[str], toss_prices_fn: PricesFn, clock, batch_size: int
) -> tuple[list[TossPrice], list[float], int, float]:
    """Return (prices, per_batch_ms, error_count, wall_ms). Fail-open per batch."""
    prices: list[TossPrice] = []
    samples: list[float] = []
    errors = 0
    wall_start = clock()
    for batch in _chunk(symbols, batch_size):
        if not batch:
            continue
        # Uppercase the OUTBOUND request to mirror production exactly
        # (fetch_toss_batch_prices sends _chunk([s.upper() ...]),
        # invest_price_fallback.py:108). A --symbols-file may carry lowercase
        # symbols and to_db_symbol does not uppercase — send them in the same
        # case production would, or coverage would not reflect production drops.
        batch = [s.upper() for s in batch]
        t0 = clock()
        try:
            batch_prices = await toss_prices_fn(batch)
        except Exception:  # noqa: BLE001 — probe fails open; a failed batch is a drop
            errors += 1
            continue
        # Only SUCCESSFUL calls contribute a latency sample; failures are counted
        # via `errors` alone, so call_count = len(samples) + errors is exact (a
        # failed batch must not inflate both buckets).
        prices.extend(batch_prices)
        samples.append((clock() - t0) * 1000.0)
    wall_ms = (clock() - wall_start) * 1000.0
    return prices, samples, errors, wall_ms


async def run_quote_parity_probe(
    *,
    kr_symbols: list[str],
    us_symbols: list[str],
    toss_prices_fn: PricesFn,
    kis_kr_fetch: KisFetchFn,
    kis_us_fetch: KisFetchFn,
    allowlist: frozenset[str] = frozenset(),
    us_kis_live_last: bool = False,
    clock: Callable[[], float] = time.monotonic,
    bars: GoBars = GoBars(),
    batch_size: int = 200,
) -> dict[str, Any]:
    # --- Toss side (one batch call per <=200 chunk) ---
    kr_prices, kr_samp, kr_err, kr_wall = await _fetch_toss_side(
        kr_symbols, toss_prices_fn, clock, batch_size
    )
    us_prices, us_samp, us_err, us_wall = await _fetch_toss_side(
        us_symbols, toss_prices_fn, clock, batch_size
    )
    toss_latency = summarize_latency(
        "toss",
        kr_samp + us_samp,
        error_count=kr_err + us_err,
        total_wall_ms=kr_wall + us_wall,
    )

    # --- KIS side (raw layer, no fallback) ---
    kis_wall_start = clock()
    kis_kr = await kis_kr_fetch(kr_symbols) if kr_symbols else {}
    kis_us = await kis_us_fetch(us_symbols) if us_symbols else {}
    kis_wall = (clock() - kis_wall_start) * 1000.0
    kis_none = sum(1 for v in {**kis_kr, **kis_us}.values() if v is None)
    kis_calls = len(kis_kr) + len(kis_us)
    kis_latency = LatencyStats(
        label="kis",
        call_count=kis_calls,
        error_count=kis_none,
        error_rate=(kis_none / kis_calls) if kis_calls else 0.0,
        p50_ms=None,
        p95_ms=None,
        p99_ms=None,
        total_wall_ms=kis_wall,
    )

    # --- Coverage / currency / off-hours ---
    kr_cov = classify_coverage(
        kr_symbols, [p.symbol for p in kr_prices], allowlist=allowlist
    )
    us_cov = classify_coverage(
        us_symbols, [p.symbol for p in us_prices], allowlist=allowlist
    )
    combined_cov = classify_coverage(
        kr_symbols + us_symbols,
        [p.symbol for p in kr_prices + us_prices],
        allowlist=allowlist,
    )
    currency = check_currency(
        [(p.symbol, "KR", p.currency) for p in kr_prices]
        + [(p.symbol, "US", p.currency) for p in us_prices]
    )

    # Echo-match Toss last_price back to the requested key (SAME rule as prod).
    def _by_requested(reqs: list[str], prices: list[TossPrice]) -> dict[str, TossPrice]:
        by_upper = {s.upper(): s for s in reqs}
        out: dict[str, TossPrice] = {}
        for p in prices:
            req = by_upper.get(str(p.symbol).upper())
            if req is not None:
                out[req] = p
        return out

    kr_toss = _by_requested(kr_symbols, kr_prices)
    us_toss = _by_requested(us_symbols, us_prices)

    kr_pairs = [
        (sym, kr_toss[sym].last_price, Decimal(str(kis_kr[sym])))
        for sym in kr_toss
        if kis_kr.get(sym) is not None
    ]
    us_pairs = [
        (sym, us_toss[sym].last_price, Decimal(str(kis_us[sym])))
        for sym in us_toss
        if kis_us.get(sym) is not None
    ]
    kr_div = summarize_divergence(kr_pairs, market="KR")
    us_div = summarize_divergence(us_pairs, market="US")

    off_hours_us = {
        sym: {"last_price": str(p.last_price), "timestamp": p.timestamp}
        for sym, p in us_toss.items()
    }

    decision = evaluate_go_no_go(
        coverage=combined_cov,
        kr_div=kr_div,
        us_div=us_div,
        currency=currency,
        toss_latency=toss_latency,
        kis_latency=kis_latency,
        us_kis_live_last=us_kis_live_last,
        bars=bars,
    )

    def _cov_dict(c: CoverageReport) -> dict[str, Any]:
        return {
            "requested_count": c.requested_count,
            "echoed_count": c.echoed_count,
            "coverage_ratio": c.coverage_ratio,
            "silent_drops": c.silent_drops,
            "allowlisted_misses": c.allowlisted_misses,
            "unexpected_echoes": c.unexpected_echoes,
        }

    def _div_dict(d: DivergenceStats) -> dict[str, Any]:
        return {
            "market": d.market,
            "count": d.count,
            "median_bps": d.median_bps,
            "p99_bps": d.p99_bps,
            "median_ticks": d.median_ticks,
            "p99_ticks": d.p99_ticks,
            "worst": d.worst,
        }

    def _lat_dict(x: LatencyStats) -> dict[str, Any]:
        return {
            "label": x.label,
            "call_count": x.call_count,
            "error_count": x.error_count,
            "error_rate": x.error_rate,
            "p50_ms": x.p50_ms,
            "p95_ms": x.p95_ms,
            "p99_ms": x.p99_ms,
            "total_wall_ms": x.total_wall_ms,
        }

    return {
        "universe": {"kr_count": len(kr_symbols), "us_count": len(us_symbols)},
        "precondition": {
            "us_kis_live_last": us_kis_live_last,
            "note": "ROB-708 must land before US divergence is a valid go-signal",
        },
        "coverage": {
            "kr": _cov_dict(kr_cov),
            "us": _cov_dict(us_cov),
            "combined": _cov_dict(combined_cov),
        },
        "currency": {
            "checked_count": currency.checked_count,
            "miskey_count": currency.miskey_count,
            "miskeys": currency.miskeys,
        },
        "divergence": {"kr": _div_dict(kr_div), "us": _div_dict(us_div)},
        "latency": {"toss": _lat_dict(toss_latency), "kis": _lat_dict(kis_latency)},
        "off_hours": {"us": off_hours_us},
        "go_no_go": {
            "decision": decision.decision,
            "bars": [
                {"name": b.name, "status": b.status, "detail": b.detail}
                for b in decision.bars
            ],
        },
    }
