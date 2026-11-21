"""선물 모의 주문의 순수 판정 규칙 (hk 5946, 설계 §1·§5·§6·§8).

🔴 I/O 없음 — DB·네트워크·시계·env 를 읽지 않는다. 입력(요청·종목마스터 행·포지션·
일일 손실 상태·현재 시각)은 전부 호출자가 넘기고, 결과는 ``Approved`` 또는 ``Refusal``
이다. 모르는 값은 0 이 아니라 거부다 ("귀속 불가는 값이 아니라 에러").

판정 순서(네트워크가 싼 것부터): 요청 형태 → 닫힌 상품 집합(단축코드) → [종목마스터
확인] → 세션 → [포지션 읽기] → 포지션 효과 → 방향 인식 가드·일일 손실·명목 상한.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.core.symbol import to_futures_contract_symbol
from app.core.timezone import KST

from . import session_calendar
from .constants import (
    CLOSED_PRODUCTS,
    LOCKED_LIMITS,
    LockedLimits,
    Product,
)

Effect = Literal["open", "close"]
Direction = Literal["long", "short"]

# 닫힌 거부 사유 어휘 (CLI·원장 reason 이 같은 문자열을 쓴다).
REFUSAL_CODES: tuple[str, ...] = (
    "invalid_side",
    "invalid_quantity",
    "quantity_exceeds_cap",
    "invalid_price",
    "price_not_on_tick",
    "order_type_not_limit",
    "notional_exceeds_cap",
    "product_not_allowed",
    "contract_unknown",
    "calendar_unknown",
    "calendar_closed",
    "session_closed",
    "contract_expired",
    "last_trade_day_entry_refused",
    "positions_unverified",
    "position_ambiguous",
    "max_open_positions_exceeded",
    "entry_price_unverified",
    "requires_human_card",
    "daily_loss_unverified",
    "daily_loss_halt",
)


@dataclass(frozen=True, slots=True)
class Refusal:
    code: str
    detail: str | None = None
    #: 사람 카드로만 풀 수 있는 거부 (방향 인식 가드). 면제가 아니라 강등이다.
    requires_human_card: bool = False

    def __post_init__(self) -> None:
        if self.code not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code {self.code!r}")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    broker_symbol: str
    side: str
    price: Decimal
    quantity: int
    order_type: str = "limit"


@dataclass(frozen=True, slots=True)
class Contract:
    broker_symbol: str
    contract_symbol: str
    product: Product
    contract_month: str  # YYYY-MM


@dataclass(frozen=True, slots=True)
class Position:
    broker_symbol: str
    direction: Direction | None  # None = 방향 판독 불가
    quantity: Decimal
    entry_price: Decimal | None  # None = 진입가 판독 불가


@dataclass(frozen=True, slots=True)
class DailyLossState:
    """당일 확정 손실. ``unverified`` 는 정산 증거 없는 당일 주문이 있다는 뜻."""

    realized_loss_krw: Decimal | None
    unverified: bool


@dataclass(frozen=True, slots=True)
class Approved:
    contract: Contract
    effect: Effect
    notional_krw: Decimal
    position: Position | None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def notional_krw(price: Decimal, quantity: int, product: Product) -> Decimal:
    return price * product.multiplier * quantity


def precheck_product(
    broker_symbol: str,
) -> Product | Refusal:
    """네트워크 전에 닫힌 상품 집합을 단축코드 형식으로 거른다."""
    for product in CLOSED_PRODUCTS:
        if product.short_code_pattern.fullmatch(broker_symbol):
            return product
    return Refusal("product_not_allowed", "closed product set")


def check_request_shape(
    request: OrderRequest,
    product: Product,
    limits: LockedLimits = LOCKED_LIMITS,
) -> Refusal | None:
    if request.order_type != "limit" or not limits.limit_only:
        return Refusal("order_type_not_limit")
    if request.side not in ("buy", "sell"):
        return Refusal("invalid_side")
    if type(request.quantity) is not int or request.quantity <= 0:
        return Refusal("invalid_quantity")
    if request.quantity > limits.max_order_qty:
        return Refusal("quantity_exceeds_cap", f"max {limits.max_order_qty}")
    price = request.price
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        return Refusal("invalid_price")
    if price % product.tick_size != 0:
        return Refusal("price_not_on_tick", f"tick {product.tick_size}")
    if (
        notional_krw(price, request.quantity, product)
        > limits.max_contract_notional_krw
    ):
        return Refusal("notional_exceeds_cap")
    return None


def resolve_contract(
    master_rows: list[dict[str, str]], broker_symbol: str, product: Product
) -> Contract | Refusal:
    """종목마스터 행으로 단축코드의 상품·결제월을 확정한다 (연도 문자 규칙 추측 없음)."""
    rows = [row for row in master_rows if row.get("short_code") == broker_symbol]
    if len(rows) != 1:
        return Refusal("contract_unknown", "master row not unique or missing")
    row = rows[0]
    match = product.master_name_pattern.fullmatch(row.get("name", ""))
    if (
        row.get("product_type") != product.master_product_type
        or row.get("underlying_name") != product.master_underlying_name
        or match is None
    ):
        return Refusal("product_not_allowed", "master row is not the closed product")
    year, month = match.group(1), match.group(2)
    # 단축코드의 연도 끝자리·월이 마스터 이름과 일치해야 한다.
    if broker_symbol[3] != year[3] or broker_symbol[4:6] != month:
        return Refusal("contract_unknown", "short code disagrees with master name")
    try:
        contract_symbol = to_futures_contract_symbol(product.code, f"{year}-{month}")
    except ValueError:
        return Refusal("contract_unknown", "contract month unreadable")
    return Contract(broker_symbol, contract_symbol, product, f"{year}-{month}")


def session_refusal(contract: Contract, now: datetime) -> Refusal | None:
    """캘린더·세션·만기 판정. 알 수 없는 날은 거부 (Q12)."""
    local = now.astimezone(KST) if now.tzinfo is not None else None
    if local is None:
        return Refusal("calendar_unknown", "naive time")
    state = session_calendar.day_state(local.date())
    if state == "unknown":
        return Refusal("calendar_unknown")
    if state == "closed":
        return Refusal("calendar_closed")
    last = session_calendar.last_trade_date(contract.contract_month)
    if last is None:
        return Refusal("calendar_unknown", "last trade date undetermined")
    if local.date() > last:
        return Refusal("contract_expired")
    if not session_calendar.is_in_session(local, contract.contract_month):
        return Refusal("session_closed")
    return None


def parse_positions(rows: object) -> tuple[Position, ...] | Refusal:
    """브로커 잔고 ``output1`` 을 엄격히 읽는다. 하나라도 판독 불가면 거부."""
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return Refusal("positions_unverified", "balance rows unreadable")
    positions: list[Position] = []
    for row in rows:
        quantity = _decimal(row.get("cblc_qty"))
        symbol = row.get("shtn_pdno")
        if quantity is None or quantity < 0 or not isinstance(symbol, str):
            return Refusal("positions_unverified", "position row unreadable")
        if quantity == 0:
            continue
        name = str(row.get("sll_buy_dvsn_name") or "")
        code = str(row.get("sll_buy_dvsn_cd") or "")
        direction: Direction | None = None
        if "매수" in name or code == "02":
            direction = "long"
        elif "매도" in name or code == "01":
            direction = "short"
        entry = _decimal(row.get("ccld_avg_unpr1"))
        positions.append(
            Position(
                symbol.strip(),
                direction,
                quantity,
                entry if entry is not None and entry > 0 else None,
            )
        )
    return tuple(positions)


def position_effect(
    request: OrderRequest,
    positions: tuple[Position, ...],
    limits: LockedLimits = LOCKED_LIMITS,
) -> tuple[Effect, Position | None] | Refusal:
    """주문이 진입(open)인지 청산(close)인지. 모호하면 청산 편의를 주지 않는다."""
    same = [p for p in positions if p.broker_symbol == request.broker_symbol]
    if len(same) > 1:
        return Refusal("position_ambiguous", "duplicate position rows")
    if same:
        position = same[0]
        if position.direction is None:
            return Refusal("position_ambiguous", "position direction unreadable")
        closes = (position.direction == "long" and request.side == "sell") or (
            position.direction == "short" and request.side == "buy"
        )
        if closes:
            return "close", position
        return Refusal("max_open_positions_exceeded", "same-direction add")
    if len(positions) >= limits.max_open_positions:
        return Refusal("max_open_positions_exceeded")
    return "open", None


def closing_guard(
    request: OrderRequest,
    position: Position,
    limits: LockedLimits = LOCKED_LIMITS,
) -> Refusal | None:
    """설계 §6 — 방향 인식 손실 가드. 면제가 아니라 사람 카드로 강등한다."""
    entry = position.entry_price
    if entry is None or position.direction is None:
        return Refusal("entry_price_unverified", requires_human_card=True)
    g = limits.guard_fraction
    if position.direction == "long" and request.price < entry * (1 - g):
        return Refusal("requires_human_card", "long close below guard", True)
    if position.direction == "short" and request.price > entry * (1 + g):
        return Refusal("requires_human_card", "short close above guard", True)
    return None


def daily_loss_refusal(
    state: DailyLossState, limits: LockedLimits = LOCKED_LIMITS
) -> Refusal | None:
    """신규 진입만 막는다. 확정 손실 + 미확정은 보수 해석 (설계 §8)."""
    if state.unverified or state.realized_loss_krw is None:
        return Refusal("daily_loss_unverified")
    if state.realized_loss_krw >= limits.daily_loss_limit_krw:
        return Refusal("daily_loss_halt")
    return None


def decide(
    request: OrderRequest,
    *,
    contract: Contract,
    now: datetime,
    positions: tuple[Position, ...],
    daily_loss: DailyLossState,
    limits: LockedLimits = LOCKED_LIMITS,
) -> Approved | Refusal:
    """모든 규칙을 고정 순서로 적용한다. 하나라도 거부하면 전송은 없다."""
    refused = check_request_shape(request, contract.product, limits)
    if refused is not None:
        return refused
    if request.broker_symbol != contract.broker_symbol:
        return Refusal("contract_unknown", "request and contract disagree")
    refused = session_refusal(contract, now)
    if refused is not None:
        return refused
    effect = position_effect(request, positions, limits)
    if isinstance(effect, Refusal):
        return effect
    kind, position = effect
    if kind == "open":
        last = session_calendar.last_trade_date(contract.contract_month)
        if last is not None and now.astimezone(KST).date() == last:
            return Refusal("last_trade_day_entry_refused")
        refused = daily_loss_refusal(daily_loss, limits)
        if refused is not None:
            return refused
    else:
        assert position is not None
        refused = closing_guard(request, position, limits)
        if refused is not None:
            return refused
    return Approved(
        contract=contract,
        effect=kind,
        notional_krw=notional_krw(request.price, request.quantity, contract.product),
        position=position,
    )
