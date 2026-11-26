"""Manual ROB-1474 crypto episodes with durable one-send claims."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.monitoring.execution_metrics import execution_stage, record_execution_stage
from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.mock_strategy.crypto_brokers import (
    CryptoBroker,
    FillEvidence,
    PreDispatchRefusal,
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
        opened_at=(
            datetime.now(UTC)
            if evidence.filled_qty and getattr(order, "opened_at", None) is None
            else None
        ),
        meta={
            "filled_qty": str(evidence.filled_qty),
            "net_received_qty": (
                str(evidence.held_qty) if evidence.held_qty is not None else None
            ),
            "fee_amount": (
                str(evidence.fee_amount) if evidence.fee_amount is not None else None
            ),
            "fee_currency": evidence.fee_currency,
            "fee_evidence_reason": evidence.fee_evidence_reason,
            "entry_terminal": evidence.terminal,
            "broker_status": evidence.status,
        },
    )


async def _send(
    recorder: Any, broker: CryptoBroker, order: Any, *, confirm: bool
) -> dict:
    venue = "binance"
    record_execution_stage(
        venue=venue,
        market="crypto",
        account_kind="mock",
        stage="receive",
        outcome="success",
        duration_seconds=0.0,
    )
    detection_started_at = time.perf_counter()
    detected = order.status == "planned"
    record_execution_stage(
        venue=venue,
        market="crypto",
        account_kind="mock",
        stage="detect",
        outcome="success" if detected else "blocked",
        duration_seconds=time.perf_counter() - detection_started_at,
    )
    require_enabled(order.venue, confirm=confirm)
    if order.status != "planned":
        return {
            "client_order_id": order.client_order_id,
            "status": "replayed",
            "submitted": False,
        }
    gate_started_at = time.perf_counter()
    try:
        await broker.preflight(order, confirm=confirm)
    except PreDispatchRefusal as exc:
        record_execution_stage(
            venue=venue,
            market="crypto",
            account_kind="mock",
            stage="gate",
            outcome="blocked",
            duration_seconds=time.perf_counter() - gate_started_at,
        )
        return {
            "client_order_id": order.client_order_id,
            "status": "preflight_refused",
            "submitted": False,
            "reason_class": exc.reason_class,
        }
    if not await recorder.claim(order.client_order_id):
        record_execution_stage(
            venue=venue,
            market="crypto",
            account_kind="mock",
            stage="gate",
            outcome="blocked",
            duration_seconds=time.perf_counter() - gate_started_at,
        )
        return {
            "client_order_id": order.client_order_id,
            "status": "replayed",
            "submitted": False,
        }
    try:
        record_execution_stage(
            venue=venue,
            market="crypto",
            account_kind="mock",
            stage="gate",
            outcome="success",
            duration_seconds=time.perf_counter() - gate_started_at,
        )
        with execution_stage(
            venue=venue,
            market="crypto",
            account_kind="mock",
            stage="broker_accept",
        ) as broker_observation:
            evidence = await broker.submit(order, confirm=confirm)
            if not evidence.broker_order_id:
                broker_observation.mark("uncertain")
        with execution_stage(
            venue=venue,
            market="crypto",
            account_kind="mock",
            stage="fill_reconcile",
        ) as fill_observation:
            await _save(recorder, order, evidence)
            if not evidence.terminal:
                fill_observation.mark("uncertain")
    except PreDispatchRefusal as exc:
        # Only the broker adapter's checks, before its POST, can grant this proof.
        await recorder.update(
            order.client_order_id,
            status="rejected",
            meta={"no_order_sent": True, "pre_dispatch_reason_class": exc.reason_class},
        )
        return {
            "client_order_id": order.client_order_id,
            "status": "rejected",
            "submitted": False,
            "reason_class": exc.reason_class,
        }
    except httpx.HTTPStatusError as exc:
        try:
            await recorder.update(order.client_order_id, status="uncertain")
        except Exception:
            raise exc

        def _continue_after_http_status(status_code: int | None) -> bool:
            return (
                status_code is not None
                and 400 <= status_code < 500
                and status_code not in {401, 403, 418, 429}
            )

        status_code = getattr(exc.response, "status_code", None)
        if order.side not in {"buy", "short"} or not _continue_after_http_status(
            status_code
        ):
            raise
        return {
            "client_order_id": order.client_order_id,
            "status": "uncertain",
            "submitted": None,
            "error_class": "HTTPStatusError",
        }
    except BaseException:
        # A committed sending fence survives even if this status update fails.
        # No exception, timeout or missing lookup grants permission to resend.
        try:
            await recorder.update(order.client_order_id, status="uncertain")
        except Exception:
            pass  # Preserve the dispatch/control exception; sending stays durable.
        raise
    return {
        "client_order_id": order.client_order_id,
        "status": _entry_status(evidence),
        "submitted": True,
    }


async def _confirmed_absent(
    recorder: Any,
    broker: Any,
    row: Any,
    *,
    window_start: datetime | None,
    all_rows: list,
    dry_run: bool,
) -> bool:
    """Binance spot demo -2013 closure: lookup absent AND no own trade in myTrades.

    Only a never-bound sending/uncertain leg qualifies; the caller re-raises the
    original not-found error when this returns False, so state is unchanged.
    """
    if (
        row.venue != "binance_spot_demo"
        or row.status not in {"sending", "uncertain"}
        or row.broker_order_id is not None
        or window_start is None
    ):
        return False
    others = frozenset(
        str(r.broker_order_id)
        for r in all_rows
        if r.broker_order_id is not None
        and r.client_order_id != row.client_order_id
        and r.symbol == row.symbol  # Binance order IDs are per-symbol sequences
    )
    if not await broker.confirm_absence(
        row, window_start=window_start, other_order_ids=others
    ):
        return False
    if not dry_run:
        closed = await recorder.confirm_binance_spot_absence(
            row.client_order_id, account_ref=row.account_ref
        )
        if closed.status != "rejected":
            return False
    return True


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
            rules = await (
                broker.entry_rules(symbol)
                if venue == "binance_futures_demo"
                else broker.rules(symbol)
            )
            price = await broker.price(symbol)
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
        would_set = []
        eligible = []
        if venue == "binance_futures_demo":
            for plan in plans:
                try:
                    change = await broker.inspect_entry_settings(plan.symbol)
                    if change is not None:
                        would_set.append(change)
                    eligible.append(plan)
                except PreDispatchRefusal as exc:
                    skipped.append({"symbol": plan.symbol, "reason": exc.reason_class})
                except Exception:
                    skipped.append(
                        {
                            "symbol": plan.symbol,
                            "reason": "settings_evidence_unavailable",
                        }
                    )
        return {
            "dry_run": True,
            "orders": [
                _output(p)
                for p in (eligible if venue == "binance_futures_demo" else plans)
            ],
            "skipped": skipped,
            "would_set": would_set,
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
    outcomes, settings_changes, settings_events = [], [], []
    for row in rows:
        if venue == "binance_futures_demo" and row.status == "planned":

            async def audit(event: dict, cid: str = row.client_order_id) -> None:
                settings_events.append(event)
                await recorder.record_settings_event(cid, event)

            try:
                change = await broker.prepare_entry_settings(
                    row.symbol, confirm=confirm, audit=audit
                )
                if change is not None:
                    settings_changes.append(change)
            except PreDispatchRefusal as exc:
                outcomes.append(
                    {
                        "client_order_id": row.client_order_id,
                        "status": "preflight_refused",
                        "submitted": False,
                        "reason_class": exc.reason_class,
                    }
                )
                continue
        outcomes.append(await _send(recorder, broker, row, confirm=confirm))
    return {
        "dry_run": False,
        "orders": outcomes,
        "skipped": skipped,
        "settings_changes": settings_changes,
        "settings_events": settings_events,
    }


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
        try:
            if not entry.strategy_id.startswith(
                "rob1474.crypto."
            ) or entry.side not in {
                "buy",
                "short",
            }:
                continue
            if entry.status == "planned":
                # Planned alone proves no fill; never send an overdue entry here.
                results.append(
                    {
                        "client_order_id": entry.client_order_id,
                        "status": "entry_not_sent",
                    }
                )
                continue
            try:
                evidence = await broker.evidence(entry)
            except BinanceDemoOrderNotFound:
                horizon = entry.meta.get("horizon")
                start = (
                    entry.exit_due_at - timedelta(days=horizon)
                    if entry.exit_due_at is not None and isinstance(horizon, int)
                    else None
                )
                if not await _confirmed_absent(
                    recorder,
                    broker,
                    entry,
                    window_start=start,
                    all_rows=await recorder.list_orders(
                        venue=venue, account_ref=broker.account_ref
                    ),
                    dry_run=dry_run,
                ):
                    raise
                results.append(
                    {
                        "client_order_id": entry.client_order_id,
                        "status": "entry_rejected_absent"
                        if not dry_run
                        else "would_reject_absent",
                    }
                )
                continue
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
            exit_fee_evidence = []
            in_flight = []
            planned_count = 0
            for leg in legs:
                if leg.status == "rejected" and (
                    leg.meta.get("no_order_sent") is True
                    or leg.meta.get("no_order_proven") is True
                ):
                    continue
                if leg.status == "planned":
                    planned_count += 1
                    pending = leg
                    continue
                try:
                    proof = await broker.evidence(leg)
                except BinanceDemoOrderNotFound:
                    if not await _confirmed_absent(
                        recorder,
                        broker,
                        leg,
                        window_start=entry.exit_due_at,
                        all_rows=all_rows,
                        dry_run=dry_run,
                    ):
                        raise
                    # Proven absent: no fill, no resend of this id. The residual
                    # is re-derived below and goes out under a new indexed id.
                    continue
                if not dry_run and leg.status != "closed":
                    await _save(recorder, leg, proof)
                sold += proof.filled_qty
                if proof.filled_qty:
                    proceeds += proof.filled_qty * positive(proof.price)
                    exit_fee_evidence.append(proof)
                if not proof.terminal:
                    in_flight.append(leg)
            if planned_count > 1:
                raise ValueError("multiple planned close legs require review")
            rules, price = (
                await broker.rules(entry.symbol),
                await broker.price(entry.symbol),
            )
            recorded_net = entry.meta.get("net_received_qty")
            try:
                recorded_net_qty = (
                    Decimal(str(recorded_net)) if recorded_net is not None else None
                )
            except Exception:
                recorded_net_qty = None
            held = evidence.held_qty or recorded_net_qty
            if venue == "alpaca_paper_crypto" and held is None:
                held = rules.floor(evidence.filled_qty * Decimal("0.9975"), price)
            if held is None:
                held = evidence.filled_qty
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
            sellable_remaining = remaining
            position_limited = False
            dust_remaining = False
            if venue == "alpaca_paper_crypto" and remaining:
                available = await broker.available_qty(entry)
                if available < remaining:
                    results.append(
                        {
                            "client_order_id": entry.client_order_id,
                            "status": "broker_qty_unavailable",
                            "reason": "broker_qty_unavailable",
                            "residual_qty": str(remaining),
                        }
                    )
                    continue
                sellable_remaining = min(remaining, available)
                position_limited = sellable_remaining < remaining
            qty = None
            if sellable_remaining:
                try:
                    qty = rules.floor(sellable_remaining, price)
                except BelowVenueMinimum:
                    if position_limited:
                        qty = None
                    elif not sold:
                        raise
            if qty is not None:
                await broker.closeable(entry, qty)
                close_id = client_id(
                    entry.client_order_id, str(len(legs)), closing=True
                )
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
                    preview = _output(
                        close if pending and pending.qty > qty else pending or close
                    )
                    if pending and pending.qty > qty:
                        preview["status"] = "would_replace_oversized_plan"
                    results.append(preview)
                    continue
                if venue == "alpaca_paper_crypto" and pending and pending.qty > qty:
                    retire = getattr(recorder, "retire_planned_exit", None)
                    if retire is None or not await retire(
                        pending.client_order_id,
                        reason="planned_exit_exceeds_attributed_quantity",
                    ):
                        results.append(
                            {
                                "client_order_id": pending.client_order_id,
                                "status": "exit_refused_quantity_exceeds_net_received",
                                "reason": "planned_exit_not_safely_retired",
                            }
                        )
                        continue
                    pending = None
                leg = pending or await recorder.record(close)
                await broker.closeable(entry, leg.qty)
                sent = await _send(recorder, broker, leg, confirm=confirm)
                if (
                    sent["status"] in {"preflight_refused", "rejected"}
                    and not sent["submitted"]
                ):
                    results.append(sent)
                    continue
                proof = await broker.evidence(leg)
                await _save(recorder, leg, proof)
                if not proof.terminal:
                    results.append(
                        {
                            "client_order_id": leg.client_order_id,
                            "status": "close_pending",
                        }
                    )
                    continue
                sold += proof.filled_qty
                if proof.filled_qty:
                    proceeds += proof.filled_qty * positive(proof.price)
                    exit_fee_evidence.append(proof)
                remaining = held - sold
                try:
                    if remaining:
                        rules.floor(remaining, price)
                except BelowVenueMinimum:
                    dust_remaining = True
                else:
                    if remaining:
                        results.append(
                            {
                                "client_order_id": leg.client_order_id,
                                "status": "close_partial",
                            }
                        )
                        continue
            fee_values = [evidence, *exit_fee_evidence]
            fee_evidence_pending = venue == "alpaca_paper_crypto" and not all(
                item.fee_amount is not None
                and item.fee_currency is not None
                and item.fee_evidence_reason is None
                for item in fee_values
            )
            if not dry_run and sold:
                pnl = proceeds - positive(evidence.price) * sold
                if entry.side == "short":
                    pnl = -pnl
                pnl_meta: dict[str, Any] = {
                    "pnl_basis": "gross_closed_quantity_excludes_fees_and_funding",
                    "gross_pnl": str(pnl),
                    "residual_qty": str(remaining),
                }
                if venue == "alpaca_paper_crypto":
                    if not fee_evidence_pending:
                        fees_usd = Decimal("0")
                        known = True
                        for item in fee_values:
                            if item.fee_currency == "USD":
                                fees_usd += item.fee_amount or Decimal("0")
                            elif item.fee_currency == entry.symbol.removesuffix("USDT"):
                                reference_price = item.price
                                if reference_price is None:
                                    known = False
                                    break
                                fees_usd += (
                                    item.fee_amount or Decimal("0")
                                ) * reference_price
                            elif item.fee_amount != 0:
                                known = False
                                break
                        if known:
                            pnl_meta["net_pnl"] = str(pnl - fees_usd)
                            pnl_meta["fee_total_usd"] = str(fees_usd)
                        else:
                            pnl_meta["net_pnl_reason"] = "fee_currency_unconvertible"
                    else:
                        pnl_meta["net_pnl_reason"] = "fee_evidence_missing"
                        pnl_meta["fee_evidence_pending"] = True
                await recorder.update(
                    entry.client_order_id,
                    status=(
                        "filled" if remaining or fee_evidence_pending else "closed"
                    ),
                    closed_at=(
                        None if remaining or fee_evidence_pending else datetime.now(UTC)
                    ),
                    exit_price=proceeds / sold,
                    realized_pnl=pnl,
                    meta=pnl_meta,
                )
            results.append(
                {
                    "client_order_id": entry.client_order_id,
                    "status": ("would_close" if dry_run else "closed")
                    if not remaining and not fee_evidence_pending
                    else "awaiting_fee_evidence"
                    if not remaining and fee_evidence_pending
                    else "waiting_for_position_availability"
                    if position_limited and qty is None
                    else "dust_remaining"
                    if dust_remaining
                    else "close_partial",
                    "residual_qty": str(remaining),
                }
            )
        except Exception as exc:
            # Keep this episode blocked, while independent due episodes can exit.
            # Never turn an evidence error into permission to resend any leg.
            results.append(
                {
                    "client_order_id": entry.client_order_id,
                    "status": "unresolved",
                    "error_class": type(exc).__name__,
                }
            )
    return {"dry_run": dry_run, "orders": results}
