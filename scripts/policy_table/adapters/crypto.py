"""Crypto (Upbit) adapter for ROB-1230 P-1 — policy_table.v1 rows.

Read-only. Imports only GET-shaped functions from
``app.services.brokers.upbit.client`` (accounts/ticker/candles) and the
active-alerts read path of the investment-reports repository. Deliberately
does **not** import ``app.services.brokers.upbit.orders`` — none of its
order-placement/cancel functions are needed here, and P-1's acceptance gate
requires a zero-order-tool import graph.

Split into ``fetch_raw_inputs`` (network + DB I/O) and
``compute_policy_table`` (pure, deterministic given those inputs) so a run's
raw inputs can be dumped and replayed to prove byte-identical output on
identical input (ROB-1230 acceptance #3).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.brokers.upbit.client import (
    fetch_krw_orderable_balance,
    fetch_my_coins,
    fetch_ohlcv,
    fetch_top_traded_coins,
    parse_upbit_account_row,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from research.kr_corpus.d3_engine.models import Position
from research.kr_corpus.d3_engine.policies import update_underwater_close
from research.kr_corpus.d3_engine.signals import support_distance
from scripts.policy_table.core.averaging import averaging_math
from scripts.policy_table.core.signal_math import (
    D3_CONSTANTS_ECHO,
    FIB_WINDOW,
    InsufficientHistory,
    SymbolSignal,
    compute_symbol_signal,
)
from scripts.policy_table.core.trust_labels import TRUST_LABELS
from scripts.policy_table.core.upbit_tick import build_upbit_krw_tick_table

MARKET = "crypto"
QUOTE_CURRENCY = "KRW"
CANDLE_PERIOD = "4h"
CANDLE_LOOKBACK_BARS = 200  # Upbit per-call max; FIB_WINDOW(120) needs >=120 real bars
DEEP_BAND_LOWER_PCT = Decimal("-0.12")
DEEP_BAND_UPPER_PCT = Decimal("-0.03")
POSITION_ALERT_PCT = Decimal("-0.30")
LOSS_GUARD_MULTIPLIER = Decimal("1.01")
AVERAGING_K_LEVELS: tuple[Decimal, Decimal] = (Decimal("0.05"), Decimal("0.10"))
# 08-08 oper-coin session §5-1 "운영자 실측 거울(중앙값 6회)" — cited constant, not derived.
MANUAL_ADD_MEDIAN_REFERENCE = 6
RECOVERY_GATE_SYMBOL = "KRW-BTC"
DEFAULT_TOP_N = 30
CANDLE_FETCH_CONCURRENCY = 5


# ---------------------------------------------------------------------------
# Raw inputs — dumpable/replayable for the determinism acceptance check.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawInputs:
    as_of: str  # ISO8601, captured once at fetch time
    holdings: list[dict[str, str]]
    watch_alerts: list[dict[str, Any]]
    top_traded: list[dict[str, str]]
    orderable_krw: str
    candles: dict[str, list[list[str]]]  # symbol -> [[close, high, low], ...] ascending

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "holdings": self.holdings,
            "watch_alerts": self.watch_alerts,
            "top_traded": self.top_traded,
            "orderable_krw": self.orderable_krw,
            "candles": self.candles,
        }

    @classmethod
    def from_jsonable(cls, payload: dict[str, Any]) -> RawInputs:
        return cls(
            as_of=payload["as_of"],
            holdings=payload["holdings"],
            watch_alerts=payload["watch_alerts"],
            top_traded=payload["top_traded"],
            orderable_krw=payload["orderable_krw"],
            candles=payload["candles"],
        )


async def _fetch_holdings_raw() -> list[dict[str, str]]:
    coins = await fetch_my_coins()
    rows: list[dict[str, str]] = []
    for coin in coins:
        currency = str(coin.get("currency", "")).upper().strip()
        if not currency or currency == "KRW":
            continue
        parsed = parse_upbit_account_row(coin)
        quantity = Decimal(str(parsed["total_quantity"]))
        if quantity <= 0:
            continue
        quote_currency = str(coin.get("unit_currency", "KRW")).upper().strip() or "KRW"
        rows.append(
            {
                "symbol": f"{quote_currency}-{currency}",
                "quantity": str(quantity),
                "average_price": str(Decimal(str(parsed["avg_buy_price"]))),
            }
        )
    return rows


async def _fetch_watch_alerts_raw(*, as_of: datetime) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        alerts = await repo.list_active_alerts(
            market=MARKET, valid_at=as_of, limit=250
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


async def _fetch_candles_raw(symbol: str) -> list[list[str]] | None:
    frame = await fetch_ohlcv(
        market=symbol, days=CANDLE_LOOKBACK_BARS, period=CANDLE_PERIOD
    )
    if frame is None or frame.empty:
        return None
    rows: list[list[str]] = []
    for _, bar in frame.iterrows():
        rows.append(
            [
                str(Decimal(str(bar["close"]))),
                str(Decimal(str(bar["high"]))),
                str(Decimal(str(bar["low"]))),
            ]
        )
    return rows


async def fetch_raw_inputs(*, top_n: int = DEFAULT_TOP_N) -> RawInputs:
    """Do all network/DB I/O once; return a JSON-safe, replayable snapshot."""

    as_of = datetime.now(UTC)
    holdings, top_traded, orderable_krw = await asyncio.gather(
        _fetch_holdings_raw(),
        fetch_top_traded_coins(fiat=QUOTE_CURRENCY),
        fetch_krw_orderable_balance(),
    )
    watch_alerts = await _fetch_watch_alerts_raw(as_of=as_of)

    holding_symbols = {row["symbol"] for row in holdings}
    watch_symbols = {row["symbol"] for row in watch_alerts}
    top_n_symbols = {
        str(row.get("market"))
        for row in top_traded[: max(top_n, 0)]
        if row.get("market")
    }
    universe = sorted(holding_symbols | watch_symbols | top_n_symbols)
    # RECOVERY_GATE_SYMBOL is always fetched — market_context needs it even
    # when BTC is not itself in the universe.
    if RECOVERY_GATE_SYMBOL not in universe:
        universe = sorted([*universe, RECOVERY_GATE_SYMBOL])

    semaphore = asyncio.Semaphore(CANDLE_FETCH_CONCURRENCY)

    async def _bounded_fetch(symbol: str) -> tuple[str, list[list[str]] | None]:
        async with semaphore:
            try:
                return symbol, await _fetch_candles_raw(symbol)
            except Exception:  # noqa: BLE001 — recorded per-symbol, not fatal
                return symbol, None

    fetched = await asyncio.gather(*(_bounded_fetch(sym) for sym in universe))
    candles = {symbol: rows for symbol, rows in fetched if rows is not None}

    top_traded_str = [
        {
            "market": str(row.get("market", "")),
            "trade_price": str(row.get("trade_price", "")),
            "acc_trade_price_24h": str(row.get("acc_trade_price_24h", "")),
            "signed_change_rate": str(row.get("signed_change_rate", "")),
        }
        for row in top_traded
    ]

    return RawInputs(
        as_of=as_of.isoformat(),
        holdings=holdings,
        watch_alerts=watch_alerts,
        top_traded=top_traded_str,
        orderable_krw=str(Decimal(str(orderable_krw))),
        candles=candles,
    )


# ---------------------------------------------------------------------------
# Pure computation — deterministic given RawInputs.
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
    breadth_rank: int | None,
    breadth_row: dict[str, Any] | None,
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

    average_price = Decimal(holding["average_price"]) if holding else None
    cost_basis = (
        average_price * Decimal(holding["quantity"]) if holding and average_price else None
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
            "in P-1 (08-08 oper-coin session hit the same gap via a blocked "
            "get_order_history call)"
        ),
        "median_6": MANUAL_ADD_MEDIAN_REFERENCE,
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
            "deep_band": deep_band,
            "averaging_math": averaging,
            "sizing_band": {
                "new_entry_notional_krw": Decimal(str(settings.upbit_buy_amount)),
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
            "alt_breadth_rank": breadth_rank,
            "signed_change_rate_24h": (
                Decimal(breadth_row["signed_change_rate"])
                if breadth_row and breadth_row.get("signed_change_rate")
                else None
            ),
        },
    }
    return row


def compute_policy_table(
    raw: RawInputs, *, top_n: int = DEFAULT_TOP_N
) -> dict[str, Any]:
    """Pure: build the full policy_table.v1 payload from RawInputs."""

    tick_table = build_upbit_krw_tick_table()

    holdings_by_symbol = {row["symbol"]: row for row in raw.holdings}
    alerts_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for alert in raw.watch_alerts:
        alerts_by_symbol.setdefault(alert["symbol"], []).append(alert)

    top_traded_by_symbol = {row["market"]: row for row in raw.top_traded}
    top_n_symbols = [row["market"] for row in raw.top_traded[: max(top_n, 0)]]
    breadth_rank_by_symbol = {
        row["market"]: index + 1 for index, row in enumerate(raw.top_traded)
    }

    universe_symbols = sorted(raw.candles.keys())
    positive_count = 0
    negative_count = 0
    for row in raw.top_traded:
        rate = row.get("signed_change_rate")
        if not rate:
            continue
        value = Decimal(rate)
        if value > 0:
            positive_count += 1
        elif value < 0:
            negative_count += 1
    swept_count = len(raw.top_traded)
    positive_pct = (
        (Decimal(positive_count) / Decimal(swept_count)) if swept_count else None
    )

    rows: list[dict[str, Any]] = []
    for symbol in universe_symbols:
        bars = raw.candles[symbol]
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
            breadth_rank=breadth_rank_by_symbol.get(symbol),
            breadth_row=top_traded_by_symbol.get(symbol),
        )
        rows.append(row)

    recovery_gate_row = next(
        (row for row in rows if row["symbol"] == RECOVERY_GATE_SYMBOL), None
    )
    recovery_gate_material: dict[str, Any]
    if recovery_gate_row is not None and not recovery_gate_row["insufficient_history"]:
        recovery_gate_material = {
            "available": True,
            "symbol": RECOVERY_GATE_SYMBOL,
            "rsi": recovery_gate_row["rsi"],
            "previous_close": recovery_gate_row["previous_close"],
            "bollinger_bands": recovery_gate_row["bollinger_bands"],
            "underwater_sessions": recovery_gate_row["C_diagnostics"][
                "underwater_sessions"
            ],
        }
    else:
        recovery_gate_material = {
            "available": False,
            "reason": "KRW-BTC candles unavailable or insufficient history",
        }

    orderable_krw = Decimal(raw.orderable_krw)
    min_balance = Decimal(str(settings.upbit_min_krw_balance))
    headroom = orderable_krw - min_balance
    if headroom < 0:
        headroom = Decimal(0)

    payload: dict[str, Any] = {
        "schema": "policy_table.v1",
        "market": MARKET,
        "generated_at": raw.as_of,
        "trust_labels": list(TRUST_LABELS),
        "config": {
            "quote_currency": QUOTE_CURRENCY,
            "candle_period": CANDLE_PERIOD,
            "candle_lookback_bars": CANDLE_LOOKBACK_BARS,
            "universe_top_n": top_n,
            "d3_constants": D3_CONSTANTS_ECHO,
            "deep_band_pct": {
                "lower": DEEP_BAND_LOWER_PCT,
                "upper": DEEP_BAND_UPPER_PCT,
            },
            "position_alert_pct": POSITION_ALERT_PCT,
            "loss_guard_multiplier": LOSS_GUARD_MULTIPLIER,
            "averaging_k_levels": list(AVERAGING_K_LEVELS),
            "manual_add_median_reference": MANUAL_ADD_MEDIAN_REFERENCE,
        },
        "universe": {
            "holdings": sorted(holdings_by_symbol.keys()),
            "watch": sorted(alerts_by_symbol.keys()),
            "top_n": top_n_symbols,
            "total_symbols": len(universe_symbols),
            "symbols_with_insufficient_history": sorted(
                row["symbol"] for row in rows if row["insufficient_history"]
            ),
        },
        "market_context": {
            "alt_breadth": {
                "swept_market_count": swept_count,
                "positive_24h_count": positive_count,
                "negative_24h_count": negative_count,
                "positive_pct": positive_pct,
                "top_n_by_trade_value_24h": top_n_symbols,
            },
            "recovery_gate_material": recovery_gate_material,
        },
        "rows": rows,
        "sizing": {
            "new_entry_notional_krw": Decimal(str(settings.upbit_buy_amount)),
            "orderable_krw": orderable_krw,
            "min_krw_balance_floor": min_balance,
            "headroom_krw": headroom,
        },
    }
    return payload


__all__ = [
    "MARKET",
    "RawInputs",
    "fetch_raw_inputs",
    "compute_policy_table",
    "DEFAULT_TOP_N",
]
