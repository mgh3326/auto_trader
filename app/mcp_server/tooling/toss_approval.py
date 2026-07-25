"""ROB-651 P6-A — Toss approval-hash + content-based clientOrderId primitives.

Pure helpers (no DB, no network, ``now`` injected) shared by
``toss_preview_order`` and ``toss_place_order`` so a previewed order and the
placed order are bound to the same canonical content, and the Toss
``clientOrderId`` is a deterministic content + trading-day-salt idempotency key
instead of a fresh uuid4 per call.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.timezone import KST

APPROVAL_TOKEN_VERSION = "p6a1"
APPROVAL_DIGEST_PREFIX = "p6a"
CLIENT_ORDER_ID_PREFIX = "tossp6"
APPROVAL_TTL_SECONDS = 300

_ET = ZoneInfo("America/New_York")


@dataclass
class ApprovalResult:
    ok: bool
    error_code: str | None = None
    message: str | None = None
    diff: dict[str, Any] | None = None
    digest: str | None = None


def build_canonical_payload(
    *,
    market: str,
    symbol: str,
    side: str,
    order_type: str,
    time_in_force: str,
    quantity: str | None,
    price: str | None,
    order_amount: str | None,
) -> dict[str, Any]:
    """Canonical order content shared by preview and place.

    ``quantity``/``price``/``order_amount`` must already be the stringified
    wire values (post tick-snap) or ``None`` so preview and place derive an
    identical digest. ``clientOrderId`` and ``confirmHighValueOrder`` are
    intentionally excluded (the former is derived from this; the latter is an
    operator flag, not economic intent).
    """
    return {
        "market": market,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
        "quantity": quantity,
        "price": price,
        "orderAmount": order_amount,
    }


def _canonical_json(canonical: dict[str, Any]) -> str:
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def derive_approval_digest(canonical: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(canonical).encode()).hexdigest()[:16]
    return f"{APPROVAL_DIGEST_PREFIX}-{digest}"


def trading_day_salt(market: str, now: datetime) -> str:
    """ISO trading-day date. ``us`` → America/New_York (DST-aware), else KST."""
    tz = _ET if market == "us" else KST
    return now.astimezone(tz).date().isoformat()


def derive_client_order_id(
    canonical: dict[str, Any],
    *,
    market: str,
    now: datetime,
    rung: str | int | None = None,
) -> str:
    salt = trading_day_salt(market, now)
    disc = "" if rung is None else str(rung)
    blob = f"{_canonical_json(canonical)}|{salt}|{disc}".encode()
    digest = hashlib.sha256(blob).hexdigest()[:16]
    return f"{CLIENT_ORDER_ID_PREFIX}-{digest}"


def encode_approval_token(canonical: dict[str, Any], *, now: datetime) -> str:
    payload = json.dumps(
        {"iat": int(now.timestamp()), "canon": canonical},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    blob = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{APPROVAL_TOKEN_VERSION}.{blob}"


def decode_approval_token(token: str) -> tuple[int, dict[str, Any]]:
    version, _, blob = token.partition(".")
    if version != APPROVAL_TOKEN_VERSION or not blob:
        raise ValueError("unsupported approval token")
    pad = "=" * (-len(blob) % 4)
    raw = base64.urlsafe_b64decode(blob + pad)
    obj = json.loads(raw)
    iat = int(obj["iat"])
    canon = obj["canon"]
    if not isinstance(canon, dict):
        raise ValueError("malformed approval token payload")
    return iat, canon


def _diff_canonical(
    previewed: dict[str, Any], placing: dict[str, Any]
) -> dict[str, Any]:
    keys = set(previewed) | set(placing)
    return {
        key: {"previewed": previewed.get(key), "placing": placing.get(key)}
        for key in sorted(keys)
        if previewed.get(key) != placing.get(key)
    }


def verify_approval_token(
    token: str, placing_canonical: dict[str, Any], *, now: datetime
) -> ApprovalResult:
    try:
        iat, previewed = decode_approval_token(token)
    except Exception:
        return ApprovalResult(
            ok=False,
            error_code="invalid_approval_hash",
            message=(
                "approval_hash is not a valid approval token; re-preview required"
            ),
        )
    if int(now.timestamp()) - iat > APPROVAL_TTL_SECONDS:
        return ApprovalResult(
            ok=False,
            error_code="approval_expired",
            message="approval_hash expired; re-preview required",
        )
    if previewed != placing_canonical:
        return ApprovalResult(
            ok=False,
            error_code="approval_hash_mismatch",
            message="placing order does not match previewed order",
            diff=_diff_canonical(previewed, placing_canonical),
        )
    return ApprovalResult(ok=True, digest=derive_approval_digest(placing_canonical))
