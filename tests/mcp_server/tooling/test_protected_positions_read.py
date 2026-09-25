"""Read-only MCP coverage for #728 protected-position declarations."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.services.protected_quantity_service import (
    ProtectedPositionSnapshot,
    ProtectedQuantityValidationError,
    normalize_protection_key,
)
from tests.mcp_server._registration_recorder import RegistrationRecorder

pytestmark = pytest.mark.unit


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


def _snapshot() -> ProtectedPositionSnapshot:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    return ProtectedPositionSnapshot(
        id=1,
        key=normalize_protection_key(
            account_scope="kis_live",
            market="us",
            symbol="BRK-B",
        ),
        protected_quantity=Decimal("10.12500000"),
        revision=4,
        last_confirmed_broker_held=Decimal("12.50000000"),
        last_confirmed_at=now,
        updated_by_user_id=7,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_get_protected_positions_is_read_only_and_preserves_decimal_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import portfolio_holdings

    calls: list[str | None] = []

    class FakeService:
        def __init__(self, db: object) -> None:
            del db

        async def list(self, *, account_scope: str | None = None) -> list[Any]:
            calls.append(account_scope)
            return [_snapshot()]

    monkeypatch.setattr(portfolio_holdings, "AsyncSessionLocal", _SessionContext)
    monkeypatch.setattr(portfolio_holdings, "ProtectedQuantityService", FakeService)

    result = await portfolio_holdings._get_protected_positions_impl(
        account_scope="kis_live",
    )

    assert calls == ["kis_live"]
    assert result == {
        "success": True,
        "positions": [
            {
                "account_scope": "kis_live",
                "market": "us",
                "symbol": "BRK.B",
                "protected_quantity": "10.12500000",
                "revision": 4,
                "last_confirmed_broker_held": "12.50000000",
                "last_confirmed_at": "2026-09-25T00:00:00+00:00",
                "updated_by_user_id": 7,
                "updated_at": "2026-09-25T00:00:00+00:00",
            }
        ],
    }


@pytest.mark.asyncio
async def test_get_protected_positions_fails_without_backend_detail_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import portfolio_holdings

    class InvalidScopeService:
        def __init__(self, db: object) -> None:
            del db

        async def list(self, *, account_scope: str | None = None) -> list[Any]:
            del account_scope
            raise ProtectedQuantityValidationError("operator input detail")

    monkeypatch.setattr(portfolio_holdings, "AsyncSessionLocal", _SessionContext)
    monkeypatch.setattr(
        portfolio_holdings,
        "ProtectedQuantityService",
        InvalidScopeService,
    )

    invalid = await portfolio_holdings._get_protected_positions_impl(
        account_scope="not-a-scope",
    )
    assert invalid == {
        "success": False,
        "error_code": "invalid_protection_scope",
        "positions": [],
    }

    class OutageService:
        def __init__(self, db: object) -> None:
            del db

        async def list(self, *, account_scope: str | None = None) -> list[Any]:
            del account_scope
            raise RuntimeError("sensitive connection error")

    monkeypatch.setattr(portfolio_holdings, "ProtectedQuantityService", OutageService)
    unavailable = await portfolio_holdings._get_protected_positions_impl()
    assert unavailable == {
        "success": False,
        "error_code": "protection_state_unavailable",
        "positions": [],
    }


def test_protected_positions_registration_exposes_only_a_read_tool() -> None:
    from app.mcp_server.tooling import portfolio_holdings

    recorder = RegistrationRecorder()
    portfolio_holdings._register_portfolio_tools_impl(recorder)  # type: ignore[arg-type]

    assert "get_protected_positions" in recorder.tools
    assert {name for name in recorder.tools if "protect" in name} == {
        "get_protected_positions"
    }
    description = recorder.options["get_protected_positions"]["description"]
    assert "read-only" in description
