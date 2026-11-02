"""Shared builders for the ROB-1470 sleeve tests (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.services.strategy_sleeves.book import SleeveFill, SleeveOrder

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
D = Decimal


def order(
    rung_id: int,
    *,
    proposal_pk: int | None = None,
    symbol: str = "AAA",
    side: str = "buy",
    state: str = "filled",
    quantity: str = "10",
    limit_price: str | None = "100",
    notional: str | None = None,
    rung_filled_qty: str | None = None,
    signal_key: str | None = None,
    signal_minutes: int = -60,
) -> SleeveOrder:
    return SleeveOrder(
        proposal_pk=proposal_pk if proposal_pk is not None else rung_id,
        rung_id=rung_id,
        symbol=symbol,
        side=side,
        state=state,
        quantity=D(quantity),
        limit_price=None if limit_price is None else D(limit_price),
        notional=None if notional is None else D(notional),
        rung_filled_qty=None if rung_filled_qty is None else D(rung_filled_qty),
        signal_key=signal_key if signal_key is not None else f"sig-{rung_id}",
        signal_at=NOW + timedelta(minutes=signal_minutes),
    )


def fill(
    ledger_id: int,
    rung_id: int,
    *,
    symbol: str = "AAA",
    side: str = "buy",
    quantity: str = "10",
    price: str = "100",
    fee: str = "0",
    minutes: int = 0,
) -> SleeveFill:
    return SleeveFill(
        ledger_id=ledger_id,
        rung_id=rung_id,
        symbol=symbol,
        side=side,
        quantity=D(quantity),
        price=D(price),
        fee=D(fee),
        filled_at=NOW + timedelta(minutes=minutes),
    )


def sleeve(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": 7,
        "sleeve_key": "us-near-market",
        "rule_id": "rob1458.us_near_market_entry",
        "account_mode": "kis_live",
        "market": "equity_us",
        "currency": "USD",
    }
    return SimpleNamespace(**{**values, **overrides})


def version_row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "sleeve_id": 7,
        "version": 2,
        "state": "live",
        "budget_cap": D("1000"),
        "loss_limit": D("100"),
        "prereg_metric": "cost-net result at H minus baseline",
        "prereg_min_effect_pct": D("1"),
        "prereg_horizon_sessions": 20,
        "prereg_max_duration_days": 90,
        "reason": "operator set amounts",
        "set_by": "operator",
        "set_at": NOW,
    }
    return SimpleNamespace(**{**values, **overrides})
