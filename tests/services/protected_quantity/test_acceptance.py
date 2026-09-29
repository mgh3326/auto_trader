"""Offline consumer and proposal acceptance coverage for #728.

All broker edges below are injected fakes.  The tests deliberately exercise
the production read consumers and proposal revalidation state machine without
creating a live broker client or an order-ledger row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services import protected_quantity_service as policy
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.revalidation import revalidate_and_submit
from app.services.order_proposals.service import RungInput
from tests.services.order_proposals.window_fakes import allow_known_session

pytestmark = pytest.mark.integration


def _snapshot(
    *,
    scope: str = "kis_live",
    market: str = "kr",
    symbol: str = "005930",
) -> policy.ProtectedPositionSnapshot:
    now = datetime.now(UTC)
    return policy.ProtectedPositionSnapshot(
        id=1,
        key=policy.normalize_protection_key(
            account_scope=scope,
            market=market,
            symbol=symbol,
        ),
        protected_quantity=Decimal("60"),
        revision=1,
        last_confirmed_broker_held=Decimal("100"),
        last_confirmed_at=now,
        updated_by_user_id=1,
        updated_at=now,
    )


def _settings(*, kis: str = "off", toss: str = "off", upbit: str = "off") -> Any:
    return SimpleNamespace(
        protected_quantity_mode_kis_live=kis,
        protected_quantity_mode_toss_live=toss,
        protected_quantity_mode_upbit_live=upbit,
    )


async def _projected_holdings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope: str,
    market: str,
    symbol: str,
    mode_settings: Any,
) -> dict[str, Any]:
    """Make C1's actual projection available to downstream consumer tests."""

    snapshot = _snapshot(scope=scope, market=market, symbol=symbol)
    monkeypatch.setattr(
        policy,
        "_read_head_snapshot",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(policy, "_is_drifted", AsyncMock(return_value=False))
    return await policy.apply_holdings_protection(
        {
            "quantity": 100.0,
            "broker_sellable_quantity": 100.0,
            "total_quantity": 100.0,
            "locked": 0.0,
            "avg_price": 90.0,
            "sellable_observed": True,
        },
        account_scope=scope,
        market=market,
        symbol=symbol,
        settings_obj=mode_settings,
    )


@pytest.mark.asyncio
async def test_c2_c3_c4_use_tactical_quantity_while_unprotected_values_stay_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview, sell validation, and crypto default sizing each preserve C1's split."""

    from app.mcp_server.tooling import order_execution, order_validation

    protected_equity = await _projected_holdings(
        monkeypatch,
        scope="kis_live",
        market="kr",
        symbol="005930",
        mode_settings=_settings(kis="enforce"),
    )
    unprotected_equity = {
        "quantity": 100.0,
        "broker_sellable_quantity": 100.0,
        "total_quantity": 100.0,
        "locked": 0.0,
        "avg_price": 90.0,
        "sellable_observed": True,
    }

    async def equity_holdings(
        symbol: str, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        return protected_equity if symbol == "005930" else unprotected_equity

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", equity_holdings)
    protected_preview = await order_validation._preview_sell(
        symbol="005930",
        order_type="limit",
        quantity=None,
        price=110.0,
        current_price=100.0,
        market_type="equity_kr",
    )
    unprotected_preview = await order_validation._preview_sell(
        symbol="000660",
        order_type="limit",
        quantity=None,
        price=110.0,
        current_price=100.0,
        market_type="equity_kr",
    )
    assert protected_preview["quantity"] == 40.0
    assert unprotected_preview["quantity"] == 100.0

    _quantity, _avg, protected_error = await order_validation._validate_sell_side(
        symbol="005930",
        normalized_symbol="005930",
        market_type="equity_kr",
        quantity=41.0,
        order_type="limit",
        price=110.0,
        current_price=100.0,
        order_error_fn=lambda message: {"error": message},
    )
    raw_quantity, _avg, unprotected_error = await order_validation._validate_sell_side(
        symbol="000660",
        normalized_symbol="000660",
        market_type="equity_kr",
        quantity=100.0,
        order_type="limit",
        price=110.0,
        current_price=100.0,
        order_error_fn=lambda message: {"error": message},
    )
    assert protected_error is not None
    assert "protected=60.0" in protected_error["error"]
    assert unprotected_error is None
    assert raw_quantity == 100.0

    protected_crypto = await _projected_holdings(
        monkeypatch,
        scope="upbit_live",
        market="crypto",
        symbol="KRW-BTC",
        mode_settings=_settings(upbit="enforce"),
    )
    assert protected_crypto["quantity"] == 40.0, protected_crypto
    raw_crypto = {
        "quantity": 100.0,
        "broker_sellable_quantity": 100.0,
        "total_quantity": 100.0,
        "locked": 0.0,
        "avg_price": 90.0,
        "sellable_observed": True,
    }

    async def crypto_holdings(
        symbol: str, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        return protected_crypto if symbol == "BTC" else raw_crypto

    sends: list[tuple[Any, ...]] = []

    async def place_sell(*args: Any, **_kwargs: Any) -> dict[str, str]:
        sends.append(args)
        return {"uuid": f"fake-{len(sends)}"}

    monkeypatch.setattr(order_execution, "_get_holdings_for_order", crypto_holdings)
    monkeypatch.setattr(order_execution.upbit_service, "place_sell_order", place_sell)
    monkeypatch.setattr(
        order_execution.upbit_service,
        "adjust_price_to_upbit_unit",
        lambda price: price,
    )
    await order_execution._execute_crypto_order("BTC", "sell", "limit", None, 100.0)
    await order_execution._execute_crypto_order("ETH", "sell", "limit", None, 100.0)

    assert sends[0][1] == "40.00000000"
    assert sends[1][1] == "100.00000000"


@pytest.mark.asyncio
async def test_c5_c6_toss_reader_preserves_raw_evidence_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6 emits raw Toss S; C5 alone converts it to tactical display S-P."""

    from app.services import invest_home_readers as readers
    from app.services.invest_home_service import InvestHomeService
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    def position(symbol: str) -> TossPortfolioPosition:
        return TossPortfolioPosition(
            account="fake-toss",
            account_name="Fake Toss",
            broker="toss",
            source="toss_api",
            instrument_type="equity_kr",
            market="kr",
            symbol=symbol,
            name=symbol,
            quantity=Decimal("100"),
            avg_buy_price=Decimal("90"),
            current_price=Decimal("100"),
            evaluation_amount=Decimal("10000"),
            profit_loss=Decimal("1000"),
            profit_rate=Decimal("0.1"),
            sellable_quantity=Decimal("100"),
        )

    async def fake_toss_snapshot(**_kwargs: Any) -> TossPortfolioSnapshot:
        return TossPortfolioSnapshot(positions=[position("005930"), position("000660")])

    protected = _snapshot(scope="toss_live", market="kr", symbol="005930")

    async def read_head(key: policy.ProtectionKey):
        return protected if key.symbol == "005930" else None

    monkeypatch.setattr(readers, "fetch_toss_portfolio_snapshot", fake_toss_snapshot)
    monkeypatch.setattr(readers, "get_shared_sellable_cache", lambda: object())
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(settings, "protected_quantity_mode_toss_live", "enforce")
    monkeypatch.setattr(policy, "_read_head_snapshot", read_head)

    reader_result = await readers.TossApiHomeReader().fetch(user_id=1)
    assert [holding.sellableQuantity for holding in reader_result.holdings] == [
        100.0,
        100.0,
    ]
    assert [holding.brokerSellableQuantity for holding in reader_result.holdings] == [
        100.0,
        100.0,
    ]

    home = InvestHomeService(
        kis_reader=None,
        upbit_reader=None,
        manual_reader=None,
    )
    await home._apply_protection_projection(reader_result.holdings)

    protected_holding, raw_holding = reader_result.holdings
    assert protected_holding.sellableQuantity == 40.0
    assert protected_holding.brokerSellableQuantity == 100.0
    assert protected_holding.protectedQuantity == 60.0
    assert raw_holding.sellableQuantity == 100.0
    assert raw_holding.brokerSellableQuantity == 100.0
    assert raw_holding.protectedQuantity == 0.0


@pytest.mark.asyncio
async def test_fake_autoapprove_revalidation_reaches_g1_shortfall_and_rejects_rung(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proposal creation plus a fake auto gate reaches the real G1 refusal path."""

    from app.mcp_server.tooling import kis_live_ledger
    from app.mcp_server.tooling import order_execution as execution

    key = policy.normalize_protection_key(
        account_scope="kis_live", market="kr", symbol="005930"
    )
    block = policy.ProtectionBlock(
        error_code="protected_quantity_shortfall",
        key=key,
        protected_quantity=Decimal("60"),
        broker_sellable=Decimal("50"),
        headroom=Decimal("0"),
        quantity=Decimal("40"),
    )

    class Lease:
        active = True

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.release_calls = 0

        async def evaluate(self, **kwargs: Any) -> policy.ProtectionDecision:
            self.calls.append(kwargs)
            return policy.ProtectionDecision(
                False,
                "shortfall",
                Decimal("0"),
                block=block,
            )

        async def release(self) -> None:
            self.release_calls += 1

    lease = Lease()
    reserve = AsyncMock(return_value=1)
    broker_send = AsyncMock(return_value={"rt_cd": "0", "odno": "fake"})
    monkeypatch.setattr(
        execution,
        "prepare_live_sell_lease",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(
        execution,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "quantity": 40.0,
                "broker_sellable_quantity": 50.0,
                "total_quantity": 50.0,
                "sellable_observed": True,
            }
        ),
    )
    monkeypatch.setattr(execution, "_execute_order", broker_send)
    monkeypatch.setattr(execution.OrderSendIntentService, "reserve", reserve)
    monkeypatch.setattr(execution, "_record_order_history", AsyncMock())
    monkeypatch.setattr(
        kis_live_ledger,
        "_record_kis_live_order",
        AsyncMock(return_value={"success": True}),
    )

    now = datetime.now(UTC)
    service = OrderProposalsService(db_session)
    proposal = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id="fake-kis-account",
        side="sell",
        order_type="limit",
        proposer="offline-728-fixture",
        thesis="offline protected quantity fixture",
        rungs=[RungInput(0, "sell", Decimal("40"), Decimal("70000"), None)],
        now=now,
    )
    await db_session.commit()
    submit_results: list[dict[str, Any]] = []

    async def fake_place_order(**kwargs: Any) -> dict[str, Any]:
        if kwargs["dry_run"] is True:
            return {
                "success": True,
                "price": kwargs["price"],
                "quantity": kwargs["quantity"],
                "current_price": Decimal("70000"),
                "approval_hash": "offline-preview",
            }
        result = await execution._execute_and_record(
            normalized_symbol="005930",
            side="sell",
            order_type="limit",
            order_quantity=float(kwargs["quantity"]),
            price=float(kwargs["price"]),
            market_type="equity_kr",
            current_price=70_000.0,
            avg_price=60_000.0,
            dry_run_result={"price": 70_000.0, "quantity": 40.0},
            order_amount=2_800_000.0,
            reason="offline proposal G1 fixture",
            exit_reason=None,
            exit_intent=None,
            thesis="offline protected quantity fixture",
            strategy=None,
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            indicators_snapshot=None,
            defensive_trim_ctx=None,
            order_error_fn=lambda message: {"success": False, "error": message},
            is_mock=False,
            idempotency_key="offline-proposal-g1",
        )
        submit_results.append(result)
        return result

    async def fake_autoapprove(**_kwargs: Any) -> dict[str, Any]:
        return {"eligible": True, "reason": "offline_fixture"}

    window = await allow_known_session(proposal, now=now)
    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=proposal.proposal_id,
        now=now,
        place_order_fn=fake_place_order,
        eligibility_gate=fake_autoapprove,
        window_evaluator=allow_known_session,
        expected_policy_stamp=window.policy_stamp,
        now_fn=lambda: now,
    )

    assert [outcome.result for outcome in outcomes] == ["error"]
    assert submit_results[0]["error_code"] == "protected_quantity_shortfall"
    assert lease.calls == [
        {
            "quantity": 40.0,
            "kind": "new",
            "fresh_broker_sellable": 50.0,
            "fresh_broker_held": 50.0,
            "sellable_observed": True,
        }
    ]
    assert lease.release_calls == 1
    reserve.assert_not_awaited()
    broker_send.assert_not_awaited()
    _group, rungs = await service.get_proposal(proposal.proposal_id)
    assert rungs[0].state == "rejected"


@pytest.mark.asyncio
async def test_fake_kis_kr_sells_headroom_once_then_rejects_the_next_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3 uses G5's real per-fragment fresh check under the fake KIS state."""

    from app.services import kis_trading_service as legacy

    snapshot = _snapshot()
    state = {"sellable": Decimal("100")}
    leases: list[Any] = []
    sends: list[int] = []

    class Lease:
        active = True

        def __init__(self) -> None:
            self.release_calls = 0

        async def evaluate(self, **kwargs: Any) -> policy.ProtectionDecision:
            return await policy._evaluate_live_sell(
                snapshot=snapshot,
                mode="enforce",
                quantity=kwargs["quantity"],
                kind=kwargs["kind"],
                fresh_broker_sellable=kwargs["fresh_broker_sellable"],
                fresh_broker_held=kwargs["fresh_broker_held"],
                sellable_observed=kwargs["sellable_observed"],
                amend_remaining_fresh=None,
            )

        async def release(self) -> None:
            self.release_calls += 1

    async def prepare(**_kwargs: Any) -> Lease:
        lease = Lease()
        leases.append(lease)
        return lease

    async def fresh_position(*_args: Any) -> tuple[Decimal, Decimal, bool]:
        return state["sellable"], Decimal("100"), True

    async def send(*args: Any, **_kwargs: Any) -> dict[str, str]:
        quantity = int(args[3])
        sends.append(quantity)
        state["sellable"] -= Decimal(quantity)
        return {"odno": f"fake-{len(sends)}"}

    monkeypatch.setattr(policy, "_is_drifted", AsyncMock(return_value=False))
    monkeypatch.setattr(legacy, "prepare_live_sell_lease", prepare)
    monkeypatch.setattr(legacy, "_legacy_fresh_kis_sell_position", fresh_position)
    ops = SimpleNamespace(market="domestic", place_order=send)

    first = await legacy._place_legacy_guarded_sell_fragment(
        ops,
        SimpleNamespace(),
        "005930",
        40,
        70_000,
        exchange_code=None,
    )
    second = await legacy._place_legacy_guarded_sell_fragment(
        ops,
        SimpleNamespace(),
        "005930",
        1,
        70_000,
        exchange_code=None,
    )

    assert first["odno"] == "fake-1"
    assert second["error_code"] == "protected_quantity_exceeded"
    assert sends == [40]
    assert state["sellable"] == Decimal("60")
    assert [lease.release_calls for lease in leases] == [1, 1]
