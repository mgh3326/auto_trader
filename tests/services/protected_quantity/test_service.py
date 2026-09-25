"""Unit coverage for the #728 long-term quantity-floor policy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import protected_quantity_service as policy

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def _settings(*, kis: str = "off", toss: str = "off", upbit: str = "off"):
    return SimpleNamespace(
        protected_quantity_mode_kis_live=kis,
        protected_quantity_mode_toss_live=toss,
        protected_quantity_mode_upbit_live=upbit,
    )


def _snapshot(
    *,
    scope: str = "kis_live",
    market: str = "kr",
    symbol: str = "005930",
    protected: str = "60",
    held: str = "100",
) -> policy.ProtectedPositionSnapshot:
    return policy.ProtectedPositionSnapshot(
        id=1,
        key=policy.normalize_protection_key(
            account_scope=scope,
            market=market,
            symbol=symbol,
        ),
        protected_quantity=Decimal(protected),
        revision=1,
        last_confirmed_broker_held=Decimal(held),
        last_confirmed_at=NOW,
        updated_by_user_id=1,
        updated_at=NOW,
    )


@pytest.fixture(autouse=True)
def _stable_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "_is_drifted", AsyncMock(return_value=False))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (True, "Decimal string"),
        (1, "Decimal string"),
        (1.0, "Decimal string"),
        ("NaN", "non-negative finite"),
        ("-1", "non-negative finite"),
        ("0.000000001", "at most 8 decimals"),
    ],
)
def test_operator_quantity_rejects_lossy_or_invalid_values(value, message) -> None:
    with pytest.raises(policy.ProtectedQuantityValidationError, match=message):
        policy.parse_operator_quantity(value, field="protected_quantity")


def test_key_normalization_uses_db_and_upbit_market_dialects() -> None:
    assert (
        policy.normalize_protection_key(
            account_scope="kis_live",
            market="us",
            symbol="BRK-B",
        ).symbol
        == "BRK.B"
    )
    assert (
        policy.normalize_protection_key(
            account_scope="upbit_live",
            market="crypto",
            symbol="KRW-BTC",
        ).symbol
        == "KRW-BTC"
    )
    with pytest.raises(policy.ProtectedQuantityValidationError):
        policy.normalize_protection_key(
            account_scope="upbit_live",
            market="kr",
            symbol="005930",
        )


@pytest.mark.asyncio
async def test_c1_enforce_replaces_only_tactical_quantity_and_never_double_subtract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    monkeypatch.setattr(policy, "_read_head_snapshot", AsyncMock(return_value=snapshot))
    result = await policy.apply_holdings_protection(
        {
            "quantity": 100,
            "total_quantity": 100,
            "locked": 0,
            "sellable_observed": True,
        },
        account_scope="kis_live",
        market="kr",
        symbol="005930",
        settings_obj=_settings(kis="enforce"),
    )

    assert result["quantity"] == 40.0
    assert result["broker_sellable_quantity"] == 100.0
    assert result["protected_quantity"] == 60.0
    assert result["tactical_sellable_quantity"] == 40.0
    assert result["total_quantity"] == 100

    decision = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=40,
        kind="new",
        fresh_broker_sellable=result["broker_sellable_quantity"],
        fresh_broker_held=result["total_quantity"],
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    assert decision.allowed is True
    assert decision.headroom == Decimal("40")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_off_and_shadow_keep_existing_sellable_value_but_expose_headroom(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(
        policy, "_read_head_snapshot", AsyncMock(return_value=_snapshot())
    )
    result = await policy.apply_position_protection(
        {
            "quantity": 100,
            "sellable_quantity": 100,
            "sellable_observed": True,
        },
        account_scope="kis_live",
        market="kr",
        symbol="005930",
        settings_obj=_settings(kis=mode),
    )

    assert result["sellable_quantity"] == 100
    assert result["broker_sellable_quantity"] == 100.0
    assert result["tactical_sellable_quantity"] == 40.0
    assert result["protected_quantity"] == 60.0


@pytest.mark.asyncio
async def test_mock_projection_is_unchanged_without_policy_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock()
    monkeypatch.setattr(policy, "_read_head_snapshot", lookup)
    original = {"quantity": 7, "total_quantity": 7, "sellable_observed": True}
    result = await policy.apply_holdings_protection(
        original,
        account_scope="kis_live",
        market="kr",
        symbol="005930",
        is_mock=True,
    )
    assert result == original
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_unobserved_c1_sellable_never_becomes_a_broker_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        policy, "_read_head_snapshot", AsyncMock(return_value=_snapshot())
    )
    result = await policy.apply_holdings_protection(
        {
            # Legacy C1 preserves total-held in quantity when ord_psbl_qty is
            # absent. The separate None field prevents G1 from treating it as S.
            "quantity": 100,
            "broker_sellable_quantity": None,
            "total_quantity": 100,
            "sellable_observed": False,
        },
        account_scope="kis_live",
        market="kr",
        symbol="005930",
        settings_obj=_settings(kis="enforce"),
    )

    assert result["quantity"] == 0.0
    assert result["broker_sellable_quantity"] is None
    assert result["tactical_sellable_quantity"] is None
    assert result["protected_quantity"] == 60.0
    assert result["protection_state"] == "unverified"


@pytest.mark.asyncio
async def test_unobserved_nonprotected_value_is_not_zeroed_in_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "_read_head_snapshot", AsyncMock(return_value=None))
    result = await policy.apply_holdings_protection(
        {
            "quantity": 100,
            "total_quantity": 100,
            "sellable_observed": False,
        },
        account_scope="kis_live",
        market="kr",
        symbol="005930",
        settings_obj=_settings(kis="enforce"),
    )

    # No protected declaration means the legacy KIS total-held fallback is
    # unchanged, but it is never relabelled as a broker sellable fact.
    assert result["quantity"] == 100
    assert result["broker_sellable_quantity"] is None
    assert result["protected_quantity"] == 0.0
    assert result["protection_state"] == "unprotected"


@pytest.mark.asyncio
async def test_c1_tactical_rejection_exposes_the_protected_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import order_validation

    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "quantity": 40.0,
                "total_quantity": 100.0,
                "locked": 0.0,
                "protected_quantity": 60.0,
            }
        ),
    )

    _, _, error = await order_validation._validate_sell_side(
        symbol="005930",
        normalized_symbol="005930",
        market_type="equity_kr",
        quantity=41.0,
        order_type="limit",
        price=70_000.0,
        current_price=70_000.0,
        order_error_fn=lambda message: {"error": message},
    )

    assert error is not None
    assert "protected=60.0" in error["error"]


@pytest.mark.asyncio
async def test_unobserved_c7_position_does_not_promote_total_held_as_sellable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        policy,
        "_read_head_snapshot",
        AsyncMock(return_value=_snapshot()),
    )
    result = await policy.apply_position_protection(
        {
            "quantity": 100,
            "sellable_quantity": 100,
            "sellable_observed": False,
        },
        account_scope="kis_live",
        market="kr",
        symbol="005930",
        settings_obj=_settings(kis="enforce"),
    )

    assert result["sellable_quantity"] == 0.0
    assert result["broker_sellable_quantity"] is None
    assert result["tactical_sellable_quantity"] is None
    assert result["protection_state"] == "unverified"


@pytest.mark.asyncio
async def test_enforce_boundaries_and_shadow_would_block() -> None:
    snapshot = _snapshot()
    allowed = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=Decimal("40.00000000"),
        kind="new",
        fresh_broker_sellable=Decimal("100"),
        fresh_broker_held=Decimal("100"),
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    blocked = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=Decimal("40.00000001"),
        kind="new",
        fresh_broker_sellable=Decimal("100"),
        fresh_broker_held=Decimal("100"),
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    shadow = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="shadow",
        quantity=41,
        kind="new",
        fresh_broker_sellable=100,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=None,
    )

    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.block is not None
    assert blocked.block.error_code == "protected_quantity_exceeded"
    assert shadow.allowed is True
    assert shadow.would_block is True
    assert shadow.block is not None
    assert shadow.block.error_code == "protected_quantity_exceeded"


@pytest.mark.asyncio
async def test_fail_closed_states_cover_unobserved_shortfall_encroachment_and_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    unobserved = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=1,
        kind="new",
        fresh_broker_sellable=None,
        fresh_broker_held=100,
        sellable_observed=False,
        amend_remaining_fresh=None,
    )
    shortfall = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=1,
        kind="new",
        fresh_broker_sellable=50,
        fresh_broker_held=50,
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    encroached = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=1,
        kind="new",
        fresh_broker_sellable=50,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    monkeypatch.setattr(policy, "_is_drifted", AsyncMock(return_value=True))
    drifted = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=1,
        kind="new",
        fresh_broker_sellable=100,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=None,
    )

    assert unobserved.block is not None
    assert unobserved.block.error_code == "protected_sellable_unobserved"
    assert shortfall.block is not None
    assert shortfall.block.error_code == "protected_quantity_shortfall"
    assert encroached.block is not None
    assert encroached.block.error_code == "protected_quantity_encroached"
    assert drifted.block is not None
    assert drifted.block.error_code == "protected_state_unverified"


@pytest.mark.asyncio
async def test_amend_and_upbit_cancel_replace_use_their_distinct_conservative_bounds() -> (
    None
):
    snapshot = _snapshot()
    amend = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=40,
        kind="amend_uncapped",
        fresh_broker_sellable=60,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    pre_cancel = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=40,
        kind="cancel_replace",
        fresh_broker_sellable=60,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=40,
    )

    assert amend.allowed is False
    assert amend.block is not None
    assert amend.block.error_code == "protected_quantity_exceeded"
    assert pre_cancel.allowed is True
    assert pre_cancel.headroom == Decimal("40")


@pytest.mark.asyncio
async def test_amend_broker_capped_only_allows_a_bounded_increment() -> None:
    snapshot = _snapshot()
    bounded = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=50,
        kind="amend_broker_capped",
        fresh_broker_sellable=100,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=40,
    )
    excessive = await policy._evaluate_live_sell(
        snapshot=snapshot,
        mode="enforce",
        quantity=81,
        kind="amend_broker_capped",
        fresh_broker_sellable=100,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=40,
    )

    assert bounded.allowed is True
    assert excessive.allowed is False
    assert excessive.block is not None
    assert excessive.block.error_code == "protected_quantity_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_sellable", [True, "NaN", -1])
async def test_invalid_sellable_values_fail_closed_for_active_protection(
    invalid_sellable: object,
) -> None:
    decision = await policy._evaluate_live_sell(
        snapshot=_snapshot(),
        mode="enforce",
        quantity=1,
        kind="new",
        fresh_broker_sellable=invalid_sellable,
        fresh_broker_held=100,
        sellable_observed=True,
        amend_remaining_fresh=None,
    )

    assert decision.allowed is False
    assert decision.block is not None
    assert decision.block.error_code == "protected_sellable_unobserved"


@pytest.mark.asyncio
async def test_invalid_held_evidence_uses_unverified_not_sellable_error() -> None:
    decision = await policy._evaluate_live_sell(
        snapshot=_snapshot(),
        mode="enforce",
        quantity=1,
        kind="new",
        fresh_broker_sellable=100,
        fresh_broker_held="NaN",
        sellable_observed=True,
        amend_remaining_fresh=None,
    )
    assert decision.allowed is False
    assert decision.block is not None
    assert decision.block.error_code == "protected_state_unverified"


@pytest.mark.asyncio
async def test_drift_reads_upbit_market_code_but_stock_db_symbol() -> None:
    class Result:
        def scalar_one(self) -> Decimal:
            return Decimal("0")

    class Db:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            return Result()

    crypto_db = Db()
    stock_db = Db()
    crypto_key = policy.normalize_protection_key(
        account_scope="upbit_live",
        market="crypto",
        symbol="KRW-BTC",
    )
    stock_key = policy.normalize_protection_key(
        account_scope="kis_live",
        market="us",
        symbol="BRK-B",
    )

    assert await policy._net_execution_quantity_since(
        crypto_db,  # type: ignore[arg-type]
        key=crypto_key,
        since=NOW,
    ) == Decimal("0")
    assert await policy._net_execution_quantity_since(
        stock_db,  # type: ignore[arg-type]
        key=stock_key,
        since=NOW,
    ) == Decimal("0")

    crypto_sql = str(crypto_db.statements[0])
    stock_sql = str(stock_db.statements[0])
    assert "execution_ledger.raw_symbol" in crypto_sql
    assert "execution_ledger.symbol =" not in crypto_sql
    assert "execution_ledger.symbol" in stock_sql
