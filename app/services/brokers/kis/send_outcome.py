"""Explicit KIS order-send outcome tracked at the real HTTP boundary.

Only KIS mock scalping passes a tracker. Other KIS callers pass ``None`` and
retain their existing behavior and response contracts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# This is intentionally an allowlist, not a redact-after-the-fact filter.  KIS
# order requests carry authentication headers, so no request headers and no
# arbitrary response headers may reach the throttle evidence event.
_RESPONSE_HEADER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "server",
        "via",
        "date",
        "content-type",
        "x-request-id",
        "x-requestid",
        "x-correlation-id",
        "x-correlationid",
        "x-kis-request-id",
        "x-kis-requestid",
        "x-kis-correlation-id",
        "x-kis-correlationid",
        "x-trace-id",
        "x-transaction-id",
        "x-gt-uid",
    }
)
_CORRELATION_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "x-request-id",
        "x-requestid",
        "x-correlation-id",
        "x-correlationid",
        "x-kis-request-id",
        "x-kis-requestid",
        "x-kis-correlation-id",
        "x-kis-correlationid",
        "x-trace-id",
        "x-transaction-id",
        "x-gt-uid",
    }
)
_CORRELATION_BODY_KEYS: frozenset[str] = frozenset(
    {
        "requestid",
        "correlationid",
        "transactionid",
        "traceid",
        "gtuid",
        "kisrequestid",
        "kiscorrelationid",
    }
)


def _safe_endpoint(url: object) -> str | None:
    """Return host plus path, excluding query, userinfo, and fragment."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            return None
        port = parsed.port
        host_with_port = f"{host}:{port}" if port is not None else host
        return f"{host_with_port}{parsed.path or '/'}"
    except (TypeError, ValueError):
        return None


def _safe_status_code(response: object) -> int | None:
    try:
        value = getattr(response, "status_code", None)
    except Exception:  # noqa: BLE001 - observation must never alter order flow
        return None
    return value if type(value) is int else None


def _safe_response_text_attribute(response: object, name: str) -> str | None:
    try:
        value = getattr(response, name, None)
    except Exception:  # noqa: BLE001 - response adapters can be malformed
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_response_header_projection(
    response: object,
) -> tuple[dict[str, str], dict[str, str]]:
    """Copy only explicitly safe response headers and their correlation IDs."""
    try:
        raw_headers = getattr(response, "headers", None)
        items = raw_headers.items()
    except Exception:  # noqa: BLE001 - observability is fail-open
        return {}, {}

    allowed_headers: dict[str, str] = {}
    correlation_ids: dict[str, str] = {}
    try:
        for raw_name, raw_value in items:
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                continue
            name = raw_name.lower()
            if name not in _RESPONSE_HEADER_ALLOWLIST:
                continue
            allowed_headers[name] = raw_value
            if name in _CORRELATION_HEADER_NAMES:
                correlation_ids[f"header:{name}"] = raw_value
    except Exception:  # noqa: BLE001 - a malformed header iterator is non-fatal
        pass
    return allowed_headers, correlation_ids


def _safe_body_correlation_ids(response_body: object) -> dict[str, str]:
    """Extract named correlation IDs without retaining any response body."""
    if type(response_body) is not dict:
        return {}

    correlation_ids: dict[str, str] = {}
    pending: list[tuple[dict[object, object], int]] = [(response_body, 0)]
    while pending and len(correlation_ids) < 16:
        current, depth = pending.pop()
        if depth > 3:
            continue
        for raw_name, value in current.items():
            if not isinstance(raw_name, str):
                continue
            normalized_name = raw_name.lower().replace("_", "").replace("-", "")
            if normalized_name in _CORRELATION_BODY_KEYS:
                if isinstance(value, str) and value:
                    correlation_ids.setdefault(f"body:{raw_name.lower()}", value)
                elif type(value) is int:
                    correlation_ids.setdefault(f"body:{raw_name.lower()}", str(value))
            if type(value) is dict:
                pending.append((value, depth + 1))
    return correlation_ids


class OrderSendDisposition(StrEnum):
    NOT_CREATED = "not_created"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


@dataclass
class OrderSendOutcomeTracker:
    disposition: OrderSendDisposition = OrderSendDisposition.NOT_CREATED
    last_http_status: int | None = None
    protocol_evidence: dict[str, Any] | None = None

    def mark_dispatched(self) -> None:
        """A POST is crossing the HTTP boundary; its outcome is now uncertain."""
        self.disposition = OrderSendDisposition.UNKNOWN
        self.last_http_status = None
        self.protocol_evidence = None

    def mark_http_response(self, status_code: int) -> None:
        self.last_http_status = status_code
        if 400 <= status_code < 500:
            self.disposition = OrderSendDisposition.NOT_CREATED
        else:
            # 2xx still needs the provider contract + order ID to prove accepted;
            # 5xx never proves that the broker did not create an order.
            self.disposition = OrderSendDisposition.UNKNOWN

    def mark_provider_rejected(self) -> None:
        # A business rejection in a normal (<500) response proves no order. A
        # business-looking payload inside a 5xx response remains outcome-unknown.
        if self.last_http_status is None or self.last_http_status < 500:
            self.disposition = OrderSendDisposition.NOT_CREATED

    def mark_accepted(self) -> None:
        # Provider payloads carried by a 5xx response are not trusted evidence
        # of a stable accepted outcome. Once a 5xx was observed, remain UNKNOWN.
        if self.last_http_status is None or self.last_http_status < 500:
            self.disposition = OrderSendDisposition.ACCEPTED

    def mark_unknown(self) -> None:
        self.disposition = OrderSendDisposition.UNKNOWN

    def record_response_protocol(
        self,
        *,
        response: object,
        endpoint_url: object,
        response_body: object,
    ) -> None:
        """Retain the safe protocol projection for a possible throttle event.

        This deliberately records neither request headers nor response body.  It
        is invoked at the HTTP boundary and is best-effort by contract: malformed
        response adapters must not change an order's execution path.
        """
        try:
            status_code = _safe_status_code(response)
            reason_phrase = _safe_response_text_attribute(response, "reason_phrase")
            http_version = _safe_response_text_attribute(response, "http_version")
            status_line_parts = [
                part
                for part in (
                    http_version,
                    str(status_code) if status_code else None,
                    reason_phrase,
                )
                if part
            ]
            response_headers, correlation_ids = _safe_response_header_projection(
                response
            )
            correlation_ids.update(_safe_body_correlation_ids(response_body))
            self.protocol_evidence = {
                "status_code": status_code,
                "reason_phrase": reason_phrase,
                "status_line": " ".join(status_line_parts) or None,
                "endpoint": _safe_endpoint(endpoint_url),
                "response_headers": response_headers,
                "correlation_ids": correlation_ids,
            }
        except Exception:  # noqa: BLE001 - observability is strictly fail-open
            self.protocol_evidence = {
                "status_code": None,
                "reason_phrase": None,
                "status_line": None,
                "endpoint": None,
                "response_headers": {},
                "correlation_ids": {},
            }

    @property
    def has_untrusted_server_error_response(self) -> bool:
        return self.last_http_status is not None and self.last_http_status >= 500


def emit_throttle_protocol_evidence(
    *,
    order_surface: str,
    provider_message_code: object,
    outcome: OrderSendOutcomeTracker,
) -> None:
    """Emit one safe structured event for a provider throttle rejection.

    The event is additive observation only.  In particular, this function must
    not participate in retry/defer/reconciliation policy and must never prevent
    the existing rejection path from continuing.
    """
    if outcome.protocol_evidence is None:
        return
    try:
        logger.warning(
            "kis_throttle_protocol_evidence",
            extra={
                "kis_throttle_protocol_evidence": {
                    "event": "kis_throttle_protocol_evidence",
                    "order_surface": order_surface,
                    "provider_message_code": (
                        provider_message_code
                        if isinstance(provider_message_code, str)
                        else None
                    ),
                    "protocol": outcome.protocol_evidence,
                }
            },
        )
    except Exception:  # noqa: BLE001 - logging must not alter an order outcome
        pass
