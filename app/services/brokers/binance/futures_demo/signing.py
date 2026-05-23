"""ROB-298 PR 2 — HMAC-SHA256 signing chokepoint for Binance Futures Demo.

Self-contained signer for the USD-M Futures Demo lane. Mirrors
``binance.spot_demo.signing`` deliberately: per ROB-296 Hermes review §1
(Option A), preserving environment-specific fail-closed isolation
outweighs deduplication. If a third signed lane is introduced and the
duplication grows untenable, extract a pure ``hmac_sign(params, secret)``
helper under ``binance/`` and have each lane wrap it with its own
fail-closed validation — but not in this PR.

Per ROB-296 §5 (carried forward to PR 2): if the operator's Futures Demo
account requires Ed25519 signing instead of HMAC-SHA256, this signer
will produce a syntactically valid but semantically rejected signature.
The read-only preflight detects that rejection (Binance error code
-1022 / -2014 / -2008) and surfaces it so the operator reports it as a
scope-expansion follow-up rather than silently falling back to a
non-HMAC path.
"""

from __future__ import annotations

import time
from typing import Any, Final
from urllib.parse import urlencode

from binance_common.utils import hmac_hashing

# Binance documents the default recvWindow as 5000 ms; allowed up to 60000.
# We pin a conservative default at the chokepoint so call-sites don't need
# to remember to pass it.
BINANCE_FUTURES_DEMO_RECV_WINDOW_MS: Final[int] = 5000


def _sign_request_params(
    *,
    params: dict[str, Any],
    api_secret: str,
) -> dict[str, Any]:
    """Return a new params dict containing ``timestamp`` + ``signature``.

    Behavior:
      * If ``params`` already contains ``timestamp``, the caller's value
        is used (tests fix this for canonical-signature verification).
      * Otherwise, the current epoch in milliseconds is attached.
      * The signed payload is the URL-encoded form of the params dict in
        insertion order.
      * The original ``params`` is NOT mutated; the returned dict is new.

    Raises:
      * ``ValueError`` if ``api_secret`` is empty.
    """
    if not api_secret:
        raise ValueError(
            "api_secret must be a non-empty string. The Futures Demo adapter "
            "init is responsible for fail-closed credential validation."
        )
    signed: dict[str, Any] = dict(params)
    if "timestamp" not in signed:
        signed["timestamp"] = int(time.time() * 1000)
    payload = urlencode(signed)
    signature = hmac_hashing(api_secret, payload)
    signed["signature"] = signature
    return signed
