"""Kiwoom Securities REST API constants (URLs, API IDs, headers).

Mock trading is the only supported runtime mode in this package. Live URL is
defined here only so we can defensively reject it; no code path may select it.
"""

from __future__ import annotations

# Base URLs
MOCK_BASE_URL = "https://mockapi.kiwoom.com"
LIVE_BASE_URL = "https://api.kiwoom.com"  # never used; defensive constant only

# OAuth (au10001)
OAUTH_API_ID = "au10001"
OAUTH_PATH = "/oauth2/token"
OAUTH_CONTENT_TYPE = "application/json;charset=UTF-8"
OAUTH_GRANT_TYPE = "client_credentials"

# Common REST headers
HEADER_AUTHORIZATION = "authorization"
HEADER_API_ID = "api-id"
HEADER_CONT_YN = "cont-yn"
HEADER_NEXT_KEY = "next-key"

# Order API (/api/dostk/ordr)
ORDER_PATH = "/api/dostk/ordr"
ORDER_BUY_API_ID = "kt10000"
ORDER_SELL_API_ID = "kt10001"
ORDER_MODIFY_API_ID = "kt10002"
ORDER_CANCEL_API_ID = "kt10003"

# Account/order query API IDs (paths centralized in client when implemented)
ACCOUNT_ORDER_DETAIL_API_ID = "kt00007"
ACCOUNT_ORDER_STATUS_API_ID = "kt00009"
ACCOUNT_ORDERABLE_AMOUNT_API_ID = "kt00010"
ACCOUNT_BALANCE_API_ID = "kt00018"

# Chart API IDs (scaffolded, deferred — NOT routed from get_ohlcv)
CHART_MINUTE_API_ID = "ka10080"
CHART_DAILY_API_ID = "ka10081"
CHART_WEEKLY_API_ID = "ka10082"
CHART_MONTHLY_API_ID = "ka10083"

# Exchange (KRX-only for mock)
MOCK_EXCHANGE_KRX = "KRX"
MOCK_REJECTED_EXCHANGES = frozenset({"NXT", "SOR"})

# Response codes (Kiwoom returns return_code / return_msg in body)
SUCCESS_RETURN_CODE = 0

# Defaults
DEFAULT_TIMEOUT = 5  # seconds
TOKEN_REFRESH_LEEWAY_SECONDS = 30  # refresh slightly before expires_dt

# ROB-418 — Kiwoom REST account-read 필수 파라미터 기본값.
# Kiwoom enum 관례 기반 기본값. 정확한 값은 operator live mock smoke로 확정한다
# (이 세션 creds 없음). 전건실패(필수입력 파라미터 누락, return_code 2)를 호출
# 성립으로 회복하는 것이 1차 목표이며, 값의 scope 정확성은 smoke가 검증한다.
ACCOUNT_BALANCE_QRY_TP_DEFAULT = "1"  # kt00018 조회구분
ACCOUNT_ORDER_STK_BOND_TP_DEFAULT = "0"  # kt00009 주식채권구분(전체)

# ROB-460 — Kiwoom REST account-cash reads also require dmst_stex_tp (국내거래소구분).
# 2026-06-09 live: get_positions(kt00018)·get_orderable_cash returned return_code 2
# (필수입력 파라미터=dmst_stex_tp). Unlike the qry_tp/stk_bond_tp convention-defaults,
# this value is PROVEN: every order endpoint (kt10000-kt10003) submits
# dmst_stex_tp=MOCK_EXCHANGE_KRX successfully. Mock is KRX-only (NXT/SOR rejected on
# the order path), so KRX is the only valid selection. Applied to the account-cash
# reads behind the two reported tools (kt00018 잔고, kt00010 주문가능금액); order-history
# reads (kt00009/kt00007) are left untouched — not proven to need it (smoke-validated).
ACCOUNT_DMST_STEX_TP_DEFAULT = MOCK_EXCHANGE_KRX  # "KRX" — 국내거래소구분

# ---------------------------------------------------------------------------
# ROB-867 — US (overseas) equity constants.
# Same mock host (mockapi.kiwoom.com), separate credentials and account number.
# ---------------------------------------------------------------------------

# US Order API (/api/us/ordr)
US_ORDER_PATH = "/api/us/ordr"
US_ORDER_BUY_API_ID = "ust20000"
US_ORDER_SELL_API_ID = "ust20001"
US_ORDER_MODIFY_API_ID = "ust20002"
US_ORDER_CANCEL_API_ID = "ust20003"

# US Account query API IDs
US_ACCOUNT_OPEN_ORDERS_API_ID = "ust21050"
US_ACCOUNT_POSITIONS_API_ID = "ust21070"
US_ACCOUNT_TODAY_ORDERS_API_ID = "ust21510"
US_ACCOUNT_DEPOSIT_DETAIL_API_ID = "ust21160"
US_ACCOUNT_FOREIGN_DEPOSIT_API_ID = "ust21110"  # optional diagnostic, not MCP-exposed

# US Account query path
US_ACCOUNT_PATH = "/api/us/acnt"

# US order type allowlist (trde_tp). Only codes proven necessary for current
# consumers. Expanding requires a separate reviewed change with broker evidence.
US_TRDE_TP_LIMIT = "00"  # limit order — positive price required
US_TRDE_TP_MARKET = "03"  # market order — price omitted / empty string
US_SUPPORTED_TRDE_TP: frozenset[str] = frozenset({US_TRDE_TP_LIMIT, US_TRDE_TP_MARKET})

# US exchange mapping: us_symbol_universe exchange -> Kiwoom stex_tp code.
# Universe stores "NASD" (not "NASDAQ"); both are accepted as input aliases.
US_EXCHANGE_MAP: dict[str, str] = {
    "NASD": "ND",
    "NASDAQ": "ND",
    "NYSE": "NY",
    "AMEX": "NA",
}
US_SUPPORTED_EXCHANGES: frozenset[str] = frozenset(US_EXCHANGE_MAP.keys())
US_KIWOOM_EXCHANGE_CODES: frozenset[str] = frozenset(US_EXCHANGE_MAP.values())
US_STEX_TYPES = US_KIWOOM_EXCHANGE_CODES
US_EXCHANGE_TO_STEX = US_EXCHANGE_MAP
