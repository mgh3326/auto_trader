from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse


class TestNormalizeName:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("  삼성전자  ", "삼성전자"),
            ("Apple Inc.", "Apple Inc."),
            ("", ""),
            ("  ", ""),
        ],
    )
    def test_strips_whitespace(self, value: str, expected: str):
        from app.services.symbol_universe_common import normalize_name

        assert normalize_name(value) == expected


class TestSyncHint:
    def test_returns_formatted_hint(self):
        from app.services.symbol_universe_common import sync_hint

        result = sync_hint("uv run python scripts/sync_kr_symbol_universe.py")
        assert (
            result == "Sync required: uv run python scripts/sync_kr_symbol_universe.py"
        )


class TestHasAnyRows:
    @pytest.mark.asyncio
    async def test_returns_true_when_row_exists(self):
        from app.services.symbol_universe_common import has_any_rows

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "005930"
        db = AsyncMock()
        db.execute.return_value = mock_result

        assert await has_any_rows(db, KRSymbolUniverse.symbol) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_table_empty(self):
        from app.services.symbol_universe_common import has_any_rows

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db = AsyncMock()
        db.execute.return_value = mock_result

        assert await has_any_rows(db, KRSymbolUniverse.symbol) is False
