"""ROB-355 — pure-helper tests for the derivatives data feasibility probe.

No network: only the deterministic helpers (range parsing, survivorship check,
panel-start flag, verdict mapping). The network RUN is operator-gated.
"""

import audit_derivatives_feasibility as aud
from audit_derivatives_feasibility import FamilySignals, classify_verdict


def test_parse_range_empty_and_blank():
    assert aud.parse_range([]) is None
    assert aud.parse_range(["", None]) is None  # type: ignore[list-item]


def test_parse_range_dedups_and_orders():
    r = aud.parse_range(["2024-03", "2024-01", "2024-03", "2024-02"])
    assert r == {"first": "2024-01", "last": "2024-03", "count": 3}


def test_survivorship_live_symbol_needs_any_data():
    # active_to None (live): OK iff data exists
    assert aud.survivorship_ok("2026-05-27", None) is True
    assert aud.survivorship_ok(None, None) is False


def test_survivorship_dead_symbol_must_reach_delisting():
    # MATIC delisted 2024-09; OI archive to 2025-01 -> covered
    assert aud.survivorship_ok("2025-01-22", "2024-09") is True
    # archive ends before delisting -> survivorship gap
    assert aud.survivorship_ok("2024-06-30", "2024-09") is False
    assert aud.survivorship_ok(None, "2024-09") is False


def test_survivorship_mixed_token_lengths():
    # day vs month compare on the month prefix
    assert aud.survivorship_ok("2024-09-05", "2024-09") is True


def test_panel_starts_late():
    assert aud.panel_starts_late("2020-09-01", "2020-01-01") is True
    assert aud.panel_starts_late("2020-01-01", "2020-01-01") is False
    assert aud.panel_starts_late(None, "2020-01-01") is False


def test_verdict_absent_archive_is_needs_vendor_data():
    # liquidation: no archive at all
    assert classify_verdict(FamilySignals(False, True, False)) == "needs_vendor_data"


def test_verdict_present_but_not_raw_is_partial():
    # liquidity sweep: banded depth / aggregate, not raw L2 evidence
    assert classify_verdict(FamilySignals(True, False, False)) == "partial"


def test_verdict_raw_but_delisted_missing_is_needs_more_data():
    assert classify_verdict(FamilySignals(True, True, False)) == "needs_more_data"


def test_verdict_raw_and_delisted_covered_is_feasible():
    # funding-only baseline and funding+OI both land here per the probe
    assert classify_verdict(FamilySignals(True, True, True)) == "feasible"
