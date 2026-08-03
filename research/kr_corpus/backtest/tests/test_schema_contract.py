"""Schema contract is declared, origin=INFERRED_FROM_LITERALS, mismatch loud."""

from __future__ import annotations

import pyarrow as pa
import pytest
from schema_contract import (
    SCHEMA_ORIGIN,
    SchemaMismatchError,
    arrow_schema_for,
    load_contract,
    validate_table_schema,
)


def test_contract_declares_inferred_origin():
    contract = load_contract()
    assert contract["schema_origin"] == "INFERRED_FROM_LITERALS"
    assert SCHEMA_ORIGIN == "INFERRED_FROM_LITERALS"
    assert "INFERRED" in contract["schema_origin_note"]


def test_ohlcv_schema_columns_match_contract():
    schema = arrow_schema_for("ohlcv")
    assert schema.names == [
        "symbol",
        "session_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trading_value",
        "market",
    ]


def test_membership_schema_columns_match_contract():
    schema = arrow_schema_for("membership")
    assert schema.names == [
        "symbol",
        "session_date",
        "market",
        "member",
        "status",
    ]


def test_schema_mismatch_is_loud_fail():
    # Wrong column set — silent coercion forbidden.
    bad = pa.table({"symbol": pa.array(["X"], type=pa.string())})
    with pytest.raises(SchemaMismatchError) as exc_info:
        validate_table_schema(bad, "ohlcv")
    assert "mismatch" in str(exc_info.value).lower()


def test_windows_literals_in_contract():
    contract = load_contract()
    w = contract["windows"]
    assert w["exploration"] == {"start": "2015-01-01", "end": "2024-12-31"}
    assert w["historical_oos_holdout"] == {
        "start": "2025-01-01",
        "end": "2026-07-31",
    }
