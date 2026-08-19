"""Single service boundary for operator-declared external cash.

The values exposed here are read-only advisory evidence.  This module offers
no conversion to a buying-power claim, order preview, sizing input, cap input,
or approval input.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.role_hierarchy import has_min_role
from app.models.trading import User, UserRole
from app.schemas.funding_advisory import (
    ExternalCashCurrentView,
    ExternalCashDeclarationRecord,
    ExternalCashDeclarationRequest,
)
from app.services.funding_advisory._external_cash_repository import (
    ExternalCashDeclarationRepository,
)

FRESHNESS_WINDOW = timedelta(hours=24)
ORIGIN = "invest_ui"


class ExternalCashDeclarationError(Exception):
    """Base exception for declaration service failures."""


class ExternalCashAuthorizationError(ExternalCashDeclarationError):
    """The actor is not an active administrator."""


class ExternalCashConflictError(ExternalCashDeclarationError):
    """Expected-head or idempotency compare-and-set failed."""


class ExternalCashAmbiguousHeadError(ExternalCashDeclarationError):
    """More than one current head exists for a declaration scope."""


class ExternalCashValidationError(ExternalCashDeclarationError):
    """A time or value invariant failed at the service boundary."""


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExternalCashValidationError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _record(row: Any) -> ExternalCashDeclarationRecord:
    return ExternalCashDeclarationRecord.model_validate(row)


def _same_instant(left: datetime, right: datetime) -> bool:
    return _aware_utc(left, field="stored datetime") == _aware_utc(
        right, field="request datetime"
    )


def _matches_replay(
    row: Any,
    request: ExternalCashDeclarationRequest,
    *,
    actor_user_id: int,
) -> bool:
    return all(
        (
            row.owner_user_id == request.owner_user_id,
            row.location_key == request.location_key,
            row.display_label == request.display_label,
            row.currency == request.currency,
            Decimal(row.amount) == request.amount,
            _same_instant(row.as_of, request.as_of),
            row.source_note == request.source_note,
            row.declared_by_user_id == actor_user_id,
            row.origin == ORIGIN,
            row.supersedes_declaration_id == request.expected_head_declaration_id,
        )
    )


def _current_view(rows: list[Any], *, now: datetime) -> ExternalCashCurrentView:
    if not rows:
        return ExternalCashCurrentView(
            status="missing",
            amount_status="unknown",
            current=None,
            route_fundable_amount=None,
            warning_code="external_cash_missing",
        )
    if len(rows) != 1:
        return ExternalCashCurrentView(
            status="ambiguous",
            amount_status="unknown",
            current=None,
            route_fundable_amount=None,
            warning_code="external_cash_head_ambiguous",
        )

    row = rows[0]
    as_of = _aware_utc(row.as_of, field="as_of")
    fresh_until = _aware_utc(row.fresh_until, field="fresh_until")
    current_now = _aware_utc(now, field="now")
    record = _record(row)
    if as_of > current_now:
        return ExternalCashCurrentView(
            status="future",
            amount_status="unknown",
            current=record,
            route_fundable_amount=None,
            warning_code="external_cash_asof_future",
        )
    if current_now >= fresh_until:
        return ExternalCashCurrentView(
            status="stale",
            amount_status="unknown",
            current=record,
            route_fundable_amount=None,
            warning_code="external_cash_stale",
        )
    return ExternalCashCurrentView(
        status="fresh",
        amount_status="known",
        current=record,
        route_fundable_amount=Decimal(row.amount),
    )


class ExternalCashDeclarationService:
    """The only domain writer and canonical reader for the declaration ledger."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        _repository: ExternalCashDeclarationRepository | None = None,
    ) -> None:
        self._session = session
        self._repository = _repository or ExternalCashDeclarationRepository(session)

    async def declare(
        self,
        request: ExternalCashDeclarationRequest,
        actor: User,
        now: datetime,
    ) -> ExternalCashDeclarationRecord:
        """Append one declaration or return an exact idempotent replay."""

        if not getattr(actor, "is_active", False) or not has_min_role(
            actor.role, UserRole.admin
        ):
            raise ExternalCashAuthorizationError("active admin role required")
        actor_id = int(actor.id)
        current_now = _aware_utc(now, field="now")
        as_of = _aware_utc(request.as_of, field="as_of")
        if as_of > current_now:
            raise ExternalCashValidationError("as_of cannot be in the future")

        try:
            await self._repository.acquire_lock(
                f"external-cash:idempotency:{request.owner_user_id}:"
                f"{request.idempotency_key}"
            )
            existing = await self._repository.get_by_idempotency(
                owner_user_id=request.owner_user_id,
                idempotency_key=request.idempotency_key,
            )
            if existing is not None:
                if not _matches_replay(existing, request, actor_user_id=actor_id):
                    raise ExternalCashConflictError(
                        "idempotency key already has a different declaration"
                    )
                await self._session.commit()
                return _record(existing)

            await self._repository.acquire_lock(
                f"external-cash:scope:{request.owner_user_id}:"
                f"{request.location_key}:{request.currency}"
            )
            heads = await self._repository.list_current_heads(
                owner_user_id=request.owner_user_id,
                location_key=request.location_key,
                currency=request.currency,
                for_update=True,
            )
            if len(heads) > 1:
                raise ExternalCashAmbiguousHeadError(
                    "multiple current declaration heads; refusing to choose"
                )

            actual_head = heads[0].declaration_id if heads else None
            if actual_head != request.expected_head_declaration_id:
                raise ExternalCashConflictError(
                    "expected declaration head does not match current head"
                )

            row = await self._repository.insert(
                declaration_id=uuid.uuid4(),
                owner_user_id=request.owner_user_id,
                location_key=request.location_key,
                display_label=request.display_label,
                currency=request.currency,
                amount=request.amount,
                as_of=request.as_of,
                fresh_until=request.as_of + FRESHNESS_WINDOW,
                source_note=request.source_note,
                declared_by_user_id=actor_id,
                origin=ORIGIN,
                supersedes_declaration_id=request.expected_head_declaration_id,
                idempotency_key=request.idempotency_key,
            )
            await self._session.commit()
            return _record(row)
        except Exception:
            await self._session.rollback()
            raise

    async def current(
        self,
        *,
        owner_user_id: int,
        location_key: str,
        currency: str,
        now: datetime,
    ) -> ExternalCashCurrentView:
        rows = await self._repository.list_current_heads(
            owner_user_id=owner_user_id,
            location_key=location_key,
            currency=currency,
        )
        return _current_view(rows, now=now)

    async def list_current(
        self, *, owner_user_id: int, now: datetime
    ) -> list[ExternalCashCurrentView]:
        rows = await self._repository.list_all_current_heads(
            owner_user_id=owner_user_id
        )
        grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in rows:
            grouped[(row.location_key, row.currency)].append(row)
        return [_current_view(group, now=now) for group in grouped.values()]

    async def history(
        self,
        *,
        owner_user_id: int,
        location_key: str,
        currency: str,
        limit: int = 100,
    ) -> list[ExternalCashDeclarationRecord]:
        if not 1 <= limit <= 200:
            raise ExternalCashValidationError("history limit must be between 1 and 200")
        rows = await self._repository.list_history(
            owner_user_id=owner_user_id,
            location_key=location_key,
            currency=currency,
            limit=limit,
        )
        return [_record(row) for row in rows]


__all__ = [
    "ExternalCashAmbiguousHeadError",
    "ExternalCashAuthorizationError",
    "ExternalCashConflictError",
    "ExternalCashDeclarationError",
    "ExternalCashDeclarationService",
    "ExternalCashValidationError",
    "FRESHNESS_WINDOW",
]
