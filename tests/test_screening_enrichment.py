# tests/test_screening_enrichment.py
"""Verify screening.enrichment functions are importable and work."""
import pytest


class TestEnrichmentImports:
    def test_apply_equity_enrichment_defaults(self):
        from app.mcp_server.tooling.screening.enrichment import _apply_equity_enrichment_defaults
        row: dict = {}
        result = _apply_equity_enrichment_defaults(row)
        assert "sector" in result

    def test_compute_target_upside_pct(self):
        from app.mcp_server.tooling.screening.enrichment import _compute_target_upside_pct
        result = _compute_target_upside_pct(avg_target=120.0, current_price=100.0)
        assert result == pytest.approx(20.0)

    def test_row_has_complete_screen_enrichment_empty(self):
        from app.mcp_server.tooling.screening.enrichment import _row_has_complete_screen_enrichment
        assert _row_has_complete_screen_enrichment({}) is False

    def test_screen_enrichment_fields_constant(self):
        from app.mcp_server.tooling.screening.enrichment import _SCREEN_ENRICHMENT_FIELDS
        assert "sector" in _SCREEN_ENRICHMENT_FIELDS
