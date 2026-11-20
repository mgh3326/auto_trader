"""A crypto limit order whose price and volume were validated before this layer.

hk 5441 (design 5438 B2, B28). ``_execute_crypto_order`` normally snaps a limit
price with ``adjust_price_to_upbit_unit``, an older table that moves a price the
published Upbit unit already allows (5 KRW steps between 1,000 and 5,000 KRW).
A caller that validated the price against the published table passes this
object instead. The order layer then:

* does **not** adjust the price, and
* refuses to send unless the price and the volume string it is about to post
  are exactly the validated ones.

The refusal is a ``PreSendFreshnessError`` raised before the request is built,
so the existing handler records it as a proven non-send. Every other caller
passes nothing and is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from app.services.brokers.kis.pre_send import PreSendFreshnessError

PRICE_NORMALIZATION_MISMATCH: Final = "price_normalization_mismatch"


@dataclass(frozen=True, slots=True)
class PrevalidatedCryptoWire:
    """The exact ``(price, volume)`` the caller's gate approved."""

    price_krw: int
    volume: Decimal

    def checked_price(self, *, price: float | None, volume_str: str) -> str:
        """The price string to post, or a proven refusal.

        ``price`` is what this layer holds after its own processing and
        ``volume_str`` is the volume it formatted for the request body. Either
        differing from the approved value means the request would not be the
        one that was approved.
        """
        try:
            same = (
                price is not None
                and Decimal(str(price)) == Decimal(self.price_krw)
                and Decimal(volume_str) == self.volume
            )
        except (InvalidOperation, ValueError):
            same = False
        if not same:
            raise PreSendFreshnessError((PRICE_NORMALIZATION_MISMATCH,))
        return str(self.price_krw)
