"""ROB-625 — dry_run 잔액부족 일원화 + 진단 breakdown.

Phase 2: dry_run=True 매수도 잔액부족이면 live와 동일하게 차단(success=False)하되,
프리뷰 본문(estimated_value 등)은 유지하고 구조화된 차단 플래그를 첨부한다.
Phase 3: equity_us 잔액부족 에러에 KIS 필드 breakdown
(frcr_dncl_amt1 / frcr_gnrl_ord_psbl_amt / source)을 노출한다.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import _check_balance_and_warn
from tests._mcp_tooling_support import _patch_runtime_attr, build_tools


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


def _fake_preview(payload: dict):
    async def _preview(*args, **kwargs):
        return dict(payload)

    return _preview


def _fake_overseas_cash(balance: float, orderable: float):
    async def fake_cash(account=None, *, is_mock=False):
        return {
            "accounts": [
                {
                    "account": "kis_overseas",
                    "currency": "USD",
                    "balance": balance,
                    "orderable": orderable,
                }
            ]
        }

    return fake_cash


# ---------------------------------------------------------------------------
# Phase 2 — dry_run blocks insufficient balance (preview retained upstream)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_balance_dry_run_insufficient_returns_error(monkeypatch):
    """dry_run=True equity_us 잔액부족 → (None, error) 반환 (이전엔 (warning, None))."""
    monkeypatch.setattr(
        order_validation, "get_cash_balance_impl", _fake_overseas_cash(84.09, 75.22)
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="ADSK",
        side="buy",
        order_amount=184.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert warning is None
    assert error is not None
    assert error["success"] is False
    assert error["insufficient_balance"] is True
    assert "Insufficient USD balance" in error["error"]


@pytest.mark.asyncio
async def test_check_balance_live_insufficient_unchanged(monkeypatch):
    """live(dry_run=False) 잔액부족은 기존대로 hard error."""
    monkeypatch.setattr(
        order_validation, "get_cash_balance_impl", _fake_overseas_cash(84.09, 75.22)
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="ADSK",
        side="buy",
        order_amount=184.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert warning is None
    assert error is not None
    assert error["success"] is False
    assert error["insufficient_balance"] is True


@pytest.mark.asyncio
async def test_check_balance_sufficient_passes(monkeypatch):
    """충분 잔액은 dry_run/live 무관하게 통과 (None, None)."""
    monkeypatch.setattr(
        order_validation, "get_cash_balance_impl", _fake_overseas_cash(500.0, 500.0)
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="ADSK",
        side="buy",
        order_amount=184.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert warning is None
    assert error is None


# ---------------------------------------------------------------------------
# Phase 3 — KIS field breakdown in the insufficient-balance error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_balance_error_includes_kis_field_breakdown(monkeypatch):
    """equity_us 잔액부족 에러에 cash/orderable KIS 필드명 + 값 노출."""
    monkeypatch.setattr(
        order_validation, "get_cash_balance_impl", _fake_overseas_cash(84.09, 75.22)
    )

    _, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="ADSK",
        side="buy",
        order_amount=184.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert error is not None
    # human-readable message carries the field names for at-a-glance triage.
    assert "frcr_dncl_amt1" in error["error"]
    assert "frcr_gnrl_ord_psbl_amt" in error["error"]

    detail = error["insufficient_balance_detail"]
    assert detail["balance"] == pytest.approx(75.22)
    assert detail["order_amount"] == pytest.approx(184.0)
    assert detail["currency"] == "USD"
    assert detail["shortfall"] == pytest.approx(184.0 - 75.22)

    breakdown = detail["breakdown"]
    assert breakdown["cash_balance"] == pytest.approx(84.09)
    # cash는 frcr_dncl_amt1 우선 / frcr_dncl_amt_2 폴백 — 라벨에 둘 다 명시.
    assert breakdown["cash_field"] == "frcr_dncl_amt1/frcr_dncl_amt_2"
    assert breakdown["orderable"] == pytest.approx(75.22)
    # breakdown의 orderable == 차단 결정에 쓰인 balance (재조회 race 모순 방지).
    assert breakdown["orderable"] == detail["balance"]
    assert breakdown["orderable_field"] == "frcr_gnrl_ord_psbl_amt"
    assert breakdown["source"] == "kis_overseas.inquire_overseas_margin"


@pytest.mark.asyncio
async def test_check_balance_crypto_insufficient_no_breakdown(monkeypatch):
    """crypto는 breakdown 없이도 정상 차단된다 (equity_us-우선 구현)."""

    class FakeUpbit:
        async def fetch_my_coins(self):
            return [{"currency": "KRW", "balance": 1000.0}]

    monkeypatch.setattr(order_validation, "upbit_service", FakeUpbit())

    _, error = await _check_balance_and_warn(
        market_type="crypto",
        normalized_symbol="KRW-BTC",
        side="buy",
        order_amount=50000.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert error is not None
    assert error["insufficient_balance"] is True
    assert "breakdown" not in error["insufficient_balance_detail"]


@pytest.mark.asyncio
async def test_check_balance_breakdown_graceful_when_lookup_fails(monkeypatch):
    """breakdown 조회 실패 시 balance만 사용, 차단은 그대로 동작(graceful degrade)."""

    call = {"n": 0}

    async def flaky_cash(account=None, *, is_mock=False):
        call["n"] += 1
        # 1st call (balance read) succeeds; 2nd call (breakdown re-fetch) fails.
        if call["n"] == 1:
            return {
                "accounts": [
                    {"account": "kis_overseas", "currency": "USD", "orderable": 75.22}
                ]
            }
        raise RuntimeError("breakdown lookup boom")

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", flaky_cash)

    _, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="ADSK",
        side="buy",
        order_amount=184.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert error is not None
    assert error["insufficient_balance"] is True
    # breakdown unavailable → omitted, but the block still carries the scalars.
    assert "breakdown" not in error["insufficient_balance_detail"]
    assert error["insufficient_balance_detail"]["balance"] == pytest.approx(75.22)


@pytest.mark.asyncio
async def test_check_balance_breakdown_none_when_account_missing(monkeypatch):
    """breakdown 재조회 결과에 kis_overseas 계정이 없으면 None (graceful), 차단은 유지."""

    call = {"n": 0}

    async def cash(account=None, *, is_mock=False):
        call["n"] += 1
        if call["n"] == 1:
            # balance read for the block decision.
            return {
                "accounts": [
                    {"account": "kis_overseas", "currency": "USD", "orderable": 75.22}
                ]
            }
        # breakdown re-fetch: matching account absent → helper returns None.
        return {"accounts": []}

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", cash)

    _, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="ADSK",
        side="buy",
        order_amount=184.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert error is not None
    assert error["insufficient_balance"] is True
    assert "breakdown" not in error["insufficient_balance_detail"]


# ---------------------------------------------------------------------------
# Phase 2 — caller keeps the dry_run preview body on the blocked response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_dry_run_insufficient_keeps_preview_body(monkeypatch):
    """dry_run 매수 잔액부족 → success=False지만 프리뷰 본문 + breakdown 유지."""
    tools = build_tools()

    class DummyKISClient:
        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "84.09",
                    "frcr_gnrl_ord_psbl_amt": "75.22",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        # AsyncMock-free: a plain coroutine keeps the marker field deterministic.
        _fake_preview(
            {"estimated_value": 184.0, "fee": 0.09, "preview_marker": "kept"}
        ),
    )

    result = await tools["place_order"](
        symbol="ADSK",
        side="buy",
        order_type="limit",
        quantity=1,
        price=184.0,
        dry_run=True,
    )

    assert result["success"] is False
    assert result["dry_run"] is True
    assert result["insufficient_balance"] is True
    assert "Insufficient USD balance" in result["error"]
    # preview body retained so the operator can size a deposit.
    assert result["estimated_value"] == pytest.approx(184.0)
    assert result["preview_marker"] == "kept"
    # Phase 3 breakdown surfaced in the error message.
    assert "frcr_gnrl_ord_psbl_amt" in result["error"]
