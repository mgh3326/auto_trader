# tests/test_candles_sync_common.py
from __future__ import annotations

import pytest


class TestNormalizeMode:
    def test_returns_incremental(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        assert normalize_mode("incremental") == "incremental"

    def test_returns_backfill(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        assert normalize_mode("BACKFILL") == "backfill"

    def test_strips_whitespace(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        assert normalize_mode("  Incremental  ") == "incremental"

    def test_rejects_invalid(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        with pytest.raises(ValueError, match="mode must be"):
            normalize_mode("unknown")

    def test_rejects_empty(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        with pytest.raises(ValueError, match="mode must be"):
            normalize_mode("")


class TestParseFloat:
    def test_parses_string_number(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float("3.14") == pytest.approx(3.14)

    def test_parses_int(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float(42) == 42.0

    def test_returns_none_for_none(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float(None) is None

    def test_returns_none_for_garbage(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float("abc") is None

    def test_returns_none_for_empty_string(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float("") is None

class TestBuildCursorSql:
    def test_kr_cursor_sql_contains_table_and_partition(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_cursor_sql

        cfg = SyncTableConfig(table_name="kr_candles_1m", partition_col="venue")
        sql_text = build_cursor_sql(cfg).text

        assert "kr_candles_1m" in sql_text
        assert "venue = :venue" in sql_text
        assert "MAX(time)" in sql_text

    def test_us_cursor_sql_uses_exchange(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_cursor_sql

        cfg = SyncTableConfig(table_name="us_candles_1m", partition_col="exchange")
        sql_text = build_cursor_sql(cfg).text

        assert "us_candles_1m" in sql_text
        assert "exchange = :exchange" in sql_text


class TestBuildUpsertSql:
    def test_kr_upsert_sql_structure(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_upsert_sql

        cfg = SyncTableConfig(table_name="kr_candles_1m", partition_col="venue")
        sql_text = build_upsert_sql(cfg).text

        assert "INSERT INTO public.kr_candles_1m" in sql_text
        assert ":venue" in sql_text
        assert "ON CONFLICT (time, symbol, venue)" in sql_text
        assert "kr_candles_1m.open IS DISTINCT FROM EXCLUDED.open" in sql_text
        assert "kr_candles_1m.volume IS DISTINCT FROM EXCLUDED.volume" in sql_text

    def test_us_upsert_sql_structure(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_upsert_sql

        cfg = SyncTableConfig(table_name="us_candles_1m", partition_col="exchange")
        sql_text = build_upsert_sql(cfg).text

        assert "INSERT INTO public.us_candles_1m" in sql_text
        assert ":exchange" in sql_text
        assert "ON CONFLICT (time, symbol, exchange)" in sql_text
        assert "us_candles_1m.close IS DISTINCT FROM EXCLUDED.close" in sql_text
