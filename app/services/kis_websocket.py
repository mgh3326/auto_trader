"""
KIS (한국투자증권) WebSocket Client for Execution Data

국내/해외 체결 데이터를 실시간으로 수신하여 Redis pub/sub으로 발행합니다.
"""

from app.services.kis_websocket_internal.approval_keys import (
    ApprovalKeyIssuanceUnavailable,
    _cache_approval_key,
    _get_cached_approval_key,
    _is_valid_approval_key,
    _issue_approval_key,
    close_approval_key_redis,
    get_approval_key,
    invalidate_and_reissue_approval_key,
)
from app.services.kis_websocket_internal.client import (
    KISAppKeyInUseError,
    KISExecutionWebSocket,
)
from app.services.kis_websocket_internal.constants import (
    APPROVAL_ENDPOINT_HOSTS,
    APPROVAL_KEY_CACHE_KEY,
    APPROVAL_KEY_CACHE_KEYS,
    APPROVAL_KEY_TTL_SECONDS,
    RECOVERABLE_APPROVAL_MSG_CODES,
    WEBSOCKET_ENDPOINT_HOSTS,
)
from app.services.kis_websocket_internal.events import build_lifecycle_event
from app.services.kis_websocket_internal.parsers import ExecutionMessageParser
from app.services.kis_websocket_internal.protocol import (
    _SIDE_MAP,
    _US_SYMBOL_RESERVED_TOKENS,
    DOMESTIC_COMPACT_FILL_FIELDS,
    DOMESTIC_EXECUTION_TR,
    DOMESTIC_EXECUTION_TR_CODES,
    DOMESTIC_EXECUTION_TR_MOCK,
    DOMESTIC_EXECUTION_TR_REAL,
    DOMESTIC_OFFICIAL_FILL_FIELDS,
    EXECUTION_TR_CODES,
    OVERSEAS_EXECUTION_TR,
    OVERSEAS_EXECUTION_TR_CODES,
    OVERSEAS_EXECUTION_TR_MOCK,
    OVERSEAS_EXECUTION_TR_REAL,
    OVERSEAS_FILL_FIELDS,
    OVERSEAS_SIDE_MAP,
    KISSubscriptionAckError,
)

__all__ = [
    "APPROVAL_ENDPOINT_HOSTS",
    "APPROVAL_KEY_CACHE_KEY",
    "APPROVAL_KEY_CACHE_KEYS",
    "APPROVAL_KEY_TTL_SECONDS",
    "WEBSOCKET_ENDPOINT_HOSTS",
    "DOMESTIC_COMPACT_FILL_FIELDS",
    "DOMESTIC_EXECUTION_TR",
    "DOMESTIC_EXECUTION_TR_CODES",
    "DOMESTIC_EXECUTION_TR_MOCK",
    "DOMESTIC_EXECUTION_TR_REAL",
    "DOMESTIC_OFFICIAL_FILL_FIELDS",
    "EXECUTION_TR_CODES",
    "ExecutionMessageParser",
    "KISExecutionWebSocket",
    "KISAppKeyInUseError",
    "KISSubscriptionAckError",
    "OVERSEAS_EXECUTION_TR",
    "OVERSEAS_EXECUTION_TR_CODES",
    "OVERSEAS_EXECUTION_TR_MOCK",
    "OVERSEAS_EXECUTION_TR_REAL",
    "OVERSEAS_FILL_FIELDS",
    "OVERSEAS_SIDE_MAP",
    "RECOVERABLE_APPROVAL_MSG_CODES",
    "ApprovalKeyIssuanceUnavailable",
    "_SIDE_MAP",
    "_US_SYMBOL_RESERVED_TOKENS",
    "_cache_approval_key",
    "_get_cached_approval_key",
    "_is_valid_approval_key",
    "_issue_approval_key",
    "build_lifecycle_event",
    "close_approval_key_redis",
    "get_approval_key",
    "invalidate_and_reissue_approval_key",
]
