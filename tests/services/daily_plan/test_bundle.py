from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.daily_plan.bundle import (
    SCHEMA_VERSION,
    bundle_sha256,
    canonical_bundle,
)


def test_canonical_bundle_has_stable_bytes():
    def row(symbol):
        return {
            "market": "kr",
            "symbol": symbol,
            "row_kind": "candidate",
            "account_ref": None,
        }

    body = {
        "schema_version": SCHEMA_VERSION,
        "rows": [row("278470"), row("005930")],
        "price": Decimal("123456789012345678901234567890.1200"),
        "asof": datetime(2026, 11, 26, 8, 30, tzinfo=timezone(timedelta(hours=9))),
    }
    assert canonical_bundle(body) == (
        b'{"asof":"2026-11-25T23:30:00.000000Z",'
        b'"price":"123456789012345678901234567890.12",'
        b'"rows":[{"account_ref":null,"market":"kr","row_kind":"candidate","symbol":"005930"},'
        b'{"account_ref":null,"market":"kr","row_kind":"candidate","symbol":"278470"}],'
        b'"schema_version":"daily-plan-bundle/v1"}'
    )
    assert bundle_sha256(body) == bundle_sha256(
        {**body, "rows": list(reversed(body["rows"]))}
    )


def test_unknown_quantity_and_known_zero_have_different_hashes():
    assert bundle_sha256(
        {"schema_version": SCHEMA_VERSION, "quantity": {"known": Decimal("-0.00")}}
    ) != bundle_sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "quantity": {"unavailable": "open_sell_unknown"},
        }
    )


def test_float_is_refused():
    with pytest.raises(TypeError, match="unsupported bundle value"):
        canonical_bundle({"schema_version": SCHEMA_VERSION, "price": 0.1})
