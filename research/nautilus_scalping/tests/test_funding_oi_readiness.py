"""ROB-356 (PR3) — deterministic ready vs needs_more_data verdict.

Mirrors ROB-355's ``classify_verdict``: the decision to open a bounded funding-OI
event backtest issue is a function of explicit coverage thresholds, not prose. Below
ANY threshold (or unproven survivorship) -> ``needs_more_data`` with named reasons, and
the builder must stop rather than hand off to a backtest.
"""

import build_funding_oi_features as b
import pit_universe as pu


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


# --------------------------------------------------------------------------- #
# ROB-362 PR1 — stratified subset selection (pure; makes the coverage RUN cheap)
# --------------------------------------------------------------------------- #
def _mk(sym: str, delisted: bool = False) -> pu.SymbolListing:
    return pu.SymbolListing(
        symbol=sym,
        listed_from=0,
        delisted_at=(100 if delisted else None),
        status=("dead" if delisted else "live"),
    )


def test_stratified_sample_respects_n_and_represents_both_strata():
    listings = [_mk(f"D{i:02d}", delisted=True) for i in range(10)] + [
        _mk(f"L{i:02d}") for i in range(30)
    ]
    sel = b.stratified_sample(listings, 12)
    assert len(sel) == 12
    assert sum(1 for x in sel if x.delisted_at is not None) >= 1  # delisted present
    assert sum(1 for x in sel if x.delisted_at is None) >= 1  # active present


def test_stratified_sample_favors_scarce_delisted_stratum():
    # delisted symbols are the survivorship-critical, scarcer stratum -> >= half of n
    listings = [_mk(f"D{i:02d}", delisted=True) for i in range(20)] + [
        _mk(f"L{i:02d}") for i in range(20)
    ]
    sel = b.stratified_sample(listings, 10)
    assert sum(1 for x in sel if x.delisted_at is not None) >= 5


def test_stratified_sample_is_deterministic():
    listings = [_mk(f"D{i:02d}", delisted=True) for i in range(20)] + [
        _mk(f"L{i:02d}") for i in range(20)
    ]
    a = [x.symbol for x in b.stratified_sample(listings, 8)]
    c = [x.symbol for x in b.stratified_sample(listings, 8)]
    assert a == c
    assert len(set(a)) == len(a)  # no duplicates


def test_stratified_sample_n_ge_total_or_zero_returns_all():
    listings = [_mk("D0", delisted=True), _mk("L0")]
    assert len(b.stratified_sample(listings, 99)) == 2
    assert len(b.stratified_sample(listings, 0)) == 2


def test_stratified_sample_backfills_when_one_stratum_short():
    # only 1 delisted available -> remaining slots backfill from active, n still honored
    listings = [_mk("D0", delisted=True)] + [_mk(f"L{i:02d}") for i in range(20)]
    sel = b.stratified_sample(listings, 10)
    assert len(sel) == 10
    assert sum(1 for x in sel if x.delisted_at is not None) == 1


# --------------------------------------------------------------------------- #
# ROB-362 PR1 — resumable progress checkpoint (crash-safe, skip-completed)
# --------------------------------------------------------------------------- #
def test_progress_roundtrip(tmp_path):
    p = tmp_path / "discovery" / "rob356" / "_progress.jsonl"
    b.append_progress(p, {"symbol": "AAA", "feature_rows": 5})
    b.append_progress(p, {"symbol": "BBB", "feature_rows": 9})
    recs = b.load_progress(p)
    assert [r["symbol"] for r in recs] == ["AAA", "BBB"]
    assert b.completed_symbols(recs) == {"AAA", "BBB"}


def test_load_progress_missing_file_is_empty(tmp_path):
    assert b.load_progress(tmp_path / "nope.jsonl") == []


def test_load_progress_tolerates_torn_final_line(tmp_path):
    # a crash mid-write leaves a partial last line; resume must not choke on it
    p = tmp_path / "_progress.jsonl"
    p.write_text('{"symbol": "AAA"}\n{"symbol": "BBB"}\n{"symbol": "CC')
    recs = b.load_progress(p)
    assert [r["symbol"] for r in recs] == ["AAA", "BBB"]
