"""KIS real-time quote/orderbook TR codes and field-index maps (read-only).

Indices follow KIS real-time TR docs (H0STCNT0 주식체결, H0STASP0 주식호가).
Encoded as named maps so a single map can be corrected if the smoke test
(PR2 Task 4) observes a different live layout.
"""

from __future__ import annotations

DOMESTIC_TRADE_TR = "H0STCNT0"  # 실시간 주식 체결가
DOMESTIC_ORDERBOOK_TR = "H0STASP0"  # 실시간 주식 호가

QUOTE_TR_CODES = frozenset({DOMESTIC_TRADE_TR, DOMESTIC_ORDERBOOK_TR})

# H0STCNT0 field indices
TRADE_FIELDS = {
    "symbol": 0,
    "time": 1,  # HHMMSS
    "last_price": 2,
}

# H0STASP0 field indices (best level only)
ORDERBOOK_FIELDS = {
    "symbol": 0,
    "ask": 3,  # ASKP1
    "bid": 13,  # BIDP1
    "ask_qty": 23,  # ASKP_RSQN1
    "bid_qty": 33,  # BIDP_RSQN1
}
