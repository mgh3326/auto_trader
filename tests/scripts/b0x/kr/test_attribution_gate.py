"""§36차 2항 — 자기 원장(fill) 귀속 게이트. legacy 보유는 불가침이다.

이 파일이 지키는 다섯 가지(브리프의 필수 mutant 5종). 각 테스트 이름 옆의
번호가 그것이다:

    ① legacy 종목이 **매도 후보**로 파생됨              → 격추
    ② legacy 종목이 **물타기 후보**로 파생됨            → 격추
    ③ 귀속 없는 포지션이 자기 것으로 분류됨             → 격추
    ④ preflight 이 flat 요구로 되돌아감(legacy 로 차단)  → 격추
    ⑤ v1.6 원장 dedup 우회                              → 격추

🔴 ①이 최우선이다 — legacy 매도는 **남의 포지션 처분**이며 이 레인에서 되돌릴
수 없는 유일한 사고다. 그래서 ①은 파생 단계(자기 포지션이 아니면 매도 레그가
만들어지지 않는다)와 제출 경계(``assert_sell_is_own``) 양쪽에서 검사한다.
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.b0x.kr import attribution as kr_attribution
from scripts.b0x.kr import cycle as kr_cycle
from scripts.b0x.kr import mock as kr_mock
from scripts.b0x.kr.cycle import broker_state, run_kr_cycle
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table
from tests.scripts.b0x.kr._attribution import (
    exploding_attribution,
    no_attribution,
    unreadable_attribution,
)
from tests.scripts.b0x.kr._pending import foreign_traces, readable_pending

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
KR_PACKAGE = REPO_ROOT / "scripts" / "b0x" / "kr"

#: 2026-08-10 11:00 KST — a Monday inside KRX RTH.
IN_SESSION_NOW = dt.datetime(2026, 8, 10, 2, 0, tzinfo=dt.UTC)

EMPTY_PENDING = readable_pending()

#: 실측 legacy 보유(2026-08-11 preflight record)의 축약판 — B0-X 가 만들지 않은
#: 보유이며 원장에 ``b0xk-`` 행이 하나도 없다.
LEGACY_HOLDINGS: list[dict[str, Any]] = [
    {
        "pdno": "005930",
        "hldg_qty": "100",
        "pchs_avg_pric": "70000",
        "evlu_amt": "9700000",
    },
    {
        "pdno": "000660",
        "hldg_qty": "10",
        "pchs_avg_pric": "180000",
        "evlu_amt": "2000000",
    },
]


@pytest.fixture
def table_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            market="kr",
            rows=[
                # 두 종목 모두 매도 레벨이 있는 행 — 자기 보유였다면 R1/R2 가
                # 파생되었을 입력이다. legacy 라서 파생되지 않아야 한다.
                make_row(
                    symbol="005930",
                    previous_close="97000",
                    buy_l1="94090",
                    buy_l2="92000",
                    sell_r1="102000",
                    sell_r2="105000",
                ),
                make_row(
                    symbol="000660",
                    previous_close="200000",
                    buy_l1="194000",
                    sell_r1="210000",
                    sell_r2="220000",
                ),
            ],
            generated_at=IN_SESSION_NOW - dt.timedelta(hours=1),
        ),
        market="kr",
    )
    return directory


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "observations"


class _FakeKrClient:
    def __init__(self, *, stocks: list[dict[str, Any]] | None = None) -> None:
        self._stocks = stocks or []

    async def inquire_cash_balance(self) -> dict[str, Any]:
        return {
            "dnca_tot_amt": 5_000_000.0,
            "stck_cash_ord_psbl_amt": 5_000_000.0,
            "raw": {
                "dnca_tot_amt": "5000000",
                "stck_cash_ord_psbl_amt": "5000000",
            },
        }

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return self._stocks

    async def close(self) -> None:  # pragma: no cover — caller-owned here
        pass


class _FakeBroker:
    def __init__(self) -> None:
        self.buy_calls: list[dict[str, Any]] = []
        self.sell_calls: list[dict[str, Any]] = []

    async def submit_buy(self, **kwargs: Any) -> dict[str, Any]:
        self.buy_calls.append(kwargs)
        return {"success": True, "odno": "FAKE-BUY", "ledger_id": 1}

    async def submit_exit_sell(self, **kwargs: Any) -> dict[str, Any]:
        self.sell_calls.append(kwargs)
        return {"success": True, "odno": "FAKE-SELL", "ledger_id": 2}


def _fresh(*holdings: dict[str, Any]) -> kr_mock.FreshTruth:
    positions = tuple(
        kr_mock.RawPosition(
            symbol=str(holding["pdno"]),
            quantity=Decimal(str(holding["hldg_qty"])),
            average_price=Decimal(str(holding["pchs_avg_pric"])),
            evaluation_amount=Decimal(str(holding["evlu_amt"])),
        )
        for holding in holdings
    )
    evaluation = sum(
        (position.evaluation_amount for position in positions), Decimal("0")
    )
    return kr_mock.FreshTruth(
        cash=Decimal("5000000"),
        nav=Decimal("5000000") + evaluation,
        positions=positions,
    )


def _own(**quantities: str) -> kr_attribution.OwnFillAttribution:
    return kr_attribution.OwnFillAttribution(
        lots=tuple(
            kr_attribution.AttributedLot(
                symbol=symbol,
                quantity=Decimal(quantity),
                average_price=Decimal("70000"),
                buy_fill_rows=1,
                sell_rows=0,
            )
            for symbol, quantity in quantities.items()
        )
    )


# ---------------------------------------------------------------------------
# MUTANT ① — legacy 는 매도 후보가 되지 않는다 (파생 + 제출 경계)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutant_1_legacy_holdings_never_become_sell_candidates(
    table_dir: Path, out_dir: Path
) -> None:
    """🔴 legacy 매도 = 남의 포지션 처분. 표에 매도 레벨이 있어도 파생 0."""

    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        pending_reader=EMPTY_PENDING,
        attribution_reader=no_attribution(),
    )

    sells = [order for order in outcome.record["orders"] if order["side"] == "sell"]
    assert sells == [], "legacy 보유가 매도 후보로 파생됐다 — §36차 2항 위반 (mutant ①)"
    # 이유가 「매도할 포지션이 없다」로 남아야 한다: 조용히 사라지는 것이 아니라
    # 자기 장부에 없다는 기록이다.
    assert {
        skip["reason"]
        for skip in outcome.record["skipped"]
        if skip["leg"].startswith("sell")
    } <= {"no_position_to_sell"}
    # 그러면서 계좌 진실은 지워지지 않는다.
    assert outcome.record["fresh_truth"]["non_dust_position_symbols"] == [
        "000660",
        "005930",
    ]


@pytest.mark.asyncio
async def test_mutant_1_submission_boundary_refuses_a_sell_of_legacy_shares(
    table_dir: Path, out_dir: Path
) -> None:
    """파생이 뚫려도 제출 경계가 막는다 — 두 번째 선의 존재 증명."""

    scoped = kr_cycle.scoped_positions(
        fresh=_fresh(*LEGACY_HOLDINGS),
        attribution=kr_attribution.OwnFillAttribution(lots=()),
    )
    with pytest.raises(kr_attribution.LegacyPositionSellBlocked):
        kr_attribution.assert_sell_is_own(
            scoped, symbol="005930", quantity=Decimal("1"), lane=kr_mock.LANE
        )


def test_mutant_1_a_sell_beyond_the_attributed_quantity_is_refused() -> None:
    """같은 심볼을 legacy 와 공유해도, 팔 수 있는 것은 자기 수량까지다."""

    scoped = kr_cycle.scoped_positions(
        fresh=_fresh(*LEGACY_HOLDINGS), attribution=_own(**{"005930": "5"})
    )
    kr_attribution.assert_sell_is_own(
        scoped, symbol="005930", quantity=Decimal("5"), lane=kr_mock.LANE
    )
    with pytest.raises(kr_attribution.LegacyPositionSellBlocked):
        kr_attribution.assert_sell_is_own(
            scoped, symbol="005930", quantity=Decimal("6"), lane=kr_mock.LANE
        )


def test_mutant_1_the_submission_loop_still_carries_the_sell_guard() -> None:
    """제출 루프에서 ``assert_sell_is_own`` 호출이 사라지면 실패한다."""

    tree = ast.parse((KR_PACKAGE / "cycle.py").read_text(encoding="utf-8"))
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "assert_sell_is_own"
    ]
    assert guarded, (
        "제출 경계의 legacy 매도 가드가 사라졌다 — 파생 한 줄의 회귀가 곧바로 "
        "남의 주식 매도가 된다 (mutant ①)"
    )


# ---------------------------------------------------------------------------
# MUTANT ② — legacy 는 물타기 후보가 되지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutant_2_legacy_holdings_never_become_averaging_candidates(
    table_dir: Path, out_dir: Path
) -> None:
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        pending_reader=EMPTY_PENDING,
        attribution_reader=no_attribution(),
    )

    assert [
        order for order in outcome.record["orders"] if order["leg"] == "averaging"
    ] == [], "legacy 보유에 물타기가 파생됐다 (mutant ②)"
    # legacy 는 자기 포지션이 아니므로 「신규 진입」 경로로 들어오고, 물타기
    # 경로(기존 포지션에 대한 추가)는 애초에 열리지 않는다.
    assert all(
        order["leg"] in {"buy_l1", "buy_l2"}
        for order in outcome.record["orders"]
        if order["side"] == "buy"
    )


def test_mutant_2_additions_stay_refused_even_for_attributed_positions() -> None:
    """자기 포지션이어도 누적 투입액을 못 읽으면 추가는 거부된다(무변경)."""

    state = broker_state(
        fresh=_fresh(*LEGACY_HOLDINGS), attribution=_own(**{"005930": "10"})
    )
    assert state.cumulative_deployment_readable is False


# ---------------------------------------------------------------------------
# MUTANT ③ — 귀속 없는 포지션은 자기 것이 아니다
# ---------------------------------------------------------------------------


def test_mutant_3_an_unattributed_holding_is_legacy_not_own() -> None:
    scoped = kr_cycle.scoped_positions(
        fresh=_fresh(*LEGACY_HOLDINGS),
        attribution=kr_attribution.OwnFillAttribution(lots=()),
    )
    assert scoped.own_positions == ()
    assert scoped.legacy_symbols == ("000660", "005930")


def test_mutant_3_an_accepted_but_unfilled_buy_is_not_attribution() -> None:
    """🔴 「나갔다」는 「받았다」가 아니다 — 체결 증거만 귀속(+)이 된다."""

    attribution = kr_attribution.build_attribution(
        [
            ("005930", "buy", "10", "70000", "accepted", None, None),
            ("005930", "buy", "3", "70000", "pending", None, None),
        ]
    )
    assert attribution.quantity("005930") == Decimal("0")

    filled = kr_attribution.build_attribution(
        [("005930", "buy", "10", "70000", "fill", "entry", "scalp_entry")]
    )
    assert filled.quantity("005930") == Decimal("10")


def test_mutant_3_the_ledger_cannot_claim_more_than_the_broker_holds() -> None:
    """원장이 뭐라 하든 계좌에 없는 주식은 우리 것이 아니다."""

    scoped = kr_cycle.scoped_positions(
        fresh=_fresh(LEGACY_HOLDINGS[1]),  # 000660 10주
        attribution=_own(**{"000660": "999"}),
    )
    assert [position.quantity for position in scoped.own_positions] == [Decimal("10")]


def test_mutant_3_a_pending_sell_is_deducted_before_it_fills() -> None:
    """매도는 상태 무관 전량 차감 — 미체결 매도가 자기 수량으로 남으면 그것이
    바로 legacy 주식을 향한 다음 매도가 된다."""

    attribution = kr_attribution.build_attribution(
        [
            ("005930", "buy", "10", "70000", "fill", "entry", None),
            ("005930", "sell", "10", "72000", "accepted", "exit", None),
        ]
    )
    assert attribution.quantity("005930") == Decimal("0")


def test_mutant_3_control_rows_are_never_attribution() -> None:
    attribution = kr_attribution.build_attribution(
        [
            (
                kr_attribution.CONTROL_SYMBOL,
                "buy",
                "1",
                "1",
                "fill",
                "tracking_degraded",
                "ledger_tracking_degraded",
            ),
            ("005930", "buy", "5", "70000", "fill", "tracking_degraded", None),
        ]
    )
    assert attribution.lots == ()


def test_mutant_3_an_unwired_caller_gets_nothing_attributed() -> None:
    """호출자가 귀속을 배선하지 않으면 「전부 내 것」이 아니라 fail-closed."""

    state = broker_state(fresh=_fresh(*LEGACY_HOLDINGS))
    assert state.positions == ()
    # 그리고 §4 상한 입력은 계좌 전체로 되돌아간다 — 신규 진입도 막힌다.
    assert state.broker_truth.position_symbols == ("000660", "005930")


@pytest.mark.asyncio
async def test_mutant_3_a_raising_reader_becomes_unreadable_not_empty(
    table_dir: Path, out_dir: Path
) -> None:
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        pending_reader=EMPTY_PENDING,
        attribution_reader=exploding_attribution(RuntimeError("db down")),
    )

    attribution = outcome.record["attribution"]
    assert attribution["unreadable"] is not None
    assert attribution["cap_basis"] == "account_wide_fail_closed"
    assert "RuntimeError" in attribution["unreadable"]["detail"]
    # 🔴 읽지 못한 상태에서는 신규 진입도 나오지 않는다(계좌 전체가 상한 입력).
    assert outcome.record["broker_truth"]["position_symbols"] == [
        "000660",
        "005930",
    ]


# ---------------------------------------------------------------------------
# MUTANT ④ — preflight 은 flat 을 요구하지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutant_4_legacy_holdings_do_not_fail_confirm_preflight(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        broker=broker,
        pending_reader=EMPTY_PENDING,
        foreign_trace_reader=foreign_traces(),
        attribution_reader=no_attribution(),
    )

    preflight = outcome.record["preflight"]
    assert preflight["passed"] is True, (
        "legacy 보유가 다시 preflight 를 막았다 — flat 요구로의 회귀 (mutant ④)"
    )
    assert "unexpected_positions" not in preflight["reasons"]
    assert preflight["positions"]["legacy_symbols"] == ["000660", "005930"]
    assert preflight["positions"]["own_attributed_symbols"] == []
    assert broker.sell_calls == []
    assert armed_confirm == ["acquired", "released"]


def test_mutant_4_the_flat_account_requirement_is_gone_from_the_source() -> None:
    source = (KR_PACKAGE / "cycle.py").read_text(encoding="utf-8")
    assert 'reasons.append("unexpected_positions")' not in source
    assert 'reasons.append("attribution_unreadable")' in source


@pytest.mark.asyncio
async def test_mutant_4_unreadable_attribution_still_fails_closed(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """게이트를 「무조건 통과」로 바꾼 것이 아니다 — 실패 조건이 옮겨갔다."""

    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        broker=broker,
        pending_reader=EMPTY_PENDING,
        foreign_trace_reader=foreign_traces(),
        attribution_reader=unreadable_attribution(),
    )

    assert outcome.zero_order_reason == "preflight_not_clean"
    assert outcome.record["preflight"]["reasons"] == ["attribution_unreadable"]
    assert broker.buy_calls == [] and broker.sell_calls == []


@pytest.mark.asyncio
async def test_mutant_4_foreign_correlation_traces_still_contaminate(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """🔴 오염 판정은 그대로다 — legacy 보유(공존)와 다른 **writer**(오염)는
    다른 것이며, 이 job 은 후자를 건드리지 않았다."""

    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        broker=broker,
        pending_reader=EMPTY_PENDING,
        foreign_trace_reader=foreign_traces("005930", order_trace_count=1),
        attribution_reader=no_attribution(),
    )

    assert outcome.zero_order_reason == "preflight_not_clean"
    assert outcome.record["preflight"]["reasons"] == [
        "CONTAMINATED_foreign_correlation_trace"
    ]
    assert broker.buy_calls == []


# ---------------------------------------------------------------------------
# MUTANT ⑤ — v1.6 원장 dedup 은 우회되지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutant_5_own_pending_still_blocks_a_symbol_after_the_gate(
    table_dir: Path, out_dir: Path
) -> None:
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        pending_reader=readable_pending("005930"),
        attribution_reader=_static_attribution(_own(**{"005930": "10"})),
    )

    assert "005930" not in {order["symbol"] for order in outcome.record["orders"]}
    assert "own_pending_order_exists" in {
        skip["reason"] for skip in outcome.record["skipped"]
    }


def test_mutant_5_the_attribution_gate_never_writes_own_pending() -> None:
    """귀속 리더가 미체결 입력을 대신하지 않는다 — 두 질문은 따로다."""

    state = broker_state(
        fresh=_fresh(*LEGACY_HOLDINGS), attribution=_own(**{"005930": "10"})
    )
    assert state.broker_truth.pending_unreadable is not None, (
        "귀속을 읽었다고 미체결이 읽힌 것으로 취급되면 v1.6 dedup 우회다"
    )


def test_mutant_5_the_attribution_module_cannot_reach_the_pending_reader() -> None:
    tree = ast.parse((KR_PACKAGE / "attribution.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "scripts.b0x.kr.pending_ledger" not in imported


# ---------------------------------------------------------------------------
# NAV — 판정된 항목이고, 넓어지지 않는 방향이다
# ---------------------------------------------------------------------------


def test_attributed_nav_excludes_legacy_and_never_widens_the_kill() -> None:
    """§4 kill 은 pct_of_nav 다 — NAV 가 크면 허용 손실 절대액이 커진다."""

    fresh = _fresh(*LEGACY_HOLDINGS)
    account_wide_nav = fresh.nav

    legacy_only = broker_state(
        fresh=fresh, attribution=kr_attribution.OwnFillAttribution(lots=())
    )
    assert legacy_only.nav == fresh.cash
    assert legacy_only.nav < account_wide_nav

    # 자기 지분만큼만 들어온다: 005930 100주 중 5주 = 평가액의 5%.
    partial = broker_state(fresh=fresh, attribution=_own(**{"005930": "5"}))
    assert partial.nav == fresh.cash + (
        Decimal("9700000") * Decimal("5") / Decimal("100")
    )
    assert partial.nav < account_wide_nav

    # 귀속 불가면 가장 좁은(가장 보수적인) 기준으로 떨어진다.
    unreadable = broker_state(
        fresh=fresh, attribution=kr_attribution.attribution_unreadable("RuntimeError")
    )
    assert unreadable.nav == fresh.cash


def test_the_nav_basis_is_recorded_not_implicit() -> None:
    assert "pct_of_nav" in kr_cycle.KR_ATTRIBUTED_NAV_BASIS
    assert "legacy 평가금액은 제외" in kr_cycle.KR_ATTRIBUTED_NAV_BASIS


# ---------------------------------------------------------------------------
# 자기 귀속 포지션은 실제로 매도 후보가 된다 — 게이트가 레인을 죽이지 않았다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_attributed_position_does_derive_its_sell_ladder(
    table_dir: Path, out_dir: Path
) -> None:
    """계약 v1.5 ③ 이 처음으로 도달 가능해진다 — 자기 보유에서 매도가 나온다."""

    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(stocks=LEGACY_HOLDINGS),
        pending_reader=EMPTY_PENDING,
        attribution_reader=_static_attribution(_own(**{"005930": "10"})),
    )

    sells = [order for order in outcome.record["orders"] if order["side"] == "sell"]
    assert {order["symbol"] for order in sells} == {"005930"}
    # 🔴 그리고 매도 수량은 자기 귀속 10주에서만 나온다 — 브로커 보유 100주가
    # 아니다 (R1/R2 = 50/50 → 5주씩).
    assert [
        order["quantity"]
        for order in outcome.record["planned"]
        if order["side"] == "sell"
    ] == [5, 5]
    # 000660(legacy) 은 여전히 매도 후보가 아니다.
    assert "000660" not in {order["symbol"] for order in sells}


def _static_attribution(attribution: kr_attribution.OwnFillAttribution):
    async def _read(*, correlation_prefix: str) -> kr_attribution.OwnFillAttribution:
        assert correlation_prefix == "b0xk-"
        return attribution

    return _read


# ---------------------------------------------------------------------------
# 불변식 — 이 job 이 건드리지 않았어야 하는 것
# ---------------------------------------------------------------------------


def test_envelope_numbers_are_untouched() -> None:
    from scripts.b0x.envelope import load_envelope

    envelope = load_envelope("kr")
    assert envelope.per_order_notional == Decimal("300000")
    assert envelope.per_symbol_total_notional == Decimal("1500000")
    assert envelope.max_concurrent_positions == 10
    assert envelope.max_new_entries_per_utc_day == 3
    assert envelope.daily_loss_kill == Decimal("0.025")
    assert envelope.daily_loss_kill_basis == "pct_of_nav"


def test_the_status_label_is_still_observation_only() -> None:
    assert kr_cycle.KR_STATUS_LABEL.startswith("OBSERVATION_DERIVATION_ONLY")


def test_the_attribution_module_has_no_write_or_broker_path() -> None:
    """읽기 전용임을 소스로 증명 — 원장에 쓰는 경로가 있으면 순환 논증이 된다."""

    source = (KR_PACKAGE / "attribution.py").read_text(encoding="utf-8")
    for forbidden in ("db.add", "db.commit", "insert(", "update(", "delete("):
        assert forbidden not in source, f"귀속 모듈에 쓰기 경로가 생겼다: {forbidden}"
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "execute" in called and "commit" not in called
