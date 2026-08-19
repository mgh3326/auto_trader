from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.models.trading import UserRole
from app.schemas.funding_advisory import ExternalCashDeclarationRequest
from app.services.funding_advisory.external_cash import (
    ExternalCashAmbiguousHeadError,
    ExternalCashAuthorizationError,
    ExternalCashConflictError,
    ExternalCashDeclarationService,
    ExternalCashValidationError,
)
from app.services.funding_advisory.initial_declaration import (
    build_initial_parking_declaration,
)

NOW = datetime(2026, 8, 15, 1, 0, tzinfo=UTC)
AS_OF = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeRepository:
    def __init__(self) -> None:
        self.locks: list[str] = []
        self.existing = None
        self.heads: list[SimpleNamespace] = []
        self.history_rows: list[SimpleNamespace] = []
        self.inserted: list[dict] = []

    async def acquire_lock(self, lock_key: str) -> None:
        self.locks.append(lock_key)

    async def get_by_idempotency(self, **_kwargs):
        return self.existing

    async def list_current_heads(self, **_kwargs):
        return list(self.heads)

    async def list_all_current_heads(self, **_kwargs):
        return list(self.heads)

    async def list_history(self, **_kwargs):
        return list(self.history_rows)

    async def insert(self, **columns):
        self.inserted.append(columns)
        row = SimpleNamespace(
            id=1,
            recorded_at=NOW,
            **columns,
        )
        self.existing = row
        self.heads = [row]
        return row


def actor(*, role: UserRole = UserRole.admin, active: bool = True):
    return SimpleNamespace(id=7, role=role, is_active=active)


def request(
    *,
    idempotency_key: str = "funding-seed-20260815-1",
    expected_head: UUID | None = None,
    amount: Decimal = Decimal("640000"),
) -> ExternalCashDeclarationRequest:
    return ExternalCashDeclarationRequest(
        owner_user_id=11,
        location_key="parking_primary",
        display_label="파킹통장",
        currency="KRW",
        amount=amount,
        as_of=NOW - timedelta(minutes=30),
        source_note="토스증권 → 파킹통장 이동",
        expected_head_declaration_id=expected_head,
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_declare_seed_shape_commits_one_append_only_snapshot() -> None:
    session = FakeSession()
    repository = FakeRepository()
    service = ExternalCashDeclarationService(
        session,  # type: ignore[arg-type]
        _repository=repository,  # type: ignore[arg-type]
    )

    result = await service.declare(request(), actor(), NOW)

    assert result.amount == Decimal("640000")
    assert result.fresh_until == result.as_of + timedelta(hours=24)
    assert result.supersedes_declaration_id is None
    assert len(repository.inserted) == 1
    assert len(repository.locks) == 2
    assert session.commits == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_exact_idempotent_replay_returns_original_without_second_insert() -> None:
    session = FakeSession()
    repository = FakeRepository()
    service = ExternalCashDeclarationService(
        session,  # type: ignore[arg-type]
        _repository=repository,  # type: ignore[arg-type]
    )
    first = await service.declare(request(), actor(), NOW)

    replay = await service.declare(request(), actor(), NOW)

    assert replay.declaration_id == first.declaration_id
    assert len(repository.inserted) == 1
    assert session.commits == 2


@pytest.mark.asyncio
async def test_idempotency_key_with_different_payload_is_conflict() -> None:
    session = FakeSession()
    repository = FakeRepository()
    service = ExternalCashDeclarationService(
        session,  # type: ignore[arg-type]
        _repository=repository,  # type: ignore[arg-type]
    )
    await service.declare(request(), actor(), NOW)

    with pytest.raises(ExternalCashConflictError):
        await service.declare(request(amount=Decimal("639999")), actor(), NOW)

    assert len(repository.inserted) == 1
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_stale_expected_head_and_ambiguous_heads_fail_closed() -> None:
    session = FakeSession()
    repository = FakeRepository()
    repository.heads = [SimpleNamespace(declaration_id=uuid4())]
    service = ExternalCashDeclarationService(
        session,  # type: ignore[arg-type]
        _repository=repository,  # type: ignore[arg-type]
    )

    with pytest.raises(ExternalCashConflictError):
        await service.declare(request(expected_head=uuid4()), actor(), NOW)

    repository.heads = [
        SimpleNamespace(declaration_id=uuid4()),
        SimpleNamespace(declaration_id=uuid4()),
    ]
    with pytest.raises(ExternalCashAmbiguousHeadError):
        await service.declare(
            request(idempotency_key="funding-seed-20260815-2"), actor(), NOW
        )

    assert repository.inserted == []


@pytest.mark.asyncio
async def test_stale_declaration_is_history_only_not_fundable() -> None:
    session = FakeSession()
    repository = FakeRepository()
    row = SimpleNamespace(
        declaration_id=uuid4(),
        owner_user_id=11,
        location_key="parking_primary",
        display_label="파킹통장",
        currency="KRW",
        amount=Decimal("640000"),
        as_of=NOW - timedelta(hours=25),
        fresh_until=NOW - timedelta(hours=1),
        source_note="토스증권 → 파킹통장 이동",
        declared_by_user_id=7,
        origin="invest_ui",
        supersedes_declaration_id=None,
        idempotency_key="funding-seed-20260815-1",
        recorded_at=NOW - timedelta(hours=25),
    )
    repository.heads = [row]
    service = ExternalCashDeclarationService(
        session,  # type: ignore[arg-type]
        _repository=repository,  # type: ignore[arg-type]
    )

    current = await service.current(
        owner_user_id=11,
        location_key="parking_primary",
        currency="KRW",
        now=NOW,
    )

    assert current.status == "stale"
    assert current.current is not None
    assert current.current.amount == Decimal("640000")
    assert current.route_fundable_amount is None
    assert current.amount_status == "unknown"


@pytest.mark.asyncio
async def test_declare_requires_active_admin_and_nonfuture_asof() -> None:
    session = FakeSession()
    repository = FakeRepository()
    service = ExternalCashDeclarationService(
        session,  # type: ignore[arg-type]
        _repository=repository,  # type: ignore[arg-type]
    )

    with pytest.raises(ExternalCashAuthorizationError):
        await service.declare(request(), actor(role=UserRole.trader), NOW)

    future_request = request().model_copy(update={"as_of": NOW + timedelta(seconds=1)})
    with pytest.raises(ExternalCashValidationError):
        await service.declare(future_request, actor(), NOW)

    assert repository.inserted == []


def test_initial_template_requires_operator_confirmed_timezone_and_kst_date() -> None:
    with pytest.raises(ValueError, match="timezone"):
        build_initial_parking_declaration(
            owner_user_id=11,
            as_of=datetime(2026, 8, 15, 10, 0),
            idempotency_key="funding-seed-20260815-1",
        )
    with pytest.raises(ValueError, match="2026-08-15"):
        build_initial_parking_declaration(
            owner_user_id=11,
            as_of=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
            idempotency_key="funding-seed-20260815-1",
        )

    built = build_initial_parking_declaration(
        owner_user_id=11,
        as_of=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        idempotency_key="funding-seed-20260815-1",
    )
    assert built.amount == Decimal("640000")
    assert built.expected_head_declaration_id is None


def test_schema_rejects_fractional_krw() -> None:
    with pytest.raises(ValidationError, match="whole won"):
        request(amount=Decimal("640000.5"))
