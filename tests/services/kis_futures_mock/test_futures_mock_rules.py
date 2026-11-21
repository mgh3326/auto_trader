"""hk 5946 — 선물 모의 순수 규칙: 닫힌 상품·잠금 상수·세션 달력·가드·일일 손실.

DB·네트워크 없음. 모든 거부 경로가 거부 코드를 낸다(전송 0건은 test_futures_mock_place).
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from app.core.symbol import (
    parse_futures_contract_symbol,
    to_futures_contract_symbol,
)
from app.core.timezone import KST
from app.services.kis_futures_mock import constants as C  # noqa: N812
from app.services.kis_futures_mock import session_calendar as cal
from app.services.kis_futures_mock.rules import (
    Contract,
    DailyLossState,
    OrderRequest,
    Position,
    Refusal,
    check_request_shape,
    closing_guard,
    daily_loss_refusal,
    decide,
    parse_positions,
    position_effect,
    precheck_product,
    resolve_contract,
    session_refusal,
)

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
    },
    {
        "product_type": "1",
        "short_code": "A01612",
        "standard_code": "KR4A016C0004",
        "name": "F 202612",
        "atm": "",
        "strike": "00000.00",
        "month_type": "1",
        "underlying_code": "2001",
        "underlying_name": "KOSPI200",
    },
]
PRODUCT = C.MINI_KOSPI200
CONTRACT = Contract("A05612", "MF200.2612", PRODUCT, "2026-12")
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=KST)  # 화요일, 시드 범위 안


def req(**kw):
    base = {
        "broker_symbol": "A05612",
        "side": "buy",
        "price": Decimal("400.00"),
        "quantity": 1,
    }
    base.update(kw)
    return OrderRequest(**base)


def code(result):
    assert isinstance(result, Refusal), result
    return result.code


# --- 심볼 헬퍼 -----------------------------------------------------------------
def test_contract_symbol_round_trip_and_rejects_bad_forms():
    assert to_futures_contract_symbol("MF200", "2026-12") == "MF200.2612"
    assert parse_futures_contract_symbol("MF200.2612") == ("MF200", "2026-12")
    for bad in ("MF200.2613", "MF200.26", "mf200.2612", "MF200-2612", "MF200.26a2"):
        with pytest.raises(ValueError):
            parse_futures_contract_symbol(bad)
    with pytest.raises(ValueError):
        to_futures_contract_symbol("MF200", "2026-13")


# --- 잠금 상수 ----------------------------------------------------------------
def test_locked_constants_match_operator_decision():
    limits = C.assert_limits_locked()
    assert limits.max_order_qty == 1
    assert limits.max_open_positions == 1
    assert limits.max_contract_notional_krw == Decimal("50000000")
    assert limits.daily_loss_limit_krw == Decimal("1000000")
    assert limits.guard_fraction == Decimal("0.02")
    assert limits.limit_only and not limits.night_session
    assert not limits.options and not limits.auto_rollover
    assert C.CLOSED_PRODUCTS == (C.MINI_KOSPI200,)
    assert C.MINI_KOSPI200.multiplier == Decimal("50000")
    assert C.MINI_KOSPI200.tick_size == Decimal("0.02")


@pytest.mark.parametrize(
    "override",
    [
        {"max_order_qty": 2},
        {"max_open_positions": 2},
        {"max_contract_notional_krw": Decimal("50000001")},
        {"daily_loss_limit_krw": Decimal("1000001")},
        {"guard_fraction": Decimal("0.03")},
        {"limit_only": False},
        {"night_session": True},
        {"options": True},
        {"auto_rollover": True},
        {"max_order_qty": True},  # bool 은 int 가 아니다
    ],
)
def test_raised_limits_are_refused_before_any_io(override):
    with pytest.raises(C.LimitsNotLocked):
        C.assert_limits_locked(replace(C.LOCKED_LIMITS, **override))


def test_env_cannot_raise_the_locked_constants(monkeypatch):
    for name in (
        "KIS_FUTURES_MOCK_MAX_ORDER_QTY",
        "KIS_FUTURES_MOCK_MAX_NOTIONAL_KRW",
        "KIS_FUTURES_MOCK_DAILY_LOSS_LIMIT_KRW",
        "KIS_FUTURES_MOCK_GUARD_FRACTION",
        "KIS_FUTURES_MOCK_NIGHT_SESSION",
    ):
        monkeypatch.setenv(name, "999999999")
    assert C.assert_limits_locked() == C.LockedLimits()
    # 1000 pt × 50,000 = 50,000,000 (상한 경계), 1000.02 는 초과.
    assert check_request_shape(req(price=Decimal("1000.00")), PRODUCT) is None
    assert code(check_request_shape(req(price=Decimal("1000.02")), PRODUCT)) == (
        "notional_exceeds_cap"
    )


def test_tampered_module_constants_are_detected(monkeypatch):
    monkeypatch.setattr(C, "LOCKED_LIMITS", replace(C.LOCKED_LIMITS, max_order_qty=5))
    with pytest.raises(C.LimitsNotLocked):
        C.assert_limits_locked()


def test_decide_and_pipeline_have_no_limit_override_parameter():
    import inspect

    from app.services.kis_futures_mock import place

    for func in (place.place_futures_mock_order, place.cancel_futures_mock_order):
        names = set(inspect.signature(func).parameters)
        assert not names & {"limits", "max_notional", "daily_loss_limit", "quantity"}
    assert not {f for f in place.PlaceRequest.__dataclass_fields__ if "limit" in f}


# --- 닫힌 상품 집합 --------------------------------------------------------------
@pytest.mark.parametrize(
    "symbol",
    [
        "A01612",
        "101W09",
        "D0561201",
        "201S03370",
        "A06612",
        "A04612",
        "",
        "A0561",
        "a05612",
    ],
)
def test_product_outside_the_closed_set_is_refused_without_network(symbol):
    assert code(precheck_product(symbol)) == "product_not_allowed"


def test_mini_kospi200_short_code_passes_precheck():
    assert precheck_product("A05612") is PRODUCT
    assert precheck_product("A05701") is PRODUCT


def test_master_row_confirms_product_and_month():
    contract = resolve_contract(MASTER, "A05612", PRODUCT)
    assert contract == CONTRACT


def test_master_refuses_unknown_wrong_product_and_disagreeing_codes():
    assert code(resolve_contract(MASTER, "A05699", PRODUCT)) == "contract_unknown"
    wrong = [{**MASTER[1], "short_code": "A05612"}]  # 일반 선물 행에 미니 코드
    assert code(resolve_contract(wrong, "A05612", PRODUCT)) == "product_not_allowed"
    mismatch = [{**MASTER[0], "name": "미니F 202712"}]  # 코드의 연도 끝자리와 다름
    assert code(resolve_contract(mismatch, "A05612", PRODUCT)) == "contract_unknown"
    dup = [MASTER[0], MASTER[0]]
    assert code(resolve_contract(dup, "A05612", PRODUCT)) == "contract_unknown"


# --- 요청 형태 -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"order_type": "market"}, "order_type_not_limit"),
        ({"side": "hold"}, "invalid_side"),
        ({"quantity": 0}, "invalid_quantity"),
        ({"quantity": 2}, "quantity_exceeds_cap"),
        ({"quantity": True}, "invalid_quantity"),
        ({"price": Decimal("0")}, "invalid_price"),
        ({"price": Decimal("NaN")}, "invalid_price"),
        ({"price": Decimal("400.01")}, "price_not_on_tick"),
        ({"price": Decimal("1001")}, "notional_exceeds_cap"),
    ],
)
def test_request_shape_refusals(kw, expected):
    assert code(check_request_shape(req(**kw), PRODUCT)) == expected


def test_valid_shape_passes():
    assert check_request_shape(req(), PRODUCT) is None
    assert check_request_shape(req(price=Decimal("400.02")), PRODUCT) is None


# --- 세션 달력 -------------------------------------------------------------------
def test_second_thursday_and_last_trade_date_from_krx_spec():
    assert cal.second_thursday(2026, 12) == cal.date(2026, 12, 10)
    assert cal.last_trade_date("2026-12") == cal.date(2026, 12, 10)
    # 둘째 목요일이 달력 범위 밖(2027-01)이면 추측하지 않는다.
    assert cal.last_trade_date("2027-01") is None


def test_holiday_thursday_moves_the_last_trade_date_earlier(monkeypatch):
    monkeypatch.setattr(
        cal,
        "SESSION_DAYS",
        {d: v for d, v in cal.SESSION_DAYS.items() if d != cal.date(2026, 12, 10)},
    )
    assert cal.last_trade_date("2026-12") == cal.date(2026, 12, 9)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 11, 20, 10, 0, tzinfo=KST), "calendar_unknown"),  # 범위 밖
        (datetime(2027, 1, 4, 10, 0, tzinfo=KST), "calendar_unknown"),
        (datetime(2026, 12, 25, 10, 0, tzinfo=KST), "calendar_closed"),  # 휴장
        (datetime(2026, 11, 28, 10, 0, tzinfo=KST), "calendar_closed"),  # 토요일
        (datetime(2026, 11, 24, 8, 44, tzinfo=KST), "session_closed"),
        (datetime(2026, 11, 24, 15, 45, tzinfo=KST), "session_closed"),
        (datetime(2026, 11, 24, 18, 30, tzinfo=KST), "session_closed"),  # 야간
        (datetime(2026, 12, 10, 15, 20, tzinfo=KST), "session_closed"),  # 최종거래일
        (datetime(2026, 12, 11, 10, 0, tzinfo=KST), "contract_expired"),
        (datetime(2026, 11, 24, 10, 0), "calendar_unknown"),  # naive 시각
    ],
)
def test_session_refusals(when, expected):
    assert code(session_refusal(CONTRACT, when)) == expected


def test_in_session_passes_and_boundaries():
    assert session_refusal(CONTRACT, NOW) is None
    assert session_refusal(CONTRACT, datetime(2026, 11, 24, 8, 45, tzinfo=KST)) is None
    assert session_refusal(CONTRACT, datetime(2026, 11, 24, 15, 44, tzinfo=KST)) is None
    assert session_refusal(CONTRACT, datetime(2026, 12, 10, 15, 19, tzinfo=KST)) is None


# --- 포지션 읽기·효과 ------------------------------------------------------------
ROW_LONG = {
    "shtn_pdno": "A05612",
    "sll_buy_dvsn_name": "매수",
    "cblc_qty": "1",
    "ccld_avg_unpr1": "400.00",
}


def test_parse_positions_reads_direction_and_entry_and_skips_flat():
    flat = {"shtn_pdno": "A05701", "cblc_qty": "0"}
    parsed = parse_positions([ROW_LONG, flat])
    assert parsed == (Position("A05612", "long", Decimal("1"), Decimal("400.00")),)
    short = parse_positions([{**ROW_LONG, "sll_buy_dvsn_name": "매도"}])
    assert short[0].direction == "short"
    by_code = parse_positions(
        [{**ROW_LONG, "sll_buy_dvsn_name": "", "sll_buy_dvsn_cd": "01"}]
    )
    assert by_code[0].direction == "short"


@pytest.mark.parametrize(
    "rows",
    [
        None,
        "x",
        [None],
        [{**ROW_LONG, "cblc_qty": "x"}],
        [{**ROW_LONG, "cblc_qty": "-1"}],
        [{**ROW_LONG, "shtn_pdno": None}],
    ],
)
def test_unreadable_positions_are_refused_not_zero(rows):
    assert code(parse_positions(rows)) == "positions_unverified"


def test_effect_open_close_and_caps():
    long_pos = Position("A05612", "long", Decimal(1), Decimal("400"))
    assert position_effect(req(), ()) == ("open", None)
    assert position_effect(req(side="sell"), (long_pos,)) == ("close", long_pos)
    assert code(position_effect(req(), (long_pos,))) == "max_open_positions_exceeded"
    other = Position("A05701", "long", Decimal(1), Decimal("400"))
    assert code(position_effect(req(), (other,))) == "max_open_positions_exceeded"
    assert code(
        position_effect(req(side="sell"), (Position("A05612", None, Decimal(1), None),))
    ) == ("position_ambiguous")
    assert code(position_effect(req(), (long_pos, long_pos))) == "position_ambiguous"


# --- 방향 인식 가드 (설계 §6) ----------------------------------------------------
def test_long_close_guard_threshold():
    pos = Position("A05612", "long", Decimal(1), Decimal("400.00"))
    # entry × (1 − 0.02) = 392.00 — 경계 포함은 통과, 미만은 카드.
    assert closing_guard(req(side="sell", price=Decimal("392.00")), pos) is None
    refusal = closing_guard(req(side="sell", price=Decimal("391.98")), pos)
    assert refusal.code == "requires_human_card" and refusal.requires_human_card
    assert closing_guard(req(side="sell", price=Decimal("410.00")), pos) is None


def test_short_close_guard_threshold():
    pos = Position("A05612", "short", Decimal(1), Decimal("400.00"))
    assert closing_guard(req(side="buy", price=Decimal("408.00")), pos) is None
    refusal = closing_guard(req(side="buy", price=Decimal("408.02")), pos)
    assert refusal.code == "requires_human_card" and refusal.requires_human_card
    assert closing_guard(req(side="buy", price=Decimal("390.00")), pos) is None


def test_unknown_entry_price_is_a_human_card_not_an_exemption():
    pos = Position("A05612", "long", Decimal(1), None)
    refusal = closing_guard(req(side="sell"), pos)
    assert refusal.code == "entry_price_unverified" and refusal.requires_human_card


def test_new_entries_are_not_guarded():
    # 숏 진입(매도)은 낮은 가격이어도 가드 대상이 아니다 (envelope 만 적용).
    result = decide(
        req(side="sell", price=Decimal("300.00")),
        contract=CONTRACT,
        now=NOW,
        positions=(),
        daily_loss=DailyLossState(Decimal(0), False),
    )
    assert result.effect == "open"


# --- 일일 손실 -------------------------------------------------------------------
def test_daily_loss_blocks_entries_at_the_limit_and_when_unverified():
    assert daily_loss_refusal(DailyLossState(Decimal("999999.99"), False)) is None
    assert (
        code(daily_loss_refusal(DailyLossState(Decimal("1000000"), False)))
        == "daily_loss_halt"
    )
    assert (
        code(daily_loss_refusal(DailyLossState(Decimal(0), True)))
        == "daily_loss_unverified"
    )
    assert (
        code(daily_loss_refusal(DailyLossState(None, False))) == "daily_loss_unverified"
    )


def test_daily_loss_does_not_block_a_closing_order():
    pos = Position("A05612", "long", Decimal(1), Decimal("400.00"))
    result = decide(
        req(side="sell", price=Decimal("400.00")),
        contract=CONTRACT,
        now=NOW,
        positions=(pos,),
        daily_loss=DailyLossState(Decimal("5000000"), True),
    )
    assert result.effect == "close"


# --- decide 순서·마지막 거래일 ----------------------------------------------------
def test_decide_refuses_entry_on_the_last_trade_day_but_allows_close():
    last_day = datetime(2026, 12, 10, 10, 0, tzinfo=KST)
    loss = DailyLossState(Decimal(0), False)
    assert code(
        decide(req(), contract=CONTRACT, now=last_day, positions=(), daily_loss=loss)
    ) == ("last_trade_day_entry_refused")
    pos = Position("A05612", "long", Decimal(1), Decimal("400.00"))
    closing = decide(
        req(side="sell"),
        contract=CONTRACT,
        now=last_day,
        positions=(pos,),
        daily_loss=loss,
    )
    assert closing.effect == "close"


def test_decide_approves_a_plain_entry_with_notional():
    result = decide(
        req(),
        contract=CONTRACT,
        now=NOW,
        positions=(),
        daily_loss=DailyLossState(Decimal(0), False),
    )
    assert result.effect == "open"
    assert result.notional_krw == Decimal("20000000.00")
    assert result.contract.contract_symbol == "MF200.2612"


def test_unknown_refusal_code_is_a_programming_error():
    with pytest.raises(ValueError):
        Refusal("made_up")


def test_module_reads_no_environment():
    import inspect

    from app.services.kis_futures_mock import rules

    source = inspect.getsource(rules)
    assert "os.environ" not in source and "getenv" not in source
    assert os.environ is not None  # noqa: S101 - 위 문자열 검사만 의미가 있다
