"""Read models and fresh broker observations for protected-position settings.

This module deliberately separates an operator screen's broker reads from the
protection policy writer.  In particular, ``fresh_broker_observation`` is a
lazy coroutine intended to be passed to ``ProtectedQuantityService.save``;
the writer invokes it only after it owns the transaction-scoped advisory lock.
Nothing in this module sends an order, changes an order ledger, or schedules
work.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_upbit_symbol
from app.models.trading import User
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit.client import fetch_my_coins
from app.services.protected_quantity_service import (
    BrokerPositionObservation,
    ProtectedPositionSnapshot,
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
    ProtectionKey,
    coerce_broker_quantity,
    headroom_for_observation,
    normalize_protection_key,
    protection_mode_for_scope,
)
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot


class BrokerObservationUnavailable(RuntimeError):
    """A settings read could not obtain fresh held and sellable evidence."""

    error = "broker_read_failed"


@dataclass(frozen=True, slots=True)
class LivePositionObservation:
    """Read-only broker evidence for one live protection key."""

    key: ProtectionKey
    name: str
    held: Decimal | None
    sellable: Decimal | None
    observed_at: datetime | None
    error: str | None = None


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _observed_now() -> datetime:
    """Stamp evidence only after the associated broker response was consumed."""

    return datetime.now(UTC)


def _key_from_broker_symbol(
    *, account_scope: str, market: str, symbol: Any
) -> ProtectionKey | None:
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    try:
        return normalize_protection_key(
            account_scope=account_scope,
            market=market,
            symbol=symbol,
        )
    except ProtectedQuantityValidationError:
        return None


def _require_quantity(value: Any, *, field: str) -> Decimal:
    try:
        return coerce_broker_quantity(value, field=field)
    except ProtectedQuantityValidationError as exc:
        raise BrokerObservationUnavailable("broker quantity is unavailable") from exc


async def _read_kis_market(*, market: str) -> list[LivePositionObservation]:
    client = KISClient()
    if market == "kr":
        rows = await client.fetch_my_stocks()
        symbol_field, name_field, held_field = "pdno", "prdt_name", "hldg_qty"
    else:
        rows = await client.fetch_my_us_stocks()
        symbol_field, name_field, held_field = (
            "ovrs_pdno",
            "ovrs_item_name",
            "ovrs_cblc_qty",
        )

    observations: list[LivePositionObservation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_symbol = row.get(symbol_field)
        key = _key_from_broker_symbol(
            account_scope="kis_live", market=market, symbol=raw_symbol
        )
        if key is None:
            continue
        held = _require_quantity(row.get(held_field), field="broker_held")
        raw_sellable = row.get("ord_psbl_qty")
        if raw_sellable is None or not str(raw_sellable).strip():
            raise BrokerObservationUnavailable("KIS sellable quantity is unavailable")
        sellable = _require_quantity(raw_sellable, field="broker_sellable")
        observations.append(
            LivePositionObservation(
                key=key,
                name=str(row.get(name_field) or key.symbol).strip() or key.symbol,
                held=held,
                sellable=sellable,
                observed_at=_observed_now(),
            )
        )
    return observations


async def _read_upbit_positions() -> list[LivePositionObservation]:
    rows = await fetch_my_coins()
    observations: list[LivePositionObservation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        currency = row.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            continue
        quote = row.get("unit_currency")
        quote_text = (
            quote.strip().upper() if isinstance(quote, str) and quote.strip() else "KRW"
        )
        symbol = to_upbit_symbol(f"{quote_text}-{currency.strip().upper()}")
        key = _key_from_broker_symbol(
            account_scope="upbit_live", market="crypto", symbol=symbol
        )
        if key is None:
            continue
        sellable = _require_quantity(row.get("balance"), field="broker_sellable")
        locked = _require_quantity(row.get("locked", "0"), field="broker_locked")
        observations.append(
            LivePositionObservation(
                key=key,
                name=key.symbol,
                held=sellable + locked,
                sellable=sellable,
                observed_at=_observed_now(),
            )
        )
    return observations


async def _read_toss_positions() -> list[LivePositionObservation]:
    # Explicitly bypass both the portfolio snapshot and the sellable cache.
    # A settings declaration must record broker evidence, never a cached S.
    snapshot = await fetch_toss_portfolio_snapshot(
        need_sellable=True,
        need_cash=False,
        use_shared_snapshot=False,
    )
    observations: list[LivePositionObservation] = []
    for position in snapshot.positions:
        key = _key_from_broker_symbol(
            account_scope="toss_live",
            market=position.market,
            symbol=position.symbol,
        )
        if key is None:
            continue
        held = _require_quantity(position.quantity, field="broker_held")
        if position.sellable_quantity is None:
            raise BrokerObservationUnavailable("Toss sellable quantity is unavailable")
        sellable = _require_quantity(
            position.sellable_quantity,
            field="broker_sellable",
        )
        observations.append(
            LivePositionObservation(
                key=key,
                name=position.name.strip() or key.symbol,
                held=held,
                sellable=sellable,
                observed_at=_observed_now(),
            )
        )
    return observations


async def _read_single_from_market(*, key: ProtectionKey) -> LivePositionObservation:
    if key.account_scope == "kis_live":
        observations = await _read_kis_market(market=key.market)
    elif key.account_scope == "toss_live":
        observations = await _read_toss_positions()
    elif key.account_scope == "upbit_live":
        observations = await _read_upbit_positions()
    else:  # normalize_protection_key makes this unreachable, retain fail-closed.
        raise BrokerObservationUnavailable("unsupported broker protection scope")

    for observation in observations:
        if observation.key == key:
            return observation
    # The broker successfully answered and did not list this position. This is
    # real zero evidence, unlike a fetch failure; a release can therefore be
    # recorded while a nonzero declaration cannot exceed it.
    return LivePositionObservation(
        key=key,
        name=key.symbol,
        held=Decimal("0"),
        sellable=Decimal("0"),
        observed_at=_observed_now(),
    )


async def fresh_broker_observation(*, key: ProtectionKey) -> BrokerPositionObservation:
    """Read fresh H/S for a declaration save.

    Callers must pass this coroutine lazily to ``ProtectedQuantityService``.
    Its timestamp is created only after the direct broker response has been
    parsed, so an observation captured before the service's advisory lock
    cannot be relabelled as post-lock evidence.
    """

    try:
        observed = await _read_single_from_market(key=key)
    except BrokerObservationUnavailable:
        raise
    except Exception as exc:
        raise BrokerObservationUnavailable("broker position read failed") from exc
    if (
        observed.error is not None
        or observed.held is None
        or observed.sellable is None
        or observed.observed_at is None
    ):
        raise BrokerObservationUnavailable("broker position read failed")
    return BrokerPositionObservation(
        held=observed.held,
        sellable=observed.sellable,
        observed_at=observed.observed_at,
    )


async def read_live_position_inventory() -> tuple[
    dict[ProtectionKey, LivePositionObservation], dict[tuple[str, str], str]
]:
    """Read declarable live holdings, retaining per-market failures as facts."""

    sources: tuple[tuple[tuple[str, str], Any], ...] = (
        (("kis_live", "kr"), _read_kis_market(market="kr")),
        (("kis_live", "us"), _read_kis_market(market="us")),
        (("toss_live", "kr"), _read_toss_positions()),
        (("upbit_live", "crypto"), _read_upbit_positions()),
    )
    # Toss can yield both KR and US positions in one response. It is recorded
    # under each applicable key below rather than issuing a second call.
    results = await asyncio.gather(
        *(coro for _, coro in sources), return_exceptions=True
    )
    observations: dict[ProtectionKey, LivePositionObservation] = {}
    failures: dict[tuple[str, str], str] = {}
    for ((scope, market), _), result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            if scope == "toss_live":
                failures[("toss_live", "kr")] = "broker_read_failed"
                failures[("toss_live", "us")] = "broker_read_failed"
            else:
                failures[(scope, market)] = "broker_read_failed"
            continue
        for observation in result:
            observations[observation.key] = observation
    return observations, failures


async def _actor_names(db: AsyncSession, actor_ids: set[int]) -> dict[int, str | None]:
    if not actor_ids:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(actor_ids)))
    names: dict[int, str | None] = {}
    for user in rows.scalars():
        names[int(user.id)] = user.nickname or user.username
    return names


def _state_from_observation(
    *, snapshot: ProtectedPositionSnapshot | None, observation: LivePositionObservation
) -> tuple[str, Decimal | None]:
    protected = snapshot.protected_quantity if snapshot is not None else Decimal("0")
    if (
        observation.error is not None
        or observation.held is None
        or observation.sellable is None
    ):
        return "unverified", None
    if protected == 0:
        return "unprotected", observation.sellable
    if observation.held < protected:
        return "shortfall", Decimal("0")
    if observation.sellable < protected:
        return "encroached", Decimal("0")
    return "covered", max(Decimal("0"), observation.sellable - protected)


def _history_item(revision: Any, *, actor_name: str | None) -> dict[str, Any]:
    return {
        "revision": int(revision.revision),
        "action": str(revision.action),
        "previous_quantity": _decimal_text(
            None
            if revision.previous_quantity is None
            else Decimal(str(revision.previous_quantity))
        ),
        "new_quantity": _decimal_text(Decimal(str(revision.new_quantity))),
        "broker_held": _decimal_text(Decimal(str(revision.broker_held_observed))),
        "broker_sellable": _decimal_text(
            Decimal(str(revision.broker_sellable_observed))
        ),
        "broker_observed_at": revision.broker_observed_at.isoformat(),
        "reason": str(revision.reason),
        "actor_user_id": int(revision.actor_user_id),
        "actor": actor_name,
        "origin": str(revision.origin),
        "recorded_at": revision.recorded_at.isoformat(),
    }


async def read_protected_position_history(
    db: AsyncSession, *, key: ProtectionKey
) -> list[dict[str, Any]]:
    service = ProtectedQuantityService(db)
    revisions = await service.list_revisions(key=key)
    actor_names = await _actor_names(
        db, {int(revision.actor_user_id) for revision in revisions}
    )
    return [
        _history_item(revision, actor_name=actor_names.get(int(revision.actor_user_id)))
        for revision in revisions
    ]


async def read_protected_position_settings(
    db: AsyncSession,
) -> list[dict[str, Any]]:
    """Return protected heads plus every currently declarable live holding."""

    service = ProtectedQuantityService(db)
    heads = await service.list()
    heads_by_key = {head.key: head for head in heads}
    observations, failures = await read_live_position_inventory()
    keys = set(heads_by_key) | set(observations)

    revisions_by_key: dict[ProtectionKey, list[Any]] = {}
    actor_ids: set[int] = set()
    for key in heads_by_key:
        revisions = await service.list_revisions(key=key)
        revisions_by_key[key] = revisions
        actor_ids.update(int(revision.actor_user_id) for revision in revisions)
    actor_names = await _actor_names(db, actor_ids)

    rows: list[dict[str, Any]] = []
    for key in sorted(
        keys, key=lambda item: (item.account_scope, item.market, item.symbol)
    ):
        snapshot = heads_by_key.get(key)
        observed = observations.get(key)
        failure = failures.get((key.account_scope, key.market))
        protected = (
            snapshot.protected_quantity if snapshot is not None else Decimal("0")
        )
        mode = "off"
        try:
            mode = protection_mode_for_scope(key.account_scope)
        except Exception:
            failure = "protection_mode_unavailable"

        if observed is None:
            observed = LivePositionObservation(
                key=key,
                name=key.symbol,
                held=None,
                sellable=None,
                observed_at=None,
                error=failure,
            )

        state, headroom = _state_from_observation(
            snapshot=snapshot, observation=observed
        )
        # For a normal successful source read, include drift detection in the
        # screen state. A policy-read failure remains visibly unverified rather
        # than silently turning the row into unprotected.
        if (
            observed.error is None
            and observed.held is not None
            and observed.sellable is not None
        ):
            try:
                projection = await headroom_for_observation(
                    account_scope=key.account_scope,
                    market=key.market,
                    symbol=key.symbol,
                    broker_sellable=observed.sellable,
                    broker_held=observed.held,
                    sellable_observed=True,
                )
                state = projection.state
                headroom = projection.tactical_sellable
            except Exception:
                state, headroom = "unverified", None

        revisions = revisions_by_key.get(key, [])
        latest = revisions[-1] if revisions else None
        rows.append(
            {
                "account_scope": key.account_scope,
                "market": key.market,
                "symbol": key.symbol,
                "name": observed.name,
                "protected_quantity": _decimal_text(protected),
                "broker_held": _decimal_text(observed.held),
                "broker_sellable": _decimal_text(observed.sellable),
                "headroom": _decimal_text(headroom),
                "state": state,
                "mode": mode,
                "broker_observed_at": (
                    observed.observed_at.isoformat()
                    if observed.observed_at is not None
                    else None
                ),
                "read_error": observed.error or failure,
                "revision": snapshot.revision if snapshot is not None else None,
                "latest_revision": (
                    None
                    if latest is None
                    else _history_item(
                        latest,
                        actor_name=actor_names.get(int(latest.actor_user_id)),
                    )
                ),
                "history_url": (
                    "/invest/api/settings/protected-positions/"
                    f"{key.account_scope}/{key.market}/{quote(key.symbol, safe='')}/history"
                ),
            }
        )
    return rows


def protection_change_preview(
    *,
    key: ProtectionKey,
    previous_quantity: Decimal,
    new_quantity: Decimal,
    observation: BrokerPositionObservation,
) -> dict[str, Any]:
    """Serialize a no-write confirmation preview from direct broker evidence."""

    held = observation.held
    sellable = observation.sellable
    if held < new_quantity:
        state, headroom = "shortfall", Decimal("0")
    elif sellable < new_quantity:
        state, headroom = "encroached", Decimal("0")
    else:
        state, headroom = "covered", max(Decimal("0"), sellable - new_quantity)
    before_headroom = max(Decimal("0"), sellable - previous_quantity)
    return {
        "account_scope": key.account_scope,
        "market": key.market,
        "symbol": key.symbol,
        "before_protected_quantity": _decimal_text(previous_quantity),
        "after_protected_quantity": _decimal_text(new_quantity),
        "broker_held": _decimal_text(held),
        "broker_sellable": _decimal_text(sellable),
        "before_headroom": _decimal_text(before_headroom),
        "headroom": _decimal_text(headroom),
        "state": state,
        "broker_observed_at": observation.observed_at.isoformat(),
    }


__all__ = [
    "BrokerObservationUnavailable",
    "LivePositionObservation",
    "fresh_broker_observation",
    "protection_change_preview",
    "read_live_position_inventory",
    "read_protected_position_history",
    "read_protected_position_settings",
]
