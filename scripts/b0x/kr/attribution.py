"""KR 자기 원장(fill) 귀속 — legacy 보유와 B0-X 보유를 가르는 단 하나의 경계.

왜 이 모듈이 생겼는가 (§36차 2항)
-----------------------------------

``kis_mock`` 계좌에는 B0-X 가 만들지 않은 **legacy 보유 11종목**이 있다
(2026-08-11 실측: ``b0xk-`` 원장 행 total 0 인데 non-dust 보유 11). X-E2 까지의
confirm preflight 는 「보유가 하나라도 있으면 이상」(``unexpected_positions``)
이었으므로 이 계좌에서는 **영구히** 한 건도 제출할 수 없었고, 동시에 계약 v1.5 ③
(「B0-X 물타기/매도 = 자기(mock) 보유에서만 파생」)과도 충돌했다 — 보유가 있어야
매도가 파생되는데 보유가 있으면 preflight 가 막았다.

운영자 결정(§36차 2항)은 flatten **기각**, **귀속 기반 공존**이다:

    B0-X KR 의 매도/물타기 파생 = 자기 원장(fill) 귀속 포지션 한정
    legacy 11종 = 불가침 무시 — 관측 전용 공존 레인 소유
    preflight 의 flat 요구를 귀속 게이트로 교체
    근거 선례 = US lane correlation 분리 귀속 · 계약 v1.5 ③ 정합

선례는 US 랩(``scripts/b0x/us/alpaca.py::_attribute_positions``)이다: 브로커
포지션을 **자기 correlation 이 붙은 실행 증거**와 대조해 own/foreign 으로 가른
뒤, ``LaneAccountState.positions`` 에는 own 만 넣는다. KR 은 브로커가 체결이력을
주지 않으므로(아래) 증거 원천이 원장이라는 점만 다르다.

증거 원천 — 왜 원장이고, 왜 이것이 v1.6 ① 의 확대가 아닌가
--------------------------------------------------------------

``review.kis_mock_order_ledger`` (+ pre-submit ``kis_mock_signal_ledger``)는
v1.6 ① 이 이미 이름을 붙인, **제출 chokepoint 가 매번 강제로 쓰는** 원장이다.
v1.6 ② 의 「포지션 진실은 계속 브로커 조회」는 그대로다: 이 모듈은 **포지션을
만들지 않는다**. 브로커가 보고한 보유 수량이 여전히 유일한 포지션 진실이고,
원장은 그 수량 중 **얼마가 우리 것인가**만 답한다(귀속). 브로커가 보고하지 않은
심볼은 원장에 무슨 행이 있든 포지션이 되지 않는다.

오차 방향 — 비대칭이며, 비대칭이 요점이다
--------------------------------------------

🔴 과소 귀속(우리 것을 남의 것으로 봄) = 매도 못 함 = 안전.
🔴 과대 귀속(남의 것을 우리 것으로 봄) = **legacy 매도** = 남의 포지션 처분.

그래서 매수·매도 쪽 증거 기준을 **일부러 다르게** 둔다:

* **매수(귀속 +)** 는 **체결 증거**가 있는 행만 센다
  (``lifecycle_state ∈ {fill, reconciled}``). ``accepted``/``pending`` 는
  체결 증거가 아니므로 세지 않는다 → 우리 수량을 **과소**하게 만든다.
* **매도(귀속 −)** 는 **자기 correlation 의 모든 sell 행**을 뺀다(상태 무관).
  체결 여부를 따져 빼기를 미루면, 이미 팔아치운 수량이 우리 것으로 남아 있다가
  **legacy 주식을 대상으로 한 매도**로 이어질 수 있다 — 이 모듈이 존재하는
  이유와 정면으로 반대되는 방향이다.

두 규칙 모두 계약 v1.6 ③ 이 명시한 「오차 방향 = 과잉 차단, 관대한 방향(누락)은
위반」과 같은 방향이다. 마지막으로 귀속 수량은 **브로커 보유 수량으로 상한**을
둔다: 원장이 뭐라 하든 계좌에 없는 주식은 우리 것이 아니다.

읽기 실패 = 「귀속 없음」이 아니라 「알 수 없음」
------------------------------------------------

DB 장애·스키마 드리프트 등 어떤 실패든 :class:`AttributionUnreadable` 을
돌려준다. 소비자(``scripts.b0x.kr.cycle``)는 그 상태에서

* 자기 포지션 = 없음 → 매도/물타기 파생 0, 그리고
* §4 동시포지션 상한 입력 = **계좌 전체**(= 보수적, 신규 진입도 막힘), 그리고
* confirm preflight = ``attribution_unreadable`` zero-order

로 **양쪽 다 닫는다**. 「조회 불가」가 「legacy 없음」으로도 「자기 것 없음」으로도
읽히지 않게 하는 것이 tri-state 를 유지하는 이유다(v1.6 ④ 와 같은 자세).

이 모듈은 DB 를 **읽기만** 한다. 쓰기 경로도, 브로커/네트워크 경로도 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.review import KISMockOrderLedger
from scripts.b0x.state import B0XPosition

#: ``account_mode`` both kis_mock ledgers pin with a CHECK constraint.
ACCOUNT_MODE: Final[str] = "kis_mock"

#: 🔴 매수 귀속(+)으로 셀 수 있는 lifecycle 상태 — **체결 증거가 있는 것만**.
#: ``app.services.brokers.kis.mock_scalping_exec.adapters.KisMockLedgerWriter``
#: 가 진입 체결을 ``fill``, 청산 확정을 ``reconciled`` 로 쓴다(인용만; 이 패키지
#: 의 AST 가드 allowlist 를 넓히지 않으려고 import 하지 않는다).
#: ``accepted``/``pending``/``submitted`` 는 「나갔다」일 뿐 「받았다」가 아니다.
OWN_FILL_EVIDENCE_STATES: Final[frozenset[str]] = frozenset({"fill", "reconciled"})

#: ROB-843 P2 control/reservation 행 — 거래가 아니다. ``ledger_state.
#: real_order_filter`` 의 술어를 **인용해 그대로** 적어 둔다(같은 이유로 import
#: 하지 않는다). 이 심볼은 브로커 보유 심볼과 절대 겹치지 않지만, 원장 소비자는
#: 전부 이 행을 걸러야 한다는 규칙을 이 레인에서도 깨지 않는다.
CONTROL_SYMBOL: Final[str] = "__ledger_tracking__"
CONTROL_ROLES: Final[frozenset[str]] = frozenset(
    {"tracking_degraded", "native_fallback"}
)
CONTROL_REASONS: Final[frozenset[str]] = frozenset(
    {"ledger_tracking_degraded", "ledger_tracking_fallback"}
)

#: 관측 기록에 남는 출처 문자열 — 「어디서 귀속을 판정했는가」가 아티팩트에서
#: 보이지 않으면 이 게이트는 검증 불가능한 주장이 된다.
ATTRIBUTION_SOURCE: Final[str] = (
    "review.kis_mock_order_ledger (correlation_id LIKE 'b0xk-%'): "
    "buy=fill|reconciled 체결증거만 가산, sell=상태무관 전량 차감, "
    "브로커 보유수량 상한 (§36차 2항 귀속 게이트)"
)

#: ``AttributionUnreadable.reason`` — v1.6 ④ 의 ``PendingUnreadable`` 과 같은
#: 자세이나 **다른 질문**(미체결이 아니라 귀속)이라 코드가 다르다.
ATTRIBUTION_UNREADABLE_REASON: Final[str] = "kis_mock_own_fill_attribution_unreadable"


class HeldPosition(Protocol):
    """브로커가 보고한 보유 한 줄 — ``scripts.b0x.kr.mock.RawPosition`` 모양.

    구조적 타이핑으로 받는다: 이 모듈이 ``mock`` 을 import 하면 순환이 되고,
    무엇보다 이 계산은 브로커 클라이언트 없이 순수하게 검증돼야 한다.
    """

    @property
    def symbol(self) -> str: ...

    @property
    def quantity(self) -> Decimal: ...

    @property
    def average_price(self) -> Decimal: ...

    @property
    def evaluation_amount(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class AttributionUnreadable:
    """귀속 원장이 답하지 못했다 — 「귀속 없음」이 아니라 「알 수 없음」."""

    reason: str
    detail: str

    def canonical(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class AttributedLot:
    """한 심볼에 대해 자기 원장이 증거하는 순수량과 자기 취득단가."""

    symbol: str
    #: Σ(체결증거 있는 자기 매수) − Σ(자기 매도 전량). 0 미만은 0 으로 바닥.
    quantity: Decimal
    #: 위 매수 행들의 수량가중 평균가. 매도로 수량이 줄어도 내리지 않는다
    #: (원가 개념).
    average_price: Decimal
    buy_fill_rows: int
    sell_rows: int

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": format(self.quantity, "f"),
            "buy_fill_rows": self.buy_fill_rows,
            "sell_rows": self.sell_rows,
        }


@dataclass(frozen=True, slots=True)
class OwnFillAttribution:
    """원장이 답한 자기 귀속 — 심볼별 순수량. 포지션 자체는 아니다."""

    lots: tuple[AttributedLot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "lots", tuple(sorted(self.lots, key=lambda lot: lot.symbol))
        )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(lot.symbol for lot in self.lots if lot.quantity > 0)

    def lot(self, symbol: str) -> AttributedLot | None:
        for lot in self.lots:
            if lot.symbol == symbol:
                return lot
        return None

    def quantity(self, symbol: str) -> Decimal:
        lot = self.lot(symbol)
        return Decimal("0") if lot is None else lot.quantity

    def canonical(self) -> dict[str, Any]:
        return {
            "source": ATTRIBUTION_SOURCE,
            "readable": True,
            "lots": [lot.canonical() for lot in self.lots],
        }


class LegacyPositionSellBlocked(RuntimeError):
    """제출 직전에 잡힌, 귀속 없는 수량에 대한 매도 시도.

    🔴 이 예외가 발생한다는 것은 파생/계획 단계 어딘가가 이미 실패했다는 뜻이다.
    그래도 여기서 다시 본다: legacy 매도는 **남의 포지션 처분**이고, 이 레인에서
    되돌릴 수 없는 유일한 사고다. 크립토 사이드카가 제출 경계에서 자기 게이트를
    재검사하는 것과 같은 자세(계약 v1.5 ①, ``assert_resubmit_allowed``).
    """


@dataclass(frozen=True, slots=True)
class ScopedPositions:
    """브로커 보유를 귀속으로 가른 결과 — 파생이 볼 수 있는 전부."""

    #: 🔴 파생 입력. 자기 귀속 수량만, non-dust 만.
    own_positions: tuple[B0XPosition, ...]
    #: 🔴 관측 전용 공존 레인 소유. 어떤 파생 입력에도 들어가지 않는다.
    legacy_symbols: tuple[str, ...]
    #: §4 동시포지션/일일신규 상한의 입력 심볼 집합.
    cap_position_symbols: tuple[str, ...]
    #: 자기 귀속분 평가금액 합 — NAV 기준(아래 ``nav_basis`` 참고).
    attributed_evaluation: Decimal
    #: 귀속 불가 상태(있으면 위 세 값은 전부 fail-closed 쪽으로 채워진다).
    unreadable: AttributionUnreadable | None

    @property
    def cap_basis(self) -> str:
        return (
            "account_wide_fail_closed"
            if self.unreadable is not None
            else "attributed_own_positions"
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "own_symbols": [pos.symbol for pos in self.own_positions],
            "own_position_count": len(self.own_positions),
            "legacy_symbols": list(self.legacy_symbols),
            "legacy_position_count": len(self.legacy_symbols),
            "cap_position_symbols": list(self.cap_position_symbols),
            "cap_basis": self.cap_basis,
            "unreadable": None
            if self.unreadable is None
            else self.unreadable.canonical(),
        }


def attribution_unreadable(cause: str) -> AttributionUnreadable:
    """읽기 실패 → tri-state. ``cause`` 는 예외 **타입 이름**만 (메시지는 DSN 을
    실어 나를 수 있고 이 값은 durable 기록에 들어간다)."""

    return AttributionUnreadable(
        reason=ATTRIBUTION_UNREADABLE_REASON,
        detail=(
            f"review.kis_mock_order_ledger 귀속 조회 실패({cause}) — 어떤 보유가 "
            "자기 것인지 증명할 수 없으므로 자기 포지션 0(매도/물타기 파생 없음) "
            "+ §4 상한 입력은 계좌 전체(신규 진입도 차단)로 양쪽을 닫는다. "
            "「조회 불가」를 「legacy 없음」으로도 「자기 것 없음」으로도 읽지 "
            "않는다 (§36차 2항 · 계약 v1.6 ③④)"
        ),
    )


def scope_positions(
    *,
    positions: tuple[HeldPosition, ...],
    attribution: OwnFillAttribution | AttributionUnreadable,
    account_wide_non_dust: tuple[str, ...],
    min_trade_unit: Decimal,
) -> ScopedPositions:
    """브로커 보유 × 자기 원장 귀속 → 파생이 볼 포지션. 순수 함수.

    ``account_wide_non_dust`` 는 브로커 진실(``FreshTruth.
    non_dust_position_symbols``)이고, ``min_trade_unit`` 은 KRX 1주 — dust 정의를
    이 모듈이 **다시 만들지 않기 위해** 둘 다 주입받는다(계약 v1.2, dust 재정의
    금지).

    귀속 불가면 자기 포지션은 비고, §4 상한 입력은 계좌 전체가 된다 — 「모르면
    아무것도 못 한다」가 되도록 두 방향을 동시에 닫는다.
    """

    if isinstance(attribution, AttributionUnreadable):
        return ScopedPositions(
            own_positions=(),
            legacy_symbols=tuple(sorted(account_wide_non_dust)),
            cap_position_symbols=tuple(sorted(account_wide_non_dust)),
            # 🔴 자기 평가금액을 모르면 0 으로 둔다 — NAV 를 키우는 방향(= kill
            # 임계를 넓히는 방향)으로 추정하지 않는다.
            attributed_evaluation=Decimal("0"),
            unreadable=attribution,
        )

    own: list[B0XPosition] = []
    legacy: list[str] = []
    attributed_evaluation = Decimal("0")
    non_dust = set(account_wide_non_dust)

    for held in sorted(positions, key=lambda pos: pos.symbol):
        symbol = held.symbol
        if symbol not in non_dust:
            # dust 는 어느 쪽 장부도 아니다 — 매도 불가 잔량.
            continue
        # 🔴 브로커 보유 수량이 상한이다. 원장이 더 많다고 말해도 계좌에 없는
        # 주식은 우리 것이 아니다.
        owned = min(attribution.quantity(symbol), held.quantity)
        if owned <= 0 or (owned // min_trade_unit) < 1:
            legacy.append(symbol)
            continue
        lot = attribution.lot(symbol)
        assert lot is not None  # owned > 0 이면 lot 이 있다
        own.append(
            B0XPosition(
                symbol=symbol,
                quantity=owned,
                average_price=lot.average_price,
                # 🔴 cost basis 이지 누적 투입액이 아니다 — 그래서 이 레인은
                # ``cumulative_deployment_readable=False`` 를 유지하고 파생은
                # 기존 포지션에 대한 **추가(물타기)를 계속 거부**한다.
                invested_notional=owned * lot.average_price,
                entry_count=0,
            )
        )
        if held.quantity > 0:
            # 같은 심볼을 legacy 와 공유할 수 있으므로 평가금액도 지분 비례로만
            # 가져온다. 브로커가 준 평가금액 자체는 손대지 않는다.
            attributed_evaluation += held.evaluation_amount * (owned / held.quantity)

    own_symbols = tuple(pos.symbol for pos in own)
    return ScopedPositions(
        own_positions=tuple(own),
        legacy_symbols=tuple(sorted(legacy)),
        cap_position_symbols=own_symbols,
        attributed_evaluation=attributed_evaluation,
        unreadable=None,
    )


def assert_sell_is_own(
    scoped: ScopedPositions, *, symbol: str, quantity: Decimal, lane: str
) -> None:
    """제출 직전 재검사 — 이 매도가 자기 귀속 수량 안에 있는가.

    🔴 legacy 종목은 여기서 ``LegacyPositionSellBlocked`` 로 죽는다. 파생이
    이미 legacy 를 후보에서 뺐더라도(그것이 1차 방어) 이 두 번째 선은 지운다고
    비용이 들지 않고, 없애면 파생 한 줄의 회귀가 곧바로 남의 주식 매도가 된다.
    """

    for position in scoped.own_positions:
        if position.symbol != symbol:
            continue
        if quantity <= position.quantity:
            return
        raise LegacyPositionSellBlocked(
            f"lane={lane} symbol={symbol}: 매도 수량 {format(quantity, 'f')} 가 "
            f"자기 귀속 수량 {format(position.quantity, 'f')} 를 초과한다 — "
            "초과분은 legacy(공존 레인) 보유다 (§36차 2항)"
        )
    raise LegacyPositionSellBlocked(
        f"lane={lane} symbol={symbol}: 자기 원장 귀속 포지션이 없다 — legacy "
        "보유에 대한 매도는 남의 포지션 처분이므로 제출 경계에서 차단한다 "
        "(§36차 2항)"
    )


def _is_control_row(symbol: str, scalping_role: str | None, reason: str | None) -> bool:
    return (
        symbol == CONTROL_SYMBOL
        or (scalping_role or "") in CONTROL_ROLES
        or (reason or "") in CONTROL_REASONS
    )


async def read_own_attribution(
    *, correlation_prefix: str
) -> OwnFillAttribution | AttributionUnreadable:
    """자기 원장 귀속 수량 — read-only, 계좌 전 기간.

    미체결 리더(``pending_ledger.read_own_pending``)와 달리 **거래일 경계가
    없다**: 포지션은 거래일을 넘어 남고, 어제 산 주식은 오늘도 우리 것이다.
    (미체결 쪽은 KRX 일중주문이라는 구조적 이유로 당일 경계를 갖는다 — 같은
    경계를 여기에 복사하면 어제 산 자기 포지션이 legacy 로 오분류된다.)

    어떤 실패든 :func:`attribution_unreadable` 로 떨어진다. 빈 결과(``lots=()``)
    는 「원장이 답했고 아무 것도 없다」이며, 실패와 구분된다.
    """

    prefix = f"{correlation_prefix}%"
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(
                        KISMockOrderLedger.symbol,
                        KISMockOrderLedger.side,
                        KISMockOrderLedger.quantity,
                        KISMockOrderLedger.price,
                        KISMockOrderLedger.lifecycle_state,
                        KISMockOrderLedger.scalping_role,
                        KISMockOrderLedger.reason,
                    ).where(
                        KISMockOrderLedger.account_mode == ACCOUNT_MODE,
                        KISMockOrderLedger.correlation_id.like(prefix),
                    )
                )
            ).all()
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 「알 수 없음」
        return attribution_unreadable(type(exc).__name__)

    return build_attribution(rows)


def build_attribution(rows: object) -> OwnFillAttribution:
    """원장 행 → 귀속. 순수 함수(쿼리 결과 모양만 알면 되고 DB 는 모른다).

    행은 ``(symbol, side, quantity, price, lifecycle_state, scalping_role,
    reason)`` 순서의 시퀀스면 된다.
    """

    bought: dict[str, Decimal] = {}
    bought_notional: dict[str, Decimal] = {}
    sold: dict[str, Decimal] = {}
    buy_rows: dict[str, int] = {}
    sell_rows: dict[str, int] = {}

    for row in rows:  # type: ignore[union-attr]
        symbol_raw, side, quantity_raw, price_raw, state, role, reason = row
        symbol = str(symbol_raw or "").strip()
        if not symbol or _is_control_row(symbol, role, reason):
            continue
        quantity = _decimal(quantity_raw)
        if quantity <= 0:
            continue
        side_text = str(side or "").strip().lower()
        if side_text == "sell":
            # 🔴 상태를 보지 않는다 — 아직 안 채워진 매도까지 미리 뺀다.
            sold[symbol] = sold.get(symbol, Decimal("0")) + quantity
            sell_rows[symbol] = sell_rows.get(symbol, 0) + 1
            continue
        if side_text != "buy":
            continue
        if str(state or "").strip() not in OWN_FILL_EVIDENCE_STATES:
            # 체결 증거 없는 매수는 아직 우리 수량이 아니다.
            continue
        bought[symbol] = bought.get(symbol, Decimal("0")) + quantity
        bought_notional[symbol] = bought_notional.get(symbol, Decimal("0")) + (
            quantity * _decimal(price_raw)
        )
        buy_rows[symbol] = buy_rows.get(symbol, 0) + 1

    lots: list[AttributedLot] = []
    for symbol in sorted(set(bought) | set(sold)):
        gross_bought = bought.get(symbol, Decimal("0"))
        net = gross_bought - sold.get(symbol, Decimal("0"))
        if net < 0:
            net = Decimal("0")
        average = (
            (bought_notional.get(symbol, Decimal("0")) / gross_bought)
            if gross_bought > 0
            else Decimal("0")
        )
        lots.append(
            AttributedLot(
                symbol=symbol,
                quantity=net,
                average_price=average,
                buy_fill_rows=buy_rows.get(symbol, 0),
                sell_rows=sell_rows.get(symbol, 0),
            )
        )
    return OwnFillAttribution(lots=tuple(lots))


def _decimal(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — 해석 불가한 수치는 0 (과소 귀속 방향)
        return Decimal("0")


class AttributionReader(Protocol):
    """``run_kr_cycle`` 이 귀속을 읽는 seam (테스트 주입점)."""

    async def __call__(
        self, *, correlation_prefix: str
    ) -> OwnFillAttribution | AttributionUnreadable: ...


__all__ = [
    "ACCOUNT_MODE",
    "ATTRIBUTION_SOURCE",
    "ATTRIBUTION_UNREADABLE_REASON",
    "OWN_FILL_EVIDENCE_STATES",
    "AttributedLot",
    "AttributionReader",
    "AttributionUnreadable",
    "HeldPosition",
    "LegacyPositionSellBlocked",
    "OwnFillAttribution",
    "ScopedPositions",
    "assert_sell_is_own",
    "attribution_unreadable",
    "build_attribution",
    "read_own_attribution",
    "scope_positions",
]
