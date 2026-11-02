"""Manual ROB-1474 crypto episodes with durable one-send claims."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.services.mock_strategy.crypto_brokers import (
    CryptoBroker,
    FillEvidence,
    require_enabled,
)
from app.services.mock_strategy.crypto_orders import (
    BelowVenueMinimum,
    client_id,
    positive,
    strategy_id,
)
from app.services.mock_strategy.crypto_signals import BASELINE, HORIZONS
from app.services.mock_strategy.recorder import OrderIntent
from research.stock_selection.dataset import decision_at


def _entry_status(evidence: FillEvidence) -> str:
    if evidence.terminal:
        if evidence.filled_qty:
            return "filled"
        return "rejected" if evidence.status == "REJECTED" else "canceled"
    return "partially_filled" if evidence.filled_qty else "accepted"


async def _save(recorder: Any, order: Any, evidence: FillEvidence) -> None:
    await recorder.update(
        order.client_order_id,
        status=_entry_status(evidence),
        broker_order_id=evidence.broker_order_id,
        entry_price=evidence.price,
        opened_at=datetime.now(UTC) if evidence.filled_qty else None,
        meta={
            "filled_qty": str(evidence.filled_qty),
            "entry_terminal": evidence.terminal,
            "broker_status": evidence.status,
        },
    )


async def _send(
    recorder: Any, broker: CryptoBroker, order: Any, *, confirm: bool
) -> dict:
    require_enabled(order.venue, confirm=confirm)
    if not await recorder.claim(order.client_order_id):
        return {
            "client_order_id": order.client_order_id,
            "status": "replayed",
            "submitted": False,
        }
    try:
        evidence = await broker.submit(order, confirm=confirm)
        await _save(recorder, order, evidence)
    except BaseException:
        # A committed sending fence survives even if this status update fails.
        # No exception, timeout or missing lookup grants permission to resend.
        await recorder.update(order.client_order_id, status="uncertain")
        raise
    return {
        "client_order_id": order.client_order_id,
        "status": _entry_status(evidence),
        "submitted": True,
    }


def _output(order: OrderIntent) -> dict:
    return {
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "qty": str(order.qty),
        "notional": str(order.notional),
        "strategy_id": order.strategy_id,
        "venue": order.venue,
        "exit_due_at": order.exit_due_at.isoformat() if order.exit_due_at else None,
    }


async def run_lists(
    payload: dict,
    *,
    venue: str,
    asof: date,
    broker: CryptoBroker,
    recorder: Any = None,
    horizon: int = 3,
    notional: Decimal = Decimal("1000"),
    dry_run: bool = True,
    confirm: bool = False,
    now: datetime | None = None,
) -> dict:
    if (
        horizon not in HORIZONS
        or payload.get("model") != BASELINE
        or payload.get("panel_venue") != "binance_usdt_spot"
        or payload.get("asof") != asof.isoformat()
    ):
        raise ValueError("frozen Binance signal contract mismatch")
    positive(notional)
    decision = decision_at("crypto", asof).to_pydatetime()
    now = now or datetime.now(UTC)
    if not dry_run:
        require_enabled(venue, confirm=confirm)
        if not decision <= now < decision + timedelta(days=1):
            raise ValueError("only the latest completed UTC candle may open orders")
    direction = "down" if venue == "binance_futures_demo" else "up"
    names = payload["horizons"][str(horizon)][direction]
    if len(names) != 10 or len({n["symbol"] for n in names}) != 10:
        raise ValueError("exactly ten distinct signals required")
    sid = strategy_id(horizon)
    symbols = [n["symbol"] for n in names]
    batch = (
        []
        if dry_run
        else [
            row
            for row in await recorder.list_orders(
                venue=venue, account_ref=broker.account_ref
            )
            if row.strategy_id == sid
            and row.side in {"buy", "short"}
            and row.meta.get("asof") == asof.isoformat()
        ]
    )
    if batch and any(
        row.meta.get("signal_symbols") != symbols
        or row.meta.get("input_prefix_sha256") != payload["input_prefix_sha256"]
        or row.meta.get("target_notional") != str(notional)
        for row in batch
    ):
        raise ValueError("dated strategy batch conflict")
    plans, skipped, existing = [], [], []
    for name in names:
        symbol = name["symbol"]
        if (
            not isinstance(symbol, str)
            or not symbol.isascii()
            or not symbol.isalnum()
            or not symbol.endswith("USDT")
        ):
            raise ValueError("invalid Binance USDT symbol")
        cid = client_id(sid, venue, broker.account_ref, asof.isoformat(), symbol)
        if batch and symbol not in batch[0].meta["planned_symbols"]:
            skipped.append(
                {"symbol": symbol, "reason": "original_batch_venue_excluded"}
            )
            continue
        if not dry_run:
            prior = await recorder.get(cid)
            if prior is not None:
                if prior.meta.get("input_prefix_sha256") != payload[
                    "input_prefix_sha256"
                ] or prior.meta.get("target_notional") != str(notional):
                    raise ValueError("dated strategy intent conflict")
                existing.append(prior)
                continue
        try:
            rules, price = await broker.rules(symbol), await broker.price(symbol)
            qty = rules.floor(notional / price, price)
        except (ValueError, StopAsyncIteration, StopIteration):
            skipped.append(
                {"symbol": symbol, "reason": "venue_sizing_or_listing_unavailable"}
            )
            continue
        plans.append(
            OrderIntent(
                strategy_id=sid,
                venue=venue,
                account_ref=broker.account_ref,
                episode_id=cid,
                symbol=symbol,
                side="short" if direction == "down" else "buy",
                qty=qty,
                notional=qty * price,
                client_order_id=cid,
                exit_due_at=decision + timedelta(days=horizon),
                meta={
                    "asof": asof.isoformat(),
                    "baseline": BASELINE,
                    "horizon": horizon,
                    "version": "v1",
                    "panel_venue": "binance_usdt_spot",
                    "target_notional": str(notional),
                    "input_prefix_sha256": payload["input_prefix_sha256"],
                },
            )
        )
    if dry_run:
        return {
            "dry_run": True,
            "orders": [_output(p) for p in plans],
            "skipped": skipped,
        }
    planned_symbols = [p.symbol for p in plans] + [r.symbol for r in existing]
    plans = [
        replace(
            p,
            meta={
                **p.meta,
                "signal_symbols": symbols,
                "planned_symbols": planned_symbols,
            },
        )
        for p in plans
    ]
    # Existing immutable intent mismatches reject the whole batch before POST.
    rows = existing + await recorder.record_many(plans)
    outcomes = [await _send(recorder, broker, row, confirm=confirm) for row in rows]
    return {"dry_run": False, "orders": outcomes, "skipped": skipped}


async def close_due(
    *,
    venue: str,
    asof: datetime,
    broker: CryptoBroker,
    recorder: Any,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict:
    if not dry_run:
        require_enabled(venue, confirm=confirm)
        if asof > datetime.now(UTC):
            raise ValueError("future close cutoff refused")
    entries = await recorder.list_due(
        venue=venue, asof=asof, account_ref=broker.account_ref
    )
    results = []
    for entry in entries:
        if not entry.strategy_id.startswith("rob1474.crypto.") or entry.side not in {
            "buy",
            "short",
        }:
            continue
        if entry.status == "planned":
            # Planned alone proves no fill; never send an overdue entry here.
            results.append(
                {"client_order_id": entry.client_order_id, "status": "entry_not_sent"}
            )
            continue
        evidence = await broker.evidence(entry)
        if not dry_run:
            await _save(recorder, entry, evidence)
        if not evidence.terminal or evidence.filled_qty <= 0:
            results.append(
                {
                    "client_order_id": entry.client_order_id,
                    "status": "entry_fill_pending",
                }
            )
            continue
        all_rows = await recorder.list_orders(
            venue=venue, account_ref=broker.account_ref
        )
        legs = [
            r
            for r in all_rows
            if r.episode_id == entry.episode_id and r.side in {"sell", "cover"}
        ]
        sold, proceeds, pending = Decimal("0"), Decimal("0"), None
        in_flight = []
        planned_count = 0
        for leg in legs:
            if leg.status == "planned":
                planned_count += 1
                pending = leg
                continue
            proof = await broker.evidence(leg)
            if not dry_run and leg.status != "closed":
                await _save(recorder, leg, proof)
            sold += proof.filled_qty
            if proof.filled_qty:
                proceeds += proof.filled_qty * positive(proof.price)
            if not proof.terminal:
                in_flight.append(leg)
        if planned_count > 1:
            raise ValueError("multiple planned close legs require review")
        held = (
            evidence.held_qty if evidence.held_qty is not None else evidence.filled_qty
        )
        remaining = held - sold
        if remaining < 0:
            raise ValueError("close fills exceed attributed entry")
        if in_flight:
            results.append(
                {
                    "client_order_id": in_flight[0].client_order_id,
                    "status": "close_pending",
                }
            )
            continue
        rules, price = (
            await broker.rules(entry.symbol),
            await broker.price(entry.symbol),
        )
        qty = None
        if remaining:
            try:
                qty = rules.floor(remaining, price)
            except BelowVenueMinimum:
                # Only an actual residual after at least one proven exit is dust.
                if not sold:
                    raise
        if qty is not None:
            await broker.closeable(entry, qty)
            close_id = client_id(entry.client_order_id, str(len(legs)), closing=True)
            close = OrderIntent(
                strategy_id=entry.strategy_id,
                venue=venue,
                account_ref=entry.account_ref,
                episode_id=entry.episode_id,
                symbol=entry.symbol,
                side="cover" if entry.side == "short" else "sell",
                qty=qty,
                notional=qty * price,
                client_order_id=close_id,
                exit_due_at=None,
                meta={
                    "entry_client_order_id": entry.client_order_id,
                    "asof": entry.meta["asof"],
                    "baseline": BASELINE,
                    "version": "v1",
                },
            )
            if dry_run:
                results.append(_output(pending or close))
                continue
            leg = pending or await recorder.record(close)
            await broker.closeable(entry, leg.qty)
            await _send(recorder, broker, leg, confirm=confirm)
            proof = await broker.evidence(leg)
            await _save(recorder, leg, proof)
            if not proof.terminal:
                results.append(
                    {"client_order_id": leg.client_order_id, "status": "close_pending"}
                )
                continue
            sold += proof.filled_qty
            if proof.filled_qty:
                proceeds += proof.filled_qty * positive(proof.price)
            remaining = held - sold
            try:
                if remaining:
                    rules.floor(remaining, price)
            except BelowVenueMinimum:
                pass  # unsellable, step/min-notional residual is reported below
            else:
                if remaining:
                    results.append(
                        {
                            "client_order_id": leg.client_order_id,
                            "status": "close_partial",
                        }
                    )
                    continue
        if not dry_run and sold:
            pnl = proceeds - positive(evidence.price) * sold
            if entry.side == "short":
                pnl = -pnl
            await recorder.update(
                entry.client_order_id,
                status="filled" if remaining else "closed",
                closed_at=None if remaining else datetime.now(UTC),
                exit_price=proceeds / sold,
                realized_pnl=pnl,
                meta={
                    "pnl_basis": "gross_closed_quantity_excludes_fees_and_funding",
                    "residual_qty": str(remaining),
                },
            )
        results.append(
            {
                "client_order_id": entry.client_order_id,
                "status": ("would_close" if dry_run else "closed")
                if not remaining
                else "dust_remaining",
                "residual_qty": str(remaining),
            }
        )
    return {"dry_run": dry_run, "orders": results}
