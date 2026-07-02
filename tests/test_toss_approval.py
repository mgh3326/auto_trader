from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.timezone import KST
from app.mcp_server.tooling.toss_approval import (
    APPROVAL_TTL_SECONDS,
    build_canonical_payload,
    decode_approval_token,
    derive_approval_digest,
    derive_client_order_id,
    encode_approval_token,
    trading_day_salt,
    verify_approval_token,
)

_ET = ZoneInfo("America/New_York")


def _canon(**overrides):
    base = dict(
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity="10",
        price="70000",
        order_amount=None,
    )
    base.update(overrides)
    return build_canonical_payload(**base)


def test_canonical_uppercases_and_is_stable():
    a = _canon()
    b = _canon()
    assert a == b
    assert a["side"] == "BUY"
    assert a["orderType"] == "LIMIT"
    assert derive_approval_digest(a) == derive_approval_digest(b)


def test_canonical_price_change_changes_digest():
    assert derive_approval_digest(_canon(price="70000")) != derive_approval_digest(
        _canon(price="70100")
    )


def test_amount_based_buy_hashes_wire_payload():
    canon = _canon(quantity=None, price=None, order_amount="1000000")
    digest = derive_approval_digest(canon)
    assert digest.startswith("p6a-")
    assert len(digest) == len("p6a-") + 16


def test_trading_day_salt_kr_uses_kst_us_uses_et():
    # 2026-07-02 23:30 KST == 2026-07-02 10:30 ET (same US calendar date here)
    now = datetime(2026, 7, 2, 23, 30, tzinfo=KST)
    assert trading_day_salt("kr", now) == "2026-07-02"
    assert trading_day_salt("us", now) == now.astimezone(_ET).date().isoformat()


def test_client_order_id_deterministic_same_day():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canon = _canon()
    assert derive_client_order_id(canon, market="kr", now=now) == derive_client_order_id(
        canon, market="kr", now=now
    )


def test_client_order_id_changes_next_trading_day():
    canon = _canon()
    today = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    tomorrow = datetime(2026, 7, 3, 10, 0, tzinfo=KST)
    assert derive_client_order_id(canon, market="kr", now=today) != derive_client_order_id(
        canon, market="kr", now=tomorrow
    )


def test_client_order_id_rung_discriminator_splits():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canon = _canon()
    base = derive_client_order_id(canon, market="kr", now=now)
    r2 = derive_client_order_id(canon, market="kr", now=now, rung=2)
    assert base != r2
    assert r2.startswith("tossp6-")


def test_client_order_id_is_safe_segment():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    cid = derive_client_order_id(_canon(), market="kr", now=now)
    assert cid.replace("-", "").replace("_", "").isalnum()
    assert len(cid) <= 40


def test_token_roundtrip_and_verify_ok():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canon = _canon()
    token = encode_approval_token(canon, now=now)
    assert token.startswith("p6a1.")
    iat, decoded = decode_approval_token(token)
    assert decoded == canon
    result = verify_approval_token(token, canon, now=now)
    assert result.ok is True
    assert result.digest == derive_approval_digest(canon)


def test_verify_mismatch_returns_diff():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    token = encode_approval_token(_canon(price="70000"), now=now)
    result = verify_approval_token(token, _canon(price="70100"), now=now)
    assert result.ok is False
    assert result.error_code == "approval_hash_mismatch"
    assert "price" in result.diff
    assert result.diff["price"] == {"previewed": "70000", "placing": "70100"}


def test_verify_expired_after_ttl():
    issued = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    token = encode_approval_token(_canon(), now=issued)
    later = issued + timedelta(seconds=APPROVAL_TTL_SECONDS + 1)
    result = verify_approval_token(token, _canon(), now=later)
    assert result.ok is False
    assert result.error_code == "approval_expired"


def test_verify_within_ttl_ok():
    issued = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    token = encode_approval_token(_canon(), now=issued)
    later = issued + timedelta(seconds=APPROVAL_TTL_SECONDS - 1)
    assert verify_approval_token(token, _canon(), now=later).ok is True


def test_verify_invalid_token():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    result = verify_approval_token("not-a-token", _canon(), now=now)
    assert result.ok is False
    assert result.error_code == "invalid_approval_hash"
