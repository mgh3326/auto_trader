"""hk 5946 — 선물 모의 place/cancel 파이프라인 (가짜 브로커·원장, DB 없음).

모든 거부 경로는 **전송 0건**을 단언한다. 한 claim 에 전송은 한 번이고 uncertain 은 다시
보내지 않는다. DB 왕복(예약·원장 writer)은 CI 의 test_futures_mock_ledger_db.py 가 본다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.timezone import KST
from app.services.brokers.kis.futures_orders import FuturesPreDispatchError
from app.services.kis_futures_mock import constants as C  # noqa: N812
from app.services.kis_futures_mock.ledger_service import (
    DuplicateOrderIntent,
    LedgerReceipt,
    OriginalOrder,
    SendReservation,
)
from app.services.kis_futures_mock.place import (
    FuturesPlaceDeps,
    PlaceRequest,
    cancel_futures_mock_order,
    classify_response,
    place_futures_mock_order,
)
from app.services.kis_futures_mock.rules import DailyLossState

MASTER = [
    {
        "product_type": "B",
        "short_code": "A05612",
        "standard_code": "KR4A056C0005",
        "name": "미니F 202612",
        "atm": "",
        "strike": "00000.00",
        "month_type": "1",
        "underlying_code": "2001",
        "underlying_name": "KOSPI200",
    }
]
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=KST)  # 화요일, 시드 범위 안 (주간 세션)

OK_ACK = {
    "rt_cd": "0",
    "msg_cd": "APBK0013",
    "msg1": "ok",
    "output": {"ODNO": "0000012345", "ORD_TMD": "100000"},
}


def run(coro):
    return asyncio.run(coro)


@dataclass
class Counters:
    sends: int = 0
    cancels: int = 0
    views_opened: int = 0
    clients_built: int = 0
    reserved: list = field(default_factory=list)
    released: list = field(default_factory=list)
    sent_kwargs: list = field(default_factory=list)


class FakeView:
    def __init__(self, c, master, balance):
        self.c, self.master, self.balance = c, master, balance
        self.closed = False

    async def futures_master(self):
        if isinstance(self.master, Exception):
            raise self.master
        return self.master

    async def futures_balance(self, account):
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance

    async def close(self):
        self.closed = True


class FakeOrderClient:
    _is_mock_client = True

    def __init__(self, c, response=OK_ACK, error=None, base_url=C.MOCK_HOST):
        self.c, self.response, self.error = c, response, error
        self._settings = SimpleNamespace(kis_base_url=base_url)
        self.closed = False

    async def futures_place_order(self, **kwargs):
        self.c.sends += 1
        self.c.sent_kwargs.append(kwargs)
        if self.error:
            raise self.error
        return self.response

    async def futures_cancel_order(self, **kwargs):
        self.c.cancels += 1
        self.c.sent_kwargs.append(kwargs)
        if self.error:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


class FakeRecorder:
    def __init__(
        self,
        c,
        *,
        duplicate=False,
        reserve_error=None,
        ledger_error=None,
        loss=None,
        original=None,
        daily_loss_error=None,
    ):
        self.c, self.duplicate, self.reserve_error = c, duplicate, reserve_error
        self.ledger_error, self.original = ledger_error, original
        self.loss = loss or DailyLossState(Decimal(0), False)
        self.daily_loss_error = daily_loss_error
        self.rows = []

    async def reserve(self, *, idempotency_key, symbol, side):
        if self.reserve_error:
            raise self.reserve_error
        if self.duplicate:
            raise DuplicateOrderIntent("dup")
        self.c.reserved.append(idempotency_key)
        return SendReservation(len(self.c.reserved), idempotency_key, side)

    async def release(self, reservation):
        self.c.released.append(reservation.idempotency_key)
        return 1

    async def existing_status(self, client_order_id):
        return "uncertain" if self.duplicate else None

    async def daily_loss_state(self, *, trading_day):
        if self.daily_loss_error:
            raise self.daily_loss_error
        return self.loss

    async def get_original_order(self, *, trading_day, broker_order_id):
        return self.original

    async def record_send(self, facts, **kw):
        if self.ledger_error:
            raise self.ledger_error
        self.rows.append(("send", facts, kw))
        return LedgerReceipt(len(self.rows), kw["status"], facts.client_order_id)

    async def record_uncertain(self, facts, **kw):
        if self.ledger_error:
            raise self.ledger_error
        self.rows.append(("uncertain", facts, kw))
        return LedgerReceipt(len(self.rows), "uncertain", facts.client_order_id)


class FakeSignals:
    def __init__(self, *, error=None, created=True):
        self.error, self.created = error, created
        self.recorded, self.outcomes, self.not_sent = [], [], []

    async def record(self, **kw):
        if self.error:
            raise self.error
        self.recorded.append(kw)
        return SimpleNamespace(row_id=1, created=self.created)

    async def mark_outcome(self, **kw):
        self.outcomes.append(kw)

    async def mark_not_sent(self, **kw):
        self.not_sent.append(kw)


class Harness:
    def __init__(
        self,
        *,
        now=NOW,
        master=MASTER,
        balance=None,
        response=OK_ACK,
        error=None,
        base_url=C.MOCK_HOST,
        account="12345678",
        signals=None,
        **recorder_kw,
    ):
        self.c = Counters()
        self.now_values = [now]
        self.recorder = FakeRecorder(self.c, **recorder_kw)
        self.signals = signals or FakeSignals()
        self.master = master
        self.balance = {"positions": [], "summary": {}} if balance is None else balance
        self.client = FakeOrderClient(self.c, response, error, base_url)
        self.view = None
        self.account = account

        def open_view():
            self.c.views_opened += 1
            self.view = FakeView(self.c, self.master, self.balance)
            return self.view

        def new_client():
            self.c.clients_built += 1
            return self.client

        self.deps = FuturesPlaceDeps(
            now=lambda: (
                self.now_values[0]
                if len(self.now_values) == 1
                else self.now_values.pop(0)
            ),
            open_read_view=open_view,
            new_order_client=new_client,
            account_no=lambda: self.account,
            recorder=lambda: self.recorder,
            signal_recorder=lambda: self.signals,
        )


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    monkeypatch.setenv(C.ENABLED_ENV, "true")


def place(h, *, confirm=True, **kw):
    fields = {
        "broker_symbol": "A05612",
        "side": "buy",
        "price": Decimal("400.00"),
        "quantity": 1,
        "strategy": "operator-smoke",
    }
    fields.update(kw)
    return run(
        place_futures_mock_order(PlaceRequest(**fields), confirm=confirm, deps=h.deps)
    )


def assert_no_send(h, response, code):
    assert response["status"] == "refused" and response["error_code"] == code, response
    assert response["success"] is False and response["dispatch_started"] is False
    assert h.c.sends == 0 and h.c.cancels == 0


LONG_ROW = {
    "shtn_pdno": "A05612",
    "sll_buy_dvsn_name": "매수",
    "cblc_qty": "1",
    "ccld_avg_unpr1": "400.00",
}


# --- 정상 경로 --------------------------------------------------------------------
def test_confirmed_entry_sends_exactly_once_and_records_accepted_not_filled():
    h = Harness()
    r = place(h)
    assert r["status"] == "accepted" and r["success"] is True and r["filled"] is False
    assert r["broker_order_id"] == "0000012345" and r["position_effect"] == "open"
    assert h.c.sends == 1 and len(h.c.reserved) == 1 and h.c.released == []
    assert h.c.sent_kwargs[0] == {
        "account": "12345678",
        "symbol": "A05612",
        "side": "buy",
        "quantity": 1,
        "price": Decimal("400.00"),
    }
    kind, facts, kw = h.recorder.rows[0]
    assert (
        kind == "send"
        and kw["status"] == "accepted"
        and kw["broker_order_id"] == "0000012345"
    )
    assert facts.contract_symbol == "MF200.2612" and facts.position_effect == "open"
    assert h.signals.recorded[0]["contract_symbol"] == "MF200.2612"
    assert h.signals.outcomes[0]["outcome_state"] == "submitted"
    assert h.client.closed and h.view.closed


def test_attribution_signal_is_committed_before_the_reservation_and_send():
    order = []
    h = Harness()
    orig_record = h.signals.record
    orig_reserve = h.recorder.reserve

    async def rec(**kw):
        order.append("signal")
        return await orig_record(**kw)

    async def res(**kw):
        order.append("reserve")
        return await orig_reserve(**kw)

    h.signals.record, h.recorder.reserve = rec, res
    h.client.futures_place_order = _wrap(h.client.futures_place_order, order, "send")
    place(h)
    assert order == ["signal", "reserve", "send"]


def _wrap(fn, order, name):
    async def inner(**kw):
        order.append(name)
        return await fn(**kw)

    return inner


def test_closing_order_with_long_position_is_effect_close():
    h = Harness(balance={"positions": [LONG_ROW], "summary": {}})
    r = place(h, side="sell", price=Decimal("400.00"))
    assert r["status"] == "accepted" and r["position_effect"] == "close"


# --- 미리보기 ---------------------------------------------------------------------
def test_preview_reads_but_never_writes_reserves_or_sends():
    h = Harness()
    r = place(h, confirm=False, strategy=None)
    assert r["status"] == "preview" and r["dry_run"] is True and r["success"] is True
    assert r["position_effect"] == "open" and r["notional_krw"] == "20000000.00"
    assert h.c.sends == 0 and h.c.reserved == [] and h.c.clients_built == 0
    assert h.signals.recorded == [] and h.recorder.rows == []


def test_preview_still_shows_the_refusal_without_a_send():
    h = Harness(balance={"positions": [LONG_ROW], "summary": {}})
    assert_no_send(h, place(h, confirm=False), "max_open_positions_exceeded")


# --- 거부 경로: 전송 0건 -----------------------------------------------------------
def test_env_gate_off_refuses_before_any_client_or_io(monkeypatch):
    monkeypatch.setenv(C.ENABLED_ENV, "True")  # 정확히 "true" 만 열린다
    h = Harness()
    assert_no_send(h, place(h), "env_gate_disabled")
    assert h.c.clients_built == 0 and h.c.views_opened == 0 and h.signals.recorded == []


@pytest.mark.parametrize("symbol", ["A01612", "101W09", "D0561201", "A06612"])
def test_product_outside_closed_set_is_refused_before_network_and_gate(
    symbol, monkeypatch
):
    monkeypatch.delenv(C.ENABLED_ENV, raising=False)
    h = Harness()
    assert_no_send(h, place(h, broker_symbol=symbol), "product_not_allowed")
    assert h.c.views_opened == 0 and h.c.clients_built == 0 and h.c.reserved == []


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"quantity": 2}, "quantity_exceeds_cap"),
        ({"price": Decimal("400.01")}, "price_not_on_tick"),
        ({"price": Decimal("1001.00")}, "notional_exceeds_cap"),
        ({"price": Decimal("0")}, "invalid_price"),
        ({"side": "short"}, "invalid_side"),
    ],
)
def test_shape_refusals_happen_before_network(kw, code):
    h = Harness()
    assert_no_send(h, place(h, **kw), code)
    assert h.c.views_opened == 0 and h.c.clients_built == 0


def test_there_is_no_market_order_path():
    # 요청에 order_type 이 없고 파이프라인은 항상 지정가로 보낸다.
    assert "order_type" not in PlaceRequest.__dataclass_fields__
    h = Harness()
    place(h)
    assert "order_type" not in h.c.sent_kwargs[0]


@pytest.mark.parametrize("strategy", [None, "", "   ", "unknown", "TBD", "n/a", "-"])
def test_missing_or_placeholder_strategy_never_reaches_the_broker(strategy):
    h = Harness()
    r = place(h, strategy=strategy)
    assert r["status"] == "refused"
    assert r["error_code"] in {"attribution_required", "placeholder_strategy"}
    assert h.c.sends == 0 and h.c.reserved == [] and h.c.views_opened == 0
    assert h.signals.recorded == []


def test_signal_record_failure_means_no_reservation_and_no_send():
    h = Harness(signals=FakeSignals(error=RuntimeError("db down")))
    assert_no_send(h, place(h), "signal_record_unavailable")
    assert h.c.reserved == []


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"now": datetime(2026, 11, 20, 10, 0, tzinfo=KST)}, "calendar_unknown"),
        ({"now": datetime(2026, 12, 25, 10, 0, tzinfo=KST)}, "calendar_closed"),
        ({"now": datetime(2026, 11, 24, 18, 30, tzinfo=KST)}, "session_closed"),
        (
            {"now": datetime(2026, 12, 10, 10, 0, tzinfo=KST)},
            "last_trade_day_entry_refused",
        ),
        (
            {"balance": {"positions": [LONG_ROW], "summary": {}}},
            "max_open_positions_exceeded",
        ),
        ({"balance": {"positions": "x", "summary": {}}}, "positions_unverified"),
        ({"balance": RuntimeError("timeout")}, "positions_unverified"),
        ({"loss": DailyLossState(Decimal("1000000"), False)}, "daily_loss_halt"),
        ({"loss": DailyLossState(Decimal(0), True)}, "daily_loss_unverified"),
        ({"daily_loss_error": RuntimeError("db")}, "daily_loss_unverified"),
        ({"master": []}, "contract_unknown"),
        ({"master": RuntimeError("download")}, "contract_unknown"),
    ],
)
def test_rule_refusals_send_nothing_and_mark_the_signal_not_sent(kw, code):
    h = Harness(**kw)
    r = place(h)
    assert_no_send(h, r, code)
    assert h.c.reserved == []
    if code not in {"contract_unknown"}:
        assert h.signals.not_sent and h.signals.not_sent[0]["reason"] == "rules_refused"


def test_guard_refusal_requires_a_human_card_and_sends_nothing():
    h = Harness(balance={"positions": [LONG_ROW], "summary": {}})
    r = place(h, side="sell", price=Decimal("391.98"))
    assert_no_send(h, r, "requires_human_card")
    assert r["requires_human_card"] is True


def test_unknown_entry_price_refuses_the_close_with_a_human_card():
    row = {**LONG_ROW, "ccld_avg_unpr1": ""}
    h = Harness(balance={"positions": [row], "summary": {}})
    r = place(h, side="sell")
    assert_no_send(h, r, "entry_price_unverified")
    assert r["requires_human_card"] is True


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"base_url": "https://openapi.koreainvestment.com:9443"}, "not_mock_host"),
        ({"account": ""}, "account_identity_unverified"),
        ({"account": "1234567"}, "account_identity_unverified"),
        ({"account": "1234567a"}, "account_identity_unverified"),
    ],
)
def test_host_and_account_identity_are_checked_before_attribution_and_send(kw, code):
    h = Harness(**kw)
    assert_no_send(h, place(h), code)
    assert h.signals.recorded == [] and h.c.reserved == []


def test_non_mock_client_is_refused():
    h = Harness()
    h.client._is_mock_client = False
    assert_no_send(h, place(h), "not_mock_client")


def test_read_gate_off_is_a_refusal_not_a_crash():
    h = Harness()

    def boom():
        raise RuntimeError("KIS_FUTURES_READ_ENABLED이 true가 아닙니다")

    r = run(
        place_futures_mock_order(
            PlaceRequest("A05612", "buy", Decimal("400.00"), 1, strategy="s"),
            confirm=True,
            deps=FuturesPlaceDeps(
                now=h.deps.now,
                open_read_view=boom,
                new_order_client=h.deps.new_order_client,
                account_no=h.deps.account_no,
                recorder=h.deps.recorder,
                signal_recorder=h.deps.signal_recorder,
            ),
        )
    )
    assert_no_send(h, r, "read_gate_disabled")


# --- 한 claim 에 한 번 / uncertain 은 재전송 없음 -----------------------------------
def test_duplicate_reservation_refuses_without_sending_again():
    h = Harness(duplicate=True)
    r = place(h)
    assert_no_send(h, r, "duplicate_intent")
    assert (
        r["existing_ledger_status"] == "uncertain" and r["reconcile_required"] is True
    )
    assert h.signals.not_sent[0]["reason"] == "duplicate_intent"


def test_reservation_failure_means_unsent_not_guessed():
    h = Harness(reserve_error=RuntimeError("db"))
    assert_no_send(h, place(h), "send_intent_unavailable")


def test_session_closing_between_reservation_and_post_releases_and_does_not_send():
    # 첫 now 는 세션 안, 두 번째 now(전송 직전 재확인)는 15:45 이후.
    h = Harness()
    # now() 호출: 귀속 날짜, 판정 시각, 전송 직전 재확인(세 번째).
    h.now_values = [NOW, NOW, datetime(2026, 11, 24, 15, 46, tzinfo=KST)]
    r = place(h)
    assert_no_send(h, r, "session_closed")
    assert r["reservation_released"] is True and h.c.released == h.c.reserved


def test_pre_dispatch_error_releases_the_reservation_so_the_same_key_can_retry():
    h = Harness(error=FuturesPreDispatchError("token"))
    r = place(h)
    assert r["status"] == "refused" and r["error_code"] == "pre_dispatch_error"
    assert r["reservation_released"] is True and h.c.released == h.c.reserved
    assert h.recorder.rows == []
    assert h.signals.not_sent[0]["reason"] == "pre_dispatch_error"


@pytest.mark.parametrize(
    "error", [TimeoutError(), ConnectionError("reset"), RuntimeError("boom")]
)
def test_post_dispatch_exception_is_uncertain_keeps_reservation_and_never_resends(
    error,
):
    h = Harness(error=error)
    r = place(h)
    assert r["status"] == "uncertain" and r["success"] is False
    assert r["reconcile_required"] is True and r["retry_allowed"] is False
    assert h.c.sends == 1 and h.c.released == [] and len(h.c.reserved) == 1
    assert h.recorder.rows[0][0] == "uncertain"
    assert h.signals.outcomes[0]["outcome_state"] == "failed"
    # 같은 키로 다시 부르면 예약이 남아 있어 전송 없이 거부된다.
    h2 = Harness(error=error, duplicate=True)
    assert_no_send(h2, place(h2), "duplicate_intent")


@pytest.mark.parametrize(
    "response",
    [
        {"rt_cd": "0", "output": {}},  # 주문번호 없는 ACK
        {"rt_cd": "0"},
        {"rt_cd": "0", "output": {"ODNO": "../x"}},
        "ok",
        None,
        {"rt_cd": "1"},  # 거절이지만 msg_cd 없음 — 증명 부족
        {"msg1": "?"},
    ],
)
def test_unproven_answers_are_uncertain_not_accepted_or_rejected(response):
    h = Harness(response=response)
    r = place(h)
    assert r["status"] == "uncertain" and h.c.sends == 1
    assert h.c.released == []


def test_explicit_rejection_is_recorded_rejected_and_not_filled():
    h = Harness(response={"rt_cd": "1", "msg_cd": "40240000", "msg1": "주문가격 오류"})
    r = place(h)
    assert (
        r["status"] == "rejected"
        and r["success"] is False
        and r["retry_allowed"] is False
    )
    assert h.recorder.rows[0][2]["status"] == "rejected"
    assert h.c.released == []  # 보수적: 같은 키는 같은 날 다시 쓰지 않는다


def test_ledger_write_failure_after_send_surfaces_without_resend():
    h = Harness(ledger_error=RuntimeError("db"))
    r = place(h)
    assert r["status"] == "accepted" and r["ledger_recorded"] is False
    assert r["reconcile_required"] is True and r["retry_allowed"] is False
    assert h.c.sends == 1


def test_classify_response_matrix():
    assert classify_response(OK_ACK) == ("accepted", "0000012345")
    assert classify_response({"rt_cd": "1", "msg_cd": "X"}) == ("rejected", None)
    assert classify_response({"rt_cd": "0"}) == ("uncertain", None)


def test_raw_response_stored_is_allowlisted():
    h = Harness(
        response={
            **OK_ACK,
            "output": {**OK_ACK["output"], "CANO": "12345678"},
            "secret": "x",
        }
    )
    place(h)
    raw = h.recorder.rows[0][2]["raw_response"]
    assert "secret" not in raw and "CANO" not in raw["output"]


# --- 취소 -------------------------------------------------------------------------
ORIGINAL = OriginalOrder(
    ledger_id=7,
    status="accepted",
    client_order_id="kfm-abc",
    broker_order_id="0000012345",
    contract_symbol="MF200.2612",
    broker_symbol="A05612",
    product_code="MF200",
    contract_month="2026-12",
    side="buy",
    position_effect="open",
    price=Decimal("400.00"),
    amount=Decimal("20000000"),
    trading_day="20261124",
)


def cancel(h, *, confirm=True, order_no="0000012345"):
    return run(
        cancel_futures_mock_order(
            broker_order_id=order_no, confirm=confirm, deps=h.deps
        )
    )


def test_cancel_sends_once_for_our_own_order_and_records_a_cancel_row():
    h = Harness(original=ORIGINAL)
    r = cancel(h)
    assert r["status"] == "accepted" and h.c.cancels == 1
    kind, facts, kw = h.recorder.rows[0]
    assert facts.operation_kind == "cancel" and facts.original_order_id == "0000012345"
    assert h.c.sent_kwargs[0] == {
        "account": "12345678",
        "original_order_no": "0000012345",
    }


def test_cancel_preview_sends_nothing():
    h = Harness(original=ORIGINAL)
    r = cancel(h, confirm=False)
    assert r["status"] == "preview" and h.c.cancels == 0 and h.c.reserved == []


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"original": None}, "original_order_unknown"),
        (
            {
                "original": ORIGINAL.__class__(
                    **{**ORIGINAL.__dict__, "status": "uncertain"}
                )
            },
            "original_not_cancellable",
        ),
        (
            {
                "original": ORIGINAL.__class__(
                    **{**ORIGINAL.__dict__, "status": "filled"}
                )
            },
            "original_not_cancellable",
        ),
        (
            {"original": ORIGINAL, "now": datetime(2026, 11, 20, 10, 0, tzinfo=KST)},
            "calendar_unknown",
        ),
        (
            {"original": ORIGINAL, "now": datetime(2026, 11, 24, 16, 0, tzinfo=KST)},
            "session_closed",
        ),
        ({"original": ORIGINAL, "duplicate": True}, "duplicate_intent"),
        (
            {
                "original": ORIGINAL,
                "base_url": "https://openapi.koreainvestment.com:9443",
            },
            "not_mock_host",
        ),
    ],
)
def test_cancel_refusals_send_nothing(kw, code):
    h = Harness(**kw)
    r = cancel(h)
    assert r["status"] == "refused" and r["error_code"] == code
    assert h.c.cancels == 0


@pytest.mark.parametrize("order_no", ["", "a b", "../x", "x" * 40])
def test_cancel_rejects_bad_order_numbers_before_any_io(order_no):
    h = Harness(original=ORIGINAL)
    r = cancel(h, order_no=order_no)
    assert (
        r["error_code"] == "invalid_order_no"
        and h.c.cancels == 0
        and h.c.clients_built == 0
    )


def test_cancel_env_gate_off_refuses(monkeypatch):
    monkeypatch.delenv(C.ENABLED_ENV, raising=False)
    h = Harness(original=ORIGINAL)
    r = cancel(h)
    assert (
        r["error_code"] == "env_gate_disabled"
        and h.c.cancels == 0
        and h.c.clients_built == 0
    )


def test_cancel_uncertain_is_recorded_and_not_resent():
    h = Harness(original=ORIGINAL, error=TimeoutError())
    r = cancel(h)
    assert r["status"] == "uncertain" and h.c.cancels == 1 and h.c.released == []
    assert h.recorder.rows[0][0] == "uncertain"


def test_cancel_pre_dispatch_error_releases_the_reservation():
    h = Harness(original=ORIGINAL, error=FuturesPreDispatchError("token"))
    r = cancel(h)
    assert r["error_code"] == "pre_dispatch_error" and r["reservation_released"] is True
    assert h.recorder.rows == []
