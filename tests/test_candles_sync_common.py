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
