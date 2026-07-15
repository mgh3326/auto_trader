# tests/test_kiwoom_mock_preflight.py
"""ROB-893 — Kiwoom mock order preflight service tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kiwoom.order_preflight import (
    PREFLIGHT_CASH_INSUFFICIENT,
    PREFLIGHT_CASH_READ_FAILED,
    PREFLIGHT_EVIDENCE_INVALID,
    PREFLIGHT_POSITION_READ_FAILED,
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
        "acnt_evlt_remn_indv_tot": rows
        if rows is not None
        else [],
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
        balance_payload=_positions_payload(
            [_position("005930", 10, 70000)]
        )
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


@pytest.mark.asyncio
async def test_preflight_sellable_exact_match_passes():
    client = _FakeAccountClient(
        balance_payload=_positions_payload([_position("005930", 10, 70000)])
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


@pytest.mark.asyncio
async def test_preflight_cash_sufficient_passes():
    client = _FakeAccountClient(
        cash_payload={
            "return_code": 0,
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
        "name" in c and "ok" in c and "detail" in c
        for c in extras["preflight_checks"]
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
