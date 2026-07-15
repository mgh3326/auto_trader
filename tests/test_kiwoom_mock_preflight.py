# tests/test_kiwoom_mock_preflight.py
"""ROB-893 — Kiwoom mock order preflight service tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kiwoom.client import KiwoomPreDispatchError
from app.services.brokers.kiwoom.order_preflight import (
    PREFLIGHT_CASH_INSUFFICIENT,
    PREFLIGHT_CASH_READ_FAILED,
    PREFLIGHT_EVIDENCE_INVALID,
    PREFLIGHT_POSITION_READ_FAILED,
    PREFLIGHT_PRICE_DISTANCE_EXCEEDED,
    PREFLIGHT_PROVENANCE_CONFLICT,
    PREFLIGHT_QUOTE_MISSING,
    PREFLIGHT_QUOTE_STALE,
    PREFLIGHT_SELLABLE_EXCEEDED,
    PREFLIGHT_TICK_INVALID,
    run_order_preflight,
)


class _FakeAccountClient:
    def __init__(
        self,
        *,
        balance_payload: dict[str, Any] | None = None,
        cash_payload: dict[str, Any] | None = None,
        balance_error: Exception | None = None,
        cash_error: Exception | None = None,
    ) -> None:
        self.balance_calls = 0
        self.cash_calls = 0
        self._balance_payload = balance_payload
        self._cash_payload = cash_payload
        self._balance_error = balance_error
        self._cash_error = cash_error

    async def get_balance(self, **_kwargs):
        self.balance_calls += 1
        if self._balance_error is not None:
            raise self._balance_error
        return self._balance_payload or {"return_code": 0}

    async def get_orderable_amount(self, **_kwargs):
        self.cash_calls += 1
        if self._cash_error is not None:
            raise self._cash_error
        return self._cash_payload or {"return_code": 0}


def _positions_payload(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "return_code": 0,
        "acnt_evlt_remn_indv_tot": rows if rows is not None else [],
    }


def _position(symbol: str, qty: int, avg: int) -> dict[str, Any]:
    return {"stk_cd": symbol, "rmnd_qty": str(qty), "pur_pric": str(avg)}


@pytest.mark.asyncio
async def test_preflight_quote_missing_fails_closed():
    client = _FakeAccountClient()
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=None,
        quote_freshness="unavailable",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_QUOTE_MISSING
    assert result.checks[0].name == "quote_freshness"
    assert result.checks[0].ok is False
    assert client.balance_calls == 0
    assert client.cash_calls == 0


@pytest.mark.asyncio
async def test_preflight_quote_stale_fails_closed():
    client = _FakeAccountClient()
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="stale",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_QUOTE_STALE
    assert client.balance_calls == 0
    assert client.cash_calls == 0


@pytest.mark.parametrize(
    ("price", "expected_tick"),
    [
        (1500, 1),
        (3000, 5),
        (10000, 10),
        (30000, 50),
        (70000, 100),
        (300000, 500),
        (1000000, 1000),
    ],
)
@pytest.mark.asyncio
async def test_preflight_tick_validation_accepts_valid_krx_ticks(price, expected_tick):
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 10, 70000)])
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=price,
        quote_price=price,
        quote_freshness="fresh",
    )
    assert result.ok is True, result.error_code
    tick_check = next(c for c in result.checks if c.name == "tick_valid")
    assert tick_check.ok is True
    assert str(expected_tick) in (tick_check.detail or "")


@pytest.mark.parametrize(
    "invalid_price",
    [
        70001,  # tick=100 for 70000 range
        70004,
        12345,  # tick=10 for 10000 range
        50001,  # tick=100 for 50000 range
    ],
)
@pytest.mark.asyncio
async def test_preflight_tick_validation_rejects_non_krx_ticks(invalid_price):
    client = _FakeAccountClient()
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=invalid_price,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_TICK_INVALID
    assert client.cash_calls == 0


@pytest.mark.parametrize("price", [40000, 100000])
@pytest.mark.asyncio
async def test_preflight_price_distance_exceeded_fails_closed(price):
    client = _FakeAccountClient(
        cash_payload={"return_code": 0, "ord_alowa": "100000000"}
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=price,
        quote_price=70000,
        quote_freshness="fresh",
    )

    assert result.ok is False
    assert result.error_code == PREFLIGHT_PRICE_DISTANCE_EXCEEDED
    distance_check = next(c for c in result.checks if c.name == "price_distance")
    assert distance_check.ok is False
    assert result.estimated_evidence["price_distance_pct"] > 30.0
    assert client.cash_calls == 0


@pytest.mark.asyncio
async def test_preflight_price_distance_at_limit_passes():
    client = _FakeAccountClient(
        cash_payload={"return_code": 0, "ord_alowa": "100000000"}
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=91000,
        quote_price=70000,
        quote_freshness="fresh",
    )

    assert result.ok is True
    distance_check = next(c for c in result.checks if c.name == "price_distance")
    assert distance_check.ok is True
    assert result.estimated_evidence["price_distance_pct"] == 30.0
    assert client.cash_calls == 1


@pytest.mark.asyncio
async def test_preflight_sellable_exceeded_fails_closed():
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 5, 70000)])
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=10,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_SELLABLE_EXCEEDED
    sellable_check = next(c for c in result.checks if c.name == "sellable")
    assert sellable_check.ok is False
    assert "sellable=5" in sellable_check.detail


@pytest.mark.parametrize("return_code", [0, "0"])
@pytest.mark.asyncio
async def test_preflight_sellable_exact_match_passes(return_code):
    client = _FakeAccountClient(
        balance_payload={
            **_positions_payload([_position("005930", 10, 70000)]),
            "return_code": return_code,
        }
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=10,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_preflight_missing_symbol_position_fails_closed():
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("000660", 10, 50000)])
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_SELLABLE_EXCEEDED
    assert result.estimated_evidence.get("sellable_quantity") == 0


@pytest.mark.asyncio
async def test_preflight_cash_insufficient_fails_closed():
    client = _FakeAccountClient(
        cash_payload={
            "return_code": 0,
            "ord_alowa": "500000",
        }
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=10,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_CASH_INSUFFICIENT
    cash_check = next(c for c in result.checks if c.name == "cash")
    assert cash_check.ok is False
    assert result.estimated_evidence.get("orderable_cash") == 500000


@pytest.mark.parametrize("return_code", [0, "0"])
@pytest.mark.asyncio
async def test_preflight_cash_sufficient_passes(return_code):
    client = _FakeAccountClient(
        cash_payload={
            "return_code": return_code,
            "ord_alowa": "100000000",
        }
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=10,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is True
    assert result.estimated_evidence.get("orderable_cash") == 100000000


@pytest.mark.asyncio
async def test_preflight_buy_marks_fee_and_tax_estimates_unavailable():
    client = _FakeAccountClient(
        cash_payload={"return_code": 0, "ord_alowa": "100000000"}
    )

    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )

    assert result.ok is True
    assert result.estimated_evidence["estimated_costs"] == {
        "fee": None,
        "tax": None,
        "currency": "KRW",
        "status": "unavailable",
        "source": None,
        "review_required": True,
        "reason": "kiwoom_mock_cost_profile_unavailable",
    }
    assert result.to_response_extras()["preflight_warnings"] == [
        "estimated_costs_unavailable"
    ]


@pytest.mark.asyncio
async def test_preflight_cash_missing_field_fails_closed():
    client = _FakeAccountClient(
        cash_payload={"return_code": 0}  # no ord_alowa
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_EVIDENCE_INVALID


@pytest.mark.parametrize(
    "return_code",
    [
        pytest.param("missing", id="missing"),
        pytest.param(None, id="none"),
        pytest.param(True, id="bool"),
        pytest.param(0.0, id="float"),
        pytest.param(1, id="nonzero-int"),
        pytest.param("1", id="nonzero-string"),
    ],
)
@pytest.mark.asyncio
async def test_preflight_cash_broker_failure_fails_closed(return_code):
    payload: dict[str, Any] = {"ord_alowa": "100000000"}
    if return_code != "missing":
        payload["return_code"] = return_code
    client = _FakeAccountClient(cash_payload=payload)

    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )

    assert result.ok is False
    assert result.error_code == PREFLIGHT_EVIDENCE_INVALID


@pytest.mark.parametrize(
    "return_code",
    [
        pytest.param("missing", id="missing"),
        pytest.param(None, id="none"),
        pytest.param(True, id="bool"),
        pytest.param(0.0, id="float"),
        pytest.param(1, id="nonzero-int"),
        pytest.param("1", id="nonzero-string"),
    ],
)
@pytest.mark.asyncio
async def test_preflight_position_broker_failure_fails_closed(return_code):
    payload = _positions_payload([_position("005930", 10, 70000)])
    if return_code == "missing":
        payload.pop("return_code")
    else:
        payload["return_code"] = return_code
    client = _FakeAccountClient(balance_payload=payload)

    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )

    assert result.ok is False
    assert result.error_code == PREFLIGHT_EVIDENCE_INVALID


@pytest.mark.asyncio
async def test_preflight_position_provenance_conflict_fails_closed():
    client = _FakeAccountClient(
        balance_payload={
            "return_code": 0,
            "provenance": {"environment": "live"},
            "acnt_evlt_remn_indv_tot": [_position("005930", 10, 70000)],
        }
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_PROVENANCE_CONFLICT


@pytest.mark.asyncio
async def test_preflight_position_transport_failure_fails_closed():
    client = _FakeAccountClient(balance_error=RuntimeError("broker timeout"))
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_POSITION_READ_FAILED


@pytest.mark.asyncio
async def test_preflight_sell_reraises_pre_dispatch_error():
    client = _FakeAccountClient(
        balance_error=KiwoomPreDispatchError(
            stage="request_build",
            api_id="kt00018",
            cause_type="ValueError",
        )
    )

    with pytest.raises(KiwoomPreDispatchError) as exc_info:
        await run_order_preflight(
            account_client=client,
            symbol="005930",
            side="sell",
            quantity=1,
            price=70000,
            quote_price=70000,
            quote_freshness="fresh",
        )

    assert exc_info.value.stage == "request_build"
    assert exc_info.value.api_id == "kt00018"
    assert client.balance_calls == 1
    assert client.cash_calls == 0


@pytest.mark.asyncio
async def test_preflight_cash_transport_failure_fails_closed():
    client = _FakeAccountClient(cash_error=RuntimeError("broker timeout"))
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_CASH_READ_FAILED


@pytest.mark.asyncio
async def test_preflight_buy_reraises_pre_dispatch_error():
    client = _FakeAccountClient(
        cash_error=KiwoomPreDispatchError(
            stage="host_validation",
            api_id="kt00010",
            cause_type="ValueError",
        )
    )

    with pytest.raises(KiwoomPreDispatchError) as exc_info:
        await run_order_preflight(
            account_client=client,
            symbol="005930",
            side="buy",
            quantity=1,
            price=70000,
            quote_price=70000,
            quote_freshness="fresh",
        )

    assert exc_info.value.stage == "host_validation"
    assert exc_info.value.api_id == "kt00010"
    assert client.balance_calls == 0
    assert client.cash_calls == 1


@pytest.mark.asyncio
async def test_preflight_estimated_evidence_classified_separately():
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 10, 80000)])
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=5,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is True
    assert result.estimated_evidence["type"] == "estimated"
    assert result.estimated_evidence["sellable_quantity"] == 10
    assert result.estimated_evidence["average_price"] == 80000
    assert result.estimated_evidence["loss_sell"] is True


@pytest.mark.parametrize(
    ("quote_price", "price", "expected_pnl", "expected_loss", "expected_warnings"),
    [
        (70000, 90000, 20000, False, ["estimated_costs_unavailable"]),
        (
            90000,
            70000,
            -20000,
            True,
            ["estimated_costs_unavailable", "estimated_loss_sell"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_preflight_sell_pnl_uses_candidate_order_price(
    quote_price,
    price,
    expected_pnl,
    expected_loss,
    expected_warnings,
):
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 10, 80000)])
    )

    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=2,
        price=price,
        quote_price=quote_price,
        quote_freshness="fresh",
    )

    assert result.ok is True
    assert result.estimated_evidence["estimated_gross_pnl"] == expected_pnl
    assert result.estimated_evidence["estimated_gross_pnl_pct"] == (
        12.5 if expected_pnl > 0 else -12.5
    )
    assert result.estimated_evidence["estimated_net_pnl"] is None
    assert result.estimated_evidence["estimated_net_pnl_pct"] is None
    assert result.estimated_evidence["net_pnl_status"] == "unavailable"
    assert result.estimated_evidence["net_pnl_review_required"] is True
    assert result.estimated_evidence["loss_sell"] is expected_loss
    assert result.estimated_evidence["loss_sell_basis"] == "gross_before_costs"
    assert result.estimated_evidence["gross_pnl_basis"] == "order_price"
    assert result.estimated_evidence["pnl_basis_price"] == price
    assert result.estimated_evidence["estimated_costs"]["fee"] is None
    assert result.estimated_evidence["estimated_costs"]["tax"] is None
    assert result.to_response_extras()["preflight_warnings"] == expected_warnings


@pytest.mark.asyncio
async def test_preflight_loss_sell_not_blocked():
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 10, 100000)])
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=5,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is True, "loss sells must not be universally blocked"
    assert result.estimated_evidence["loss_sell"] is True


@pytest.mark.asyncio
async def test_preflight_does_not_mutate_account_position_for_sell():
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 10, 70000)])
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is True
    assert client.balance_calls == 1
    assert result.estimated_evidence["sellable_quantity"] == 10
    # Note: preflight itself does not mutate. Sellable quantity is the
    # authoritative read; we re-read positions on the next preflight call.


@pytest.mark.asyncio
async def test_preflight_buy_path_never_calls_position_read():
    client = _FakeAccountClient(
        cash_payload={"return_code": 0, "ord_alowa": "100000000"}
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is True
    assert client.balance_calls == 0
    assert client.cash_calls == 1


@pytest.mark.asyncio
async def test_preflight_sell_path_skips_cash_read():
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 10, 70000)])
    )
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is True
    assert client.cash_calls == 0


@pytest.mark.asyncio
async def test_preflight_to_response_extras_shape():
    result = await run_order_preflight(
        account_client=_FakeAccountClient(),
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="stale",
    )
    extras = result.to_response_extras()
    assert "preflight_checks" in extras
    assert "estimated_evidence" in extras
    assert all(
        "name" in c and "ok" in c and "detail" in c for c in extras["preflight_checks"]
    )


@pytest.mark.asyncio
async def test_preflight_zero_sellable_with_nonzero_quantity_blocks():
    client = _FakeAccountClient(balance_payload=_positions_payload([]))
    result = await run_order_preflight(
        account_client=client,
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        quote_price=70000,
        quote_freshness="fresh",
    )
    assert result.ok is False
    assert result.error_code == PREFLIGHT_SELLABLE_EXCEEDED
    assert result.estimated_evidence["sellable_quantity"] == 0


# ---------------------------------------------------------------------------
# ROB-893 v2: KiwoomAuthClient concurrent refresh dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_auth_refresh_dedupes_to_single_mint():
    """Three concurrent get_token() calls share one token-mint HTTP (asyncio.Lock)."""
    import datetime as dt

    import httpx

    from app.services.brokers.kiwoom import constants
    from app.services.brokers.kiwoom.auth import KiwoomAuthClient

    expires = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).strftime("%Y%m%d%H%M%S")
    mint_count = 0

    def handler(request):  # noqa: ARG001
        nonlocal mint_count
        mint_count += 1
        return httpx.Response(
            200,
            json={"return_code": 0, "token": "tok-1", "expires_dt": expires},
        )

    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="k",
        app_secret="s",
        transport=httpx.MockTransport(handler),
    )

    import asyncio

    tokens = await asyncio.gather(auth.get_token(), auth.get_token(), auth.get_token())

    assert mint_count == 1, "concurrent refresh must dedupe to a single mint"
    assert all(t == "tok-1" for t in tokens)
