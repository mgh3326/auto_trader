from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.trading import UserRole
from app.routers import invest_funding


@pytest.mark.asyncio
async def test_page_get_refreshes_view_without_delivery_api(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeService:
        def __init__(self, _db):
            pass

        async def refresh_detail(self, **kwargs):
            calls.append(("refresh", kwargs))
            return {"status": "triggered", "delivery": {"action": "none"}}

    monkeypatch.setattr(invest_funding, "FundingAdvisoryService", FakeService)
    monkeypatch.setattr(
        invest_funding,
        "_now",
        lambda: datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
    )
    advisory_id = uuid4()

    result = await invest_funding.get_advisory(
        advisory_id=advisory_id,
        user=SimpleNamespace(id=11),
        db=object(),
        refresh=True,
    )

    assert result["delivery"] == {"action": "none"}
    assert calls == [
        (
            "refresh",
            {
                "advisory_id": advisory_id,
                "owner_user_id": 11,
                "now": datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
            },
        )
    ]
    source = inspect.getsource(invest_funding.get_advisory)
    assert "deliver_claimed_advisory" not in source
    assert "_claim_delivery" not in source


def test_router_write_delegates_to_service_and_never_commits_directly() -> None:
    source = inspect.getsource(invest_funding.declare_external_cash)
    assert "ExternalCashDeclarationService" in source
    assert ".declare(" in source
    assert ".commit(" not in source


def test_invest_api_prefix_remains_csrf_protected() -> None:
    main_source = (invest_funding.__file__ and inspect.getsource(invest_funding)) or ""
    assert 'prefix="/invest/api/funding"' in main_source
    app_main = (Path(invest_funding.__file__).parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert 're.compile(r"^/invest/api/")' not in app_main


NOW = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)
NOTICE = "선언은 매수력에 자동 가산되지 않음 — 입금 필요 알림의 근거"


def _record(
    *,
    amount: str = "0",
    declaration_id=None,
    supersedes=None,
    as_of: datetime | None = None,
):
    from app.schemas.funding_advisory import ExternalCashDeclarationRecord

    stamp = as_of or (NOW - timedelta(minutes=5))
    return ExternalCashDeclarationRecord(
        declaration_id=declaration_id or uuid4(),
        owner_user_id=11,
        location_key="parking_primary",
        display_label="파킹통장",
        currency="KRW",
        amount=Decimal(amount),
        as_of=stamp,
        fresh_until=stamp + timedelta(hours=24),
        source_note="운영자 선언",
        declared_by_user_id=7,
        origin="invest_ui",
        supersedes_declaration_id=supersedes,
        idempotency_key=f"funding-ui:{uuid4()}",
        recorded_at=stamp,
    )


def _view(record):
    from app.schemas.funding_advisory import ExternalCashCurrentView

    return ExternalCashCurrentView(
        status="fresh",
        amount_status="known",
        current=record,
        route_fundable_amount=record.amount,
    )


class MemoryCashService:
    def __init__(self, _db=None) -> None:
        self.rows: list = []

    async def list_current(self, **_kwargs):
        superseded = {
            row.supersedes_declaration_id
            for row in self.rows
            if row.supersedes_declaration_id is not None
        }
        heads = [row for row in self.rows if row.declaration_id not in superseded]
        return [_view(row) for row in heads]

    async def history(self, **_kwargs):
        return list(reversed(self.rows))

    async def declare(self, request, actor, now):
        from app.services.funding_advisory.external_cash import (
            ExternalCashAuthorizationError,
            ExternalCashConflictError,
            ExternalCashValidationError,
        )

        if getattr(actor, "role", None) != UserRole.admin:
            raise ExternalCashAuthorizationError("active admin role required")
        if request.as_of > now:
            raise ExternalCashValidationError("as_of cannot be in the future")
        heads = [
            row
            for row in self.rows
            if row.declaration_id
            not in {
                item.supersedes_declaration_id
                for item in self.rows
                if item.supersedes_declaration_id is not None
            }
        ]
        actual = heads[0] if heads else None
        actual_id = actual.declaration_id if actual is not None else None
        if actual_id != request.expected_head_declaration_id:
            raise ExternalCashConflictError(
                "expected declaration head does not match current head",
                current_head=actual,
            )
        row = _record(
            amount=str(request.amount),
            supersedes=request.expected_head_declaration_id,
            as_of=request.as_of,
        )
        self.rows.append(row)
        return row


def _app(*, user, service: MemoryCashService, override_admin: bool = True):
    from fastapi import FastAPI

    from app.auth.admin_router import require_admin
    from app.core.db import get_db
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_funding.router)
    app.dependency_overrides[get_authenticated_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: object()
    if override_admin:
        app.dependency_overrides[require_admin] = lambda: user
    return app, service


async def _client(app):
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_ac1_zero_parking_head_then_append_shows_two_history_rows(
    monkeypatch,
) -> None:
    service = MemoryCashService()
    zero = _record(amount="0")
    service.rows.append(zero)
    monkeypatch.setattr(
        invest_funding, "ExternalCashDeclarationService", lambda _db: service
    )
    monkeypatch.setattr(invest_funding, "_now", lambda: NOW)
    admin = SimpleNamespace(id=7, role=UserRole.admin, is_active=True)
    app, _svc = _app(user=admin, service=service)

    async with await _client(app) as client:
        current = await client.get("/invest/api/funding/external-cash/current")
        assert current.status_code == 200
        body = current.json()
        assert body["notice"] == NOTICE
        assert body["count"] == 1
        assert body["heads"][0]["current"]["amount"] == "0"
        assert body["heads"][0]["current"]["display_label"] == "파킹통장"

        created = await client.post(
            "/invest/api/funding/external-cash/declarations",
            json={
                "owner_user_id": 11,
                "location_key": "parking_primary",
                "display_label": "파킹통장",
                "currency": "KRW",
                "amount": "1500000",
                "as_of": NOW.isoformat(),
                "source_note": "급여 착지 후 잔액",
                "expected_head_declaration_id": str(zero.declaration_id),
                "idempotency_key": "funding-ui:ac1",
            },
        )
        assert created.status_code == 201
        assert created.json()["amount"] == "1500000"
        assert created.json()["supersedes_declaration_id"] == str(zero.declaration_id)

        after = await client.get("/invest/api/funding/external-cash/current")
        assert after.json()["heads"][0]["current"]["amount"] == "1500000"

        history = await client.get("/invest/api/funding/external-cash/history")
        assert history.status_code == 200
        assert history.json()["count"] == 2


@pytest.mark.asyncio
async def test_ac2_stale_head_returns_409_with_current_head(monkeypatch) -> None:
    service = MemoryCashService()
    head = _record(amount="0")
    service.rows.append(head)
    monkeypatch.setattr(
        invest_funding, "ExternalCashDeclarationService", lambda _db: service
    )
    monkeypatch.setattr(invest_funding, "_now", lambda: NOW)
    admin = SimpleNamespace(id=7, role=UserRole.admin, is_active=True)
    app, _svc = _app(user=admin, service=service)

    async with await _client(app) as client:
        response = await client.post(
            "/invest/api/funding/external-cash/declarations",
            json={
                "owner_user_id": 11,
                "location_key": "parking_primary",
                "display_label": "파킹통장",
                "currency": "KRW",
                "amount": "1",
                "as_of": NOW.isoformat(),
                "source_note": "stale submit",
                "expected_head_declaration_id": str(uuid4()),
                "idempotency_key": "funding-ui:stale",
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "expected_head_conflict"
    assert detail["current_head"]["declaration_id"] == str(head.declaration_id)
    assert detail["current_head"]["amount"] == "0"
    assert detail["notice"] == NOTICE


@pytest.mark.asyncio
async def test_ac3_non_admin_write_is_rejected(monkeypatch) -> None:
    trader = SimpleNamespace(id=2, role=UserRole.trader, is_active=True)

    async def session_user(_request, _db):
        return trader

    monkeypatch.setattr(
        "app.auth.admin_router.get_current_user_from_session",
        session_user,
    )
    service = MemoryCashService()
    monkeypatch.setattr(
        invest_funding, "ExternalCashDeclarationService", lambda _db: service
    )
    monkeypatch.setattr(invest_funding, "_now", lambda: NOW)
    app, _svc = _app(user=trader, service=service, override_admin=False)

    async with await _client(app) as client:
        form = await client.get("/invest/api/funding/external-cash/form")
        write = await client.post(
            "/invest/api/funding/external-cash/declarations",
            json={
                "owner_user_id": 11,
                "location_key": "parking_primary",
                "display_label": "파킹통장",
                "currency": "KRW",
                "amount": "1",
                "as_of": NOW.isoformat(),
                "source_note": "trader write",
                "expected_head_declaration_id": None,
                "idempotency_key": "funding-ui:trader",
            },
        )
        current = await client.get("/invest/api/funding/external-cash/current")

    assert form.status_code == 403
    assert write.status_code == 403
    assert current.status_code == 200
    assert service.rows == []


@pytest.mark.asyncio
async def test_as_of_future_is_rejected_on_the_router_surface(monkeypatch) -> None:
    service = MemoryCashService()
    monkeypatch.setattr(
        invest_funding, "ExternalCashDeclarationService", lambda _db: service
    )
    monkeypatch.setattr(invest_funding, "_now", lambda: NOW)
    admin = SimpleNamespace(id=7, role=UserRole.admin, is_active=True)
    app, _svc = _app(user=admin, service=service)

    async with await _client(app) as client:
        form = await client.get("/invest/api/funding/external-cash/form")
        response = await client.post(
            "/invest/api/funding/external-cash/declarations",
            json={
                "owner_user_id": 11,
                "location_key": "parking_primary",
                "display_label": "파킹통장",
                "currency": "KRW",
                "amount": "0",
                "as_of": (NOW + timedelta(seconds=1)).isoformat(),
                "source_note": "future",
                "expected_head_declaration_id": None,
                "idempotency_key": "funding-ui:future",
            },
        )

    assert form.status_code == 200
    assert form.json()["as_of_fixed"] is True
    assert str(form.json()["as_of"]).startswith("2026-08-20T07:30:00")
    assert response.status_code == 422
    assert service.rows == []
