"""ROB-356 (PR3) — deterministic ready vs needs_more_data verdict.

Mirrors ROB-355's ``classify_verdict``: the decision to open a bounded funding-OI
event backtest issue is a function of explicit coverage thresholds, not prose. Below
ANY threshold (or unproven survivorship) -> ``needs_more_data`` with named reasons, and
the builder must stop rather than hand off to a backtest.
"""

import build_funding_oi_features as b


def _ready_inputs(**over):
    base = {
        "usable_symbols": 30,
        "delisted_usable": 8,
        "all_delisted_survivorship_ok": True,
        "min_oi_window_rows": 2000,
        "max_missingness": 0.01,
    }
    base.update(over)
    return b.ReadinessInputs(**base)


def test_all_thresholds_met_is_ready():
    verdict, reasons = b.classify_feature_readiness(_ready_inputs())
    assert verdict == "ready"
    assert reasons == []


def test_too_few_usable_symbols_blocks():
    verdict, reasons = b.classify_feature_readiness(_ready_inputs(usable_symbols=3))
    assert verdict == "needs_more_data"
    assert any("usable_symbols" in r for r in reasons)


def test_unproven_delisted_survivorship_blocks():
    verdict, reasons = b.classify_feature_readiness(
        _ready_inputs(all_delisted_survivorship_ok=False)
    )
    assert verdict == "needs_more_data"
    assert any("survivorship" in r for r in reasons)


def test_too_few_delisted_usable_blocks():
    verdict, reasons = b.classify_feature_readiness(_ready_inputs(delisted_usable=0))
    assert verdict == "needs_more_data"
    assert any("delisted" in r for r in reasons)


def test_short_oi_window_blocks():
    verdict, reasons = b.classify_feature_readiness(
        _ready_inputs(min_oi_window_rows=10)
    )
    assert verdict == "needs_more_data"
    assert any("oi_window" in r for r in reasons)


def test_excess_missingness_blocks():
    verdict, reasons = b.classify_feature_readiness(_ready_inputs(max_missingness=0.5))
    assert verdict == "needs_more_data"
    assert any("missingness" in r for r in reasons)


def test_multiple_failures_all_reported():
    verdict, reasons = b.classify_feature_readiness(
        _ready_inputs(usable_symbols=1, delisted_usable=0)
    )
    assert verdict == "needs_more_data"
    assert len(reasons) >= 2


def test_custom_thresholds_respected():
    thr = b.ReadinessThresholds(min_usable_symbols=2)
    verdict, _ = b.classify_feature_readiness(_ready_inputs(usable_symbols=2), thr)
    assert verdict == "ready"


# --------------------------------------------------------------------------- #
# pure coverage helpers
# --------------------------------------------------------------------------- #
def test_expected_days_inclusive_span():
    assert b.expected_days("2024-01-01", "2024-01-01") == 1
    assert b.expected_days("2024-01-01", "2024-01-31") == 31
    assert b.expected_days(None, "2024-01-31") == 0


def test_survivorship_ok_live_symbol_needs_any_data():
    assert b.survivorship_ok("2024-01-10", delisted_at=None) is True
    assert b.survivorship_ok(None, delisted_at=None) is False


def test_survivorship_ok_delisted_archive_must_reach_delist_day():
    # delisted_at is EXCLUSIVE epoch ms; archive must reach the last active day (delist-1).
    from datetime import UTC, datetime

    delist_ms = int(datetime(2024, 1, 12, tzinfo=UTC).timestamp() * 1000)  # exclusive
    assert b.survivorship_ok("2024-01-11", delist_ms) is True  # reaches last active day
    assert b.survivorship_ok("2024-01-09", delist_ms) is False  # archive ends early


def test_summarize_drops_internal_feats_and_emits_verdict():
    stats = [
        {
            "symbol": "BTCUSDT",
            "status": "live",
            "delisted": False,
            "feature_rows": 9,
            "missingness": 0.0,
            "survivorship_ok": True,
            "_feats": [1, 2, 3],
        },
    ]
    out = b.summarize(
        stats, b.ReadinessThresholds(min_usable_symbols=1, min_oi_window_rows=1000)
    )
    assert "_feats" not in out["per_symbol"][0]
    assert (
        out["verdict"] == "needs_more_data"
    )  # 9 rows < 1000 -> not usable -> 0 usable symbols
