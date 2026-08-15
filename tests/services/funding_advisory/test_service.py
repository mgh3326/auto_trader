from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.schemas.funding_advisory import (
    ExternalCashCurrentView,
    ExternalCashDeclarationRecord,
)
from app.services.funding_advisory.contracts import (
    FundingAssessment,
    FundingCandidateEvent,
    PassedNonFundingGateEvidence,
)
from app.services.funding_advisory.service import FundingAdvisoryService

NOW = datetime(2026, 8, 15, 0, 5, tzinfo=UTC)


def event(
    *,
    required: str = "100000",
    available: str = "40000",
    other_pending: str = "20000",
    reserved: str = "10000",
) -> FundingCandidateEvent:
    evidence = PassedNonFundingGateEvidence.issue(
        owner_user_id=11,
        source_kind="upbit_live_candidate",
        source_candidate_id="candidate-1",
        gate_name="crypto_non_funding_gate",
        gate_version="crypto-gate.v1",
        gate_verdict="passed",
        gate_evaluated_at=NOW - timedelta(minutes=5),
        valid_until=NOW + timedelta(hours=1),
        market="crypto",
        target_account_mode="upbit",
        broker_account_id="upbit-primary",
        currency="KRW",
        symbol="KRW-BTC",
        side="buy",
        order_type="limit",
        quantity="0.001",
        price_reference="100000000",
        blocking_reasons=[],
        non_funding_checks=[
            {
                "check_id": "candidate_quality",
                "check_version": "v3",
                "verdict": "passed",
                "evaluated_at": NOW - timedelta(minutes=5),
            }
        ],
    )
    assessment = FundingAssessment(
        required_cash=Decimal(required),
        target_buying_power=Decimal(available),
        other_pending_required=Decimal(other_pending),
        reserved_cash=Decimal(reserved),
        currency="KRW",
        observed_at=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=30),
        source="upbit_accounts_free_krw",
    )
    return FundingCandidateEvent(evidence=evidence, assessment=assessment)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeExternalCash:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []

    async def list_current(self, **_kwargs):
        return list(self.rows)


class FakeRepository:
    def __init__(self) -> None:
        self.advisory = None
        self.revisions: list[SimpleNamespace] = []
        self.delivery = None
        self.delivery_inserts = 0
        self.locks: list[str] = []

    async def acquire_lock(self, lock_key: str) -> None:
        self.locks.append(lock_key)

    async def get_advisory_by_thread(self, _thread_key, **_kwargs):
        return self.advisory

    async def get_advisory(self, advisory_id: UUID, **_kwargs):
        if self.advisory and self.advisory.advisory_id == advisory_id:
            return self.advisory
        return None

    async def insert_advisory(self, **columns):
        self.advisory = SimpleNamespace(id=1, **columns)
        return self.advisory

    async def update_advisory(self, row, **columns) -> None:
        for key, value in columns.items():
            setattr(row, key, value)

    async def latest_revision(self, _advisory_id):
        return self.revisions[-1] if self.revisions else None

    async def get_revision_by_fingerprint(self, **kwargs):
        return next(
            (
                row
                for row in self.revisions
                if row.advisory_id == kwargs["advisory_id"]
                and row.fingerprint == kwargs["fingerprint"]
            ),
            None,
        )

    async def insert_revision(self, **columns):
        row = SimpleNamespace(id=len(self.revisions) + 1, **columns)
        self.revisions.append(row)
        return row

    async def list_advisories(self, **_kwargs):
        return [self.advisory] if self.advisory else []

    async def get_delivery(self, **_kwargs):
        return self.delivery

    async def get_delivery_by_id(self, delivery_id, **_kwargs):
        if self.delivery and self.delivery.delivery_id == delivery_id:
            return self.delivery
        return None

    async def insert_delivery(self, **columns):
        self.delivery_inserts += 1
        self.delivery = SimpleNamespace(
            id=1,
            chat_id=None,
            message_id=None,
            failure_code=None,
            delivered_at=None,
            **columns,
        )
        return self.delivery

    async def update_delivery(self, row, **columns) -> None:
        for key, value in columns.items():
            setattr(row, key, value)


def external_cash_view() -> ExternalCashCurrentView:
    record = ExternalCashDeclarationRecord(
        declaration_id=uuid4(),
        owner_user_id=11,
        location_key="parking_primary",
        display_label="파킹통장",
        currency="KRW",
        amount=Decimal("640000"),
        as_of=NOW - timedelta(hours=1),
        fresh_until=NOW + timedelta(hours=23),
        source_note="토스증권 → 파킹통장 이동",
        declared_by_user_id=7,
        origin="invest_ui",
        supersedes_declaration_id=None,
        idempotency_key="seed-1",
        recorded_at=NOW - timedelta(hours=1),
    )
    return ExternalCashCurrentView(
        status="fresh",
        amount_status="known",
        current=record,
        route_fundable_amount=record.amount,
    )


def service(*, external_rows=None):
    session = FakeSession()
    repository = FakeRepository()
    instance = FundingAdvisoryService(
        session,  # type: ignore[arg-type]
        _repository=repository,  # type: ignore[arg-type]
        _external_cash_service=FakeExternalCash(external_rows),  # type: ignore[arg-type]
    )
    return instance, repository, session


@pytest.mark.asyncio
async def test_shortfall_is_candidate_only_and_other_pending_is_disclosed() -> None:
    instance, repository, session = service(external_rows=[external_cash_view()])

    result = await instance.evaluate_candidate_event(event(), now=NOW)

    assert result["status"] == "triggered"
    assert result["need"] == {
        "required_cash": "100000",
        "target_buying_power": "40000",
        "shortfall": "60000",
        "funding_needed": "60000",
        "other_pending_required": "20000",
        "reserved_cash": "10000",
        "operational_gap_including_other_pending": "90000",
        "shortfall_scope": "this_candidate_only",
    }
    assert result["delivery"]["action"] == "send"
    assert result["combination"]["selected"] is False
    assert len(result["routes"]) == 5
    assert result["routes"][0]["route_fundable_amount"] == "60000"
    assert result["routes"][0]["counted_fundable_amount"] == "0"
    assert repository.delivery_inserts == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_declared_cash_never_changes_available_required_or_shortfall() -> None:
    without_declaration, _repository, _session = service()
    with_declaration, _repository_with, _session_with = service(
        external_rows=[external_cash_view()]
    )

    baseline = await without_declaration.evaluate_candidate_event(event(), now=NOW)
    declared = await with_declaration.evaluate_candidate_event(event(), now=NOW)

    assert declared["need"] == baseline["need"]
    assert declared["routes"][0]["route_fundable_amount"] == "60000"
    assert declared["routes"][0]["counted_fundable_amount"] == "0"
    assert declared["safety"]["authoritative_for_order_gate"] is False


@pytest.mark.asyncio
async def test_same_event_reuses_revision_and_daily_delivery_claim() -> None:
    instance, repository, _session = service()
    first = await instance.evaluate_candidate_event(event(), now=NOW)

    second = await instance.evaluate_candidate_event(
        event(), now=NOW + timedelta(minutes=1)
    )

    assert first["revision_id"] == second["revision_id"]
    assert len(repository.revisions) == 1
    assert repository.delivery_inserts == 1
    assert second["delivery"]["action"] == "none"
    assert second["delivery"]["reason"] == "same_day_delivery_already_claimed"


@pytest.mark.asyncio
async def test_same_day_changed_revision_edits_existing_message_not_new_send() -> None:
    instance, repository, _session = service()
    await instance.evaluate_candidate_event(event(), now=NOW)
    repository.delivery.state = "sent"
    repository.delivery.chat_id = "chat-1"
    repository.delivery.message_id = 42

    changed = await instance.evaluate_candidate_event(
        event(available="30000"), now=NOW + timedelta(minutes=1)
    )

    assert len(repository.revisions) == 2
    assert repository.delivery_inserts == 1
    assert changed["delivery"] == {
        "action": "edit",
        "delivery_id": str(repository.delivery.delivery_id),
        "chat_id": "chat-1",
        "message_id": 42,
    }


@pytest.mark.asyncio
async def test_page_refresh_recalculates_revision_without_delivery_claim() -> None:
    instance, repository, _session = service()
    created = await instance.evaluate_candidate_event(event(), now=NOW)
    claims_before = repository.delivery_inserts

    refreshed = await instance.refresh_detail(
        advisory_id=UUID(created["advisory_id"]),
        owner_user_id=11,
        now=NOW + timedelta(minutes=1),
    )

    assert refreshed["delivery"] == {
        "action": "none",
        "reason": "page_refresh_no_delivery",
    }
    assert repository.delivery_inserts == claims_before


@pytest.mark.asyncio
async def test_no_shortfall_resolves_without_route_or_delivery() -> None:
    instance, repository, _session = service()

    result = await instance.evaluate_candidate_event(
        event(required="100000", available="110000"), now=NOW
    )

    assert result["status"] == "not_triggered"
    assert result["reason"] == "no_candidate_shortfall"
    assert repository.advisory is None
    assert repository.delivery_inserts == 0
