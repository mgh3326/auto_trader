# app/services/market_events/catalyst/guard.py
"""upcoming-catalyst 트림/매수 가드 (ROB-408 Slice 1, 순수 함수).

trim/sell 전 positive 촉매가 D-N 내면 경고(이벤트 후 재평가);
buy/add 전 negative 촉매가 D-N 내면 경고.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.market_events.catalyst.contract import CatalystEvent, CatalystGuard

_TRIM_SIDES = frozenset({"trim", "sell"})
_BUY_SIDES = frozenset({"buy", "add"})


def evaluate_catalyst_guard(
    events: Sequence[CatalystEvent], *, side: str, within_days: int
) -> CatalystGuard:
    in_window = [e for e in events if 0 <= e.days_until <= within_days]
    positive = tuple(
        sorted(
            (e for e in in_window if e.polarity == "positive"),
            key=lambda e: (e.days_until, e.symbol or ""),
        )
    )
    negative = tuple(
        sorted(
            (e for e in in_window if e.polarity == "negative"),
            key=lambda e: (e.days_until, e.symbol or ""),
        )
    )

    flag: str | None = None
    nearest_days: int | None = None
    reason: str | None = None

    if side in _TRIM_SIDES and positive:
        flag = "upcoming_positive_catalyst"
        nearest_days = positive[0].days_until
        reason = "임박 positive 촉매 — 이벤트 후 재평가 권고"
    elif side in _BUY_SIDES and negative:
        flag = "upcoming_negative_catalyst"
        nearest_days = negative[0].days_until
        reason = "임박 negative 촉매 — 매수 전 재확인 권고"

    return CatalystGuard(
        flag=flag,
        nearest_days=nearest_days,
        positive=positive,
        negative=negative,
        reason=reason,
    )
