"""Broker-truth reconciliation for abandoned Binance Demo root reservations.

Candidates are *pre-acknowledgement* open roots: a still-persisted ``planned``
root, or a ``previewed``/``validated`` root whose ``broker_order_id`` is still
NULL (ROB-906 — the broker POST never reached the venue, so no ack exists).
``submitted``/``filled``/``anomaly`` roots carry a broker acknowledgement and
remain owned by the fill-evidence reconcile path. A reservation is released
solely when Binance explicitly reports the client order missing, or a terminal
order with zero executed quantity. Transport errors, malformed truth, open
orders, and any executed quantity remain blocking.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)

_NO_EXPOSURE_TERMINAL = {
    "spot": frozenset({"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}),
    "usdm_futures": frozenset({"CANCELED", "REJECTED", "EXPIRED"}),
}
_EXPECTED_VENUE_HOST = {
    "spot": "demo-api.binance.com",
    "usdm_futures": "demo-fapi.binance.com",
}
# Binance documents an order-query retention window shorter than 90 days for
# some order histories. Treat an explicit -2013 as authoritative only while the
# reservation age is strictly below this deliberately conservative hard bound.
# At the exact boundary and beyond, absence is ambiguous and must fail closed.
_NOT_FOUND_RELEASE_MAX_AGE = dt.timedelta(days=89)


def _finite_nonnegative_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _normalize_broker_order_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized.isascii() or not normalized.isdecimal():
        return None
    return normalized if int(normalized) > 0 else None


def _normalize_broker_truth_payload(
    payload: Any,
) -> tuple[str, str, str, Decimal, str] | None:
    """Strictly parse broker-owned fields without caller-value substitution."""
    if not isinstance(payload, Mapping):
        return None
    required = {"clientOrderId", "symbol", "status", "executedQty", "orderId"}
    if not required.issubset(payload):
        return None
    client_order_id = payload["clientOrderId"]
    symbol = payload["symbol"]
    status = payload["status"]
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (client_order_id, symbol, status)
    ):
        return None
    executed = _finite_nonnegative_decimal(payload.get("executedQty"))
    broker_order_id = _normalize_broker_order_id(payload.get("orderId"))
    if executed is None or broker_order_id is None:
        return None
    return (
        client_order_id.strip(),
        symbol.strip(),
        status.strip().upper(),
        executed,
        broker_order_id,
    )


def _normalize_spot_truth(payload: Any) -> tuple[str, str, str, Decimal, str] | None:
    return _normalize_broker_truth_payload(payload)


def _normalize_futures_truth(payload: Any) -> tuple[str, str, str, Decimal, str] | None:
    # The general Futures DTO intentionally preserves backwards-compatible
    # defaults for ordinary polling callers. Reconciliation is a stronger trust
    # boundary: validate the redacted broker payload itself so a missing field
    # can never be synthesized from request arguments or zero defaults.
    return _normalize_broker_truth_payload(
        getattr(payload, "raw_response_redacted", None)
    )


async def _lookup_order(client: Any, *, product: str, symbol: str, cid: str) -> Any:
    if product == "spot":
        return await client.get_order_status(symbol=symbol, client_order_id=cid)
    return await client.get_order(symbol=symbol, client_order_id=cid)


def _candidate_where_clauses(
    stale_before: dt.datetime,
) -> tuple[ColumnElement[bool], ...]:
    """Shared predicate for the discovery snapshot and the FOR UPDATE re-read.

    A candidate is an old *pre-acknowledgement* open root:

    * a ``planned`` root (original ROB-844 semantics, unchanged), or
    * a ``previewed``/``validated`` root whose ``broker_order_id`` is still NULL
      (ROB-906 — a crash after ``record_validated`` but before the broker POST
      leaves a durable open root that no ack-keyed fill-evidence path can free).

    ``submitted``/``filled``/``anomaly`` roots carry a broker acknowledgement and
    are deliberately excluded here. ``planned_at`` is set at insert and never
    cleared across pre-ack transitions, so it remains the age gate for all three
    states. The re-read applies the same predicate, so a row that advanced to
    ``submitted`` (or gained a ``broker_order_id``) between snapshot and lock is
    silently dropped, never kept.
    """
    return (
        BinanceDemoOrderLedger.parent_client_order_id.is_(None),
        BinanceDemoOrderLedger.planned_at <= stale_before,
        or_(
            BinanceDemoOrderLedger.lifecycle_state == "planned",
            and_(
                BinanceDemoOrderLedger.lifecycle_state.in_(("previewed", "validated")),
                BinanceDemoOrderLedger.broker_order_id.is_(None),
            ),
        ),
    )


async def reconcile_binance_demo_root_reservations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clients: Mapping[str, Any],
    now: dt.datetime,
    stale_before: dt.datetime,
    limit: int = 100,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Classify and optionally release old, abandoned committed reservations.

    Age is only an eligibility guard; it never proves absence. Discovery is a
    lock-free id snapshot. Each id is then re-read under its own independent
    ``FOR UPDATE SKIP LOCKED`` transaction, held across only that candidate's
    broker lookup and optional mutation and committed immediately. Thus a live
    executor either owns that row first (and is skipped) or observes terminal
    truth before submit, while slow broker I/O never locks later candidates.
    """
    async with session_factory() as discovery_session:
        candidate_ids = list(
            (
                await discovery_session.scalars(
                    select(BinanceDemoOrderLedger.id)
                    .where(*_candidate_where_clauses(stale_before))
                    .order_by(
                        BinanceDemoOrderLedger.planned_at,
                        BinanceDemoOrderLedger.id,
                    )
                    .limit(limit)
                )
            ).all()
        )

    released = 0
    kept = 0
    scanned = 0
    outcomes: list[dict[str, Any]] = []

    for candidate_id in candidate_ids:
        # A fresh session/transaction per candidate is intentional: never keep
        # a batch of rows locked while sequential network requests are in flight.
        async with session_factory() as candidate_session:
            async with candidate_session.begin():
                candidate_result = await candidate_session.execute(
                    select(BinanceDemoOrderLedger, CryptoInstrument.venue_symbol)
                    .join(
                        CryptoInstrument,
                        CryptoInstrument.id == BinanceDemoOrderLedger.instrument_id,
                    )
                    .where(
                        BinanceDemoOrderLedger.id == candidate_id,
                        *_candidate_where_clauses(stale_before),
                    )
                    .with_for_update(skip_locked=True, of=BinanceDemoOrderLedger)
                    .execution_options(populate_existing=True)
                )
                candidate = candidate_result.one_or_none()
                if candidate is None:
                    continue
                row, symbol = candidate
                scanned += 1
                # Snapshot the pre-release lifecycle state for operator-readable
                # outcomes (ROB-906). Reported on every outcome — including
                # released rows — so the operator sees which pre-ack state
                # (planned/previewed/validated) was reconciled rather than the
                # post-release ``reconciled``.
                lifecycle_state = row.lifecycle_state
                client = clients.get(row.product)
                if client is None:
                    kept += 1
                    outcomes.append(
                        {
                            "client_order_id": row.client_order_id,
                            "action": "kept",
                            "reason": "client_unavailable",
                            "lifecycle_state": lifecycle_state,
                        }
                    )
                    continue
                if row.venue_host != _EXPECTED_VENUE_HOST.get(row.product):
                    kept += 1
                    outcomes.append(
                        {
                            "client_order_id": row.client_order_id,
                            "action": "kept",
                            "reason": "venue_host_mismatch",
                            "lifecycle_state": lifecycle_state,
                        }
                    )
                    continue
                metadata = row.extra_metadata
                persisted_fingerprint = (
                    metadata.get("credential_fingerprint")
                    if isinstance(metadata, Mapping)
                    else None
                )
                if not isinstance(persisted_fingerprint, str) or not (
                    persisted_fingerprint.strip()
                ):
                    kept += 1
                    outcomes.append(
                        {
                            "client_order_id": row.client_order_id,
                            "action": "kept",
                            "reason": "credential_fingerprint_missing",
                            "lifecycle_state": lifecycle_state,
                        }
                    )
                    continue
                try:
                    current_fingerprint = getattr(
                        client, "credential_fingerprint", None
                    )
                except Exception:  # noqa: BLE001 - identity uncertainty fails closed
                    current_fingerprint = None
                if not isinstance(current_fingerprint, str) or not (
                    current_fingerprint.strip()
                ):
                    kept += 1
                    outcomes.append(
                        {
                            "client_order_id": row.client_order_id,
                            "action": "kept",
                            "reason": "client_credential_fingerprint_unavailable",
                            "lifecycle_state": lifecycle_state,
                        }
                    )
                    continue
                if current_fingerprint != persisted_fingerprint:
                    kept += 1
                    outcomes.append(
                        {
                            "client_order_id": row.client_order_id,
                            "action": "kept",
                            "reason": "credential_fingerprint_mismatch",
                            "lifecycle_state": lifecycle_state,
                        }
                    )
                    continue

                release_reason: str | None = None
                broker_order_id = ""
                try:
                    truth = await _lookup_order(
                        client,
                        product=row.product,
                        symbol=symbol,
                        cid=row.client_order_id,
                    )
                except BinanceDemoOrderNotFound:
                    if (
                        row.planned_at is None
                        or now - row.planned_at >= _NOT_FOUND_RELEASE_MAX_AGE
                    ):
                        kept += 1
                        outcomes.append(
                            {
                                "client_order_id": row.client_order_id,
                                "action": "kept",
                                "reason": "broker_lookup_retention_exceeded",
                                "lifecycle_state": lifecycle_state,
                            }
                        )
                        continue
                    release_reason = "broker_order_not_found"
                except Exception:  # noqa: BLE001 - uncertainty must fail closed
                    kept += 1
                    outcomes.append(
                        {
                            "client_order_id": row.client_order_id,
                            "action": "kept",
                            "reason": "broker_lookup_failed",
                            "lifecycle_state": lifecycle_state,
                        }
                    )
                    continue
                else:
                    normalized = (
                        _normalize_spot_truth(truth)
                        if row.product == "spot"
                        else _normalize_futures_truth(truth)
                    )
                    if normalized is None:
                        kept += 1
                        outcomes.append(
                            {
                                "client_order_id": row.client_order_id,
                                "action": "kept",
                                "reason": "malformed_broker_truth",
                                "lifecycle_state": lifecycle_state,
                            }
                        )
                        continue
                    (
                        truth_cid,
                        truth_symbol,
                        status,
                        executed_qty,
                        broker_order_id,
                    ) = normalized
                    if truth_cid != row.client_order_id or truth_symbol != symbol:
                        kept += 1
                        outcomes.append(
                            {
                                "client_order_id": row.client_order_id,
                                "action": "kept",
                                "reason": "broker_identity_mismatch",
                                "lifecycle_state": lifecycle_state,
                            }
                        )
                        continue
                    if (
                        status in _NO_EXPOSURE_TERMINAL.get(row.product, frozenset())
                        and executed_qty == 0
                    ):
                        release_reason = "terminal_zero_fill"
                    else:
                        kept += 1
                        outcomes.append(
                            {
                                "client_order_id": row.client_order_id,
                                "action": "kept",
                                "reason": "broker_exposure_not_disproven",
                                "lifecycle_state": lifecycle_state,
                            }
                        )
                        continue

                evidence = {
                    "reservation_reconcile_reason": release_reason,
                    "reservation_reconciled_at": now.isoformat(),
                }
                if broker_order_id:
                    evidence["reservation_reconcile_broker_order_id"] = broker_order_id
                if not dry_run:
                    ledger = BinanceDemoLedgerService(candidate_session)
                    await ledger.record_cancelled(
                        client_order_id=row.client_order_id,
                        now=now,
                        extra_metadata_merge=evidence,
                    )
                    await ledger.record_reconciled(
                        client_order_id=row.client_order_id,
                        now=now,
                        extra_metadata_merge=evidence,
                    )
                    released += 1
                    action = "released"
                else:
                    action = "would_release"
                outcomes.append(
                    {
                        "client_order_id": row.client_order_id,
                        "action": action,
                        "reason": release_reason,
                        "lifecycle_state": lifecycle_state,
                    }
                )

    return {
        "status": "ok",
        "dry_run": dry_run,
        "scanned": scanned,
        "released": released,
        "kept": kept,
        "outcomes": outcomes,
    }


async def run_binance_demo_root_reservation_reconciliation_from_env(
    *, now: dt.datetime, stale_before: dt.datetime, dry_run: bool
) -> dict[str, Any]:
    """Construct each enabled Demo lane independently for the task boundary.

    A missing/invalid Spot lane must not force a Futures deployment to broaden
    its gates or credentials (and vice versa). Unavailable lanes are omitted;
    their candidates remain blocking through the kernel's stable
    ``client_unavailable`` outcome.
    """
    spot_client: Any = None
    futures_client: Any = None
    clients: dict[str, Any] = {}
    client_initialization: dict[str, str] = {}
    try:
        try:
            spot_client = BinanceSpotDemoExecutionClient.from_env()
        except Exception:  # noqa: BLE001 - unavailable lane fails closed per candidate
            client_initialization["spot"] = "unavailable"
        else:
            clients["spot"] = spot_client
            client_initialization["spot"] = "available"
        try:
            futures_client = BinanceFuturesDemoExecutionClient.from_env()
        except Exception:  # noqa: BLE001 - unavailable lane fails closed per candidate
            client_initialization["usdm_futures"] = "unavailable"
        else:
            clients["usdm_futures"] = futures_client
            client_initialization["usdm_futures"] = "available"

        result = await reconcile_binance_demo_root_reservations(
            AsyncSessionLocal,
            clients=clients,
            now=now,
            stale_before=stale_before,
            dry_run=dry_run,
        )
        return {**result, "client_initialization": client_initialization}
    finally:
        cleanup_errors: list[BaseException] = []
        for client in (spot_client, futures_client):
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except BaseException as exc:  # noqa: BLE001 - close both clients
                    cleanup_errors.append(exc)
        if cleanup_errors:
            active_error = sys.exception()
            if active_error is None:
                raise cleanup_errors[0]
            for cleanup_error in cleanup_errors:
                active_error.add_note(
                    f"Binance Demo client cleanup also failed: {cleanup_error!r}"
                )
