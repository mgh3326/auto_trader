"""Explicit KIS order-send outcome tracked at the real HTTP boundary.

Only KIS mock scalping passes a tracker. Other KIS callers pass ``None`` and
retain their existing behavior and response contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OrderSendDisposition(StrEnum):
    NOT_CREATED = "not_created"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


@dataclass
class OrderSendOutcomeTracker:
    disposition: OrderSendDisposition = OrderSendDisposition.NOT_CREATED
    last_http_status: int | None = None

    def mark_dispatched(self) -> None:
        """A POST is crossing the HTTP boundary; its outcome is now uncertain."""
        self.disposition = OrderSendDisposition.UNKNOWN
        self.last_http_status = None

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
        self.disposition = OrderSendDisposition.ACCEPTED

    def mark_unknown(self) -> None:
        self.disposition = OrderSendDisposition.UNKNOWN
