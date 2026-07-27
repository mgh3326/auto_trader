"""Focused contracts for the dedicated directional-lab Alpaca paper profile."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError

_MIGRATION = (
    Path(__file__).parents[1]
    / "alembic/versions/20260727_alpaca_paper_lab_account_mode.py"
)


@pytest.mark.unit
def test_default_client_order_id_remains_exact_legacy_value():
    from app.services.alpaca_paper_submit_service import (
        build_canonical_payload,
        derive_client_order_id,
    )

    canonical = build_canonical_payload(
        symbol="AAPL",
        side="buy",
        type="limit",
        time_in_force="day",
        qty=Decimal("1"),
        notional=None,
        limit_price=Decimal("150"),
        asset_class="us_equity",
    )

    assert derive_client_order_id(canonical) == "rob73-bbdb88d52431a3c2"
    assert (
        derive_client_order_id(canonical, account_mode="alpaca_paper")
        == "rob73-bbdb88d52431a3c2"
    )


@pytest.mark.unit
def test_lab_client_order_id_has_distinct_global_namespace():
    from app.services.alpaca_paper_submit_service import (
        build_canonical_payload,
        derive_client_order_id,
    )

    canonical = build_canonical_payload(
        symbol="AAPL",
        side="buy",
        type="limit",
        time_in_force="day",
        qty=Decimal("1"),
        notional=None,
        limit_price=Decimal("150"),
        asset_class="us_equity",
    )

    lab_key = derive_client_order_id(
        canonical,
        account_mode="alpaca_paper_lab",
    )

    assert lab_key == "dlab-rob73-905d0cf1fff848d6"
    assert lab_key != derive_client_order_id(canonical)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lab_submit_tool_fails_before_confirmation_without_lab_credentials(
    monkeypatch,
):
    from app.core.config import settings
    from app.mcp_server.tooling import alpaca_paper_orders as orders

    monkeypatch.setattr(
        orders,
        "_service_factory",
        orders._default_service_factory,
    )
    monkeypatch.setattr(settings, "alpaca_paper_api_key", "legacy-key")
    monkeypatch.setattr(
        settings,
        "alpaca_paper_api_secret",
        SecretStr("legacy-secret"),
    )
    monkeypatch.setattr(settings, "alpaca_paper_lab_api_key", None)
    monkeypatch.setattr(settings, "alpaca_paper_lab_api_secret", None)

    with pytest.raises(
        AlpacaPaperConfigurationError,
        match="alpaca_paper_lab_api_key and alpaca_paper_lab_api_secret",
    ):
        await orders.alpaca_paper_submit_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=Decimal("1"),
            limit_price=Decimal("150"),
            account_mode="alpaca_paper_lab",
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lab_submit_confirmation_preview_uses_lab_identity(monkeypatch):
    from app.core.config import settings
    from app.mcp_server.tooling import alpaca_paper_orders as orders

    monkeypatch.setattr(
        orders,
        "_service_factory",
        orders._default_service_factory,
    )
    monkeypatch.setattr(settings, "alpaca_paper_lab_api_key", "lab-key")
    monkeypatch.setattr(
        settings,
        "alpaca_paper_lab_api_secret",
        SecretStr("lab-secret"),
    )

    result = await orders.alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("150"),
        account_mode="alpaca_paper_lab",
    )

    assert result["account_mode"] == "alpaca_paper_lab"
    assert result["submitted"] is False
    assert result["client_order_id"].startswith("dlab-rob73-")


@pytest.mark.unit
def test_existing_check_constraints_only_gain_lab_value():
    from app.models.review import AlpacaPaperOrderLedger, TradeRetrospective

    ledger_checks = " ".join(
        str(item.sqltext)
        for item in AlpacaPaperOrderLedger.__table__.constraints
        if hasattr(item, "sqltext")
    )
    retrospective_checks = " ".join(
        str(item.sqltext)
        for item in TradeRetrospective.__table__.constraints
        if hasattr(item, "sqltext")
    )

    assert "account_mode IN ('alpaca_paper','alpaca_paper_lab')" in ledger_checks
    assert "'alpaca_paper','alpaca_paper_lab','upbit_live'" in retrospective_checks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ledger_lookup_is_scoped_to_selected_account_mode():
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    class _Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    service = AlpacaPaperLedgerService(db, account_mode="alpaca_paper_lab")

    assert await service.get_by_client_order_id("dlab-test") is None

    statement = db.execute.await_args.args[0]
    assert "alpaca_paper_order_ledger.account_mode" in str(statement)
    assert "alpaca_paper_lab" in statement.compile().params.values()


@pytest.mark.unit
def test_migration_has_protective_down_and_does_not_change_unique_indexes():
    source = _MIGRATION.read_text()

    assert "RAISE EXCEPTION" in source
    assert "alpaca_paper_lab ledger rows exist" in source
    assert "alpaca_paper_lab retrospective rows exist" in source
    assert "CREATE UNIQUE" not in source
    assert "DROP INDEX" not in source
