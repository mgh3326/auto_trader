"""ROB-461 — kis_mock equity sells may book a loss (손절 / stop-loss practice).

KIS mock is a practice sandbox with no real money, so the avg*1.01 floor and the
below-current-price guard must NOT block a loss-sell there. The bypass is scoped to
`is_mock AND market_type in {equity_kr, equity_us}` (the codebase idiom for "KIS mock
equities"); live (is_mock=False) and crypto (real Upbit funds) stay fully guarded.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    DefensiveTrimContext,
    LossCutContext,
    ScalpingExitContext,
    evaluate_sell_price_guards,
)


# ---------------------------------------------------------------------------
# Pure guard: allow_loss_sell bypasses BOTH the avg*1.01 floor and current-price
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_allow_loss_sell_bypasses_floor_and_current_price() -> None:
    # price below floor (avg*1.01) AND below current price.
    err = evaluate_sell_price_guards(
        price=68000.0,
        current_price=68500.0,
        avg_price=90400.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=True,
    )
    assert err is None


@pytest.mark.unit
def test_allow_loss_sell_bypasses_below_current_only() -> None:
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1100.0,
        avg_price=900.0,  # above floor, but below current
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=True,
    )
    assert err is None


@pytest.mark.unit
def test_allow_loss_sell_false_still_blocks_floor() -> None:
    # Default (live) behavior is byte-for-byte unchanged.
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1000.0,
        avg_price=1000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err is not None and "below minimum" in err


@pytest.mark.unit
def test_allow_loss_sell_default_is_false() -> None:
    # Omitting the kwarg keeps the floor guard (no accidental relaxation).
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1000.0,
        avg_price=1000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
    )
    assert err is not None and "below minimum" in err


# ---------------------------------------------------------------------------
# _preview_sell: mock equity allows loss; live + crypto stay blocked
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_preview_sell_mock_equity_allows_loss_sell(
    monkeypatch, market_type
) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    # The exact 2026-06-09 repro: DL이앤씨 avg 90,400 / current ~68,500, sell at 68,000.
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="limit",
        quantity=10,
        price=68000.0,
        current_price=68500.0,
        market_type=market_type,
        is_mock=True,
    )
    assert "error" not in result
    assert result["price"] == 68000.0
    assert result["realized_pnl"] < 0  # an honest loss is booked


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_live_equity_still_blocks_loss(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="limit",
        quantity=10,
        price=68000.0,
        current_price=68500.0,
        market_type="equity_kr",
        is_mock=False,
    )
    assert "error" in result and "below minimum" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_mock_crypto_still_blocks_loss(monkeypatch) -> None:
    # Crypto routes to real Upbit funds; a kis_mock+crypto symbol must NOT relax
    # the floor even though is_mock=True (defense-in-depth).
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 40000000.0, "quantity": 0.1}),
    )
    result = await order_validation._preview_sell(
        symbol="KRW-BTC",
        order_type="limit",
        quantity=0.1,
        price=30000000.0,
        current_price=31000000.0,
        market_type="crypto",
        is_mock=True,
    )
    assert "error" in result and "below minimum" in result["error"]


# ---------------------------------------------------------------------------
# _validate_sell_side: same matrix on the execution path
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_mock_equity_allows_loss_sell(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    monkeypatch.setattr(
        order_validation,
        "_get_kis_mock_shadow_exposure",
        AsyncMock(
            return_value={
                "confidence": "db_shadow_pending",
                "sell_reserved_quantity": 0,
            }
        ),
    )
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="375500",
        normalized_symbol="375500",
        market_type="equity_kr",
        quantity=10,
        order_type="limit",
        price=68000.0,
        current_price=68500.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=True,
        dry_run=True,
    )
    assert err is None and errors == []
    assert avg == 90400.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_mock_crypto_still_blocks_loss(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 40000000.0, "quantity": 0.1, "locked": 0}),
    )
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="KRW-BTC",
        normalized_symbol="KRW-BTC",
        market_type="crypto",
        quantity=0.1,
        order_type="limit",
        price=30000000.0,
        current_price=31000000.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=True,
        dry_run=True,
    )
    assert err is not None
    assert "below minimum" in errors[0]


# ---------------------------------------------------------------------------
# Audit trail: the bypass must be logged (observability is the whole story)
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_mock_equity_loss_sell_emits_audit_log(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    monkeypatch.setattr(
        order_validation,
        "_get_kis_mock_shadow_exposure",
        AsyncMock(
            return_value={
                "confidence": "db_shadow_pending",
                "sell_reserved_quantity": 0,
            }
        ),
    )
    # _log_mock_loss_sell_bypass is a sync function -> plain Mock (not AsyncMock).
    log_mock = Mock()
    monkeypatch.setattr(order_validation, "_log_mock_loss_sell_bypass", log_mock)

    await order_validation._preview_sell(
        symbol="375500",
        order_type="limit",
        quantity=10,
        price=68000.0,
        current_price=68500.0,
        market_type="equity_kr",
        is_mock=True,
    )
    await order_validation._validate_sell_side(
        symbol="375500",
        normalized_symbol="375500",
        market_type="equity_kr",
        quantity=10,
        order_type="limit",
        price=68000.0,
        current_price=68500.0,
        order_error_fn=lambda m: {"error": m},
        is_mock=True,
        dry_run=True,
    )
    # Both the preview and execution phases audit the bypass.
    phases = {c.kwargs["phase"] for c in log_mock.call_args_list}
    assert phases == {"preview", "execution"}
    for call in log_mock.call_args_list:
        assert call.kwargs["symbol"] == "375500"
        assert call.kwargs["market_type"] == "equity_kr"
        assert call.kwargs["price"] == 68000.0
        assert call.kwargs["avg_price"] == 90400.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_mock_market_loss_allowed_and_audited(monkeypatch) -> None:
    # ROB-518 superseded the old "market sells skip price guards" exemption:
    # live market sells below the floor are now blocked (see
    # test_live_loss_sell_hard_guard.py). Mock equity keeps the ROB-461
    # practice bypass, and the market-path bypass is audited like the limit one.
    log_mock = Mock()
    monkeypatch.setattr(order_validation, "_log_mock_loss_sell_bypass", log_mock)
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="market",
        quantity=10,
        price=None,
        current_price=68500.0,
        market_type="equity_kr",
        is_mock=True,
    )
    assert "error" not in result
    assert result["price"] == 68500.0  # execution_price == current_price
    assert result["realized_pnl"] < 0
    assert log_mock.call_args.kwargs["phase"] == "preview"


@pytest.mark.unit
def test_scalping_exit_takes_precedence_over_allow_loss_sell() -> None:
    # If a mock equity scalping exit and allow_loss_sell are both in play, the guard
    # still returns None (both bypass), and the caller's elif chain attributes the
    # bypass to scalping (checked first) — never double-counted as a loss-sell.
    err = evaluate_sell_price_guards(
        price=68000.0,
        current_price=68500.0,
        avg_price=90400.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=ScalpingExitContext(strategy_id="s", reason="stop_loss"),
        allow_loss_sell=True,
    )
    assert err is None


# ---------------------------------------------------------------------------
# ROB-912: Sell Price Guard Marketable Band Tests
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_rob_912_sell_marketable_band_guards() -> None:
    # a. [XOM 재현] price=145.51, current=145.71, avg=100.0(floor 무관), ctx 전부 None -> None(허용)
    err_a = evaluate_sell_price_guards(
        price=145.51,
        current_price=145.71,
        avg_price=100.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err_a is None

    # b. [밴드 경계] price == current*(1-0.02) 정확히 -> None(허용, >= 시맨틱스)
    # 145.71 * 0.98 = 142.7958
    err_b = evaluate_sell_price_guards(
        price=142.7958,
        current_price=145.71,
        avg_price=100.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err_b is None

    # c. [fat-finger 차단] price = current*0.97 (밴드 밖) -> "below marketable band floor" 메시지 반환
    # 145.71 * 0.97 = 141.3387
    err_c = evaluate_sell_price_guards(
        price=141.3387,
        current_price=145.71,
        avg_price=100.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err_c is not None and "below marketable band floor" in err_c

    # d. [손실매도 여전히 차단] avg=150, current=145.71, price=145.51 (밴드 안이지만 avg*1.01=151.5 미달) -> floor 메시지 반환
    err_d = evaluate_sell_price_guards(
        price=145.51,
        current_price=145.71,
        avg_price=150.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err_d is not None and "below minimum" in err_d

    trim_ctx = DefensiveTrimContext(
        approval_issue_id="issue",
        requester_agent_id="agent",
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )

    # 밴드 안(<current) -> None
    err_e1 = evaluate_sell_price_guards(
        price=145.51,
        current_price=145.71,
        avg_price=150.0,
        defensive_trim_ctx=trim_ctx,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err_e1 is None

    # 밴드 밖(<current*0.98) -> band 메시지
    err_e2 = evaluate_sell_price_guards(
        price=141.3387,
        current_price=145.71,
        avg_price=150.0,
        defensive_trim_ctx=trim_ctx,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err_e2 is not None and "below marketable band floor" in err_e2

    loss_ctx = LossCutContext(
        retrospective_id=1,
        exit_reason="loss_cut",
        approval_issue_id=None,
        requester_agent_id="test",
        max_slip=0.05,
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )
    # price가 current*(1-0.05) = 145.71 * 0.95 = 138.4245 보다 작으면 차단되어야 함.
    # price=140.0 은 2% 밴드 밖(142.7958 미만)이지만 loss_cut 5% 밴드 안이므로 None(허용)이어야 함.
    err_f1 = evaluate_sell_price_guards(
        price=140.0,
        current_price=145.71,
        avg_price=150.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
        loss_cut_ctx=loss_ctx,
    )
    assert err_f1 is None

    # g. [시세 불가] current_price=0 -> 현재가 가드 미발동(기존 시맨틱스) 유지.
    err_g = evaluate_sell_price_guards(
        price=105.0,
        current_price=0.0,
        avg_price=100.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err_g is None
