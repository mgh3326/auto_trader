"""ROB-417 — US kis_mock buy is fail-closed unsupported (OPSQ0002), made explicit."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    _check_balance_and_warn,
    _get_balance_for_order,
    _kis_mock_us_orderable_unsupported,
)
from app.services.brokers.kis.account import AccountClient
from app.services.us_dual_paper.capability_matrix import get_capability_matrix


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


class _FakeKIS:
    """Fake mock transport that exercises the real VTTS3007R account parser."""

    class _Settings:
        kis_account_no = "12345678-01"
        kis_access_token = "test-token"

    _settings = _Settings()
    _hdr_base = {"appkey": "key", "appsecret": "secret", "custtype": "P"}

    def __init__(self, response: dict | Exception) -> None:
        self.response = response
        self.request_calls: list[dict] = []
        self._account = AccountClient(self)

    async def _ensure_token(self) -> None:
        return None

    def _kis_url(self, path: str) -> str:
        return f"https://mock.example{path}"

    async def _request_with_rate_limit(self, *_args, **kwargs):  # noqa: ANN002, ANN003
        self.request_calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def inquire_mock_overseas_buyable_amount(self) -> dict:
        return await self._account.inquire_mock_overseas_buyable_amount()


def test_kis_mock_us_orderable_unsupported_reflects_capability_matrix():
    # ROB-951: VTTS3007R provides verified USD buying power in mock mode.
    assert _kis_mock_us_orderable_unsupported() is False


def test_capability_matrix_changes_only_kis_mock_cash_read():
    matrix = get_capability_matrix()
    assert matrix["kis_mock"]["account_cash_read"] is True
    assert matrix["kis_mock"]["open_orders_read"] is False
    assert matrix["alpaca_paper"]["account_cash_read"] is True
    assert matrix["alpaca_paper"]["open_orders_read"] is True


@pytest.mark.asyncio
async def test_us_mock_buy_parses_vtts3007_output1_before_passing_preflight(
    monkeypatch,
):
    fake_kis = _FakeKIS(
        {
            "rt_cd": "0",
            # Probe-measured VTTS3007R fields: exercise the production parser,
            # not a monkeypatched orderable-cash shortcut.
            "output1": {
                "ord_psbl_frcr_amt": "99996.18",
                "sll_ruse_psbl_amt": "13.95",
                "exrt": "1488.88",
            },
        }
    )

    async def fake_exposure(*_a, **_k):
        return {"confidence": "db_shadow_pending", "buy_reserved_amount": 0.0}

    monkeypatch.setattr(
        order_validation, "_create_kis_client", lambda **_kwargs: fake_kis
    )
    monkeypatch.setattr(
        order_validation, "_get_kis_mock_shadow_exposure", fake_exposure
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert warning is None
    assert error is None
    assert fake_kis.request_calls[0]["tr_id"] == "VTTS3007R"
    assert fake_kis.request_calls[0]["params"]["ITEM_CD"] == "AAPL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_text"),
    [
        ({"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "not supported"}, "OPSQ0002"),
        ({"rt_cd": "0"}, "missing output"),
        (RuntimeError("VTTS3007R timeout"), "VTTS3007R timeout"),
    ],
)
async def test_us_mock_buy_vtts3007_failures_remain_fail_closed(
    monkeypatch, response, error_text
):
    fake_kis = _FakeKIS(response)
    monkeypatch.setattr(
        order_validation, "_create_kis_client", lambda **_kwargs: fake_kis
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert warning is None
    assert error is not None
    assert error_text in error["error"]
    assert "refusing to submit without verified orderable cash" in error["error"]


@pytest.mark.asyncio
async def test_us_mock_buy_blocks_when_vtts3007_orderable_is_insufficient(monkeypatch):
    async def spy_balance(*_a, **_k):
        return 99.99

    async def fake_exposure(*_a, **_k):
        return {"confidence": "db_shadow_pending", "buy_reserved_amount": 0.0}

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)
    monkeypatch.setattr(
        order_validation, "_get_kis_mock_shadow_exposure", fake_exposure
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=100.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )

    assert warning is None
    assert error is not None
    assert "Insufficient USD balance" in error["error"]


@pytest.mark.asyncio
async def test_kr_mock_buy_not_guarded_enters_balance_path(monkeypatch):
    called = {"balance": False}

    async def spy_balance(market_type, is_mock=False):
        called["balance"] = True
        return 10_000_000.0  # ample KRW

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    # KR mock has a DB-shadow-exposure guard; stub it to the pass-through state.
    async def fake_exposure(*_a, **_k):
        return {"confidence": "db_shadow_pending", "buy_reserved_amount": 0.0}

    monkeypatch.setattr(
        order_validation, "_get_kis_mock_shadow_exposure", fake_exposure
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_kr",
        normalized_symbol="005930",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert error is None
    assert called["balance"] is True  # guard did NOT short-circuit KR


@pytest.mark.asyncio
async def test_us_live_buy_not_guarded(monkeypatch):
    called = {"balance": False}

    async def spy_balance(*_a, **_k):
        called["balance"] = True
        return 10_000.0

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,  # live
    )
    assert error is None
    assert called["balance"] is True  # live enters the real precheck


@pytest.mark.asyncio
async def test_us_live_balance_keeps_live_orderable_helper(monkeypatch):
    async def live_orderable(account_token: str) -> float:
        assert account_token == "kis_overseas"
        return 321.0

    monkeypatch.setattr(order_validation, "_live_kis_orderable", live_orderable)
    monkeypatch.setattr(
        order_validation,
        "_create_kis_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("mock client used")),
    )

    assert await _get_balance_for_order("equity_us", is_mock=False) == 321.0
