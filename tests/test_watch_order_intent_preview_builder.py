from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.watch_intent_policy import IntentPolicy
from app.services.watch_order_intent_preview_builder import (
    IntentBuildFailure,
    IntentBuildSuccess,
    build_preview,
)


def _watch(
    market: str = "kr",
    symbol: str = "005930",
    threshold: Decimal = Decimal("70000"),
) -> dict:
    return {
        "market": market,
        "target_kind": "asset",
        "symbol": symbol,
        "condition_type": "price_below",
        "threshold": threshold,
        "threshold_key": str(threshold),
    }


def _intent_policy(**overrides: object) -> IntentPolicy:
    base = {
        "action": "create_order_intent",
        "side": "buy",
        "quantity": 1,
        "notional_krw": None,
        "limit_price": None,
        "max_notional_krw": None,
    }
    base.update(overrides)
    return IntentPolicy(**base)  # type: ignore[arg-type]


class TestKrQuantitySuccess:
    def test_basic_buy_uses_threshold_as_limit_price(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=1, max_notional_krw=Decimal("1500000")),
            watch=_watch(),
            triggered_value=Decimal("68500"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        line = result.preview_line
        assert line.symbol == "005930"
        assert line.market == "kr"
        assert line.side == "buy"
        assert line.account_mode == "kis_mock"
        assert line.execution_source == "watch"
        assert line.lifecycle_state == "previewed"
        assert line.quantity == pytest.approx(Decimal("1"))
        assert line.limit_price == pytest.approx(Decimal("70000"))
        assert line.notional == pytest.approx(Decimal("70000"))
        assert line.currency == "KRW"
        assert line.guard.execution_allowed is False
        assert line.guard.approval_required is True
        assert result.notional_krw_evaluated == pytest.approx(Decimal("70000"))
        assert result.fx_usd_krw_used is None

    def test_static_limit_price_override_used(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=2, limit_price=Decimal("69000")),
            watch=_watch(),
            triggered_value=Decimal("68500"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        assert result.preview_line.limit_price == pytest.approx(Decimal("69000"))
        assert result.preview_line.notional == pytest.approx(Decimal("138000"))
        assert result.notional_krw_evaluated == pytest.approx(Decimal("138000"))


class TestKrNotionalKrwSizing:
    def test_notional_krw_resolves_to_floor_quantity(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=None, notional_krw=Decimal("141000")),
            watch=_watch(threshold=Decimal("70000")),
            triggered_value=Decimal("68000"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        # floor(141000 / 70000) == 2
        assert result.preview_line.quantity == pytest.approx(Decimal("2"))
        assert result.preview_line.notional == pytest.approx(Decimal("140000"))

    def test_notional_krw_floor_below_one_is_qty_zero_failure(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=None, notional_krw=Decimal("69999")),
            watch=_watch(threshold=Decimal("70000")),
            triggered_value=Decimal("68000"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildFailure)
        assert result.blocked_by == "qty_zero"


class TestUsWithFxAndCap:
    def test_us_quantity_uses_fx_for_krw_evaluation(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=10, max_notional_krw=Decimal("3000000")),
            watch=_watch(market="us", symbol="AAPL", threshold=Decimal("180")),
            triggered_value=Decimal("181"),
            fx_quote=Decimal("1400"),
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        line = result.preview_line
        assert line.currency == "USD"
        assert line.limit_price == pytest.approx(Decimal("180"))
        assert line.notional == pytest.approx(Decimal("1800"))
        # 10 * 180 * 1400 = 2_520_000
        assert result.notional_krw_evaluated == pytest.approx(Decimal("2520000"))
        assert result.fx_usd_krw_used == pytest.approx(Decimal("1400"))

    def test_us_without_fx_quote_is_fx_unavailable_failure(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=10, max_notional_krw=Decimal("3000000")),
            watch=_watch(market="us", symbol="AAPL", threshold=Decimal("180")),
            triggered_value=Decimal("181"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildFailure)
        assert result.blocked_by == "fx_unavailable"

    def test_cap_blocked_failure(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=10, max_notional_krw=Decimal("100000")),
            watch=_watch(market="us", symbol="AAPL", threshold=Decimal("180")),
            triggered_value=Decimal("181"),
            fx_quote=Decimal("1400"),
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildFailure)
        assert result.blocked_by == "max_notional_krw_cap"
        # Failure still records evaluated KRW so the ledger row carries it
        assert result.notional_krw_evaluated == pytest.approx(Decimal("2520000"))
        assert result.fx_usd_krw_used == pytest.approx(Decimal("1400"))
