# tests/test_order_approval.py
from datetime import datetime
from zoneinfo import ZoneInfo

from app.mcp_server.tooling import order_approval as oa

KST = ZoneInfo("Asia/Seoul")


def _canon(**over):
    base = {
        "market_type": "equity_kr",
        "symbol": "005930",
        "side": "buy",
        "order_type": "limit",
        "quantity": "10",
        "price": "70000",
    }
    base.update(over)
    return oa.build_order_canonical_payload(**base)


def test_canonical_is_deterministic_and_side_upcased():
    c = _canon()
    assert c["side"] == "BUY"
    assert c["orderType"] == "LIMIT"
    assert c["market_type"] == "equity_kr"
    assert c == _canon()  # stable


def test_salt_market_maps_us_else_kr():
    assert oa.salt_market_for("equity_us") == "us"
    assert oa.salt_market_for("equity_kr") == "kr"
    assert oa.salt_market_for("crypto") == "kr"


def test_token_roundtrip_and_mismatch_diff():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canonical = _canon()
    token = oa.encode_approval_token(canonical, now=now)
    ok = oa.verify_approval_token(token, canonical, now=now)
    assert ok.ok is True and ok.digest.startswith("p6a-")

    changed = _canon(quantity="11")
    bad = oa.verify_approval_token(token, changed, now=now)
    assert bad.ok is False and bad.error_code == "approval_hash_mismatch"
    assert bad.diff["quantity"] == {"previewed": "10", "placing": "11"}


def test_idempotency_key_same_day_stable_next_day_differs():
    canonical = _canon()
    d1 = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    d1b = datetime(2026, 7, 2, 15, 0, tzinfo=KST)
    d2 = datetime(2026, 7, 3, 10, 0, tzinfo=KST)
    k1 = oa.derive_client_order_id(canonical, market="kr", now=d1)
    k1b = oa.derive_client_order_id(canonical, market="kr", now=d1b)
    k2 = oa.derive_client_order_id(canonical, market="kr", now=d2)
    assert k1 == k1b
    assert k1 != k2
