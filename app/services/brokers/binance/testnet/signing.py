"""ROB-286 — HMAC-SHA256 signing chokepoint for Binance testnet.

Single function ``_sign_request_params`` is the only place in the
codebase that produces a Binance signed-endpoint signature. If any
caller inlines its own HMAC, the audit grep in
``tests/services/brokers/binance/testnet/test_audit_no_live_host.py`` is
not enough — but the convention is enforced by code review: signing
lives here and nowhere else.

Open item #1 lean adopted: ``binance_common.utils.hmac_hashing`` (the
SDK-shipped standalone signer) is used as the HMAC primitive. If a
future SDK refactor renames or removes it, fall back to stdlib
``hmac.new(secret, payload, sha256).hexdigest()`` (the implementation is
identical — see the SDK source).
"""

from __future__ import annotations

import time
from typing import Any, Final
from urllib.parse import urlencode

from binance_common.utils import hmac_hashing

# Binance documents the default recvWindow as 5000 ms; allowed up to 60000.
# We pin a conservative default at the chokepoint so call-sites don't need
# to remember to pass it.
BINANCE_RECV_WINDOW_MS: Final[int] = 5000


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
        insertion order (Binance is order-sensitive only inside the
        query string the client constructs; the HMAC is over whatever
        order we provide here).
      * The original ``params`` is NOT mutated; the returned dict is new.

    Raises:
      * ``ValueError`` if ``api_secret`` is empty.
    """
    if not api_secret:
        raise ValueError(
            "api_secret must be a non-empty string. The adapter init is "
            "responsible for fail-closed credential validation."
        )
    signed: dict[str, Any] = dict(params)
    if "timestamp" not in signed:
        signed["timestamp"] = int(time.time() * 1000)
    payload = urlencode(signed)
    signature = hmac_hashing(api_secret, payload)
    signed["signature"] = signature
    return signed
