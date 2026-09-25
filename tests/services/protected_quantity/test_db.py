"""Run-owned PostgreSQL coverage for #728 protection declarations."""

from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.models.base import Base
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services import protected_quantity_service as policy
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.protected_quantity_service import (
    BrokerPositionObservation,
    ProtectedQuantityConflictError,
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
    normalize_protection_key,
    prepare_live_sell_lease,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = ROOT / "alembic/versions/20260925_rob728_protected_positions.py"
TABLE = "protected_positions"
SCHEMA = "review"


def _observation(*, held: str = "10", sellable: str = "10"):
    return BrokerPositionObservation(
        held=Decimal(held),
        sellable=Decimal(sellable),
        observed_at=datetime.now(UTC),
    )


def _provider(*, held: str = "10", sellable: str = "10"):
    async def observe() -> BrokerPositionObservation:
        return _observation(held=held, sellable=sellable)

    return observe


def _crypto_send_kwargs(symbol: str) -> dict[str, Any]:
    return {
        "normalized_symbol": symbol,
        "side": "sell",
        "order_type": "limit",
        "order_quantity": 100.0,
        "price": 1.0,
        "market_type": "crypto",
        "current_price": 1.0,
        "avg_price": 0.5,
        "dry_run_result": {"price": 1.0, "quantity": 100.0},
        "order_amount": 100.0,
        "reason": "quote alias test",
        "exit_reason": None,
        "thesis": None,
        "strategy": None,
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": None,
        "indicators_snapshot": None,
        "defensive_trim_ctx": None,
        "order_error_fn": lambda message: {"success": False, "error": message},
        "is_mock": False,
        "idempotency_key": f"alias-{uuid4()}",
    }


async def _save(
    service: ProtectedQuantityService,
    *,
    symbol: str,
    quantity: str,
    expected_revision: int | None,
    idempotency_key: str,
    confirm_symbol: str | None = None,
    reconfirm: bool = False,
    held: str = "10",
    sellable: str = "10",
):
    return await service.save(
        account_scope="kis_live",
        market="kr",
        symbol=symbol,
        protected_quantity=quantity,
        expected_revision=expected_revision,
        reason="test declaration",
        idempotency_key=idempotency_key,
        actor_user_id=7,
        origin="invest_ui",
        observation_provider=_provider(held=held, sellable=sellable),
        reconfirm=reconfirm,
        confirm_protection_change=True,
        confirm_symbol=confirm_symbol,
    )


@pytest.mark.asyncio
async def test_service_writes_head_and_append_only_revision_in_one_contract(
    db_session,
) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
    )
    reconfirmed = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=declared.revision,
        idempotency_key=f"reconfirm-{uuid4()}",
        reconfirm=True,
    )

    assert declared.revision == 1
    assert reconfirmed.revision == 2
    assert reconfirmed.action == "reconfirm"

    head = await service.get(
        key=normalize_protection_key(
            account_scope="kis_live", market="kr", symbol=symbol
        )
    )
    history = await service.list_revisions(key=head.key) if head else []
    assert head is not None
    assert head.protected_quantity == Decimal("6")
    assert [(row.revision, row.action) for row in history] == [
        (1, "declare"),
        (2, "reconfirm"),
    ]


@pytest.mark.asyncio
async def test_service_confirmation_staleness_and_replay(db_session) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    idempotency_key = f"declare-{uuid4()}"
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=idempotency_key,
    )
    replay = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=idempotency_key,
    )
    assert replay.idempotent_replay is True
    assert replay.revision == declared.revision == 1

    with pytest.raises(ProtectedQuantityConflictError) as confirm_error:
        await service.save(
            account_scope="kis_live",
            market="kr",
            symbol=symbol,
            protected_quantity="5",
            expected_revision=1,
            reason="test declaration",
            idempotency_key=f"unconfirmed-{uuid4()}",
            actor_user_id=7,
            origin="invest_ui",
            observation_provider=_provider(),
        )
    assert confirm_error.value.error == "confirm_required"

    with pytest.raises(ProtectedQuantityConflictError) as symbol_error:
        await _save(
            service,
            symbol=symbol,
            quantity="5",
            expected_revision=1,
            idempotency_key=f"missing-symbol-{uuid4()}",
        )
    assert symbol_error.value.error == "symbol_confirmation_required"

    decreased = await _save(
        service,
        symbol=symbol,
        quantity="5",
        expected_revision=1,
        idempotency_key=f"decrease-{uuid4()}",
        confirm_symbol=symbol,
    )
    assert decreased.action == "decrease"
    assert decreased.revision == 2

    with pytest.raises(ProtectedQuantityConflictError) as stale_error:
        await _save(
            service,
            symbol=symbol,
            quantity="4",
            expected_revision=1,
            idempotency_key=f"stale-{uuid4()}",
            confirm_symbol=symbol,
        )
    assert stale_error.value.error == "stale_form"

    history = await service.list_revisions(
        key=normalize_protection_key(
            account_scope="kis_live", market="kr", symbol=symbol
        )
    )
    assert [(row.revision, row.action) for row in history] == [
        (1, "declare"),
        (2, "decrease"),
    ]


@pytest.mark.asyncio
async def test_service_rejects_floor_above_fresh_held(db_session) -> None:
    service = ProtectedQuantityService(db_session)
    with pytest.raises(ProtectedQuantityValidationError, match="must not exceed"):
        await service.save(
            account_scope="kis_live",
            market="kr",
            symbol=f"Z{uuid4().hex[:7].upper()}",
            protected_quantity="10.00000001",
            expected_revision=None,
            reason="test declaration",
            idempotency_key=f"above-held-{uuid4()}",
            actor_user_id=7,
            origin="invest_ui",
            observation_provider=_provider(held="10"),
            confirm_protection_change=True,
        )


@pytest.mark.asyncio
async def test_service_allows_floor_equal_to_fresh_held(db_session) -> None:
    service = ProtectedQuantityService(db_session)
    saved = await _save(
        service,
        symbol=f"Z{uuid4().hex[:7].upper()}",
        quantity="10",
        expected_revision=None,
        idempotency_key=f"equal-held-{uuid4()}",
        held="10",
        sellable="10",
    )

    assert saved.head.protected_quantity == Decimal("10")


@pytest.mark.asyncio
async def test_floor_vs_fresh_held_boundary_has_assertion_red_mutant_oracle(
    db_session,
) -> None:
    """Pin both sides of P_new <= H with assertions, not an uncaught exception."""

    service = ProtectedQuantityService(db_session)

    async def accepted(*, quantity: str) -> bool:
        try:
            await _save(
                service,
                symbol=f"Z{uuid4().hex[:7].upper()}",
                quantity=quantity,
                expected_revision=None,
                idempotency_key=f"boundary-{uuid4()}",
                held="10",
                sellable="10",
            )
        except ProtectedQuantityValidationError:
            return False
        return True

    assert await accepted(quantity="10") is True
    assert await accepted(quantity="10.00000001") is False


@pytest.mark.asyncio
async def test_idempotency_key_reuse_requires_the_exact_protected_request(
    db_session,
) -> None:
    service = ProtectedQuantityService(db_session)
    symbol = f"Z{uuid4().hex[:7].upper()}"
    idempotency_key = f"exact-retry-{uuid4()}"
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=idempotency_key,
    )
    exact_retry = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=idempotency_key,
    )

    assert exact_retry.idempotent_replay is True
    assert exact_retry.revision == declared.revision

    reuse_attempts = (
        {
            "symbol": f"Z{uuid4().hex[:7].upper()}",
            "quantity": "6",
            "expected_revision": None,
            "reconfirm": False,
        },
        {
            "symbol": symbol,
            "quantity": "5",
            "expected_revision": None,
            "reconfirm": False,
        },
        {
            "symbol": symbol,
            "quantity": "6",
            "expected_revision": declared.revision,
            "reconfirm": True,
        },
    )
    for attempt in reuse_attempts:
        with pytest.raises(ProtectedQuantityConflictError) as reused:
            await _save(
                service,
                symbol=attempt["symbol"],
                quantity=attempt["quantity"],
                expected_revision=attempt["expected_revision"],
                idempotency_key=idempotency_key,
                reconfirm=attempt["reconfirm"],
            )
        assert reused.value.error == "idempotency_key_reused"


@pytest.mark.asyncio
async def test_exact_idempotent_retry_replays_its_revision_snapshot_after_head_advances(
    db_session,
) -> None:
    """A v1 retry cannot return v2's floor paired with v1's revision."""

    service = ProtectedQuantityService(db_session)
    symbol = f"Z{uuid4().hex[:7].upper()}"
    declaration_key = f"declare-replay-{uuid4()}"
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=declaration_key,
    )
    advanced = await _save(
        service,
        symbol=symbol,
        quantity="7",
        expected_revision=declared.revision,
        idempotency_key=f"increase-after-declare-{uuid4()}",
    )

    replay = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=declaration_key,
    )

    assert advanced.revision == 2
    assert replay.idempotent_replay is True
    assert replay.revision == replay.head.revision == declared.revision == 1
    assert (
        replay.head.protected_quantity
        == declared.head.protected_quantity
        == Decimal("6")
    )
    assert replay.head.protected_quantity != advanced.head.protected_quantity


@pytest.mark.asyncio
async def test_reconfirm_service_boundary_resolves_growth_unverified_without_changing_p(
    db_session,
) -> None:
    """The PR-1 service boundary supports step 6 without adding a PR-2 route."""

    service = ProtectedQuantityService(db_session)
    symbol = f"Z{uuid4().hex[:7].upper()}"
    declared = await _save(
        service,
        symbol=symbol,
        quantity="60",
        expected_revision=None,
        idempotency_key=f"growth-declare-{uuid4()}",
        held="100",
        sellable="100",
    )

    unverified = await policy._evaluate_live_sell(
        snapshot=declared.head,
        mode="enforce",
        quantity="1",
        kind="new",
        fresh_broker_sellable="500",
        fresh_broker_held="500",
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    assert unverified.allowed is False
    assert unverified.block is not None
    assert unverified.block.error_code == "protected_state_unverified"

    reconfirmed = await _save(
        service,
        symbol=symbol,
        quantity="60",
        expected_revision=declared.revision,
        idempotency_key=f"growth-reconfirm-{uuid4()}",
        reconfirm=True,
        held="500",
        sellable="500",
    )
    resolved = await policy._evaluate_live_sell(
        snapshot=reconfirmed.head,
        mode="enforce",
        quantity="1",
        kind="new",
        fresh_broker_sellable="500",
        fresh_broker_held="500",
        sellable_observed=True,
        amend_remaining_fresh=None,
    )

    assert reconfirmed.action == "reconfirm"
    assert (
        reconfirmed.head.protected_quantity
        == declared.head.protected_quantity
        == Decimal("60")
    )
    assert resolved.allowed is True
    assert resolved.headroom == Decimal("440")


@pytest.mark.asyncio
async def test_upbit_drift_matches_execution_ledger_market_code_key(db_session) -> None:
    """Crypto drift must use raw KRW-BTC, not the ledger base symbol BTC."""

    service = ProtectedQuantityService(db_session)
    declared = await service.save(
        account_scope="upbit_live",
        market="crypto",
        symbol="KRW-BTC",
        protected_quantity="60",
        expected_revision=None,
        reason="market-code drift fixture",
        idempotency_key=f"crypto-drift-{uuid4()}",
        actor_user_id=7,
        origin="invest_ui",
        observation_provider=_provider(held="100", sellable="100"),
        confirm_protection_change=True,
    )
    observed_at = declared.head.last_confirmed_at
    fill = ExecutionLedgerUpsert(
        broker="upbit",
        account_mode="live",
        venue="upbit_krw",
        instrument_type="crypto",
        symbol="BTC",
        raw_symbol="KRW-BTC",
        side="sell",
        broker_order_id=f"protected-drift-{uuid4()}",
        fill_seq=0,
        filled_qty=Decimal("1"),
        filled_price=Decimal("100000000"),
        filled_notional=Decimal("100000000"),
        filled_at=observed_at + timedelta(seconds=1),
        currency="KRW",
        source="reconciler",
        raw_payload_json={"fixture": "protected-quantity"},
    )
    await ExecutionLedgerRepository(db_session).upsert_fill(fill)
    await db_session.commit()

    assert (
        await policy._is_drifted(
            snapshot=declared.head,
            fresh_held=Decimal("99"),
        )
        is False
    )
    assert (
        await policy._is_drifted(
            snapshot=declared.head,
            fresh_held=Decimal("100"),
        )
        is True
    )


@pytest.mark.asyncio
async def test_upbit_quote_alias_is_blocked_before_new_sell_send(
    db_session, monkeypatch
) -> None:
    import app.services.brokers.upbit.client as upbit_client
    from app.mcp_server.tooling import order_execution as execution

    coin = f"Z{uuid4().hex[:6].upper()}"
    await ProtectedQuantityService(db_session).save(
        account_scope="upbit_live",
        market="crypto",
        symbol=f"KRW-{coin}",
        protected_quantity="60",
        expected_revision=None,
        reason="quote alias declaration",
        idempotency_key=f"alias-declare-{uuid4()}",
        actor_user_id=7,
        origin="invest_ui",
        observation_provider=_provider(held="100", sellable="100"),
        confirm_protection_change=True,
    )
    monkeypatch.setattr(settings, "protected_quantity_mode_upbit_live", "enforce")
    monkeypatch.setattr(
        upbit_client,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": coin, "balance": "100", "locked": "0"}]),
    )

    class BrokerReached(Exception):
        pass

    broker_send = AsyncMock(side_effect=BrokerReached)
    monkeypatch.setattr(execution, "_execute_order", broker_send)

    try:
        result = await execution._execute_and_record(
            **_crypto_send_kwargs(f"USDT-{coin}")
        )
    except BrokerReached:
        result = {}

    broker_send.assert_not_awaited()
    assert result["error_code"] == "protected_quantity_unresolved"
    assert result["symbol"] == f"USDT-{coin}"


@pytest.mark.asyncio
async def test_upbit_quote_alias_blocks_cancel_and_reorder_before_cancel(
    db_session, monkeypatch
) -> None:
    import app.services.brokers.upbit.client as upbit_client
    import app.services.brokers.upbit.orders as upbit_orders

    coin = f"Z{uuid4().hex[:6].upper()}"
    await ProtectedQuantityService(db_session).save(
        account_scope="upbit_live",
        market="crypto",
        symbol=f"KRW-{coin}",
        protected_quantity="60",
        expected_revision=None,
        reason="quote alias declaration",
        idempotency_key=f"alias-declare-{uuid4()}",
        actor_user_id=7,
        origin="invest_ui",
        observation_provider=_provider(held="100", sellable="100"),
        confirm_protection_change=True,
    )
    monkeypatch.setattr(settings, "protected_quantity_mode_upbit_live", "enforce")
    original = {
        "uuid": "order-1",
        "state": "wait",
        "ord_type": "limit",
        "side": "ask",
        "market": f"USDT-{coin}",
        "remaining_volume": "40",
    }
    monkeypatch.setattr(
        upbit_orders, "fetch_order_detail", AsyncMock(return_value=original)
    )
    monkeypatch.setattr(
        upbit_client,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": coin, "balance": "60", "locked": "40"}]),
    )
    cancel = AsyncMock()
    replace = AsyncMock()
    monkeypatch.setattr(upbit_orders, "cancel_orders", cancel)
    monkeypatch.setattr(upbit_orders, "place_sell_order", replace)

    result = await upbit_orders.cancel_and_reorder(
        "order-1", new_price=1.0, new_quantity=100
    )

    cancel.assert_not_awaited()
    replace.assert_not_awaited()
    assert result["protection_phase"] == "pre_cancel"
    assert result["error_code"] == "protected_quantity_unresolved"


@pytest.mark.asyncio
async def test_upbit_quote_alias_shadow_records_would_block_without_refusing(
    db_session, caplog
) -> None:
    coin = f"Z{uuid4().hex[:6].upper()}"
    await ProtectedQuantityService(db_session).save(
        account_scope="upbit_live",
        market="crypto",
        symbol=f"KRW-{coin}",
        protected_quantity="60",
        expected_revision=None,
        reason="quote alias declaration",
        idempotency_key=f"alias-declare-{uuid4()}",
        actor_user_id=7,
        origin="invest_ui",
        observation_provider=_provider(held="100", sellable="100"),
        confirm_protection_change=True,
    )
    lease = await prepare_live_sell_lease(
        account_scope="upbit_live",
        market="crypto",
        symbol=f"USDT-{coin}",
        settings_obj=SimpleNamespace(protected_quantity_mode_upbit_live="shadow"),
    )
    try:
        with caplog.at_level("WARNING", logger=policy.__name__):
            decision = await lease.evaluate(
                quantity="100",
                kind="new",
                fresh_broker_sellable="100",
                fresh_broker_held="100",
                sellable_observed=True,
            )
    finally:
        await lease.release()

    assert decision.allowed is True
    assert decision.would_block is True
    assert decision.block is not None
    assert decision.block.error_code == "protected_quantity_unresolved"
    assert any(
        "protected_quantity_would_block" in row.message for row in caplog.records
    )
    projection = await policy.headroom_for_observation(
        account_scope="upbit_live",
        market="crypto",
        symbol=f"USDT-{coin}",
        broker_sellable="100",
        broker_held="100",
        sellable_observed=True,
        settings_obj=SimpleNamespace(protected_quantity_mode_upbit_live="shadow"),
    )
    assert projection.key.symbol == f"USDT-{coin}"
    assert projection.protected_quantity == Decimal("60")
    assert projection.tactical_sellable == Decimal("0")
    assert projection.state == "unverified"


@pytest.mark.asyncio
async def test_upbit_pre_cancel_uses_remaining_after_asset_lock_wait(
    db_session, monkeypatch
) -> None:
    import app.services.brokers.upbit.client as upbit_client
    import app.services.brokers.upbit.orders as upbit_orders

    coin = f"Z{uuid4().hex[:6].upper()}"
    market = f"KRW-{coin}"
    await ProtectedQuantityService(db_session).save(
        account_scope="upbit_live",
        market="crypto",
        symbol=market,
        protected_quantity="60",
        expected_revision=None,
        reason="remaining quantity race",
        idempotency_key=f"remaining-{uuid4()}",
        actor_user_id=7,
        origin="invest_ui",
        observation_provider=_provider(held="100", sellable="100"),
        confirm_protection_change=True,
    )
    monkeypatch.setattr(settings, "protected_quantity_mode_upbit_live", "enforce")
    monkeypatch.setattr(policy, "_is_drifted", AsyncMock(return_value=False))
    state = {"remaining": Decimal("40"), "locked": Decimal("40")}

    async def fetch_order_detail(order_uuid: str) -> dict[str, Any]:
        return {
            "uuid": order_uuid,
            "state": "wait",
            "ord_type": "limit",
            "side": "ask",
            "market": market,
            "remaining_volume": str(state["remaining"]),
        }

    async def fetch_my_coins() -> list[dict[str, Any]]:
        return [
            {
                "currency": coin,
                "balance": "60",
                "locked": str(state["locked"]),
            }
        ]

    cancel = AsyncMock()
    replace = AsyncMock()
    monkeypatch.setattr(upbit_orders, "fetch_order_detail", fetch_order_detail)
    monkeypatch.setattr(upbit_client, "fetch_my_coins", fetch_my_coins)
    monkeypatch.setattr(upbit_orders, "cancel_orders", cancel)
    monkeypatch.setattr(upbit_orders, "place_sell_order", replace)

    blocker = await prepare_live_sell_lease(
        account_scope="upbit_live", market="crypto", symbol=market
    )
    pending = asyncio.create_task(
        upbit_orders.cancel_and_reorder("order-1", new_price=1.0, new_quantity=40)
    )
    try:
        await asyncio.sleep(0.1)
        assert pending.done() is False
        state["locked"] = Decimal("15")
        state["remaining"] = Decimal("15")
    finally:
        await blocker.release()
    result = await asyncio.wait_for(pending, timeout=5)

    cancel.assert_not_awaited()
    replace.assert_not_awaited()
    assert result["protection_phase"] == "pre_cancel"
    assert result["error_code"] == "protected_quantity_exceeded"


@pytest.mark.asyncio
async def test_service_rejects_every_declaration_direction_without_fresh_evidence(
    db_session,
) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
    )

    async def unavailable_observation() -> BrokerPositionObservation:
        return cast(BrokerPositionObservation, None)

    attempts: tuple[tuple[str, bool, str | None], ...] = (
        ("7", False, None),
        ("5", False, symbol),
        ("0", False, symbol),
        ("6", True, None),
    )

    for protected_quantity, reconfirm, confirm_symbol in attempts:
        with pytest.raises(
            ProtectedQuantityValidationError,
            match="fresh broker observation is required",
        ):
            await service.save(
                account_scope="kis_live",
                market="kr",
                symbol=symbol,
                protected_quantity=protected_quantity,
                expected_revision=declared.revision,
                reason="must have fresh broker evidence",
                idempotency_key=f"no-evidence-{uuid4()}",
                actor_user_id=7,
                origin="invest_ui",
                observation_provider=unavailable_observation,
                reconfirm=reconfirm,
                confirm_protection_change=True,
                confirm_symbol=confirm_symbol,
            )

    head = await service.get(
        key=normalize_protection_key(
            account_scope="kis_live", market="kr", symbol=symbol
        )
    )
    assert head is not None
    assert head.revision == declared.revision == 1
    assert head.protected_quantity == Decimal("6")


@pytest.mark.asyncio
async def test_declaration_write_waits_for_active_live_sell_lease(db_session) -> None:
    """A P increase cannot race a protected sell between G and broker reply."""

    symbol = f"Z{uuid4().hex[:7].upper()}"
    declared = await _save(
        ProtectedQuantityService(db_session),
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
    )
    settings_obj = SimpleNamespace(
        protected_quantity_mode_kis_live="enforce",
        protected_quantity_mode_toss_live="off",
        protected_quantity_mode_upbit_live="off",
    )
    lease = await prepare_live_sell_lease(
        account_scope="kis_live",
        market="kr",
        symbol=symbol,
        settings_obj=settings_obj,
    )
    assert lease.active is True

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as writer_db:
        writer = ProtectedQuantityService(writer_db)
        pending = asyncio.create_task(
            _save(
                writer,
                symbol=symbol,
                quantity="7",
                expected_revision=declared.revision,
                idempotency_key=f"increase-{uuid4()}",
            )
        )
        try:
            await asyncio.sleep(0.1)
            assert pending.done() is False
        finally:
            await lease.release()
        updated = await asyncio.wait_for(pending, timeout=3)

    assert updated.action == "increase"
    assert updated.revision == 2


@pytest.mark.asyncio
async def test_write_observes_held_after_sell_lease_releases(db_session) -> None:
    """A write waiting behind a sell must test the post-send broker H."""

    from app.core.db import AsyncSessionLocal

    symbol = f"Z{uuid4().hex[:7].upper()}"
    declared = await _save(
        ProtectedQuantityService(db_session),
        symbol=symbol,
        quantity="60",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
        held="100",
        sellable="100",
    )
    settings_obj = SimpleNamespace(protected_quantity_mode_kis_live="enforce")
    lease = await prepare_live_sell_lease(
        account_scope="kis_live", market="kr", symbol=symbol, settings_obj=settings_obj
    )
    broker = {"held": Decimal("100"), "sellable": Decimal("100")}
    observed: list[Decimal] = []

    async def observe_after_lock() -> BrokerPositionObservation:
        observed.append(broker["held"])
        return BrokerPositionObservation(
            held=broker["held"],
            sellable=broker["sellable"],
            observed_at=datetime.now(UTC),
        )

    decision = await lease.evaluate(
        quantity=Decimal("40"),
        kind="new",
        fresh_broker_sellable=broker["sellable"],
        fresh_broker_held=broker["held"],
        sellable_observed=True,
    )
    assert decision.allowed is True
    async with AsyncSessionLocal() as writer_db:
        pending = asyncio.create_task(
            ProtectedQuantityService(writer_db).save(
                account_scope="kis_live",
                market="kr",
                symbol=symbol,
                protected_quantity="100",
                expected_revision=declared.revision,
                reason="race check",
                idempotency_key=f"increase-{uuid4()}",
                actor_user_id=7,
                origin="invest_ui",
                observation_provider=observe_after_lock,
                confirm_protection_change=True,
            )
        )
        try:
            await asyncio.sleep(0.1)
            assert pending.done() is False
            assert observed == []
            broker["held"] = Decimal("60")
            broker["sellable"] = Decimal("60")
        finally:
            await lease.release()
        with pytest.raises(
            ProtectedQuantityValidationError,
            match="protected_quantity must not exceed fresh broker held quantity",
        ):
            await asyncio.wait_for(pending, timeout=5)

    assert observed == [Decimal("60")]
    head = await ProtectedQuantityService(db_session).get(key=declared.head.key)
    assert head is not None
    assert head.protected_quantity == Decimal("60")
    assert head.revision == 1


@pytest.mark.asyncio
async def test_toss_two_sells_recheck_sellable_after_real_lease_wait(
    db_session, monkeypatch
) -> None:
    """Both callers see stale S=100 before locking; only one may POST."""

    from app.mcp_server.tooling import orders_toss_variants as toss

    symbol = f"9{uuid4().int % 100000:05d}"
    await ProtectedQuantityService(db_session).save(
        account_scope="toss_live",
        market="kr",
        symbol=symbol,
        protected_quantity="60",
        expected_revision=None,
        reason="Toss lock race",
        idempotency_key=f"toss-{uuid4()}",
        actor_user_id=7,
        origin="invest_ui",
        observation_provider=_provider(held="100", sellable="100"),
        confirm_protection_change=True,
    )
    monkeypatch.setattr(settings, "protected_quantity_mode_toss_live", "enforce")
    pre_lock_barrier = asyncio.Event()

    class FakeToss:
        def __init__(self) -> None:
            self.sellable = Decimal("100")
            self.held = Decimal("100")
            self.sellable_reads: list[Decimal] = []
            self.posts: list[dict[str, Any]] = []

        async def sellable_quantity(self, *, symbol: str) -> Any:
            index = len(self.sellable_reads)
            observed = self.sellable
            self.sellable_reads.append(observed)
            if index < 2:
                if index == 1:
                    pre_lock_barrier.set()
                await pre_lock_barrier.wait()
            return SimpleNamespace(sellable_quantity=observed)

        async def holdings(self, *, symbol: str | None = None) -> Any:
            return SimpleNamespace(
                items=[SimpleNamespace(symbol=symbol, quantity=self.held)]
            )

        async def place_order(self, payload: dict[str, Any]) -> Any:
            self.posts.append(dict(payload))
            self.sellable -= Decimal(str(payload["quantity"]))
            return SimpleNamespace(
                order_id=f"fake-{len(self.posts)}", client_order_id="fake-client"
            )

    client = FakeToss()

    @asynccontextmanager
    async def fake_client_context():
        yield client

    monkeypatch.setattr(toss, "_entry_guard", lambda *_: None)
    monkeypatch.setattr(toss, "_client_context", fake_client_context)
    monkeypatch.setattr(
        toss,
        "_snap_kr_limit_price",
        AsyncMock(return_value=(Decimal("70000"), None, {})),
    )
    monkeypatch.setattr(toss, "_live_mutation_disabled_error", lambda *_: None)
    monkeypatch.setattr(
        toss,
        "check_warnings_guard",
        AsyncMock(
            return_value=SimpleNamespace(ok=True, warnings=[], error_message=None)
        ),
    )
    monkeypatch.setattr(toss, "_opposite_pending_error", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_sell_loss_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_nxt_preflight_context", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_invalidate_sellable_after_sell_mutation", AsyncMock())
    monkeypatch.setattr(toss, "record_toss_place_order", AsyncMock(return_value={}))

    results = await asyncio.gather(
        *(
            toss._toss_place_order_impl(
                symbol=symbol,
                side="sell",
                quantity="40",
                price="70000",
                market="kr",
                dry_run=False,
                confirm=True,
                account_mode="toss_live",
                rung=index + 1,
            )
            for index in range(2)
        )
    )

    assert len({post["clientOrderId"] for post in client.posts}) == len(client.posts)
    assert len(client.posts) == 1, (client.posts, results, client.sellable_reads)
    blocked = [row for row in results if row.get("success") is not True]
    assert len(blocked) == 1, results
    assert blocked[0]["error_code"] == "protected_quantity_exceeded"
    assert blocked[0]["broker_sellable"] == "60"
    assert client.sellable_reads[:2] == [Decimal("100"), Decimal("100")]
    assert client.sellable == Decimal("60")


@pytest.mark.asyncio
async def test_double_protected_sell_serializes_and_rechecks_fresh_headroom(
    db_session,
) -> None:
    """Two q=40 sends cannot both consume an S=100, P=60 headroom."""

    symbol = f"Z{uuid4().hex[:7].upper()}"
    await _save(
        ProtectedQuantityService(db_session),
        symbol=symbol,
        quantity="60",
        expected_revision=None,
        idempotency_key=f"declare-{uuid4()}",
        held="100",
        sellable="100",
    )
    settings_obj = SimpleNamespace(
        protected_quantity_mode_kis_live="enforce",
        protected_quantity_mode_toss_live="off",
        protected_quantity_mode_upbit_live="off",
    )
    first_checked = asyncio.Event()
    release_first = asyncio.Event()
    sellable = Decimal("100")
    sends: list[str] = []

    async def simulated_send(name: str) -> bool:
        nonlocal sellable
        lease = await prepare_live_sell_lease(
            account_scope="kis_live",
            market="kr",
            symbol=symbol,
            settings_obj=settings_obj,
        )
        try:
            decision = await lease.evaluate(
                quantity=Decimal("40"),
                kind="new",
                fresh_broker_sellable=sellable,
                fresh_broker_held=Decimal("100"),
                sellable_observed=True,
            )
            if not decision.allowed:
                return False
            sends.append(name)
            # A successful broker response has reserved the sellable amount
            # before the next caller gets the same protected-key lease.
            sellable -= Decimal("40")
            if name == "first":
                first_checked.set()
                await release_first.wait()
            return True
        finally:
            await lease.release()

    first = asyncio.create_task(simulated_send("first"))
    await asyncio.wait_for(first_checked.wait(), timeout=3)
    second = asyncio.create_task(simulated_send("second"))
    await asyncio.sleep(0.1)
    assert second.done() is False
    release_first.set()

    assert await asyncio.wait_for(first, timeout=3) is True
    assert await asyncio.wait_for(second, timeout=3) is False
    assert sends == ["first"]


@pytest.mark.asyncio
async def test_database_checks_and_append_only_revision_trigger(db_session) -> None:
    symbol = f"Z{uuid4().hex[:7].upper()}"
    service = ProtectedQuantityService(db_session)
    declared = await _save(
        service,
        symbol=symbol,
        quantity="6",
        expected_revision=None,
        idempotency_key=f"append-{uuid4()}",
    )

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "UPDATE review.protected_position_revisions "
                "SET reason = 'mutated' WHERE protected_position_id = :id"
            ),
            {"id": declared.head.id},
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(text("TRUNCATE review.protected_position_revisions"))
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "DELETE FROM review.protected_position_revisions "
                "WHERE protected_position_id = :id"
            ),
            {"id": declared.head.id},
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "INSERT INTO review.protected_positions "
                "(account_scope, market, symbol, protected_quantity, purpose, revision, "
                "last_confirmed_broker_held, last_confirmed_at, updated_by_user_id) "
                "VALUES ('kis_live', 'kr', :symbol, -1, 'long_term', 1, 0, now(), 7)"
            ),
            {"symbol": f"Z{uuid4().hex[:7].upper()}"},
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "INSERT INTO review.protected_positions "
                "(account_scope, market, symbol, protected_quantity, purpose, revision, "
                "last_confirmed_broker_held, last_confirmed_at, updated_by_user_id) "
                "VALUES ('upbit_live', 'kr', :symbol, 0, 'long_term', 1, 0, now(), 7)"
            ),
            {"symbol": f"Z{uuid4().hex[:7].upper()}"},
        )
        await db_session.commit()
    await db_session.rollback()


def _load_migration():
    spec = importlib.util.spec_from_file_location("rob728_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_fits_default_alembic_version_column() -> None:
    migration = _load_migration()
    assert len(migration.revision) <= 32


def _has_table(connection: sa.Connection) -> bool:
    return sa.inspect(connection).has_table(TABLE, schema=SCHEMA)


def _roundtrip(connection: sa.Connection) -> tuple[list[bool], dict[str, bool]]:
    migration = _load_migration()
    context = MigrationContext.configure(
        connection=connection,
        opts={"target_metadata": Base.metadata},
    )
    with Operations.context(context):
        migration.downgrade()
        removed = _has_table(connection)
        migration.upgrade()
        restored = _has_table(connection)
    head_id = connection.execute(
        text(
            "INSERT INTO review.protected_positions "
            "(account_scope, market, symbol, protected_quantity, "
            "last_confirmed_broker_held, last_confirmed_at, updated_by_user_id) "
            "VALUES ('kis_live', 'kr', :symbol, 1, 1, now(), 7) RETURNING id"
        ),
        {"symbol": f"Z{uuid4().hex[:7].upper()}"},
    ).scalar_one()
    revision_id = connection.execute(
        text(
            "INSERT INTO review.protected_position_revisions "
            "(protected_position_id, revision, action, new_quantity, "
            "broker_held_observed, broker_sellable_observed, broker_observed_at, "
            "reason, actor_user_id, origin, idempotency_key) "
            "VALUES (:head_id, 1, 'declare', 1, 1, 1, now(), "
            "'migration trigger probe', 7, 'invest_ui', :idempotency_key) "
            "RETURNING id"
        ),
        {"head_id": head_id, "idempotency_key": str(uuid4())},
    ).scalar_one()
    mutations = {
        "update": "UPDATE review.protected_position_revisions SET reason='changed' WHERE id=:id",
        "delete": "DELETE FROM review.protected_position_revisions WHERE id=:id",
        "truncate": "TRUNCATE review.protected_position_revisions",
    }
    outcomes: dict[str, bool] = {}
    for operation, sql in mutations.items():
        savepoint = connection.begin_nested()
        try:
            connection.execute(text(sql), {"id": revision_id})
        except DBAPIError:
            outcomes[operation] = True
        else:
            outcomes[operation] = False
        finally:
            savepoint.rollback()
    return [removed, restored], outcomes


@pytest.mark.asyncio
async def test_migration_upgrade_downgrade_roundtrip_uses_only_run_owned_test_db(
    _bootstrap_test_schema,
) -> None:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        trace = await connection.run_sync(_roundtrip)
        await session.rollback()

    assert trace == (
        [False, True],
        {"update": True, "delete": True, "truncate": True},
    )
