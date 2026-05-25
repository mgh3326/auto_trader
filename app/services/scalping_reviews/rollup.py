"""ROB-315 Phase 1 — pure daily rollup of ``scalp_trade_analytics`` rows.

No DB, no network. Aggregates a day's worth of demo scalping round-trip rows
into the metrics the review draft stores. Rollup semantics (locked in the
ROB-315 issue):

* ``trade_count`` counts **fill-proven** round-trips only (``entry_price`` not
  NULL). Rows with no derivable fill price count toward ``anomaly_count`` and
  never into the success metrics — they are a data-quality signal, not a trade.
* Telemetry averages are computed over non-NULL values only and reported as
  ``None`` ("n/a") when no row carries the value — never a misleading ``0``.
* ``net_return_bps`` is capital-weighted: ``sum(net_pnl) / sum(entry_notional)``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

_BPS = Decimal("10000")


class _AnalyticsRow(Protocol):
    open_client_order_id: str
    entry_price: Decimal | None
    entry_notional_usdt: Decimal | None
    net_pnl_usdt: Decimal | None
    gross_pnl_usdt: Decimal | None
    entry_slippage_bps: Decimal | None
    entry_spread_bps: Decimal | None
    mae_bps: Decimal | None
    mfe_bps: Decimal | None
    holding_seconds: int | None
    exit_reason: str | None


@dataclass(frozen=True)
class RollupResult:
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    anomaly_count: int = 0
    gross_pnl_usdt: Decimal | None = None
    net_pnl_usdt: Decimal | None = None
    net_return_bps: Decimal | None = None
    avg_slippage_bps: Decimal | None = None
    avg_spread_bps: Decimal | None = None
    avg_mae_bps: Decimal | None = None
    avg_mfe_bps: Decimal | None = None
    avg_holding_seconds: int | None = None
    exit_reason_counts: dict[str, int] = field(default_factory=dict)
    source_payload: dict[str, Any] = field(default_factory=dict)


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _sum_or_none(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0"))


def build_rollup(rows: list[_AnalyticsRow]) -> RollupResult:
    """Aggregate one day's analytics rows into a ``RollupResult``."""
    fill_proven = [r for r in rows if r.entry_price is not None]
    anomaly = [r for r in rows if r.entry_price is None]

    net_pnls = [r.net_pnl_usdt for r in fill_proven if r.net_pnl_usdt is not None]
    gross_pnls = [r.gross_pnl_usdt for r in fill_proven if r.gross_pnl_usdt is not None]
    notionals = [
        r.entry_notional_usdt
        for r in fill_proven
        if r.net_pnl_usdt is not None and r.entry_notional_usdt is not None
    ]

    net_total = _sum_or_none(net_pnls)
    notional_total = sum(notionals, Decimal("0"))
    net_return_bps = (
        net_total / notional_total * _BPS
        if net_total is not None and notional_total > 0
        else None
    )

    exit_counts: Counter[str] = Counter(
        r.exit_reason for r in rows if r.exit_reason is not None
    )

    return RollupResult(
        trade_count=len(fill_proven),
        win_count=sum(1 for v in net_pnls if v > 0),
        loss_count=sum(1 for v in net_pnls if v < 0),
        anomaly_count=len(anomaly),
        gross_pnl_usdt=_sum_or_none(gross_pnls),
        net_pnl_usdt=net_total,
        net_return_bps=net_return_bps,
        avg_slippage_bps=_mean(
            [
                r.entry_slippage_bps
                for r in fill_proven
                if r.entry_slippage_bps is not None
            ]
        ),
        avg_spread_bps=_mean(
            [r.entry_spread_bps for r in fill_proven if r.entry_spread_bps is not None]
        ),
        avg_mae_bps=_mean([r.mae_bps for r in fill_proven if r.mae_bps is not None]),
        avg_mfe_bps=_mean([r.mfe_bps for r in fill_proven if r.mfe_bps is not None]),
        avg_holding_seconds=(
            int(
                _mean(
                    [
                        Decimal(r.holding_seconds)
                        for r in fill_proven
                        if r.holding_seconds is not None
                    ]
                )
            )
            if any(r.holding_seconds is not None for r in fill_proven)
            else None
        ),
        exit_reason_counts=dict(exit_counts),
        source_payload={
            "row_count": len(rows),
            "fill_proven_count": len(fill_proven),
            "anomaly_count": len(anomaly),
            "open_client_order_ids": [r.open_client_order_id for r in rows],
        },
    )
