"""
KIS (한국투자증권) WebSocket Client for Execution Data

국내/해외 체결 데이터를 실시간으로 수신하여 Redis pub/sub으로 발행합니다.
"""

import asyncio
import base64
import json
import logging
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import redis.asyncio as redis
import websockets
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import settings

logger = logging.getLogger(__name__)

from app.services.kis_websocket_internal.constants import (
    APPROVAL_KEY_CACHE_KEY,
    APPROVAL_KEY_TTL_SECONDS,
    RECOVERABLE_APPROVAL_MSG_CODES,
)
from app.services.kis_websocket_internal.protocol import (
    DOMESTIC_EXECUTION_TR_REAL,
    OVERSEAS_EXECUTION_TR_REAL,
    DOMESTIC_EXECUTION_TR_MOCK,
    OVERSEAS_EXECUTION_TR_MOCK,
    DOMESTIC_EXECUTION_TR,
    OVERSEAS_EXECUTION_TR,
    DOMESTIC_EXECUTION_TR_CODES,
    OVERSEAS_EXECUTION_TR_CODES,
    EXECUTION_TR_CODES,
    _SIDE_MAP,
    _US_SYMBOL_RESERVED_TOKENS,
    OVERSEAS_FILL_FIELDS,
    OVERSEAS_SIDE_MAP,
    DOMESTIC_OFFICIAL_FILL_FIELDS,
    DOMESTIC_COMPACT_FILL_FIELDS,
    KISSubscriptionAckError,
)
from app.services.kis_websocket_internal.approval_keys import (
    _cache_approval_key,
    _get_cached_approval_key,
    _is_valid_approval_key,
    _issue_approval_key,
    close_approval_key_redis,
    get_approval_key,
)


from app.services.kis_websocket_internal.parsers import ExecutionMessageParser
from app.services.kis_websocket_internal.client import KISExecutionWebSocket
