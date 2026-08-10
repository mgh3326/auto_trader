"""US equities adapter for ROB-1230 P-2-US / B0-X U-1 — policy_table.v1 rows.

Read-only. Universe selection is the ``invest_screener_snapshots`` table
(``market='us'``) — same DB-snapshot-forced pattern as the KR adapter (design
doc §2: night batch cannot depend on a live session scan). Indicator inputs
(fib/BB/RSI need ≥120 daily bars) are read from ``us_candles_1d`` via
``DailyCandlesRepository`` (partition = exchange from ``us_symbol_universe``).

Holdings come from **Alpaca paper lab** only
(``account_mode=alpaca_paper_lab`` / profile ``lab``) — the B0-X US account
per operator account map (``mock/CLAUDE.md`` §1). ``alpaca_paper`` (default
identity) is deliberately never touched. The service is used for
``list_positions`` (GET) only; this adapter never calls order placement /
cancel / submit methods (U-1 scope: table only, no U-2 order adapter).

Labels: base 3 trust labels + ``CROSS_MARKET_TRANSFER_UNVALIDATED`` (B0-X
contract §1). Per-row sell side still carries ``SELL_SIDE_MODEL_MISMATCH``.

Split into ``fetch_raw_inputs`` (network + DB I/O) and
``compute_policy_table`` (pure, deterministic given those inputs), same
contract as crypto/KR adapters.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.halt_detection import classify_ohlcv_rows
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.invest_view_model.us_quality_guards import US_MIN_MARKET_CAP_USD
from app.services.investment_reports.repository import InvestmentReportsRepository
from research.kr_corpus.d3_engine.models import Position
from research.kr_corpus.d3_engine.policies import update_underwater_close
from research.kr_corpus.d3_engine.signals import support_distance
from scripts.policy_table.core.averaging import averaging_math
from scripts.policy_table.core.max_table_age import max_table_age_stamp
from scripts.policy_table.core.signal_math import (
    D3_CONSTANTS_ECHO,
    FIB_WINDOW,
    InsufficientHistory,
    SymbolSignal,
    compute_symbol_signal,
)
from scripts.policy_table.core.trust_labels import US_TRUST_LABELS
from scripts.policy_table.core.us_tick import TICK_SOURCE, build_us_equity_tick_table

MARKET = "us"
SNAPSHOT_MARKET = "us"
WATCH_MARKET = "equity_us"  # InvestmentWatchAlert.market
DB_MARKET_KEY = MarketKey.US
QUOTE_CURRENCY = "USD"
CANDLE_GRANULARITY = "1d"
CANDLE_LOOKBACK_BARS = 200  # >= FIB_WINDOW(120) with headroom
CANDLE_FETCH_CONCURRENCY = 8
DEFAULT_EXCHANGE_PARTITION = "NASD"
DEEP_BAND_LOWER_PCT = Decimal("-0.12")
DEEP_BAND_UPPER_PCT = Decimal("-0.03")
POSITION_ALERT_PCT = Decimal("-0.30")
LOSS_GUARD_MULTIPLIER = Decimal("1.01")
AVERAGING_K_LEVELS: tuple[Decimal, Decimal] = (Decimal("0.05"), Decimal("0.10"))

# B0-X contract §4 US column — "B0 규칙($150~450)".
US_NEW_ENTRY_NOTIONAL_USD_MIN = Decimal("150")
US_NEW_ENTRY_NOTIONAL_USD_MAX = Decimal("450")
# Mid of the band for single-value sizing_band echo (range also stamped in config).
US_NEW_ENTRY_NOTIONAL_USD = Decimal("300")

# Reused US quality floor (app/services/invest_view_model/us_quality_guards.py).
MIN_MARKET_CAP_USD = US_MIN_MARKET_CAP_USD  # $100M
# US adapter liquidity floor (no pre-existing shared constant analogous to
# DEFAULT_MIN_TURNOVER_KRW). $1M daily notional — conservative small-cap cut.
MIN_TURNOVER_USD = Decimal("1000000")

# Yahoo-backed valuation rows are the trusted US market-cap source (builder
# stamps source='yahoo' for market='us'). invest_screener_snapshots.market_cap
# is not filled by build_snapshot_for_symbol — same column often NULL for KR
# and may be NULL for US; we join valuation and record which source won.
US_VALUATION_MARKET_CAP_SOURCE = "yahoo"

# B0-X US account only — never fall back to default alpaca_paper.
ALPACA_HOLDINGS_PROFILE = "lab"
ALPACA_ACCOUNT_MODE = "alpaca_paper_lab"


# ---------------------------------------------------------------------------
# Raw inputs — dumpable/replayable for the determinism acceptance check.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawInputs:
    as_of: str  # ISO8601, captured once at fetch time
    holdings: list[dict[str, str]]
    watch_alerts: list[dict[str, Any]]
    universe_pool: list[dict[str, Any]]
    snapshot_partition_date: str | None
    snapshot_breadth: dict[str, Any] | None
    universe_symbols: list[str]
    # symbol -> [[close, high, low, volume], ...] ascending. ROB-1236 appended
    # ``volume`` for halt detection; three-element rows from an older replay
    # dump still classify (zero-variation rule only), so existing dumps replay.
    candles: dict[str, list[list[str]]]
    market_cap_fill_stats: dict[str, Any]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "holdings": self.holdings,
            "watch_alerts": self.watch_alerts,
            "universe_pool": self.universe_pool,
            "snapshot_partition_date": self.snapshot_partition_date,
            "snapshot_breadth": self.snapshot_breadth,
            "universe_symbols": self.universe_symbols,
            "candles": self.candles,
            "market_cap_fill_stats": self.market_cap_fill_stats,
        }

    @classmethod
    def from_jsonable(cls, payload: dict[str, Any]) -> RawInputs:
        return cls(
            as_of=payload["as_of"],
            holdings=payload["holdings"],
            watch_alerts=payload["watch_alerts"],
            universe_pool=payload["universe_pool"],
            snapshot_partition_date=payload["snapshot_partition_date"],
            snapshot_breadth=payload["snapshot_breadth"],
            universe_symbols=payload["universe_symbols"],
            candles=payload["candles"],
            market_cap_fill_stats=payload.get("market_cap_fill_stats") or {},
        )


def _passes_liquidity_filter(row: dict[str, Any]) -> bool:
    market_cap = row.get("market_cap")
    turnover = row.get("daily_turnover")
    if not market_cap or not turnover:
        return False
    # Common-stock gate: only True passes. None/False fail closed (US screener
    # activation ROB-204 — preferred/warrant/unit noise).
    if row.get("is_common_stock") is not True:
        return False
    return (
        Decimal(market_cap) >= MIN_MARKET_CAP_USD
        and Decimal(turnover) >= MIN_TURNOVER_USD
    )


async def _fetch_holdings_raw() -> list[dict[str, str]]:
    """Read-only Alpaca paper lab positions. No order methods called."""

    service = AlpacaPaperBrokerService(profile=ALPACA_HOLDINGS_PROFILE)
    positions = await service.list_positions()
    rows: list[dict[str, str]] = []
    for pos in positions:
        symbol = str(pos.symbol or "").strip().upper()
        if not symbol:
            continue
        quantity = Decimal(str(pos.qty))
        if quantity <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "quantity": str(quantity),
                "average_price": str(Decimal(str(pos.avg_entry_price))),
            }
        )
    return rows


async def _fetch_watch_alerts_raw(*, as_of: datetime) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        alerts = await repo.list_active_alerts(
            market=WATCH_MARKET, valid_at=as_of, limit=250
        )
    return [
        {
            "alert_uuid": str(alert.alert_uuid),
            "symbol": alert.symbol,
            "target_kind": alert.target_kind,
            "metric": alert.metric,
            "operator": alert.operator,
            "threshold": str(alert.threshold),
            "threshold_high": (
                str(alert.threshold_high) if alert.threshold_high is not None else None
            ),
            "rationale": alert.rationale,
        }
        for alert in alerts
    ]


async def _load_us_yahoo_market_caps(
    session: Any, symbols: list[str]
) -> dict[str, tuple[Decimal, str]]:
    """Newest yahoo market_cap per symbol from market_valuation_snapshots."""

    if not symbols:
        return {}
    result = await session.execute(
        sa.select(
            MarketValuationSnapshot.symbol,
            MarketValuationSnapshot.market_cap,
            MarketValuationSnapshot.source,
        )
        .where(
            MarketValuationSnapshot.market == "us",
            MarketValuationSnapshot.symbol.in_(symbols),
            MarketValuationSnapshot.source == US_VALUATION_MARKET_CAP_SOURCE,
            MarketValuationSnapshot.market_cap.is_not(None),
            MarketValuationSnapshot.market_cap > 0,
        )
        .order_by(
            MarketValuationSnapshot.symbol.asc(),
            MarketValuationSnapshot.snapshot_date.desc(),
            MarketValuationSnapshot.computed_at.desc(),
        )
    )
    caps: dict[str, tuple[Decimal, str]] = {}
    for row in result.all():
        if row.symbol in caps:
            continue
        caps[row.symbol] = (Decimal(str(row.market_cap)), str(row.source))
    return caps


async def _load_us_symbol_meta(
    session: Any, symbols: list[str]
) -> dict[str, dict[str, Any]]:
    """exchange + is_common_stock from us_symbol_universe."""

    if not symbols:
        return {}
    result = await session.execute(
        sa.select(
            USSymbolUniverse.symbol,
            USSymbolUniverse.exchange,
            USSymbolUniverse.is_common_stock,
        ).where(USSymbolUniverse.symbol.in_(symbols))
    )
    out: dict[str, dict[str, Any]] = {}
    for row in result.all():
        out[row.symbol] = {
            "exchange": row.exchange or DEFAULT_EXCHANGE_PARTITION,
            "is_common_stock": row.is_common_stock,
        }
    return out


async def _fetch_universe_pool_raw() -> tuple[
    list[dict[str, Any]], str | None, dict[str, Any] | None, dict[str, Any]
]:
    async with AsyncSessionLocal() as db:
        repo = InvestScreenerSnapshotsRepository(db)
        rows = await repo.list_candidate_pool(market=SNAPSHOT_MARKET, limit=None)
        breadth = await repo.breadth(market=SNAPSHOT_MARKET)
        symbols = [row.symbol for row in rows]
        valuation_caps = await _load_us_yahoo_market_caps(db, symbols)
        meta = await _load_us_symbol_meta(db, symbols)

    # Empirical fill accounting for the report (MARKET_CAP_SOURCE).
    snapshot_cap_filled = 0
    valuation_cap_used = 0
    neither = 0
    pool: list[dict[str, Any]] = []
    for row in rows:
        snap_cap = row.market_cap
        snap_src = row.market_cap_source
        if snap_cap is not None:
            snapshot_cap_filled += 1
            market_cap = str(Decimal(str(snap_cap)))
            market_cap_source = snap_src or "invest_screener_snapshots"
        elif row.symbol in valuation_caps:
            valuation_cap_used += 1
            cap_val, cap_src = valuation_caps[row.symbol]
            market_cap = str(cap_val)
            market_cap_source = f"market_valuation_snapshots:{cap_src}"
        else:
            neither += 1
            market_cap = None
            market_cap_source = None

        symbol_meta = meta.get(row.symbol, {})
        pool.append(
            {
                "symbol": row.symbol,
                "latest_close": str(row.latest_close),
                "market_cap": market_cap,
                "market_cap_source": market_cap_source,
                "daily_turnover": (
                    str(row.daily_turnover) if row.daily_turnover is not None else None
                ),
                "daily_volume": (
                    str(row.daily_volume) if row.daily_volume is not None else None
                ),
                "snapshot_date": row.snapshot_date.isoformat(),
                "exchange": symbol_meta.get("exchange", DEFAULT_EXCHANGE_PARTITION),
                "is_common_stock": symbol_meta.get("is_common_stock"),
            }
        )

    partition_date = rows[0].snapshot_date.isoformat() if rows else None
    breadth_dict = {
        "total": breadth.total,
        "advancers": breadth.advancers,
        "decliners": breadth.decliners,
        "unchanged": breadth.unchanged,
        "partition_date": (
            breadth.partition_date.isoformat() if breadth.partition_date else None
        ),
    }
    fill_stats = {
        "snapshot_total": len(rows),
        "snapshot_market_cap_non_null": snapshot_cap_filled,
        "valuation_yahoo_fallback_used": valuation_cap_used,
        "market_cap_missing": neither,
        "note": (
            "invest_screener_snapshots.builder does not write market_cap; "
            "prefer snapshot column when present, else market_valuation_snapshots "
            f"source={US_VALUATION_MARKET_CAP_SOURCE}."
        ),
    }
    return pool, partition_date, breadth_dict, fill_stats


async def _fetch_us_daily_candles_raw(
    symbol: str, *, partition: str
) -> list[list[str]] | None:
    async with AsyncSessionLocal() as db:
        repo = DailyCandlesRepository(session=db)
        rows = await repo.fetch_recent(
            market=DB_MARKET_KEY,
            symbol=symbol,
            partition=partition,
            count=CANDLE_LOOKBACK_BARS,
        )
    if not rows:
        return None
    return [
        [
            str(Decimal(str(r.close))),
            str(Decimal(str(r.high))),
            str(Decimal(str(r.low))),
            str(Decimal(str(r.volume))),
        ]
        for r in rows
    ]


async def fetch_raw_inputs() -> RawInputs:
    """Do all network/DB I/O once; return a JSON-safe, replayable snapshot."""

    as_of = datetime.now(UTC)
    pool, partition_date, breadth, fill_stats = await _fetch_universe_pool_raw()
    watch_alerts = await _fetch_watch_alerts_raw(as_of=as_of)
    holdings = await _fetch_holdings_raw()

    filtered_symbols = {row["symbol"] for row in pool if _passes_liquidity_filter(row)}
    holding_symbols = {row["symbol"] for row in holdings}
    watch_symbols = {row["symbol"] for row in watch_alerts}
    universe_symbols = sorted(filtered_symbols | holding_symbols | watch_symbols)

    partition_by_symbol = {
        row["symbol"]: row.get("exchange") or DEFAULT_EXCHANGE_PARTITION for row in pool
    }
    # Holdings/watch outside the snapshot pool still need an exchange.
    missing = [s for s in universe_symbols if s not in partition_by_symbol]
    if missing:
        async with AsyncSessionLocal() as db:
            meta = await _load_us_symbol_meta(db, missing)
        for sym in missing:
            partition_by_symbol[sym] = meta.get(sym, {}).get(
                "exchange", DEFAULT_EXCHANGE_PARTITION
            )

    semaphore = asyncio.Semaphore(CANDLE_FETCH_CONCURRENCY)

    async def _bounded_fetch(symbol: str) -> tuple[str, list[list[str]] | None]:
        async with semaphore:
            partition = partition_by_symbol.get(symbol, DEFAULT_EXCHANGE_PARTITION)
            return symbol, await _fetch_us_daily_candles_raw(
                symbol, partition=partition
            )

    fetched = await asyncio.gather(*(_bounded_fetch(sym) for sym in universe_symbols))
    candles = {symbol: rows for symbol, rows in fetched if rows is not None}

    return RawInputs(
        as_of=as_of.isoformat(),
        holdings=holdings,
        watch_alerts=watch_alerts,
        universe_pool=pool,
        snapshot_partition_date=partition_date,
        snapshot_breadth=breadth,
        universe_symbols=universe_symbols,
        candles=candles,
        market_cap_fill_stats=fill_stats,
    )


# ---------------------------------------------------------------------------
# Pure computation — deterministic given RawInputs.
# Helpers mirror kr.py (market-agnostic); not extracted this PR (same note as KR).
# ---------------------------------------------------------------------------


def _cluster_view_row(view: Any) -> dict[str, Any]:
    return {
        "price": view.representative,
        "distance_pct": view.distance_pct,
        "sources": list(view.sources),
        "source_count": view.source_count,
        "qualifies_two_source": view.qualifies_two_source,
        "within_d3_support_window": view.within_d3_support_window,
    }


def _select_invalidation_line(
    alerts_for_symbol: list[dict[str, Any]], *, current_price: Decimal
) -> dict[str, Any]:
    candidates = [
        alert
        for alert in alerts_for_symbol
        if alert.get("operator") == "below" and alert.get("threshold") not in (None, "")
    ]
    if not candidates:
        return {"available": False, "reason": "no_active_below_operator_watch_alert"}
    chosen = min(
        candidates,
        key=lambda alert: abs(Decimal(alert["threshold"]) - current_price),
    )
    price = Decimal(chosen["threshold"])
    return {
        "available": True,
        "price": price,
        "distance_pct": support_distance(price, current_price),
        "metric": chosen.get("metric"),
        "alert_uuid": chosen.get("alert_uuid"),
        "rationale": chosen.get("rationale"),
    }


def _underwater_sessions_within_lookback(
    *, closes: list[Decimal], average_price: Decimal
) -> int:
    position = Position(symbol="_", quantity=1, average_price=average_price)
    streak = 0
    for close in closes:
        outcome = update_underwater_close(position, close=close)
        streak = outcome.streak
    return streak


def _build_row(
    *,
    symbol: str,
    closes: list[Decimal],
    highs: list[Decimal],
    lows: list[Decimal],
    tick_table: Any,
    holding: dict[str, str] | None,
    watch_alerts_for_symbol: list[dict[str, Any]],
    pool_row: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        signal: SymbolSignal | None = compute_symbol_signal(
            closes=closes, highs=highs, lows=lows, tick_table=tick_table
        )
    except InsufficientHistory as exc:
        return {
            "symbol": symbol,
            "held": holding is not None,
            "insufficient_history": True,
            "bars_available": len(closes),
            "bars_required": FIB_WINDOW,
            "note": str(exc),
        }

    close = signal.previous_close
    deep_band = {
        "lower": close * (Decimal(1) + DEEP_BAND_LOWER_PCT),
        "upper": close * (Decimal(1) + DEEP_BAND_UPPER_PCT),
    }
    nearest_support_distance_pct = (
        support_distance(signal.buy_l2, close) if signal.buy_l2 is not None else None
    )

    average_price = Decimal(holding["average_price"]) if holding else None
    cost_basis = (
        average_price * Decimal(holding["quantity"])
        if holding and average_price
        else None
    )

    averaging: dict[str, Any] | None = None
    loss_guard_floor = None
    tolerance_alerts: dict[str, Any] = {
        "portfolio_mdd_25pct": {
            "available": False,
            "reason": (
                "requires a historical portfolio equity curve; a point-in-time "
                "holdings snapshot cannot reconstruct max drawdown"
            ),
        }
    }
    underwater_sessions: dict[str, Any] = {
        "available": False,
        "reason": "not held",
    }
    manual_add_count = {
        "count": None,
        "available": False,
        "reason": (
            "requires broker order-history classification of manual vs. DCA-"
            "tranche fills; not derivable from read-only account/candle data "
            "(same read-only-data gap ROB-1230 P-1/P-2 hit)"
        ),
        "median_6": None,
    }

    if holding is not None and average_price is not None and cost_basis is not None:
        averaging = {
            f"k_{int(k * 100)}pct": averaging_math(
                cost_basis=cost_basis,
                average_price=average_price,
                current_price=close,
                k=k,
            )
            for k in AVERAGING_K_LEVELS
        }
        loss_guard_floor = average_price * LOSS_GUARD_MULTIPLIER
        position_alert_price = average_price * (Decimal(1) + POSITION_ALERT_PCT)
        tolerance_alerts["position_minus_30pct"] = {
            "threshold_price": position_alert_price,
            "distance_pct": support_distance(position_alert_price, close),
        }
        underwater_sessions = {
            "available": True,
            "sessions_within_lookback": _underwater_sessions_within_lookback(
                closes=closes, average_price=average_price
            ),
            "lookback_bars": len(closes),
            "note": (
                "arm-neutral post-fill close clock (D3 R1c semantics), capped "
                "by the fetched candle lookback window, not the true cycle "
                "start"
            ),
        }

    invalidation_line = _select_invalidation_line(
        watch_alerts_for_symbol, current_price=close
    )

    row: dict[str, Any] = {
        "symbol": symbol,
        "held": holding is not None,
        "insufficient_history": False,
        "bars_used": len(closes),
        "previous_close": close,
        "rsi": signal.rsi,
        "bollinger_bands": {
            "lower": signal.bb_lower,
            "middle": signal.bb_middle,
            "upper": signal.bb_upper,
        },
        "fib_window": {"low": signal.fib_window_low, "high": signal.fib_window_high},
        "A_buy_side": {
            "support_levels": [_cluster_view_row(c) for c in signal.support_clusters],
            "buy_l1": {
                "price": signal.buy_l1,
                "basis": "t_minus_1_close_x_0.97_tick_aligned",
            },
            "buy_l2": (
                {"price": signal.buy_l2, "basis": signal.buy_l2_source}
                if signal.buy_l2 is not None
                else None
            ),
            "nearest_support_distance_pct": nearest_support_distance_pct,
            "deep_band": deep_band,
            "averaging_math": averaging,
            "sizing_band": {
                "new_entry_notional_usd": US_NEW_ENTRY_NOTIONAL_USD,
                "new_entry_notional_usd_min": US_NEW_ENTRY_NOTIONAL_USD_MIN,
                "new_entry_notional_usd_max": US_NEW_ENTRY_NOTIONAL_USD_MAX,
                "account_mode": ALPACA_ACCOUNT_MODE,
            },
        },
        "B_sell_side": {
            "label": "SELL_SIDE_MODEL_MISMATCH",
            "resistance_levels": [
                _cluster_view_row(c) for c in signal.resistance_clusters_above_close
            ],
            "sell_r1": signal.sell_r1,
            "sell_r2": signal.sell_r2,
            "loss_guard_floor": loss_guard_floor,
        },
        "C_diagnostics": {
            "underwater_sessions": underwater_sessions,
            "manual_add_count": manual_add_count,
            "invalidation_line": invalidation_line,
            "tolerance_alerts": tolerance_alerts,
        },
        "D_context": {
            "market_cap": (
                Decimal(pool_row["market_cap"])
                if pool_row and pool_row.get("market_cap")
                else None
            ),
            "market_cap_source": pool_row.get("market_cap_source")
            if pool_row
            else None,
            "daily_turnover": (
                Decimal(pool_row["daily_turnover"])
                if pool_row and pool_row.get("daily_turnover")
                else None
            ),
            "is_common_stock": pool_row.get("is_common_stock") if pool_row else None,
            "exchange": pool_row.get("exchange") if pool_row else None,
            "in_filtered_snapshot_pool": pool_row is not None
            and _passes_liquidity_filter(pool_row),
        },
    }
    return row


def compute_policy_table(raw: RawInputs) -> dict[str, Any]:
    """Pure: build the full policy_table.v1 payload from RawInputs."""

    tick_table = build_us_equity_tick_table()

    holdings_by_symbol = {row["symbol"]: row for row in raw.holdings}
    alerts_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for alert in raw.watch_alerts:
        alerts_by_symbol.setdefault(alert["symbol"], []).append(alert)
    pool_by_symbol = {row["symbol"]: row for row in raw.universe_pool}
    filtered_symbols = {
        symbol
        for symbol, row in pool_by_symbol.items()
        if _passes_liquidity_filter(row)
    }

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    halted_suspect: list[dict[str, Any]] = []
    for symbol in raw.universe_symbols:
        bars = raw.candles.get(symbol, [])
        # ROB-1236: an inert recent series (consecutive zero-volume or
        # zero-variation sessions) is dropped from ``rows`` entirely rather
        # than emitted with signals computed off dead candles. Dropping is
        # fail-closed for every consumer of this payload and needs no change
        # on their side; the symbol stays visible under
        # ``universe.halted_suspect`` so the removal is auditable.
        halt = classify_ohlcv_rows(bars)
        if halt.suspected:
            halted_suspect.append(
                {
                    "symbol": symbol,
                    "held": symbol in holdings_by_symbol,
                    **halt.to_dict(),
                }
            )
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": "halted_suspect",
                    "bars_available": len(bars),
                }
            )
            continue
        closes = [Decimal(bar[0]) for bar in bars]
        highs = [Decimal(bar[1]) for bar in bars]
        lows = [Decimal(bar[2]) for bar in bars]
        row = _build_row(
            symbol=symbol,
            closes=closes,
            highs=highs,
            lows=lows,
            tick_table=tick_table,
            holding=holdings_by_symbol.get(symbol),
            watch_alerts_for_symbol=alerts_by_symbol.get(symbol, []),
            pool_row=pool_by_symbol.get(symbol),
        )
        rows.append(row)
        if row["insufficient_history"]:
            reason = (
                "no_candles_in_db"
                if row["bars_available"] == 0
                else "insufficient_history_lt_120_sessions"
            )
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": reason,
                    "bars_available": row["bars_available"],
                }
            )

    payload: dict[str, Any] = {
        "schema": "policy_table.v1",
        "market": MARKET,
        "generated_at": raw.as_of,
        "trust_labels": list(US_TRUST_LABELS),
        "config": {
            "quote_currency": QUOTE_CURRENCY,
            "candle_granularity": CANDLE_GRANULARITY,
            "candle_lookback_bars": CANDLE_LOOKBACK_BARS,
            "candle_source": (
                "db_daily_candles(us_candles_1d, partition=exchange from "
                "us_symbol_universe) — no live fetch"
            ),
            "tick_source": TICK_SOURCE,
            "account_mode": ALPACA_ACCOUNT_MODE,
            "b0x_labels": [
                "B0_UNVALIDATED",
                "SELL_SIDE_MODEL_MISMATCH",
                "FIDELITY_INCONCLUSIVE_COVERAGE",
                "CROSS_MARKET_TRANSFER_UNVALIDATED",
            ],
            "d3_constants": D3_CONSTANTS_ECHO,
            "deep_band_pct": {
                "lower": DEEP_BAND_LOWER_PCT,
                "upper": DEEP_BAND_UPPER_PCT,
            },
            "position_alert_pct": POSITION_ALERT_PCT,
            "loss_guard_multiplier": LOSS_GUARD_MULTIPLIER,
            "averaging_k_levels": list(AVERAGING_K_LEVELS),
            "new_entry_notional_usd": US_NEW_ENTRY_NOTIONAL_USD,
            "new_entry_notional_usd_min": US_NEW_ENTRY_NOTIONAL_USD_MIN,
            "new_entry_notional_usd_max": US_NEW_ENTRY_NOTIONAL_USD_MAX,
            "universe_filter": {
                "min_market_cap_usd": MIN_MARKET_CAP_USD,
                "min_turnover_usd": MIN_TURNOVER_USD,
                "require_is_common_stock_true": True,
                "market_cap_sources": [
                    "invest_screener_snapshots.market_cap (if non-null)",
                    f"market_valuation_snapshots source={US_VALUATION_MARKET_CAP_SOURCE}",
                ],
                "quality_floor_reused": (
                    "app.services.invest_view_model.us_quality_guards."
                    "US_MIN_MARKET_CAP_USD"
                ),
            },
            "market_cap_fill_stats": raw.market_cap_fill_stats,
            # Contract v1.1 §2-2 literal (US = 36h) — not a worker-chosen constant.
            **max_table_age_stamp(MARKET),
        },
        "universe": {
            "holdings": sorted(holdings_by_symbol.keys()),
            "watch": sorted(alerts_by_symbol.keys()),
            "snapshot_partition_date": raw.snapshot_partition_date,
            "snapshot_total_symbols": len(pool_by_symbol),
            "filter_passed_symbols": len(filtered_symbols),
            "attempted_symbols": len(raw.universe_symbols),
            "computed_symbols": len(raw.universe_symbols) - len(skipped),
            "skipped": skipped,
            # ROB-1236 — excluded from ``rows``, kept visible here. Suspicion
            # from inert bars, not a confirmed halt (no halt master feed).
            "halted_suspect": halted_suspect,
        },
        "market_context": {
            "snapshot_breadth": raw.snapshot_breadth,
        },
        "rows": rows,
    }
    return payload


# ---------------------------------------------------------------------------
# Human-readable summary — blend ranking is display-only (design doc §2).
# ---------------------------------------------------------------------------

BLEND_WEIGHT_RSI = 0.4
BLEND_WEIGHT_SUPPORT = 0.4
BLEND_WEIGHT_TURNOVER = 0.2


def _blend_rank(payload: dict[str, Any]) -> list[dict[str, Any]]:
    eligible = [row for row in payload["rows"] if not row["insufficient_history"]]

    def rsi_key(row: dict[str, Any]) -> float:
        rsi = row.get("rsi")
        return float(rsi) if rsi is not None else 100.0

    def support_key(row: dict[str, Any]) -> float:
        dist = row["A_buy_side"].get("nearest_support_distance_pct")
        return abs(float(dist)) if dist is not None else 1.0

    def turnover_key(row: dict[str, Any]) -> float:
        turnover = row.get("D_context", {}).get("daily_turnover")
        return -float(turnover) if turnover is not None else 0.0

    def rank_index(key_fn: Any) -> dict[str, int]:
        ordered = sorted(eligible, key=key_fn)
        return {row["symbol"]: i for i, row in enumerate(ordered)}

    rsi_rank = rank_index(rsi_key)
    support_rank = rank_index(support_key)
    turnover_rank = rank_index(turnover_key)

    def blend_score(row: dict[str, Any]) -> float:
        symbol = row["symbol"]
        return (
            BLEND_WEIGHT_RSI * rsi_rank[symbol]
            + BLEND_WEIGHT_SUPPORT * support_rank[symbol]
            + BLEND_WEIGHT_TURNOVER * turnover_rank[symbol]
        )

    return sorted(eligible, key=blend_score)


def render_summary_md(payload: dict[str, Any], *, top_n: int = 50) -> str:
    lines: list[str] = []
    lines.append(f"# Policy Table — {payload['market']} — {payload['generated_at']}")
    lines.append("")
    for label in payload["trust_labels"]:
        lines.append(f"> {label}")
    lines.append("")

    u = payload["universe"]
    lines.append(
        f"universe: snapshot_total={u['snapshot_total_symbols']} -> "
        f"filter_passed={u['filter_passed_symbols']} -> "
        f"attempted={u['attempted_symbols']} -> "
        f"computed={u['computed_symbols']} "
        f"(holdings={len(u['holdings'])}, watch={len(u['watch'])}, "
        f"snapshot_partition_date={u['snapshot_partition_date']})"
    )
    if u["skipped"]:
        by_reason: dict[str, int] = {}
        for s in u["skipped"]:
            by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
        reason_str = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items()))
        lines.append(f"skipped: {len(u['skipped'])} ({reason_str})")
    # ROB-1236 — name the excluded symbols; a silent drop of a real candidate
    # is the failure mode of this filter, so it has to be readable.
    for entry in u.get("halted_suspect") or []:
        lines.append(
            f"halted_suspect (excluded, NOT a confirmed halt): {entry['symbol']}"
            f"{' [held]' if entry.get('held') else ''} — "
            f"{entry['frozen_sessions']} inert sessions, "
            f"reasons={'+'.join(entry['reasons'])}"
        )
    fill = payload.get("config", {}).get("market_cap_fill_stats") or {}
    if fill:
        lines.append(
            "market_cap_fill: "
            f"snapshot_non_null={fill.get('snapshot_market_cap_non_null')} / "
            f"total={fill.get('snapshot_total')}; "
            f"yahoo_fallback_used={fill.get('valuation_yahoo_fallback_used')}; "
            f"missing={fill.get('market_cap_missing')}"
        )
    lines.append("")

    breadth = payload["market_context"].get("snapshot_breadth")
    if breadth and breadth.get("total"):
        lines.append(
            f"snapshot_breadth ({breadth.get('partition_date')}): "
            f"{breadth['advancers']}/{breadth['total']} advancers, "
            f"{breadth['decliners']}/{breadth['total']} decliners"
        )
    lines.append("")
    lines.append(
        f"blend ranking (display-only, weights: rsi={BLEND_WEIGHT_RSI} "
        f"support={BLEND_WEIGHT_SUPPORT} turnover={BLEND_WEIGHT_TURNOVER})"
    )
    lines.append("")

    ranked = _blend_rank(payload)
    lines.append(
        "| symbol | held | RSI | support_dist_pct | buy_l1 | buy_l2 | "
        "sell_r1 | resistance_mismatch |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    for row in ranked[:top_n]:
        buy = row["A_buy_side"]
        sell = row["B_sell_side"]
        buy_l2 = buy["buy_l2"]["price"] if buy["buy_l2"] else "-"
        dist = buy.get("nearest_support_distance_pct")
        dist_str = dist if dist is not None else "-"
        lines.append(
            f"| {row['symbol']} | {'Y' if row['held'] else ''} | {row['rsi']} | "
            f"{dist_str} | {buy['buy_l1']['price']} | {buy_l2} | "
            f"{sell['sell_r1'] or '-'} | {sell['label']} |"
        )
    lines.append("")
    lines.append(f"policy_table_hash: `{payload['stamps']['policy_table_hash']}`")
    lines.append(f"auto_trader_head: `{payload['stamps']['auto_trader_head']}`")
    return "\n".join(lines) + "\n"


__all__ = [
    "MARKET",
    "RawInputs",
    "fetch_raw_inputs",
    "compute_policy_table",
    "render_summary_md",
    "ALPACA_ACCOUNT_MODE",
]
