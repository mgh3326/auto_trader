"""Pure read-only crypto detail pre-order checklist helpers.

The checklist is intentionally informational. It must not persist results or
produce executable order payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.invest_crypto import CryptoPendingOrdersSummary
from app.schemas.invest_stock_detail import (
    CryptoPreOrderCheckItem,
    CryptoPreOrderChecklist,
    CryptoRecentTrades,
    StockDetailHolding,
    StockDetailOrderbook,
    StockDetailQuote,
)


def _spread_pct(orderbook: StockDetailOrderbook | None) -> float | None:
    if not orderbook or not orderbook.asks or not orderbook.bids:
        return None
    ask = orderbook.asks[0].price
    bid = orderbook.bids[0].price
    if bid <= 0:
        return None
    return ((ask - bid) / bid) * 100


def _data_freshness_item(
    *,
    quote: StockDetailQuote | None,
    orderbook: StockDetailOrderbook | None,
    recent_trades: CryptoRecentTrades,
    warning_set: set[str],
    computed_at: datetime,
) -> CryptoPreOrderCheckItem:
    data_unavailable = any(
        token in warning_set
        for token in {
            "crypto_ticker_unavailable",
            "crypto_orderbook_unavailable",
            "crypto_recent_trades_unavailable",
        }
    )
    if quote is None or orderbook is None or recent_trades.state == "unavailable":
        data_unavailable = True
    return CryptoPreOrderCheckItem(
        key="data_freshness",
        label="데이터 신선도",
        state="warning" if data_unavailable else "ok",
        detail=(
            "일부 Upbit 공개 데이터가 없어 참고 신뢰도를 낮춥니다."
            if data_unavailable
            else "시세·호가·체결 공개 데이터를 확인했습니다."
        ),
        source="upbit_public_reads",
        computedAt=computed_at,
    )


def _spread_item(
    orderbook: StockDetailOrderbook | None, *, computed_at: datetime
) -> CryptoPreOrderCheckItem:
    spread = _spread_pct(orderbook)
    spread_state = "unavailable"
    spread_detail = "호가 스프레드를 계산할 수 없습니다."
    if spread is not None:
        if spread > 1.0:
            spread_state = "danger"
            spread_detail = f"최우선 호가 스프레드가 {spread:.2f}%로 넓습니다."
        elif spread > 0.5:
            spread_state = "warning"
            spread_detail = f"최우선 호가 스프레드가 {spread:.2f}%입니다."
        else:
            spread_state = "ok"
            spread_detail = f"최우선 호가 스프레드가 {spread:.2f}%입니다."
    return CryptoPreOrderCheckItem(
        key="orderbook_spread",
        label="호가 스프레드",
        state=spread_state,  # type: ignore[arg-type]
        detail=spread_detail,
        source="upbit_orderbook",
        computedAt=computed_at,
    )


def _volatility_item(
    quote: StockDetailQuote | None, *, computed_at: datetime
) -> CryptoPreOrderCheckItem:
    move = quote.changeRate if quote else None
    if move is None:
        state = "unavailable"
        detail = "24시간 변동률을 확인할 수 없습니다."
    elif abs(move) >= 10:
        state = "warning"
        detail = f"24시간 변동률이 {move:.2f}%로 큽니다."
    else:
        state = "ok"
        detail = f"24시간 변동률 {move:.2f}%입니다."
    return CryptoPreOrderCheckItem(
        key="volatility_24h",
        label="24시간 변동성",
        state=state,  # type: ignore[arg-type]
        detail=detail,
        source="upbit_ticker",
        computedAt=computed_at,
    )


def build_crypto_preorder_checklist(
    *,
    quote: StockDetailQuote | None,
    orderbook: StockDetailOrderbook | None,
    recent_trades: CryptoRecentTrades,
    holding: StockDetailHolding | None,
    pending_orders: CryptoPendingOrdersSummary,
    warnings: list[str] | None = None,
    computed_at: datetime | None = None,
) -> CryptoPreOrderChecklist:
    """Build a non-executable risk summary from already-read data."""

    now = computed_at or datetime.now(UTC)
    warning_set = set(warnings or [])
    items: list[CryptoPreOrderCheckItem] = [
        _data_freshness_item(
            quote=quote,
            orderbook=orderbook,
            recent_trades=recent_trades,
            warning_set=warning_set,
            computed_at=now,
        ),
        _spread_item(orderbook, computed_at=now),
        _volatility_item(quote, computed_at=now),
    ]

    has_pending = bool(pending_orders.items)
    items.append(
        CryptoPreOrderCheckItem(
            key="pending_duplicate",
            label="미체결 중복 확인",
            state="warning" if has_pending else "ok",
            detail=(
                f"같은 코인에 미체결 주문 {len(pending_orders.items)}건이 있습니다."
                if has_pending
                else "같은 코인의 미체결 주문이 없습니다."
            ),
            source="pending_orders",
            computedAt=now,
        )
    )

    items.append(
        CryptoPreOrderCheckItem(
            key="position_exposure",
            label="보유 노출",
            state="info" if holding else "ok",
            detail=(
                f"현재 보유 수량 {holding.totalQuantity:g}을(를) 표시합니다."
                if holding
                else "현재 보유 수량이 없습니다."
            ),
            source="invest_home_read_model",
            computedAt=now,
        )
    )

    items.append(
        CryptoPreOrderCheckItem(
            key="read_only_guardrail",
            label="읽기 전용 가드레일",
            state="info",
            detail="이 체크리스트는 참고용이며 주문 생성·수정·취소를 실행하지 않습니다.",
            source="read_only_mvp",
            computedAt=now,
        )
    )

    return CryptoPreOrderChecklist(
        items=items,
        sources=sorted(
            {
                "upbit_ticker",
                "upbit_orderbook",
                "upbit_recent_trades",
                "pending_orders",
                "invest_home_read_model",
                "read_only_mvp",
            }
        ),
    )


__all__ = ["build_crypto_preorder_checklist"]
