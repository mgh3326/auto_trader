"""Unit tests for the is_common_stock column on USSymbolUniverse (ROB-204)."""
import pytest
from sqlalchemy import text

from app.models.us_symbol_universe import USSymbolUniverse


@pytest.mark.unit
def test_us_symbol_universe_has_is_common_stock_nullable() -> None:
    table = USSymbolUniverse.__table__
    column = table.columns.get("is_common_stock")
    assert column is not None, "is_common_stock column missing"
    assert column.nullable is True, "is_common_stock must be nullable"
    assert column.type.python_type is bool


@pytest.mark.unit
def test_us_symbol_universe_has_active_common_stock_partial_index() -> None:
    indexes = USSymbolUniverse.__table__.indexes
    target = next(
        (i for i in indexes if "common" in i.name.lower() and "active" in i.name.lower()),
        None,
    )
    assert target is not None, "expected partial index on is_common_stock + is_active not found"
