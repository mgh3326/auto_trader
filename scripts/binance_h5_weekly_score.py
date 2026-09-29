"""Offline H5 score and computed-control export; no broker imports or calls."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.binance_h5 import (
    BinanceH5NavSample,
    BinanceH5Opportunity,
    BinanceH5Signal,
)
from app.services.brokers.binance.h5.control import (
    ActualTrip,
    ControlUnavailable,
    Opportunity,
    build_control_ledger,
)
from app.services.brokers.binance.h5.scoring import NavSample, score_h5


def _parse_snapshot(
    payload: dict[str, Any],
) -> tuple[list[ActualTrip], list[Opportunity], list[NavSample]]:
    actual = [
        ActualTrip(
            signal_key=row["signal_key"],
            symbol=row["symbol"],
            side=row["side"],
            decision_ts=int(row["decision_ts"]),
            entry_notional_usdt=Decimal(row["entry_notional_usdt"]),
            opened_at=dt.datetime.fromisoformat(row["opened_at"]),
            closed_at=dt.datetime.fromisoformat(row["closed_at"]),
            gross_pnl_usdt=Decimal(row["gross_pnl_usdt"]),
            fees_usdt=Decimal(row["fees_usdt"]),
        )
        for row in payload["actual_trips"]
    ]
    grid = [
        Opportunity(
            symbol=row["symbol"],
            decision_ts=int(row["decision_ts"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            bid=Decimal(row["bid"]),
            ask=Decimal(row["ask"]),
        )
        for row in payload["opportunities"]
    ]
    nav = [
        NavSample(
            observed_at=dt.datetime.fromisoformat(row["observed_at"]),
            nav_usdt=Decimal(row["nav_usdt"]),
        )
        for row in payload["nav_samples"]
    ]
    return actual, grid, nav


async def _read_snapshot_from_db() -> tuple[
    list[ActualTrip], list[Opportunity], list[NavSample]
]:
    async with AsyncSessionLocal() as db:
        signals = (
            await db.scalars(
                select(BinanceH5Signal).where(BinanceH5Signal.state == "closed")
            )
        ).all()
        opportunities = (await db.scalars(select(BinanceH5Opportunity))).all()
        nav_rows = (await db.scalars(select(BinanceH5NavSample))).all()
    actual = [
        ActualTrip(
            signal_key=row.signal_key,
            symbol=row.symbol,
            side=row.side,
            decision_ts=row.decision_ts,
            entry_notional_usdt=row.entry_qty * row.entry_price,
            opened_at=row.entered_at,
            closed_at=row.exit_at,
            gross_pnl_usdt=row.realized_pnl_usdt,
            fees_usdt=row.fees_usdt,
        )
        for row in signals
        if row.entry_price is not None
        and row.entered_at is not None
        and row.exit_at is not None
    ]
    grid = [
        Opportunity(
            symbol=row.symbol,
            decision_ts=row.decision_ts,
            high=row.bar_high,
            low=row.bar_low,
            close=Decimal(row.bar_close_text),
            bid=row.bid,
            ask=row.ask,
        )
        for row in opportunities
    ]
    nav = [
        NavSample(observed_at=row.observed_at, nav_usdt=row.nav_usdt)
        for row in nav_rows
    ]
    return actual, grid, nav


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    raise TypeError(f"unserializable {type(value).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline H5 weekly score")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot-json", type=Path)
    source.add_argument("--read-db", action="store_true")
    parser.add_argument(
        "--t0", required=True, help="Operator-recorded first H5 runner start, ISO-8601"
    )
    parser.add_argument("--as-of", required=True, help="Score as-of, ISO-8601")
    parser.add_argument("--score-out", required=True, type=Path)
    parser.add_argument("--control-out", required=True, type=Path)
    args = parser.parse_args()
    if args.snapshot_json:
        actual, grid, nav = _parse_snapshot(json.loads(args.snapshot_json.read_text()))
    else:
        actual, grid, nav = asyncio.run(_read_snapshot_from_db())
    control = None
    control_error = None
    try:
        control = build_control_ledger(actual, grid)
    except ControlUnavailable as exc:
        control_error = str(exc)
    score = score_h5(
        actual=actual,
        control=control,
        nav_samples=nav,
        t0=dt.datetime.fromisoformat(args.t0),
        as_of=dt.datetime.fromisoformat(args.as_of),
    )
    args.control_out.write_text(
        json.dumps(
            {
                "seed": 84720260928,
                "control_error": control_error,
                "trips": [dataclasses.asdict(row) for row in control or []],
            },
            default=_json_default,
            indent=2,
        )
        + "\n"
    )
    args.score_out.write_text(
        json.dumps(dataclasses.asdict(score), default=_json_default, indent=2) + "\n"
    )
    print(score.label)


if __name__ == "__main__":
    main()
