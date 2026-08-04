"""Schema contract is declared, origin=SEALED_CORPUS_V1, mismatch loud."""

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


def test_contract_declares_sealed_origin():
    contract = load_contract()
    assert contract["schema_origin"] == "SEALED_CORPUS_V1"
    assert contract["corpus_id"] == "kr-corpus-v1"
    assert SCHEMA_ORIGIN == "SEALED_CORPUS_V1"
    assert "sealed" in contract["schema_origin_note"].lower()


def test_ohlcv_schema_columns_match_sealed_kr_corpus():
    schema = arrow_schema_for("ohlcv")
    assert schema.names == [
        "session",
        "market",
        "ticker",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
        "price_mode",
        "source_product",
    ]
    assert schema.field("open").type == pa.int64()
    assert schema.field("value").nullable is True


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
    bad = pa.table({"ticker": pa.array(["X"], type=pa.string())})
    with pytest.raises(SchemaMismatchError) as exc_info:
        validate_table_schema(bad, "ohlcv")
    assert "mismatch" in str(exc_info.value).lower()


def test_ohlcv_allows_clamp_extra_columns():
    required = arrow_schema_for("ohlcv")
    base = pa.Table.from_pylist(
        [
            {
                "session": "2024-01-02",
                "market": "KOSPI",
                "ticker": "000020",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "value": None,
                "price_mode": "adjusted",
                "source_product": "pykrx",
            }
        ],
        schema=required,
    )
    table = base.append_column("clamped", pa.array([False])).append_column(
        "admitted", pa.array([True])
    )
    validate_table_schema(table, "ohlcv")  # no raise


def test_windows_literals_in_contract():
    contract = load_contract()
    w = contract["windows"]
    assert w["exploration"] == {"start": "2015-01-01", "end": "2024-12-31"}
    assert w["historical_oos_holdout"] == {
        "start": "2025-01-01",
        "end": "2026-07-31",
    }
