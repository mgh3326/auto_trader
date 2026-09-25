"""#728 protected-position settings route contract and DB-backed safety tests."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import admin_router
from app.auth.admin_router import require_admin
from app.core.db import AsyncSessionLocal, engine, get_db
from app.middleware.csrf import TemplateFormCSRFMiddleware
from app.models.trading import UserRole
from app.routers import invest_protected_positions as protected_router
from app.routers.dependencies import get_authenticated_user
from app.services.protected_position_settings import BrokerObservationUnavailable
from app.services.protected_quantity_service import (
    _RELEASE_ADVISORY_LOCK,
    _TRY_ADVISORY_LOCK,
    BrokerPositionObservation,
    ProtectedQuantityService,
    _advisory_key,
    normalize_protection_key,
)
from tests._run_owned_database import validate_run_owned_database_url

pytestmark = pytest.mark.integration

URL = "/invest/api/settings/protected-positions"
SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


def _route(method: str, suffix: str = ""):
    path = f"{URL}{suffix}"
    for route in protected_router.router.routes:
        if getattr(route, "path", None) == path and method in route.methods:
            return route
    raise AssertionError(f"{method} {path} not registered")


def _key(symbol: str):
    return normalize_protection_key(
        account_scope="kis_live", market="kr", symbol=symbol
    )


def _observation(*, held: str = "10", sellable: str = "8") -> BrokerPositionObservation:
    return BrokerPositionObservation(
        held=Decimal(held),
        sellable=Decimal(sellable),
        observed_at=datetime.now(UTC),
    )


async def _require_db() -> None:
    validate_run_owned_database_url(engine.url)
    async with SessionLocal() as session:
        assert (
            await session.execute(
                text("SELECT to_regclass('review.protected_positions')")
            )
        ).scalar_one_or_none() is not None


def _app(
    *, role: UserRole = UserRole.admin, actor_id: int = 728001, csrf: bool = False
) -> FastAPI:
    app = FastAPI()
    if csrf:

        @app.get("/csrf-seed")
        async def csrf_seed():
            return {"ok": True}

    app.include_router(protected_router.router)

    async def db_dependency():
        async with AsyncSessionLocal() as session:
            yield session

    actor = SimpleNamespace(id=actor_id, role=role)
    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_authenticated_user] = lambda: actor
    if csrf:
        app.add_middleware(TemplateFormCSRFMiddleware, secret="protected-position-test")
    return app


@pytest.fixture
def session_user(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, Any] = {"user": None}

    async def lookup(_request, _db):
        return holder["user"]

    monkeypatch.setattr(admin_router, "get_current_user_from_session", lookup)
    return holder


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _body(
    *,
    quantity: Any = "6",
    expected_revision: int | None = None,
    confirmed: bool = True,
    key: str | None = None,
    reason: str = "operator checked lot plan",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "protected_quantity": quantity,
        "reason": reason,
        "expected_revision": expected_revision,
        "idempotency_key": key or f"route-{uuid4()}",
        "confirm_protection_change": confirmed,
        **extra,
    }


async def _put(client: httpx.AsyncClient, symbol: str, **body: Any) -> httpx.Response:
    return await client.put(f"{URL}/kis_live/kr/{symbol}", json=body)


def _admin(session_user: dict[str, Any], actor_id: int) -> None:
    session_user["user"] = SimpleNamespace(id=actor_id, role=UserRole.admin)


@pytest.mark.unit
def test_surface_requires_session_admin_and_csrf_protected_mount() -> None:
    get_calls = {dep.call for dep in _route("GET").dependant.dependencies}
    put_calls = {
        dep.call
        for dep in _route(
            "PUT", "/{account_scope}/{market}/{symbol}"
        ).dependant.dependencies
    }
    assert get_authenticated_user in get_calls
    assert require_admin in put_calls
    source = (
        Path(protected_router.__file__)
        .parents[1]
        .joinpath("main.py")
        .read_text(encoding="utf-8")
    )
    assert "app.include_router(invest_protected_positions.router)" in source
    assert URL.startswith("/invest/api/")
    assert 're.compile(r"^/invest/' not in source


@pytest.mark.unit
def test_owner_context_is_fixed_and_mcp_and_cli_stay_read_only() -> None:
    route_source = inspect.getsource(protected_router)
    mcp_source = Path("app/mcp_server/tooling/protected_positions.py").read_text(
        encoding="utf-8"
    )
    cli_source = Path("scripts/protected_positions.py").read_text(encoding="utf-8")
    assert "user_settings_tools.MCP_USER_ID" in route_source
    assert "actor_user_id=admin.id" in route_source
    assert "owner_user_id=admin.id" not in route_source
    assert "get_protected_positions" in mcp_source
    assert ".save(" not in mcp_source
    assert "save" not in {"list", "show", "history"}
    assert ".save(" not in cli_source
    assert "brokers" not in cli_source


@pytest.mark.asyncio
async def test_csrf_blocks_mutation_before_broker_provider(
    session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(session_user, 728002)
    calls = 0

    async def broker(**_kwargs):
        nonlocal calls
        calls += 1
        return _observation()

    monkeypatch.setattr(protected_router, "fresh_broker_observation", broker)
    async with await _client(_app(actor_id=728002, csrf=True)) as client:
        response = await _put(client, "TCSRF728", **_body())
    assert response.status_code == 403
    assert calls == 0


@pytest.mark.asyncio
async def test_read_exposes_unverified_not_synthetic_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read_rows(_db):
        return [
            {
                "account_scope": "kis_live",
                "market": "kr",
                "symbol": "TUNVERIFIED",
                "name": "검증 불가",
                "protected_quantity": "4",
                "broker_held": None,
                "broker_sellable": None,
                "headroom": None,
                "state": "unverified",
                "mode": "off",
                "read_error": "broker_read_failed",
                "revision": 3,
                "latest_revision": None,
                "history_url": f"{URL}/kis_live/kr/TUNVERIFIED/history",
                "broker_observed_at": None,
            }
        ]

    monkeypatch.setattr(protected_router, "read_protected_position_settings", read_rows)
    async with await _client(_app(role=UserRole.viewer)) as client:
        response = await client.get(URL)
    assert response.status_code == 200
    row = response.json()["positions"][0]
    assert response.json()["can_edit"] is False
    assert row["state"] == "unverified"
    assert row["broker_held"] is None and row["broker_sellable"] is None
    assert row["protected_quantity"] == "4"


@pytest.mark.asyncio
async def test_preview_requires_confirmation_with_fresh_evidence_and_bad_quantity_is_422(
    session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(session_user, 728003)

    async def broker(**_kwargs):
        return _observation(held="10", sellable="8")

    monkeypatch.setattr(protected_router, "fresh_broker_observation", broker)
    async with await _client(_app(actor_id=728003)) as client:
        preview = await _put(
            client,
            "TPREVIEW728",
            **_body(quantity="6", confirmed=False),
        )
        too_large = await _put(
            client,
            "TPREVIEWTOO728",
            **_body(quantity="11", confirmed=False),
        )
    assert preview.status_code == 409
    detail = preview.json()["detail"]
    assert detail["error"] == "confirm_required"
    assert detail["preview"] == {
        "account_scope": "kis_live",
        "market": "kr",
        "symbol": "TPREVIEW728",
        "before_protected_quantity": "0",
        "after_protected_quantity": "6",
        "broker_held": "10",
        "broker_sellable": "8",
        "before_headroom": "8",
        "headroom": "2",
        "state": "covered",
        "broker_observed_at": detail["preview"]["broker_observed_at"],
    }
    assert too_large.status_code == 422
    assert too_large.json()["detail"]["error"] == "protected_quantity_exceeds_held"


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [1, 1.0, True, "NaN", "Infinity", "-1"])
async def test_exact_decimal_strings_reject_json_numbers_and_nonfinite_values(
    session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch, quantity: Any
) -> None:
    _admin(session_user, 728004)
    calls = 0

    async def broker(**_kwargs):
        nonlocal calls
        calls += 1
        return _observation()

    monkeypatch.setattr(protected_router, "fresh_broker_observation", broker)
    async with await _client(_app(actor_id=728004)) as client:
        response = await _put(
            client, f"TBAD{uuid4().hex[:7].upper()}", **_body(quantity=quantity)
        )
        extra = await _put(
            client, f"TEXTRA{uuid4().hex[:7].upper()}", **_body(unexpected=True)
        )
        bad_boolean = await _put(
            client,
            f"TBOOL{uuid4().hex[:7].upper()}",
            **_body(confirm_protection_change="true"),
        )
    assert response.status_code == 422
    assert extra.status_code == 422
    assert bad_boolean.status_code == 422
    assert calls == 0


@pytest.mark.asyncio
async def test_confirmed_save_replays_idempotently_and_broker_failure_rolls_back(
    session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _require_db()
    actor_id = 728005
    _admin(session_user, actor_id)
    calls = 0

    async def broker(**_kwargs):
        nonlocal calls
        calls += 1
        return _observation()

    monkeypatch.setattr(protected_router, "fresh_broker_observation", broker)
    symbol = f"TIDEMP{uuid4().hex[:7].upper()}"
    payload = _body(key=f"idempotent-{uuid4()}")
    async with await _client(_app(actor_id=actor_id)) as client:
        first = await _put(client, symbol, **payload)
        replay = await _put(client, symbol, **payload)
        history = await client.get(f"{URL}/kis_live/kr/{symbol}/history")
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert history.status_code == 200, history.text
    assert history.json()["history"][0]["reason"] == "operator checked lot plan"
    assert replay.json()["idempotent_replay"] is True
    assert calls == 1

    async def unavailable(**_kwargs):
        raise BrokerObservationUnavailable("fake broker unavailable")

    monkeypatch.setattr(protected_router, "fresh_broker_observation", unavailable)
    failed_symbol = f"TROLLBACK{uuid4().hex[:7].upper()}"
    async with await _client(_app(actor_id=actor_id)) as client:
        failed = await _put(client, failed_symbol, **_body())
    assert failed.status_code == 422
    assert failed.json()["detail"]["error"] == "broker_read_failed"
    async with SessionLocal() as session:
        assert (
            await ProtectedQuantityService(session).get(key=_key(failed_symbol)) is None
        )


@pytest.mark.asyncio
async def test_decrease_requires_exact_symbol_and_stale_form_is_rejected(
    session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _require_db()
    actor_id = 728006
    _admin(session_user, actor_id)

    async def broker(**_kwargs):
        return _observation()

    monkeypatch.setattr(protected_router, "fresh_broker_observation", broker)
    symbol = f"TDECREASE{uuid4().hex[:6].upper()}"
    async with await _client(_app(actor_id=actor_id)) as client:
        declared = await _put(client, symbol, **_body(quantity="6"))
        assert declared.status_code == 200
        missing_symbol = await _put(
            client,
            symbol,
            **_body(quantity="4", expected_revision=1),
        )
        wrong_symbol = await _put(
            client,
            symbol,
            **_body(quantity="4", expected_revision=1, confirm_symbol="WRONG"),
        )
        stale = await _put(
            client,
            symbol,
            **_body(quantity="7", expected_revision=0),
        )
        decreased = await _put(
            client,
            symbol,
            **_body(quantity="4", expected_revision=1, confirm_symbol=symbol),
        )
    assert missing_symbol.status_code == 409
    assert missing_symbol.json()["detail"]["error"] == "symbol_confirmation_required"
    assert wrong_symbol.status_code == 409
    assert wrong_symbol.json()["detail"]["error"] == "symbol_confirmation_required"
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "stale_form"
    assert decreased.status_code == 200


@pytest.mark.asyncio
async def test_route_provider_runs_after_same_key_transaction_lock(
    session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutating provider placement before the service lock fails this assertion."""

    await _require_db()
    actor_id = 728007
    _admin(session_user, actor_id)
    symbol = f"TLOCK{uuid4().hex[:8].upper()}"
    key = _key(symbol)

    async def broker(**_kwargs):
        connection = await engine.connect()
        acquired = False
        try:
            acquired = bool(
                (
                    await connection.execute(
                        _TRY_ADVISORY_LOCK, {"key": _advisory_key(key)}
                    )
                ).scalar_one()
            )
            if acquired:
                await connection.execute(
                    _RELEASE_ADVISORY_LOCK, {"key": _advisory_key(key)}
                )
            await connection.commit()
        finally:
            await connection.close()
        assert not acquired, (
            "provider ran before ProtectedQuantityService acquired its xact lock"
        )
        return _observation()

    monkeypatch.setattr(protected_router, "fresh_broker_observation", broker)
    async with await _client(_app(actor_id=actor_id)) as client:
        response = await _put(client, symbol, **_body())
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_write_waits_for_same_lock_used_by_live_sell_then_reads_fresh(
    session_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent live-sell lease blocks the write before its broker provider."""

    await _require_db()
    actor_id = 728008
    _admin(session_user, actor_id)
    symbol = f"TCONCURRENT{uuid4().hex[:6].upper()}"
    key = _key(symbol)
    entered_provider = asyncio.Event()

    async def broker(**_kwargs):
        entered_provider.set()
        return _observation()

    monkeypatch.setattr(protected_router, "fresh_broker_observation", broker)
    lock_connection = await engine.connect()
    lock_key = _advisory_key(key)
    try:
        acquired = bool(
            (
                await lock_connection.execute(_TRY_ADVISORY_LOCK, {"key": lock_key})
            ).scalar_one()
        )
        await lock_connection.commit()
        assert acquired
        async with await _client(_app(actor_id=actor_id)) as client:
            writer = asyncio.create_task(_put(client, symbol, **_body()))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(entered_provider.wait(), timeout=0.08)
            await lock_connection.execute(_RELEASE_ADVISORY_LOCK, {"key": lock_key})
            await lock_connection.commit()
            response = await asyncio.wait_for(writer, timeout=3)
    finally:
        await lock_connection.close()
    assert response.status_code == 200, response.text
    assert entered_provider.is_set()
