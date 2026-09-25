"""Task 733 regressions for a lease cleanup failure after a broker response."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import protected_quantity_service as policy

pytestmark = pytest.mark.unit


def _verified_cleanup_lease(
    *,
    unlock_error: BaseException | None = RuntimeError("advisory unlock failed"),
    commit_error: BaseException | None = None,
    invalidate_error: BaseException | None = None,
    close_error: BaseException | None = None,
) -> tuple[policy.LiveSellProtectionLease, SimpleNamespace]:
    """Build a real lease whose advisory cleanup fails after broker acceptance.

    The fake connection is intentionally below LiveSellProtectionLease rather
    than a generic fake lease.  This proves that only a verified invalidation
    allows a caller to preserve an already-observed broker result.
    """

    key = policy.normalize_protection_key(
        account_scope="kis_live",
        market="kr",
        symbol="005930",
    )
    snapshot = policy.ProtectedPositionSnapshot(
        id=733,
        key=key,
        protected_quantity=Decimal("60"),
        revision=1,
        last_confirmed_broker_held=Decimal("100"),
        last_confirmed_at=datetime(2026, 9, 26, tzinfo=UTC),
        updated_by_user_id=1,
        updated_at=datetime(2026, 9, 26, tzinfo=UTC),
    )
    connection = SimpleNamespace(
        execute=AsyncMock(side_effect=unlock_error),
        commit=AsyncMock(side_effect=commit_error),
        invalidate=AsyncMock(side_effect=invalidate_error),
        close=AsyncMock(side_effect=close_error),
    )
    lease = policy.LiveSellProtectionLease(
        snapshot=snapshot,
        mode="shadow",
        connection=connection,
        lock_key=733,
    )
    lease.evaluate = AsyncMock(  # type: ignore[method-assign]
        return_value=policy.ProtectionDecision(True, "covered", Decimal("40"))
    )
    return lease, connection


def _assert_cleanup_warning(result: dict[str, object]) -> None:
    warnings = result.get("warnings")
    assert isinstance(warnings, list), result
    assert any("Protection lease cleanup failed" in str(item) for item in warnings)


@pytest.mark.asyncio
async def test_kis_kr_amend_acceptance_keeps_rob395_repoint_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An accepted KR amendment must still re-point its accepted-only ledger row."""

    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import orders_modify_cancel as modify

    lease, connection = _verified_cleanup_lease()
    broker_modify = AsyncMock(return_value={"odno": "NEW-KR"})
    broker = SimpleNamespace(
        inquire_korea_orders=AsyncMock(
            return_value=[
                {
                    "odno": "OLD-KR",
                    "ord_unpr": "70000",
                    "ord_qty": "40",
                    "sll_buy_dvsn_cd": "01",
                }
            ]
        ),
        modify_korea_order=broker_modify,
    )
    repoint = AsyncMock(return_value=1)
    monkeypatch.setattr(modify, "_create_kis_client", lambda **_: broker)
    monkeypatch.setattr(
        modify, "_live_sell_reprice_floor_error", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        modify, "_kr_security_type_or_none", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(modify, "adjust_tick_size_kr", lambda price, *_: price)
    monkeypatch.setattr(
        modify,
        "_prepare_kis_live_sell_modify_protection",
        AsyncMock(return_value=(lease, None)),
    )
    monkeypatch.setattr(kis_live_ledger, "_repoint_ledger_after_modify", repoint)
    caplog.set_level(logging.WARNING, logger="app.services.protected_quantity_service")

    result = await modify.modify_order_impl(
        "OLD-KR",
        "005930",
        market="kr",
        new_price=71_000,
        new_quantity=40,
        dry_run=False,
    )

    assert result["success"] is True, result
    assert result["new_order_id"] == "NEW-KR"
    _assert_cleanup_warning(result)
    broker_modify.assert_awaited_once()
    repoint.assert_awaited_once()
    assert repoint.await_args.kwargs["old_order_no"] == "OLD-KR"
    assert repoint.await_args.kwargs["new_order_no"] == "NEW-KR"
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()
    assert any(
        "protected sell lease cleanup verified after broker response" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_kis_kr_amend_close_only_failure_preserves_repoint_after_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close-only failure preserves acceptance only after backend discard."""

    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import orders_modify_cancel as modify

    lease, connection = _verified_cleanup_lease(
        unlock_error=None,
        close_error=RuntimeError("close-only unavailable"),
    )
    broker_modify = AsyncMock(return_value={"odno": "NEW-KR-CLOSE"})
    broker = SimpleNamespace(
        inquire_korea_orders=AsyncMock(
            return_value=[
                {
                    "odno": "OLD-KR-CLOSE",
                    "ord_unpr": "70000",
                    "ord_qty": "40",
                    "sll_buy_dvsn_cd": "01",
                }
            ]
        ),
        modify_korea_order=broker_modify,
    )
    repoint = AsyncMock(return_value=1)
    monkeypatch.setattr(modify, "_create_kis_client", lambda **_: broker)
    monkeypatch.setattr(
        modify, "_live_sell_reprice_floor_error", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        modify, "_kr_security_type_or_none", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(modify, "adjust_tick_size_kr", lambda price, *_: price)
    monkeypatch.setattr(
        modify,
        "_prepare_kis_live_sell_modify_protection",
        AsyncMock(return_value=(lease, None)),
    )
    monkeypatch.setattr(kis_live_ledger, "_repoint_ledger_after_modify", repoint)

    result = await modify.modify_order_impl(
        "OLD-KR-CLOSE",
        "005930",
        market="kr",
        new_price=71_000,
        new_quantity=40,
        dry_run=False,
    )

    assert result["success"] is True, result
    assert result["new_order_id"] == "NEW-KR-CLOSE"
    _assert_cleanup_warning(result)
    broker_modify.assert_awaited_once()
    repoint.assert_awaited_once()
    connection.execute.assert_awaited_once()
    connection.commit.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_kr_amend_close_only_invalidation_failure_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close-only failure with failed disposal does not create a safe result."""

    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import orders_modify_cancel as modify

    lease, connection = _verified_cleanup_lease(
        unlock_error=None,
        invalidate_error=RuntimeError("close-only invalidation unavailable"),
        close_error=RuntimeError("close-only unavailable"),
    )
    broker_modify = AsyncMock(return_value={"odno": "NEW-KR-CLOSE-UNSAFE"})
    broker = SimpleNamespace(
        inquire_korea_orders=AsyncMock(
            return_value=[
                {
                    "odno": "OLD-KR-CLOSE-UNSAFE",
                    "ord_unpr": "70000",
                    "ord_qty": "40",
                    "sll_buy_dvsn_cd": "01",
                }
            ]
        ),
        modify_korea_order=broker_modify,
    )
    repoint = AsyncMock(return_value=1)
    monkeypatch.setattr(modify, "_create_kis_client", lambda **_: broker)
    monkeypatch.setattr(
        modify, "_live_sell_reprice_floor_error", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        modify, "_kr_security_type_or_none", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(modify, "adjust_tick_size_kr", lambda price, *_: price)
    monkeypatch.setattr(
        modify,
        "_prepare_kis_live_sell_modify_protection",
        AsyncMock(return_value=(lease, None)),
    )
    monkeypatch.setattr(kis_live_ledger, "_repoint_ledger_after_modify", repoint)

    result = await modify.modify_order_impl(
        "OLD-KR-CLOSE-UNSAFE",
        "005930",
        market="kr",
        new_price=71_000,
        new_quantity=40,
        dry_run=False,
    )

    broker_modify.assert_awaited_once()
    assert result["success"] is False
    assert "close-only invalidation unavailable" in str(result["error"])
    assert "warnings" not in result
    repoint.assert_not_awaited()
    connection.execute.assert_awaited_once()
    connection.commit.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_kr_amend_invalidation_failure_is_not_hidden_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted broker reply cannot be presented as safely cleaned up without proof."""

    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import orders_modify_cancel as modify

    lease, connection = _verified_cleanup_lease(
        invalidate_error=RuntimeError("invalidation unavailable"),
    )
    broker_modify = AsyncMock(return_value={"odno": "NEW-KR-UNSAFE"})
    broker = SimpleNamespace(
        inquire_korea_orders=AsyncMock(
            return_value=[
                {
                    "odno": "OLD-KR-UNSAFE",
                    "ord_unpr": "70000",
                    "ord_qty": "40",
                    "sll_buy_dvsn_cd": "01",
                }
            ]
        ),
        modify_korea_order=broker_modify,
    )
    repoint = AsyncMock(return_value=1)
    monkeypatch.setattr(modify, "_create_kis_client", lambda **_: broker)
    monkeypatch.setattr(
        modify, "_live_sell_reprice_floor_error", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        modify, "_kr_security_type_or_none", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(modify, "adjust_tick_size_kr", lambda price, *_: price)
    monkeypatch.setattr(
        modify,
        "_prepare_kis_live_sell_modify_protection",
        AsyncMock(return_value=(lease, None)),
    )
    monkeypatch.setattr(kis_live_ledger, "_repoint_ledger_after_modify", repoint)

    result = await modify.modify_order_impl(
        "OLD-KR-UNSAFE",
        "005930",
        market="kr",
        new_price=71_000,
        new_quantity=40,
        dry_run=False,
    )

    broker_modify.assert_awaited_once()
    assert result["success"] is False
    assert "invalidation unavailable" in str(result["error"])
    assert "warnings" not in result
    repoint.assert_not_awaited()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_us_amend_acceptance_preserves_result_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted US amendment must not be rewritten as a failed amendment."""

    from app.mcp_server.tooling import orders_modify_cancel as modify

    lease, connection = _verified_cleanup_lease()
    broker_modify = AsyncMock(return_value={"odno": "NEW-US"})
    broker = SimpleNamespace(modify_overseas_order=broker_modify)
    monkeypatch.setattr(modify, "_create_kis_client", lambda **_: broker)
    monkeypatch.setattr(
        modify,
        "_find_us_open_order_by_id",
        AsyncMock(
            return_value=(
                {
                    "ft_ord_unpr3": "100",
                    "ft_ord_qty": "40",
                    "sll_buy_dvsn_cd": "01",
                },
                "NASD",
                ["NASD"],
            )
        ),
    )
    monkeypatch.setattr(
        modify, "_live_sell_reprice_floor_error", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        modify,
        "_prepare_kis_live_sell_modify_protection",
        AsyncMock(return_value=(lease, None)),
    )

    result = await modify.modify_order_impl(
        "OLD-US",
        "AAPL",
        market="us",
        new_price=101,
        new_quantity=40,
        dry_run=False,
    )

    assert result["success"] is True, result
    assert result["new_order_id"] == "NEW-US"
    _assert_cleanup_warning(result)
    broker_modify.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


def _crypto_execute_kwargs() -> dict[str, object]:
    return {
        "normalized_symbol": "BTC",
        "side": "sell",
        "order_type": "limit",
        "order_quantity": 0.01,
        "price": 50_000_000.0,
        "market_type": "crypto",
        "current_price": 50_000_000.0,
        "avg_price": 0.0,
        "dry_run_result": {
            "market": "KRW-BTC",
            "price": 50_000_000.0,
            "quantity": 0.01,
            "estimated_value": 500_000.0,
        },
        "order_amount": 500_000.0,
        "reason": "task 733 fake broker acceptance",
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
        "idempotency_key": "task733-crypto-acceptance",
    }


@pytest.mark.asyncio
async def test_g1_crypto_acceptance_records_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted crypto response must reach history and accepted-only ledger."""

    from app.mcp_server.tooling import live_order_ledger
    from app.mcp_server.tooling import order_execution as execution

    lease, connection = _verified_cleanup_lease()
    history = AsyncMock()
    accepted_ledger = AsyncMock(
        return_value={"success": True, "broker_status": "accepted", "ledger_id": 733}
    )
    monkeypatch.setattr(
        execution, "prepare_live_sell_lease", AsyncMock(return_value=lease)
    )
    monkeypatch.setattr(
        execution,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "broker_sellable_quantity": 1,
                "total_quantity": 1,
                "sellable_observed": True,
            }
        ),
    )
    monkeypatch.setattr(
        execution,
        "_execute_order",
        AsyncMock(return_value={"uuid": "UPBIT-733", "market": "KRW-BTC"}),
    )
    monkeypatch.setattr(execution, "_record_order_history", history)
    monkeypatch.setattr(live_order_ledger, "_record_live_order", accepted_ledger)
    monkeypatch.setattr(execution, "record_order_performance", lambda **_: None)

    try:
        result = await execution._execute_and_record(**_crypto_execute_kwargs())
    except RuntimeError as exc:
        # This turns the pre-fix regression into an assertion RED rather than
        # accepting an exception-only test oracle.
        result = {"success": False, "error": str(exc)}

    assert result["success"] is True, result
    _assert_cleanup_warning(result)
    history.assert_awaited_once()
    accepted_ledger.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_g1_preserves_pre_send_block_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-send failure remains a refusal, not a fabricated accepted result."""

    from app.mcp_server.tooling import order_execution as execution
    from app.services.brokers.kis.pre_send import PreSendFreshnessError

    lease, connection = _verified_cleanup_lease()
    monkeypatch.setattr(
        execution, "prepare_live_sell_lease", AsyncMock(return_value=lease)
    )
    monkeypatch.setattr(
        execution,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "broker_sellable_quantity": 1,
                "total_quantity": 1,
                "sellable_observed": True,
            }
        ),
    )
    monkeypatch.setattr(
        execution,
        "_execute_order",
        AsyncMock(side_effect=PreSendFreshnessError(("freshness_lost",))),
    )
    history = AsyncMock()
    monkeypatch.setattr(execution, "_record_order_history", history)

    try:
        result = await execution._execute_and_record(**_crypto_execute_kwargs())
    except RuntimeError as exc:
        result = {"success": False, "error": str(exc)}

    assert result["success"] is False
    assert result.get("pre_send_blocked") is True, result
    history.assert_not_awaited()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_g1_broker_rejection_is_not_promoted_to_acceptance_by_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected fake broker result stays failed even when release also fails."""

    from app.mcp_server.tooling import live_order_ledger
    from app.mcp_server.tooling import order_execution as execution

    lease, connection = _verified_cleanup_lease()
    rejected_ledger = AsyncMock(
        return_value={"success": True, "broker_status": "rejected", "ledger_id": 735}
    )
    monkeypatch.setattr(
        execution, "prepare_live_sell_lease", AsyncMock(return_value=lease)
    )
    monkeypatch.setattr(
        execution,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "broker_sellable_quantity": 1,
                "total_quantity": 1,
                "sellable_observed": True,
            }
        ),
    )
    monkeypatch.setattr(
        execution,
        "_execute_order",
        AsyncMock(return_value={"error": "broker rejected", "market": "KRW-BTC"}),
    )
    monkeypatch.setattr(execution, "_record_order_history", AsyncMock())
    monkeypatch.setattr(live_order_ledger, "_record_live_order", rejected_ledger)
    monkeypatch.setattr(execution, "record_order_performance", lambda **_: None)

    try:
        result = await execution._execute_and_record(**_crypto_execute_kwargs())
    except RuntimeError as exc:
        result = {"success": False, "error": str(exc)}

    # The live ledger's success flag describes recording, not broker acceptance.
    # A cleanup warning must never relabel a rejected broker outcome as accepted.
    assert result["broker_status"] == "rejected"
    assert "warnings" not in result
    rejected_ledger.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_g1_untrusted_server_response_remains_unknown_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response with untrusted server status cannot become an accepted order."""

    from app.mcp_server.tooling import live_order_ledger
    from app.mcp_server.tooling import order_execution as execution
    from app.services.brokers.kis.send_outcome import OrderSendOutcomeTracker

    lease, connection = _verified_cleanup_lease()
    history = AsyncMock()
    accepted_ledger = AsyncMock()
    monkeypatch.setattr(
        execution, "prepare_live_sell_lease", AsyncMock(return_value=lease)
    )
    monkeypatch.setattr(
        execution,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "broker_sellable_quantity": 1,
                "total_quantity": 1,
                "sellable_observed": True,
            }
        ),
    )
    monkeypatch.setattr(
        execution,
        "_execute_order",
        AsyncMock(return_value={"uuid": "UPBIT-500", "market": "KRW-BTC"}),
    )
    monkeypatch.setattr(execution, "_record_order_history", history)
    monkeypatch.setattr(live_order_ledger, "_record_live_order", accepted_ledger)
    tracker = OrderSendOutcomeTracker(last_http_status=500)

    with pytest.raises(execution.OrderSendOutcomeUnknown):
        await execution._execute_and_record(
            **_crypto_execute_kwargs(),
            send_outcome=tracker,
        )

    history.assert_not_awaited()
    accepted_ledger.assert_not_awaited()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_commit_failure_invalidates_and_closes_dedicated_connection() -> (
    None
):
    """A normal commit error is typed only after invalidation and close succeed."""

    lease, connection = _verified_cleanup_lease(
        unlock_error=None,
        commit_error=RuntimeError("commit response unavailable"),
    )

    with pytest.raises(policy.VerifiedLiveSellLeaseCleanupError) as caught:
        await lease.release()

    assert isinstance(caught.value.cleanup_error, RuntimeError)
    assert caught.value.close_completed is True
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_boundary_does_not_swallow_cancellation() -> None:
    """Cancellation must retain its control-flow meaning at the new boundary."""

    lease, connection = _verified_cleanup_lease(
        unlock_error=asyncio.CancelledError(),
        close_error=RuntimeError("close during cancellation unavailable"),
    )

    with pytest.raises(asyncio.CancelledError):
        await policy.release_live_sell_lease_preserving_outcome(
            lease,
            operation="cancellation_test",
        )

    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_boundary_propagates_invalidation_failure() -> None:
    """No accepted-result warning is allowed without a discarded backend proof."""

    lease, connection = _verified_cleanup_lease(
        invalidate_error=RuntimeError("invalidation unavailable"),
    )

    with pytest.raises(RuntimeError, match="invalidation unavailable"):
        await policy.release_live_sell_lease_preserving_outcome(
            lease,
            operation="invalidation_failure_test",
        )

    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_boundary_explicitly_reports_close_failure_after_invalidation() -> (
    None
):
    """A post-invalidation wrapper-close error is explicit, not silently safe."""

    lease, connection = _verified_cleanup_lease(
        close_error=RuntimeError("wrapper close unavailable"),
    )

    warning = await policy.release_live_sell_lease_preserving_outcome(
        lease,
        operation="close_failure_test",
    )

    assert warning is not None
    assert "backend was invalidated" in warning
    assert "wrapper close also failed" in warning
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_toss_place_acceptance_keeps_accepted_ledger_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G2 must keep a Toss acceptance and its accepted-only ledger record."""

    from app.mcp_server.tooling import orders_toss_variants as toss

    lease, connection = _verified_cleanup_lease()
    client = SimpleNamespace(
        place_order=AsyncMock(
            return_value=SimpleNamespace(
                order_id="TOSS-733",
                client_order_id="toss-client-733",
            )
        )
    )

    @asynccontextmanager
    async def fake_client_context():
        yield client

    recorded = AsyncMock(return_value={"ledger_id": 733})
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
    monkeypatch.setattr(
        toss,
        "_fresh_sellable_preflight",
        AsyncMock(
            return_value=(
                {
                    "fresh_sellable_quantity": "100",
                    "sellable_quantity_source": "fake",
                },
                None,
            )
        ),
    )
    monkeypatch.setattr(toss, "prepare_live_sell_lease", AsyncMock(return_value=lease))
    monkeypatch.setattr(
        toss,
        "_find_holding",
        AsyncMock(return_value=SimpleNamespace(quantity=Decimal("100"))),
    )
    monkeypatch.setattr(toss, "_invalidate_sellable_after_sell_mutation", AsyncMock())
    monkeypatch.setattr(toss, "record_toss_place_order", recorded)

    result = await toss._toss_place_order_impl(
        symbol="005930",
        side="sell",
        quantity="40",
        price="70000",
        market="kr",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert result["success"] is True, result
    assert result["order_id"] == "TOSS-733"
    _assert_cleanup_warning(result)
    client.place_order.assert_awaited_once()
    recorded.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_toss_modify_acceptance_keeps_replacement_ledger_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G3 must preserve an accepted replacement order and its ledger record."""

    from app.mcp_server.tooling import orders_toss_variants as toss

    lease, connection = _verified_cleanup_lease()
    client = SimpleNamespace(
        get_order=AsyncMock(
            return_value=SimpleNamespace(
                symbol="005930",
                side="sell",
                order_type="limit",
                quantity=Decimal("40"),
                price=Decimal("70000"),
                order_amount=None,
                time_in_force="DAY",
                currency="KRW",
            )
        ),
        modify_order=AsyncMock(return_value=SimpleNamespace(order_id="TOSS-NEW-733")),
    )

    @asynccontextmanager
    async def fake_client_context():
        yield client

    recorded = AsyncMock(return_value={"ledger_id": 734})
    monkeypatch.setattr(toss, "_entry_guard", lambda *_: None)
    monkeypatch.setattr(toss, "_client_context", fake_client_context)
    monkeypatch.setattr(
        toss,
        "_snap_kr_limit_price",
        AsyncMock(return_value=(Decimal("71000"), None, {})),
    )
    monkeypatch.setattr(toss, "_live_mutation_disabled_error", lambda *_: None)
    monkeypatch.setattr(toss, "_sell_loss_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(toss, "_nxt_preflight_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        toss,
        "_fresh_sellable_preflight",
        AsyncMock(return_value=({"fresh_sellable_quantity": "100"}, None)),
    )
    monkeypatch.setattr(toss, "prepare_live_sell_lease", AsyncMock(return_value=lease))
    monkeypatch.setattr(
        toss,
        "_find_holding",
        AsyncMock(return_value=SimpleNamespace(quantity=Decimal("100"))),
    )
    monkeypatch.setattr(toss, "_invalidate_sellable_after_sell_mutation", AsyncMock())
    monkeypatch.setattr(toss, "record_toss_replacement_order", recorded)

    result = await toss.toss_modify_order(
        "TOSS-OLD-733",
        new_price="71000",
        new_quantity="40",
        market="kr",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert result["success"] is True, result
    assert result["replacement_order_id"] == "TOSS-NEW-733"
    _assert_cleanup_warning(result)
    client.modify_order.assert_awaited_once()
    recorded.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_upbit_cancel_and_reorder_preserves_cancel_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G4 retains the cancellation fact and accepted replacement on cleanup loss."""

    import app.services.brokers.upbit.orders as upbit_orders

    lease, connection = _verified_cleanup_lease()
    original = {
        "uuid": "UPBIT-OLD-733",
        "state": "wait",
        "ord_type": "limit",
        "side": "ask",
        "market": "KRW-BTC",
        "remaining_volume": "40",
    }
    cancel = AsyncMock(return_value=[{"uuid": "UPBIT-OLD-733", "state": "cancel"}])
    replace = AsyncMock(return_value={"uuid": "UPBIT-NEW-733"})
    monkeypatch.setattr(
        upbit_orders,
        "fetch_order_detail",
        AsyncMock(side_effect=[original, original]),
    )
    monkeypatch.setattr(
        upbit_orders, "prepare_live_sell_lease", AsyncMock(return_value=lease)
    )
    monkeypatch.setattr(
        upbit_orders,
        "_fresh_upbit_sell_position",
        AsyncMock(return_value=(100, 100, True)),
    )
    monkeypatch.setattr(upbit_orders, "cancel_orders", cancel)
    monkeypatch.setattr(upbit_orders, "place_sell_order", replace)

    result = await upbit_orders.cancel_and_reorder(
        "UPBIT-OLD-733",
        new_price=56_000_000,
        new_quantity=40,
    )

    assert result["cancel_result"]["state"] == "cancel"
    assert result["new_order"]["uuid"] == "UPBIT-NEW-733"
    _assert_cleanup_warning(result)
    cancel.assert_awaited_once_with(["UPBIT-OLD-733"])
    replace.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_kis_fragment_preserves_accepted_order_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G5 leaves an accepted legacy KIS fragment visible to its caller."""

    from app.services import kis_trading_service as legacy

    lease, connection = _verified_cleanup_lease()
    send = AsyncMock(return_value={"rt_cd": "0", "odno": "LEGACY-733"})
    ops = SimpleNamespace(market="domestic", place_order=send)
    monkeypatch.setattr(
        legacy, "prepare_live_sell_lease", AsyncMock(return_value=lease)
    )
    monkeypatch.setattr(
        legacy,
        "_legacy_fresh_kis_sell_position",
        AsyncMock(return_value=(100, 100, True)),
    )

    result = await legacy._place_legacy_guarded_sell_fragment(
        ops,
        SimpleNamespace(),
        "005930",
        40,
        70_000,
        exchange_code=None,
    )

    assert result["odno"] == "LEGACY-733"
    _assert_cleanup_warning(result)
    send.assert_awaited_once()
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()
