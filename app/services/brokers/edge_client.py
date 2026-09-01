"""Minimal client for the opt-in KIS mock broker-edge command port.

The edge owns broker credentials.  This client deliberately sends only a
versioned command envelope to an operator-selected HTTP endpoint and never
imports or instantiates a KIS provider client.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.brokers.kis.send_outcome import OrderSendOutcomeTracker

KIS_MOCK_EDGE_URL_ENV = "KIS_MOCK_EDGE_URL"
_COMMAND_PATH = "/v1/commands"
_COMMAND_SCHEMA_VERSION = "execution-command/v1"
_RECEIPT_SCHEMA_VERSION = "execution-receipt/v1"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=3.0)


class ExecutionDisposition(StrEnum):
    """Wire-level disposition emitted by broker-edge."""

    ACCEPTED = "ACCEPTED"
    NOT_CREATED = "NOT_CREATED"
    UNKNOWN = "UNKNOWN"


class BrokerEdgeError(Exception):
    """Base class for a parsed edge result that cannot become a KIS response."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class BrokerEdgeNotCreated(BrokerEdgeError):
    """The edge conclusively reports that it did not create an order."""


class BrokerEdgeOutcomeUnknown(BrokerEdgeError):
    """The edge cannot prove whether a command created an order."""


@dataclass(frozen=True)
class ExecutionCommandV1:
    """The exact broker-edge execution-command/v1 envelope."""

    command_id: str
    account_scope: str
    side: str
    stock_code: str
    quantity: str
    price: str
    order_type: str
    issued_at: str

    def payload(self) -> dict[str, str]:
        return {
            "schema_version": _COMMAND_SCHEMA_VERSION,
            "command_id": self.command_id,
            "account_scope": self.account_scope,
            "side": self.side,
            "stock_code": self.stock_code,
            "quantity": self.quantity,
            "price": self.price,
            "order_type": self.order_type,
            "issued_at": self.issued_at,
        }


@dataclass(frozen=True)
class ExecutionReceiptV1:
    """Validated broker-edge execution-receipt/v1 response."""

    command_id: str
    disposition: ExecutionDisposition
    broker_order_id: str | None
    error_code: str | None
    recorded_at: str

    @classmethod
    def from_payload(cls, payload: object) -> ExecutionReceiptV1:
        if not isinstance(payload, dict):
            raise BrokerEdgeOutcomeUnknown("invalid_edge_receipt")

        if payload.get("schema_version") != _RECEIPT_SCHEMA_VERSION:
            raise BrokerEdgeOutcomeUnknown("invalid_edge_receipt")

        command_id = _non_blank_string(payload.get("command_id"))
        recorded_at = _non_blank_string(payload.get("recorded_at"))
        if command_id is None or recorded_at is None:
            raise BrokerEdgeOutcomeUnknown("invalid_edge_receipt")

        try:
            disposition = ExecutionDisposition(payload.get("disposition"))
        except (TypeError, ValueError):
            raise BrokerEdgeOutcomeUnknown("invalid_edge_receipt") from None

        broker_order_id = _non_blank_string(payload.get("broker_order_id"))
        error_code = _safe_error_code(payload.get("error_code"))
        if disposition is ExecutionDisposition.ACCEPTED and broker_order_id is None:
            raise BrokerEdgeOutcomeUnknown("invalid_edge_receipt")

        return cls(
            command_id=command_id,
            disposition=disposition,
            broker_order_id=broker_order_id,
            error_code=error_code,
            recorded_at=recorded_at,
        )


def get_kis_mock_edge_url() -> str | None:
    """Return the default-off edge URL without caching environment state."""
    value = os.getenv(KIS_MOCK_EDGE_URL_ENV)
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_kis_mock_execution_command(
    *,
    command_id: str | None,
    side: str,
    stock_code: str,
    quantity: int,
    price: int,
    order_type: str,
) -> ExecutionCommandV1:
    """Build an edge command from the KIS mock normalized wire values."""
    normalized_command_id = _non_blank_string(command_id)
    if normalized_command_id is None:
        raise BrokerEdgeNotCreated("idempotency_key_required")

    return ExecutionCommandV1(
        command_id=normalized_command_id,
        account_scope="kis_mock",
        side=side,
        stock_code=stock_code,
        quantity=str(quantity),
        price=str(price),
        order_type=order_type,
        issued_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )


async def execute_kis_mock_command(
    command: ExecutionCommandV1,
    *,
    base_url: str,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
    send_outcome: OrderSendOutcomeTracker | None = None,
) -> dict[str, Any]:
    """POST one edge command and map its receipt to the legacy KIS result form.

    A parsed ``UNKNOWN`` and any malformed/untrusted edge response remain
    explicit unknown outcomes; callers must not retry them as a new order.
    """
    normalized_base_url = _validated_base_url(base_url)

    async with httpx.AsyncClient(
        base_url=normalized_base_url,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        if pre_send_hook is not None:
            await pre_send_hook()
        if send_outcome is not None:
            send_outcome.mark_dispatched()
        response = await client.post(_COMMAND_PATH, json=command.payload())

    if send_outcome is not None:
        send_outcome.mark_http_response(response.status_code)

    try:
        receipt = ExecutionReceiptV1.from_payload(response.json())
    except (TypeError, ValueError) as exc:
        if send_outcome is not None:
            send_outcome.mark_unknown()
        raise BrokerEdgeOutcomeUnknown("invalid_edge_receipt") from exc

    if receipt.command_id != command.command_id:
        if send_outcome is not None:
            send_outcome.mark_unknown()
        raise BrokerEdgeOutcomeUnknown("edge_command_id_mismatch")

    if receipt.disposition is ExecutionDisposition.ACCEPTED:
        if not 200 <= response.status_code < 300:
            if send_outcome is not None:
                send_outcome.mark_unknown()
            raise BrokerEdgeOutcomeUnknown("edge_http_status_uncertain")
        if send_outcome is not None:
            send_outcome.mark_accepted()
        # KIS mock ledger normalization deliberately accepts only rt_cd == "0"
        # plus a nonblank odno. Preserve that established contract.
        return {
            "rt_cd": "0",
            "odno": receipt.broker_order_id,
            "broker_order_id": receipt.broker_order_id,
            "msg": "broker edge receipt accepted",
            "msg_cd": "EDGE_ACCEPTED",
        }

    if receipt.disposition is ExecutionDisposition.NOT_CREATED:
        # A redirect/server error never proves no broker-side order even if its
        # body looks definitive. Only a normal 2xx/4xx receipt is terminal.
        if not (200 <= response.status_code < 300 or 400 <= response.status_code < 500):
            if send_outcome is not None:
                send_outcome.mark_unknown()
            raise BrokerEdgeOutcomeUnknown("edge_http_status_uncertain")
        if send_outcome is not None:
            send_outcome.mark_provider_rejected()
        raise BrokerEdgeNotCreated(receipt.error_code or "edge_not_created")

    if send_outcome is not None:
        send_outcome.mark_unknown()
    raise BrokerEdgeOutcomeUnknown(receipt.error_code or "edge_outcome_unknown")


def _validated_base_url(value: str) -> str:
    """Reject ambiguous URLs before the command crosses the send boundary."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise BrokerEdgeNotCreated("invalid_edge_url") from None
    return value.rstrip("/")


def _non_blank_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _safe_error_code(value: object) -> str | None:
    candidate = _non_blank_string(value)
    if candidate is None:
        return None
    if len(candidate) > 96 or any(
        not (
            character.isascii()
            and (character.islower() or character.isdigit() or character == "_")
        )
        for character in candidate
    ):
        return "invalid_edge_error_code"
    return candidate


__all__ = [
    "BrokerEdgeNotCreated",
    "BrokerEdgeOutcomeUnknown",
    "ExecutionCommandV1",
    "ExecutionDisposition",
    "ExecutionReceiptV1",
    "build_kis_mock_execution_command",
    "execute_kis_mock_command",
    "get_kis_mock_edge_url",
]
