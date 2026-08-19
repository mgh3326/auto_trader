"""Run-owned DB round-trip for append-only external-cash declarations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models.trading import UserRole
from app.schemas.funding_advisory import ExternalCashDeclarationRequest
from app.services.funding_advisory.external_cash import (
    ExternalCashConflictError,
    ExternalCashDeclarationService,
    ExternalCashValidationError,
)

NOW = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)


def _admin(user):
    user.role = UserRole.admin
    user.is_active = True
    return user


def _request(*, owner_id: int, amount: str, expected_head, idempotency: str, as_of):
    return ExternalCashDeclarationRequest(
        owner_user_id=owner_id,
        location_key="parking_primary",
        display_label="파킹통장",
        currency="KRW",
        amount=Decimal(amount),
        as_of=as_of,
        source_note="운영자 선언",
        expected_head_declaration_id=expected_head,
        idempotency_key=idempotency,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_db_zero_head_append_history_and_stale_conflict(db_session, user):
    admin = _admin(user)
    await db_session.flush()
    service = ExternalCashDeclarationService(db_session)

    zero = await service.declare(
        _request(
            owner_id=admin.id,
            amount="0",
            expected_head=None,
            idempotency=f"funding-db-zero-{uuid4()}",
            as_of=NOW - timedelta(minutes=2),
        ),
        admin,
        NOW,
    )
    next_row = await service.declare(
        _request(
            owner_id=admin.id,
            amount="1500000",
            expected_head=zero.declaration_id,
            idempotency=f"funding-db-next-{uuid4()}",
            as_of=NOW - timedelta(minutes=1),
        ),
        admin,
        NOW,
    )

    current = await service.current(
        owner_user_id=admin.id,
        location_key="parking_primary",
        currency="KRW",
        now=NOW,
    )
    history = await service.history(owner_user_id=admin.id)

    assert current.current is not None
    assert current.current.declaration_id == next_row.declaration_id
    assert current.current.amount == Decimal("1500000")
    assert [row.amount for row in history[:2]] == [
        Decimal("1500000"),
        Decimal("0"),
    ]

    with pytest.raises(ExternalCashConflictError) as exc_info:
        await service.declare(
            _request(
                owner_id=admin.id,
                amount="2",
                expected_head=zero.declaration_id,
                idempotency=f"funding-db-stale-{uuid4()}",
                as_of=NOW,
            ),
            admin,
            NOW,
        )
    assert exc_info.value.current_head is not None
    assert exc_info.value.current_head.declaration_id == next_row.declaration_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_db_append_only_rejects_update_and_future_as_of(db_session, user):
    admin = _admin(user)
    await db_session.flush()
    service = ExternalCashDeclarationService(db_session)
    row = await service.declare(
        _request(
            owner_id=admin.id,
            amount="0",
            expected_head=None,
            idempotency=f"funding-db-append-{uuid4()}",
            as_of=NOW - timedelta(minutes=1),
        ),
        admin,
        NOW,
    )

    with pytest.raises(ExternalCashValidationError, match="future"):
        await service.declare(
            _request(
                owner_id=admin.id,
                amount="1",
                expected_head=row.declaration_id,
                idempotency=f"funding-db-future-{uuid4()}",
                as_of=NOW + timedelta(seconds=1),
            ),
            admin,
            NOW,
        )

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "UPDATE review.external_cash_declarations "
                "SET amount = 1 WHERE declaration_id = :declaration_id"
            ),
            {"declaration_id": str(row.declaration_id)},
        )
        await db_session.commit()
    await db_session.rollback()
