"""KR (KRX equities) adapter for ROB-1230 P-2 — policy_table.v1 rows.

Read-only. Universe selection is the ``invest_screener_snapshots`` table
(``market='kr'``) — the design doc is explicit that a live KRX market-data
scan (``screen_stocks``) is unavailable at the night-batch build time the
KRX session is closed, so the DB-cached snapshot partition is the mandatory
source, not a live screener call. Indicator inputs (fib/BB/RSI need >=120
daily bars) are read straight from the ``kr_candles_1d`` DB table via
``DailyCandlesRepository`` — also DB-only, no live KIS candle fetch — so
this adapter has no live-market dependency for the compute path at all.

The one live call this adapter makes is a **read-only** KIS domestic-account
query (holdings) via a minimal facade that composes only ``AccountClient``
(``app.services.brokers.kis.account``), never importing
``app.services.brokers.kis.client`` (the full ``KISClient`` facade, which
unconditionally imports ``DomesticOrderClient``/``OverseasOrderClient`` at
module scope) or any order/orders module. This keeps the P-1 "zero
order-tool import graph, zero broker mutation" bar intact for KR too.

Split into ``fetch_raw_inputs`` (network + DB I/O) and
``compute_policy_table`` (pure, deterministic given those inputs), same
contract as the crypto adapter, so a run's raw inputs can be dumped and
replayed to prove byte-identical output on identical input (ROB-1230
acceptance #2/#3).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.account import AccountClient
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.protocols import KISClientProtocol
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.halt_detection import classify_ohlcv_rows
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.invest_screener_snapshots.support_proximity_policy import (
    DEFAULT_MIN_MARKET_CAP_KRW,
    DEFAULT_MIN_TURNOVER_KRW,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.market_valuation_snapshots.normalized_market_cap import (
    load_normalized_kr_market_caps,
)
from research.kr_corpus.d3_engine.models import Position
from research.kr_corpus.d3_engine.policies import update_underwater_close
from research.kr_corpus.d3_engine.signals import support_distance
from scripts.policy_table.core.averaging import averaging_math
from scripts.policy_table.core.kr_tick import build_kr_krx_tick_table
from scripts.policy_table.core.max_table_age import max_table_age_stamp
from scripts.policy_table.core.signal_math import (
    D3_CONSTANTS_ECHO,
    FIB_WINDOW,
    InsufficientHistory,
    SymbolSignal,
    compute_symbol_signal,
)
from scripts.policy_table.core.trust_labels import TRUST_LABELS

MARKET = "kr"
SNAPSHOT_MARKET = "kr"  # invest_screener_snapshots.market
WATCH_MARKET = (
    "equity_kr"  # InvestmentWatchAlert.market (app/jobs/watch_market_data.py)
)
DB_MARKET_KEY = MarketKey.KR
DB_PARTITION = "KRX"  # app.mcp_server.tooling.market_data_indicators._cache_first_kr
QUOTE_CURRENCY = "KRW"
CANDLE_GRANULARITY = "1d"
CANDLE_LOOKBACK_BARS = (
    200  # >= FIB_WINDOW(120) with headroom, mirrors P-1 crypto choice
)
CANDLE_FETCH_CONCURRENCY = 8
DEEP_BAND_LOWER_PCT = Decimal(
    "-0.12"
)  # design doc §1-A "성문 정책" — shared with crypto
DEEP_BAND_UPPER_PCT = Decimal("-0.03")
POSITION_ALERT_PCT = Decimal(
    "-0.30"
)  # design doc §1-C "성문 정책" — shared with crypto
LOSS_GUARD_MULTIPLIER = Decimal(
    "1.01"
)  # design doc §1-B "코드 레일 미러" — shared with crypto
AVERAGING_K_LEVELS: tuple[Decimal, Decimal] = (Decimal("0.05"), Decimal("0.10"))
# design doc §7-1: "KR 30만(신규)" — new-entry sizing band, distinct from crypto's
# settings.upbit_buy_amount (no KR equivalent env setting exists).
KR_NEW_ENTRY_NOTIONAL_KRW = Decimal("300000")
# Reused, not reinvented: same market-cap/turnover floor already governing the
# support-proximity build over this same invest_screener_snapshots table
# (app/services/invest_screener_snapshots/support_proximity_policy.py).
MIN_MARKET_CAP_KRW = DEFAULT_MIN_MARKET_CAP_KRW
MIN_TURNOVER_KRW = DEFAULT_MIN_TURNOVER_KRW


# ---------------------------------------------------------------------------
# Minimal read-only KIS domestic-account facade.
# ---------------------------------------------------------------------------


class _ReadOnlyKISDomesticClient(BaseKISClient):
    """Live KIS domestic-account reads only — no order-tool imports.

    Composes only ``AccountClient`` (balance/holdings reads). Deliberately
    does not use ``app.services.brokers.kis.client.KISClient`` — that facade
    unconditionally imports ``DomesticOrderClient``/``OverseasOrderClient``
    at module scope, which would pull order-placement code into this
    adapter's import graph even though only a read is ever called.
    ``AccountClient`` only needs the auth/HTTP protocol ``BaseKISClient``
    already implements (see ``KISClientProtocol``), so no other composition
    is required.
    """

    def __init__(self) -> None:
        super().__init__()
        parent = cast(KISClientProtocol, cast(object, self))
        self._account = AccountClient(parent)

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return await self._account.fetch_my_stocks(is_mock=False, is_overseas=False)


# ---------------------------------------------------------------------------
# Raw inputs — dumpable/replayable for the determinism acceptance check.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawInputs:
    as_of: str  # ISO8601, captured once at fetch time
    holdings: list[dict[str, str]]
    watch_alerts: list[dict[str, Any]]
    universe_pool: list[dict[str, Any]]  # full invest_screener_snapshots(market='kr')
    snapshot_partition_date: str | None
    snapshot_breadth: dict[str, Any] | None
    universe_symbols: list[str]  # filter_passed ∪ holdings ∪ watch (attempted set)
    # symbol -> [[close, high, low, volume], ...] ascending. ROB-1236 appended
    # ``volume`` for halt detection; three-element rows from an older replay
    # dump still classify (zero-variation rule only), so existing dumps replay.
    candles: dict[str, list[list[str]]]

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
        )


def _passes_liquidity_filter(row: dict[str, Any]) -> bool:
    market_cap = row.get("market_cap")
    turnover = row.get("daily_turnover")
    if not market_cap or not turnover:
        return False
    return (
        Decimal(market_cap) >= MIN_MARKET_CAP_KRW
        and Decimal(turnover) >= MIN_TURNOVER_KRW
    )


async def _fetch_holdings_raw() -> list[dict[str, str]]:
    client = _ReadOnlyKISDomesticClient()
    stocks = await client.fetch_my_stocks()
    rows: list[dict[str, str]] = []
    for stock in stocks:
        symbol = str(stock.get("pdno", "")).strip()
        if not symbol:
            continue
        quantity = Decimal(str(stock.get("hldg_qty") or "0"))
        if quantity <= 0:
            continue
        average_price = Decimal(str(stock.get("pchs_avg_pric") or "0"))
        rows.append(
            {
                "symbol": symbol,
                "quantity": str(quantity),
                "average_price": str(average_price),
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


async def _fetch_universe_pool_raw() -> tuple[
    list[dict[str, Any]], str | None, dict[str, Any] | None
]:
    async with AsyncSessionLocal() as db:
        repo = InvestScreenerSnapshotsRepository(db)
        rows = await repo.list_candidate_pool(market=SNAPSHOT_MARKET, limit=None)
        breadth = await repo.breadth(market=SNAPSHOT_MARKET)
        # invest_screener_snapshots.market_cap is unpopulated for every KR row
        # (verified empirically against prod 2026-08-07: 3,925/3,925 NULL) —
        # the trusted KRW-normalized source is market_valuation_snapshots
        # (naver_finance), the same one support_proximity_builder.py already
        # uses for this exact quality-floor purpose. Reused, not reinvented.
        market_caps = await load_normalized_kr_market_caps(
            db, [row.symbol for row in rows]
        )

    pool = [
        {
            "symbol": row.symbol,
            "latest_close": str(row.latest_close),
            "market_cap": (
                str(market_caps[row.symbol].value)
                if row.symbol in market_caps
                else None
            ),
            "market_cap_source": (
                market_caps[row.symbol].source if row.symbol in market_caps else None
            ),
            "daily_turnover": (
                str(row.daily_turnover) if row.daily_turnover is not None else None
            ),
            "daily_volume": (
                str(row.daily_volume) if row.daily_volume is not None else None
            ),
            "snapshot_date": row.snapshot_date.isoformat(),
        }
        for row in rows
    ]
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
    return pool, partition_date, breadth_dict


async def _fetch_kr_daily_candles_raw(symbol: str) -> list[list[str]] | None:
    async with AsyncSessionLocal() as db:
        repo = DailyCandlesRepository(session=db)
        rows = await repo.fetch_recent(
            market=DB_MARKET_KEY,
            symbol=symbol,
            partition=DB_PARTITION,
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
    pool, partition_date, breadth = await _fetch_universe_pool_raw()
    watch_alerts = await _fetch_watch_alerts_raw(as_of=as_of)
    holdings = await _fetch_holdings_raw()

    filtered_symbols = {row["symbol"] for row in pool if _passes_liquidity_filter(row)}
    holding_symbols = {row["symbol"] for row in holdings}
    watch_symbols = {row["symbol"] for row in watch_alerts}
    universe_symbols = sorted(filtered_symbols | holding_symbols | watch_symbols)

    semaphore = asyncio.Semaphore(CANDLE_FETCH_CONCURRENCY)

    async def _bounded_fetch(symbol: str) -> tuple[str, list[list[str]] | None]:
        async with semaphore:
            return symbol, await _fetch_kr_daily_candles_raw(symbol)

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
    )


# ---------------------------------------------------------------------------
# Pure computation — deterministic given RawInputs.
#
# ``_cluster_view_row``/``_select_invalidation_line``/
# ``_underwater_sessions_within_lookback`` intentionally mirror
# ``scripts/policy_table/adapters/crypto.py``'s identical helpers byte-for-
# byte (same market-agnostic logic, no crypto-specific behavior). P-2's brief
# is explicit that P-1 must not be touched/reimplemented, so this duplicates
# rather than extracting a shared core module — a candidate DRY pass for a
# future PR, not this one.
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
            "(same read-only-data gap ROB-1230 P-1 hit for crypto)"
        ),
        # Crypto's median_6 is an Upbit-specific empirical constant (08-08
        # oper-coin session); no equivalent KR reference has been measured.
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
            "sizing_band": {"new_entry_notional_krw": KR_NEW_ENTRY_NOTIONAL_KRW},
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
            "in_filtered_snapshot_pool": pool_row is not None
            and _passes_liquidity_filter(pool_row),
        },
    }
    return row


def compute_policy_table(raw: RawInputs) -> dict[str, Any]:
    """Pure: build the full policy_table.v1 payload from RawInputs."""

    tick_table = build_kr_krx_tick_table()

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
        # ROB-1236: a symbol whose recent bars are inert (consecutive
        # zero-volume or zero-variation sessions) is dropped from ``rows``
        # entirely rather than emitted with signals computed off dead candles.
        # Dropping is fail-closed for every downstream consumer of this payload
        # (B0-X reads these rows) and needs no change on their side; the
        # symbol stays visible under ``universe.halted_suspect`` so the removal
        # is auditable and can be overruled.
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
        "trust_labels": list(TRUST_LABELS),
        "config": {
            "quote_currency": QUOTE_CURRENCY,
            "candle_granularity": CANDLE_GRANULARITY,
            "candle_lookback_bars": CANDLE_LOOKBACK_BARS,
            "candle_source": "db_daily_candles(kr_candles_1d, partition=KRX) — no live fetch",
            "tick_source": "app.mcp_server.tick_size.get_tick_size_kr (runtime, not D3 frozen)",
            "d3_constants": D3_CONSTANTS_ECHO,
            "deep_band_pct": {
                "lower": DEEP_BAND_LOWER_PCT,
                "upper": DEEP_BAND_UPPER_PCT,
            },
            "position_alert_pct": POSITION_ALERT_PCT,
            "loss_guard_multiplier": LOSS_GUARD_MULTIPLIER,
            "averaging_k_levels": list(AVERAGING_K_LEVELS),
            "new_entry_notional_krw": KR_NEW_ENTRY_NOTIONAL_KRW,
            "universe_filter": {
                "min_market_cap_krw": MIN_MARKET_CAP_KRW,
                "min_turnover_krw": MIN_TURNOVER_KRW,
                "source": (
                    "app.services.invest_screener_snapshots.support_proximity_policy"
                    " (reused, not reinvented)"
                ),
            },
            # Contract v1.1 §2-2 literal (KR = 36h) — not a worker-chosen constant.
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
            # from inert bars, not a confirmed halt (no KRX halt master).
            "halted_suspect": halted_suspect,
        },
        "market_context": {
            "snapshot_breadth": raw.snapshot_breadth,
        },
        "rows": rows,
    }
    return payload


# ---------------------------------------------------------------------------
# Human-readable summary — blend ranking is display-only (design doc §2:
# "표 자체엔 무영향 — 랭킹은 표시 순서일 뿐"), never affects the JSON rows.
# ---------------------------------------------------------------------------

# Initial weights (pilot — adjust during use, per design doc §2). Support
# proximity and RSI oversold are the two core buy-timing signals (weighted
# equally); trade value is a secondary liquidity sanity check.
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
]
