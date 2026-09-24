"""#671 — /invest manual_cash settings: auth, write guards, and read-back.

The read-back tests go through the real persistence and the real capital
read (``get_manual_cash_setting`` → ``get_available_capital_impl`` →
``resolve_parking_balance_krw`` → ``evaluate_deployment_cap``) against the
pytest-owned database. Only the broker cash fetch is stubbed.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import admin_router
from app.auth.admin_router import require_admin
from app.core.db import engine, get_db
from app.mcp_server.tooling import portfolio_cash, user_settings_tools
from app.models.trading import UserRole
from app.routers import invest_manual_cash
from app.routers.dependencies import get_authenticated_user
from app.services.deployment_cap import (
    PARKING_SOURCE_ABSENT,
    PARKING_SOURCE_MANUAL_CASH,
    PARKING_SOURCE_STALE,
)
from app.services.manual_cash_settings import (
    MANUAL_CASH_MAX_KRW,
    SOURCE_OPERATOR_CONFIRMED,
)
from tests._run_owned_database import validate_run_owned_database_url

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)
URL = "/invest/api/settings/manual-cash"


# --------------------------------------------------------------------------
# Static contract: auth + CSRF surface
# --------------------------------------------------------------------------


def _route(method: str):
    for route in invest_manual_cash.router.routes:
        if getattr(route, "path", None) == URL and method in route.methods:
            return route
    raise AssertionError(f"{method} {URL} not registered")


@pytest.mark.unit
def test_write_requires_admin_and_read_requires_session() -> None:
    put_calls = {dep.call for dep in _route("PUT").dependant.dependencies}
    get_calls = {dep.call for dep in _route("GET").dependant.dependencies}
    assert require_admin in put_calls
    assert get_authenticated_user in get_calls
    # the only mutating verb is PUT
    methods = {m for r in invest_manual_cash.router.routes for m in r.methods}
    assert methods == {"GET", "PUT"}


@pytest.mark.unit
def test_router_is_mounted_under_csrf_protected_invest_api() -> None:
    main_source = (Path(invest_manual_cash.__file__).parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "app.include_router(invest_manual_cash.router)" in main_source
    assert URL.startswith("/invest/api/")
    assert 're.compile(r"^/invest/' not in main_source


@pytest.mark.unit
def test_write_targets_the_row_the_capital_read_consumes() -> None:
    source = inspect.getsource(invest_manual_cash)
    assert "user_settings_tools.MCP_USER_ID" in source
    assert "admin.id" in source  # only as actor, never as owner
    assert "owner_user_id=admin.id" not in source


# --------------------------------------------------------------------------
# DB-backed tests
# --------------------------------------------------------------------------


async def _require_db() -> None:
    validate_run_owned_database_url(engine.url)
    async with SessionLocal() as session:
        row = await session.execute(text("SELECT to_regclass('user_settings')"))
        assert row.scalar_one_or_none() is not None, "user_settings not migrated"


async def _create_user(role: str = "admin") -> int:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:username, :email, :role, 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"manual_cash_671_{suffix}",
                    "email": f"manual_cash_671_{suffix}@example.com",
                    "role": role,
                },
            )
        ).scalar_one()
        await session.commit()
        return int(user_id)


async def _delete_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await session.commit()


async def _raw_value(user_id: int) -> Any:
    async with SessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT value FROM user_settings "
                    "WHERE user_id = :id AND key = 'manual_cash'"
                ),
                {"id": user_id},
            )
        ).scalar_one_or_none()


async def _backdate(user_id: int, delta: timedelta) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE user_settings SET updated_at = now() - CAST(:delta AS interval) "
                "WHERE user_id = :id AND key = 'manual_cash'"
            ),
            {"id": user_id, "delta": delta},
        )
        await session.commit()


@pytest_asyncio.fixture
async def owner(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[int]:
    """A fresh user standing in for MCP_USER_ID (reader and writer agree)."""
    await _require_db()
    user_id = await _create_user()
    monkeypatch.setattr(user_settings_tools, "MCP_USER_ID", user_id)
    try:
        yield user_id
    finally:
        await _delete_user(user_id)


def _app(*, role: UserRole = UserRole.admin, actor_id: int = 1) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_manual_cash.router)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with SessionLocal() as session:
            yield session

    actor = SimpleNamespace(id=actor_id, role=role)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_authenticated_user] = lambda: actor
    # require_admin itself stays real; only its session lookup is stubbed
    # (see the ``session_user`` fixture).
    return app


@pytest.fixture
def session_user(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, Any] = {"user": None}

    async def _lookup(_request, _db):
        return holder["user"]

    monkeypatch.setattr(admin_router, "get_current_user_from_session", _lookup)
    return holder


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _get(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(URL)
    assert response.status_code == 200, response.text
    return response.json()


async def _put(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.put(URL, json=body)


async def _capital() -> dict[str, Any]:
    async def _broker_cash(account=None, **_kwargs):
        return {
            "accounts": [
                {"account": "kis_domestic", "currency": "KRW", "orderable": 1_000_000.0}
            ],
            "summary": {"total_krw": 1_000_000.0},
            "errors": [],
        }

    original = portfolio_cash.get_cash_balance_impl
    portfolio_cash.get_cash_balance_impl = _broker_cash  # type: ignore[assignment]
    try:
        return await portfolio_cash.get_available_capital_impl()
    finally:
        portfolio_cash.get_cash_balance_impl = original  # type: ignore[assignment]


def _admin(session_user: dict[str, Any], actor_id: int) -> None:
    session_user["user"] = SimpleNamespace(id=actor_id, role=UserRole.admin)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_is_read_back_by_invest_and_deployment_cap(
    owner: int, session_user: dict[str, Any]
) -> None:
    _admin(session_user, owner)
    async with await _client(_app(actor_id=owner)) as client:
        before = await _get(client)
        assert before["manual_cash"]["present"] is False
        assert before["can_edit"] is True

        response = await _put(
            client,
            accounts=[
                {"name": "토스 파킹", "amount": 3_000_000},
                {"name": "CMA", "amount": 2_000_000},
            ],
            expected_updated_at=None,
            confirm_large_change=True,  # absent → positive is a jump
        )
        assert response.status_code == 200, response.text
        saved = response.json()["manual_cash"]
        assert saved["amount"] == 5_000_000
        assert saved["source"] == SOURCE_OPERATOR_CONFIRMED
        assert saved["stale"] is False

        after = await _get(client)
        assert after["manual_cash"]["amount"] == 5_000_000
        assert after["manual_cash"]["accounts"] == [
            {"name": "토스 파킹", "amount": 3_000_000},
            {"name": "CMA", "amount": 2_000_000},
        ]
        assert after["manual_cash"]["updated_at"] == saved["updated_at"]

    stored = await _raw_value(owner)
    assert stored["amount"] == 5_000_000
    assert stored["source"] == SOURCE_OPERATOR_CONFIRMED
    assert stored["confirmed_by_user_id"] == owner

    # the real MCP read path
    setting = await user_settings_tools.get_manual_cash_setting()
    assert setting is not None and setting["value"]["amount"] == 5_000_000

    capital = await _capital()
    assert capital["manual_cash"]["amount"] == 5_000_000
    assert capital["manual_cash"]["included_in_total"] is True
    assert capital["summary"]["total_orderable_krw"] == pytest.approx(6_000_000)
    cap = capital["summary"]["deployment_cap_advisory"]
    assert cap["parking_balance_krw"] == "5000000"
    assert cap["parking_balance_source"] == PARKING_SOURCE_MANUAL_CASH
    assert cap["broker_orderable_total_krw"] == "1000000"
    assert cap["denominator_krw"] == "6000000"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_large_change_is_blocked_without_confirm_and_nothing_is_written(
    owner: int, session_user: dict[str, Any]
) -> None:
    _admin(session_user, owner)
    async with await _client(_app(actor_id=owner)) as client:
        first = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 10_000_000}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
        assert first.status_code == 200
        head = first.json()["manual_cash"]["updated_at"]

        # first save from nothing also needs confirm
        await _delete_setting(owner)
        blocked_first = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 1}],
            expected_updated_at=None,
        )
        assert blocked_first.status_code == 409
        assert blocked_first.json()["detail"]["error"] == "confirm_required"
        assert await _raw_value(owner) is None

        again = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 10_000_000}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
        head = again.json()["manual_cash"]["updated_at"]

        # extra-zero typo: 10,000,000 → 100,000,000 (+900%)
        blocked = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 100_000_000}],
            expected_updated_at=head,
        )
        assert blocked.status_code == 409
        detail = blocked.json()["detail"]
        assert detail["error"] == "confirm_required"
        assert detail["current_amount"] == 10_000_000
        assert detail["new_amount"] == 100_000_000
        assert (await _raw_value(owner))["amount"] == 10_000_000

        # confirm must be a real boolean, not a truthy string
        coerced = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 100_000_000}],
            expected_updated_at=head,
            confirm_large_change="true",
        )
        assert coerced.status_code == 422
        assert (await _raw_value(owner))["amount"] == 10_000_000

        # exactly +50% passes without confirm
        within = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 15_000_000}],
            expected_updated_at=head,
        )
        assert within.status_code == 200, within.text
        head = within.json()["manual_cash"]["updated_at"]

        confirmed = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 100_000_000}],
            expected_updated_at=head,
            confirm_large_change=True,
        )
        assert confirmed.status_code == 200
        assert (await _raw_value(owner))["amount"] == 100_000_000


async def _delete_setting(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM user_settings WHERE user_id = :id AND key = 'manual_cash'"
            ),
            {"id": user_id},
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_form_is_rejected(owner: int, session_user: dict[str, Any]) -> None:
    _admin(session_user, owner)
    async with await _client(_app(actor_id=owner)) as client:
        first = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 1_000_000}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
        head = first.json()["manual_cash"]["updated_at"]

        # someone else (e.g. the MCP set_user_setting path) updates it
        await user_settings_tools.set_user_setting("manual_cash", {"amount": 1_100_000})

        stale = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 1_000_000}],
            expected_updated_at=head,
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["error"] == "stale_form"
        assert (await _raw_value(owner)) == {"amount": 1_100_000}

        # a form opened when nothing existed cannot overwrite an existing row
        blind = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 1_000_000}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
        assert blind.status_code == 409
        assert blind.json()["detail"]["error"] == "stale_form"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accounts",
    [
        [{"name": "파킹", "amount": -1}],
        [{"name": "파킹", "amount": MANUAL_CASH_MAX_KRW + 1}],
        [{"name": "파킹", "amount": "1000000"}],
        [{"name": "파킹", "amount": 1000000.0}],
        [{"name": "파킹", "amount": 1.5}],
        [{"name": "파킹", "amount": True}],
        [{"name": "파킹", "amount": None}],
        [{"name": "", "amount": 1}],
        [],
    ],
)
async def test_invalid_amounts_are_rejected_without_write(
    owner: int, session_user: dict[str, Any], accounts: list[Any]
) -> None:
    _admin(session_user, owner)
    async with await _client(_app(actor_id=owner)) as client:
        response = await _put(
            client,
            accounts=accounts,
            expected_updated_at=None,
            confirm_large_change=True,
        )
    assert response.status_code == 422, response.text
    assert await _raw_value(owner) is None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e3"])
async def test_non_integer_json_literals_are_rejected(
    owner: int, session_user: dict[str, Any], literal: str
) -> None:
    _admin(session_user, owner)
    body = (
        '{"accounts": [{"name": "p", "amount": ' + literal + "}],"
        ' "expected_updated_at": null, "confirm_large_change": true}'
    )
    async with await _client(_app(actor_id=owner)) as client:
        response = await client.put(
            URL, content=body, headers={"content-type": "application/json"}
        )
    assert response.status_code == 422, response.text
    assert await _raw_value(owner) is None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, MANUAL_CASH_MAX_KRW])
async def test_boundary_amounts_are_saved(
    owner: int, session_user: dict[str, Any], value: int
) -> None:
    _admin(session_user, owner)
    async with await _client(_app(actor_id=owner)) as client:
        response = await _put(
            client,
            accounts=[{"name": "파킹", "amount": value}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
    assert response.status_code == 200, response.text
    assert (await _raw_value(owner))["amount"] == value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_admin_cannot_write(owner: int, session_user: dict[str, Any]) -> None:
    session_user["user"] = SimpleNamespace(id=owner, role=UserRole.viewer)
    async with await _client(_app(role=UserRole.viewer, actor_id=owner)) as client:
        view = await _get(client)
        assert view["can_edit"] is False
        response = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 1}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
    assert response.status_code == 403
    assert await _raw_value(owner) is None

    session_user["user"] = None
    async with await _client(_app(actor_id=owner)) as client:
        response = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 1}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
    assert response.status_code == 401
    assert await _raw_value(owner) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_value_is_shown_stale_and_zeroed_in_deployment_cap(
    owner: int, session_user: dict[str, Any]
) -> None:
    _admin(session_user, owner)
    async with await _client(_app(actor_id=owner)) as client:
        await _put(
            client,
            accounts=[{"name": "파킹", "amount": 7_000_000}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
        await _backdate(owner, timedelta(days=3, minutes=1))
        view = (await _get(client))["manual_cash"]
    assert view["stale"] is True
    assert view["stale_after_hours"] == 72
    assert datetime.fromisoformat(view["stale_at"]) < datetime.now(UTC)

    capital = await _capital()
    assert capital["manual_cash"]["stale_warning"] is True
    assert capital["manual_cash"]["included_in_total"] is False
    assert capital["summary"]["manual_cash_excluded_krw"] == pytest.approx(7_000_000)
    assert capital["summary"]["total_orderable_krw"] == pytest.approx(1_000_000)
    cap = capital["summary"]["deployment_cap_advisory"]
    assert cap["parking_balance_krw"] == "0"
    assert cap["parking_balance_source"] == PARKING_SOURCE_STALE
    assert cap["cap_is_lower_bound"] is True


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["NaN", "Infinity", -5_000_000, "garbage"])
async def test_malformed_stored_amount_never_reaches_totals_or_cap(
    owner: int, amount: Any
) -> None:
    # written through the generic MCP settings path, bypassing the UI guards
    await user_settings_tools.set_user_setting("manual_cash", {"amount": amount})

    capital = await _capital()
    assert capital["manual_cash"]["invalid_amount"] is True
    assert capital["manual_cash"]["included_in_total"] is False
    assert capital["summary"]["total_orderable_krw"] == pytest.approx(1_000_000)
    assert {"source": "manual_cash", "error": "invalid_amount"} in capital["errors"]
    cap = capital["summary"]["deployment_cap_advisory"]
    assert cap["parking_balance_krw"] == "0"
    assert cap["parking_balance_source"] == PARKING_SOURCE_ABSENT
    assert cap["denominator_krw"] == "1000000"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_first_save_race_does_not_overwrite_a_concurrent_insert(
    owner: int, session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both writers saw 'absent'; the one that loses must not overwrite."""
    from app.services import manual_cash_settings

    _admin(session_user, owner)
    real_load = manual_cash_settings.load_manual_cash_row

    async def load_as_if_before_the_other_insert(
        db, *, owner_user_id, for_update=False
    ):
        if for_update:
            # Our locking SELECT ran before the other writer committed.
            await user_settings_tools.set_user_setting(
                "manual_cash", {"amount": 29_000_000}
            )
            return None
        return await real_load(db, owner_user_id=owner_user_id)

    monkeypatch.setattr(
        manual_cash_settings, "load_manual_cash_row", load_as_if_before_the_other_insert
    )
    async with await _client(_app(actor_id=owner)) as client:
        response = await _put(
            client,
            accounts=[{"name": "파킹", "amount": 290_000_000}],
            expected_updated_at=None,
            confirm_large_change=True,
        )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "stale_form"
    assert detail["current"]["amount"] == 29_000_000
    assert await _raw_value(owner) == {"amount": 29_000_000}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_lands_on_mcp_user_row_not_the_admin_row(
    owner: int, session_user: dict[str, Any]
) -> None:
    other_admin = await _create_user()
    try:
        _admin(session_user, other_admin)
        async with await _client(_app(actor_id=other_admin)) as client:
            response = await _put(
                client,
                accounts=[{"name": "파킹", "amount": 3_000_000}],
                expected_updated_at=None,
                confirm_large_change=True,
            )
        assert response.status_code == 200, response.text
        stored = await _raw_value(owner)
        assert stored["amount"] == 3_000_000
        assert stored["confirmed_by_user_id"] == other_admin
        assert await _raw_value(other_admin) is None
        capital = await _capital()
        cap = capital["summary"]["deployment_cap_advisory"]
        assert cap["parking_balance_krw"] == "3000000"
    finally:
        await _delete_user(other_admin)
