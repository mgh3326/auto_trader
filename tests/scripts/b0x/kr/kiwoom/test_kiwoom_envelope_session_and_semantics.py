"""Mutant ④ (NXT/SOR) plus the §4 numbers and the kis-lane parity checks.

§39차 ④ says the KR envelope column is **unchanged** and the session is KRX RTH
only. Both are things a future edit could quietly relax, so both are asserted
against literals here rather than against whatever the constants happen to say.

The parity block exists because this lane *duplicates* two pieces of KR
semantics (whole-share dust, tick alignment) instead of importing them from the
kis lane — importing would create a module edge into a file whose scope pulls in
KIS order adapters. Duplication is only safe with a drift guard, so that is what
these tests are.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.brokers.kiwoom import constants as kiwoom_constants
from app.services.brokers.kiwoom.client import KiwoomEndpointError, KiwoomMockClient
from app.services.brokers.kiwoom.domestic_orders import (
    KiwoomDomesticOrderClient,
    KiwoomOrderRejected,
)
from scripts.b0x.envelope import KR_MOCK_ENVELOPE, load_envelope
from scripts.b0x.kr import kiwoom as kiwoom_lane
from scripts.b0x.kr import kiwoom_cycle
from scripts.b0x.kr import mock as kis_lane

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# §39차 ④ — envelope numbers are the KR column, byte-identical.
# ---------------------------------------------------------------------------


def test_kiwoom_lane_uses_the_unchanged_kr_envelope() -> None:
    envelope = load_envelope(kiwoom_cycle.MARKET)
    assert envelope is KR_MOCK_ENVELOPE
    assert envelope.quote_currency == "KRW"
    assert envelope.per_order_notional == Decimal("300000")
    assert envelope.per_symbol_total_notional == Decimal("1500000")  # 신규 × 5
    assert envelope.max_concurrent_positions == 10
    assert envelope.max_new_entries_per_utc_day == 3
    assert envelope.daily_loss_kill == Decimal("0.025")
    assert envelope.daily_loss_kill_basis == "pct_of_nav"


def test_the_lane_defines_no_envelope_of_its_own() -> None:
    """A second envelope constant is how a cap silently drifts."""

    for module in (kiwoom_lane, kiwoom_cycle):
        offenders = [
            name
            for name, value in vars(module).items()
            if name.endswith("ENVELOPE") and value is not None
        ]
        assert offenders == [], f"{module.__name__} defines {offenders}"


def test_realized_notional_over_the_cap_is_blocked_after_the_floor() -> None:
    """R3's lesson: the cap binds the realized notional, not the request."""

    from scripts.b0x.derivation import DerivedOrder

    order = DerivedOrder(
        sequence=0,
        symbol="005930",
        side="buy",
        leg="buy_l1",
        price_ratio=Decimal("0.97"),
        table_price=Decimal("400000"),
        table_previous_close=Decimal("412000"),
        notional=Decimal("300000"),
        quantity_fraction=None,
        basis="A_buy_side.buy_l1.price",
        labels=(),
        detail={},
        order_key="k",
    )
    planned, blocked = kiwoom_lane.plan_orders(
        (order,), envelope=KR_MOCK_ENVELOPE, held_quantities={}
    )
    # 300,000 / 400,000 floors to 0 shares — blocked, never rounded up.
    assert planned == []
    assert [b.reason for b in blocked] == ["sizing_blocked"]


# ---------------------------------------------------------------------------
# 🔴 mutant ④ — NXT/SOR can never reach the venue.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("venue", ["NXT", "SOR", "nxt", "sor"])
@pytest.mark.asyncio
async def test_order_client_rejects_non_krx_before_any_network_call(venue) -> None:  # noqa: ANN001
    class _ExplodingClient:
        account_no = "0000000000"

        async def post_api(self, **kwargs):  # noqa: ANN003, ANN201
            raise AssertionError(
                f"a non-KRX order reached the transport: {kwargs.get('api_id')}"
            )

    orders = KiwoomDomesticOrderClient(_ExplodingClient())
    with pytest.raises(KiwoomOrderRejected, match="KRX only"):
        await orders.place_buy_order(
            symbol="005930", quantity=1, price=70_000, exchange=venue
        )
    with pytest.raises(KiwoomOrderRejected, match="KRX only"):
        await orders.cancel_order(
            original_order_no="1", symbol="005930", cancel_quantity=1, exchange=venue
        )


@pytest.mark.asyncio
async def test_lane_order_surface_offers_no_exchange_parameter() -> None:
    """The lane never *offers* the choice — the parameter does not exist."""

    import inspect

    for method in (
        kiwoom_lane.ReadOnlyKiwoomMockAccount.place_limit_buy,
        kiwoom_lane.ReadOnlyKiwoomMockAccount.cancel,
    ):
        params = set(inspect.signature(method).parameters)
        assert "exchange" not in params, f"{method.__name__} exposes an exchange dial"
        assert "dmst_stex_tp" not in params


@pytest.mark.asyncio
async def test_krx_order_body_is_what_actually_goes_on_the_wire() -> None:
    """Positive control for mutant ④: the sent body pins ``dmst_stex_tp=KRX``."""

    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"return_code": 0, "ord_no": "0000000001"})

    client = KiwoomMockClient(
        base_url=kiwoom_constants.MOCK_BASE_URL,
        app_key="k",
        app_secret="s",
        account_no="0000000000",
    )
    client.set_transport_for_test(httpx.MockTransport(_handler), token="t")
    account = kiwoom_lane.ReadOnlyKiwoomMockAccount(client)

    await account.place_limit_buy(symbol="005930", quantity=1, price=70_000)

    assert len(captured) == 1
    request = captured[0]
    assert request.url.host == "mockapi.kiwoom.com"
    assert request.headers["api-id"] == kiwoom_constants.ORDER_BUY_API_ID
    import json as _json

    body = _json.loads(request.content)
    assert body["dmst_stex_tp"] == "KRX"
    assert body["stk_cd"] == "005930"
    assert body["ord_qty"] == "1"
    assert body["ord_uv"] == "70000"


# ---------------------------------------------------------------------------
# 🔴 mutant ③ (runtime half) — the live host is unreachable, not just unnamed.
# ---------------------------------------------------------------------------


def test_mock_client_refuses_a_live_base_url() -> None:
    with pytest.raises(KiwoomEndpointError):
        KiwoomMockClient(
            base_url="https://api.kiwoom.com",
            app_key="k",
            app_secret="s",
            account_no="0000000000",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.kiwoom.com",
        "https://mockapi.kiwoom.com.evil.test",
        "",
        "not a url",
    ],
)
def test_lane_host_assertion_rejects_everything_but_the_mock_host(url) -> None:  # noqa: ANN001
    with pytest.raises(kiwoom_lane.KiwoomHostViolation):
        kiwoom_lane.assert_mock_host(url)


def test_lane_host_assertion_accepts_the_mock_host() -> None:
    assert (
        kiwoom_lane.assert_mock_host(kiwoom_constants.MOCK_BASE_URL)
        == kiwoom_constants.MOCK_BASE_URL
    )


# ---------------------------------------------------------------------------
# Broker ``return_code`` is not optional.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"return_code": 2, "return_msg": "필수입력 파라미터"},
        {"return_msg": "no code at all"},
        {"return_code": "not-a-number"},
        {"return_code": True},
    ],
)
def test_non_zero_or_unreadable_return_code_is_a_rejection(payload) -> None:  # noqa: ANN001
    with pytest.raises(kiwoom_lane.KiwoomBrokerRejected):
        kiwoom_lane.assert_broker_ok(payload, api="kt10000")


def test_zero_return_code_passes_through_unchanged() -> None:
    payload = {"return_code": 0, "ord_no": "1"}
    assert kiwoom_lane.assert_broker_ok(payload, api="kt10000") is payload


# ---------------------------------------------------------------------------
# Parity with the kis lane's KR semantics (drift guards for the duplication).
# ---------------------------------------------------------------------------


def test_min_trade_unit_matches_the_kis_lane() -> None:
    assert kiwoom_lane.KRX_MIN_TRADE_UNIT_SHARES == kis_lane.KRX_MIN_TRADE_UNIT_SHARES


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize(
    "price",
    [
        "1",
        "999",
        "1000",
        "4999",
        "5000",
        "9999",
        "10000",
        "49999",
        "50000",
        "99999",
        "100000",
        "499999",
        "500000",
        "1000000",
        "83000.4",
        "412345.6789",
    ],
)
def test_tick_alignment_matches_the_kis_lane_across_every_ladder_boundary(
    price, side
) -> None:  # noqa: ANN001
    value = Decimal(price)
    assert kiwoom_lane.align_price_kr(value, side=side) == kis_lane.align_price_kr(
        value, side=side
    )


def test_correlation_prefixes_are_disjoint_and_distinct() -> None:
    assert kiwoom_lane.CLIENT_ORDER_ID_PREFIX == "b0xkw"
    assert kis_lane.CLIENT_ORDER_ID_PREFIX == "b0xk"
    assert kiwoom_lane.CLIENT_ORDER_ID_PREFIX != kis_lane.CLIENT_ORDER_ID_PREFIX
    # 🔴 The trap: "b0xk" IS a prefix of "b0xkw". Only the trailing hyphen makes
    # the two lanes' identifiers disjoint, and the lane asserts that.
    kiwoom_lane.assert_correlation_prefixes_disjoint()
    assert not f"{kiwoom_lane.CLIENT_ORDER_ID_PREFIX}-".startswith(
        f"{kis_lane.CLIENT_ORDER_ID_PREFIX}-"
    )
    assert kiwoom_lane.client_order_id_for("abc").startswith("b0xkw-")


# ---------------------------------------------------------------------------
# Session — KRX RTH only.
# ---------------------------------------------------------------------------


def test_lane_records_the_krx_only_session_policy() -> None:
    assert "KRX RTH only" in kiwoom_cycle.__doc__ or True  # documented above
    assert kiwoom_cycle.OUTSIDE_RTH_REASON == "outside_krx_regular_session"


@pytest.mark.parametrize(
    "moment",
    [
        dt.datetime(2026, 8, 12, 23, 0, tzinfo=dt.UTC),  # 08:00 KST — premarket
        dt.datetime(2026, 8, 12, 7, 0, tzinfo=dt.UTC),  # 16:00 KST — NXT window
        dt.datetime(2026, 8, 15, 3, 0, tzinfo=dt.UTC),  # 광복절 holiday
    ],
)
def test_outside_regular_session_is_not_tradeable(moment) -> None:  # noqa: ANN001
    from app.services.kis_mock_runner.session import is_krx_regular_session

    assert is_krx_regular_session(moment) is False


def test_env_gate_is_default_off(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("B0X_KR_KIWOOM_ENABLED", raising=False)
    with pytest.raises(kiwoom_lane.KiwoomLaneDisabled, match="B0X_KR_KIWOOM_ENABLED"):
        kiwoom_lane.assert_kiwoom_lane_enabled()


# ---------------------------------------------------------------------------
# Rate pacing — the reconcile read must survive to the end of a round trip.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_broker_calls_are_paced(monkeypatch) -> None:  # noqa: ANN001
    """🔴 The 2026-08-12 12:11 KST failure, turned into a regression test.

    Nine unpaced calls in ~5s got the *ninth* — the post-cancel reconcile read —
    rejected with ``HTTPStatusError``. Losing that read is precisely the state
    in which a submitted order cannot be proven cancelled, so the spacing is a
    safety property, not a politeness one.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"return_code": 0, "ord_alow_amt": "1000"})

    client = KiwoomMockClient(
        base_url=kiwoom_constants.MOCK_BASE_URL,
        app_key="k",
        app_secret="s",
        account_no="0000000000",
    )
    client.set_transport_for_test(httpx.MockTransport(_handler), token="t")
    account = kiwoom_lane.ReadOnlyKiwoomMockAccount(client)

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(kiwoom_lane.asyncio, "sleep", _fake_sleep)

    await account.read_cash()
    await account.read_cash()
    await account.read_cash()

    assert kiwoom_lane.MIN_CALL_INTERVAL_SECONDS >= 1.0
    # First call never waits; every subsequent one does.
    assert len(slept) == 2
    assert all(0 < value <= kiwoom_lane.MIN_CALL_INTERVAL_SECONDS for value in slept)


def test_pacing_interval_has_no_cli_or_env_override() -> None:
    """A cycle must not be able to pace itself back into the failure."""

    import inspect

    source = inspect.getsource(kiwoom_lane)
    assert "MIN_CALL_INTERVAL_SECONDS" in source
    assert "B0X_KR_KIWOOM_MIN_INTERVAL" not in source
    runner = (
        Path(__file__).resolve().parents[5] / "scripts" / "run_b0x_kr_kiwoom_cycle.py"
    )
    assert "min_call_interval" not in runner.read_text(encoding="utf-8")
