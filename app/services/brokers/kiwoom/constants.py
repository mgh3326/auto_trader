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
