"""ROB-1012 regression for empty H3 candidate-buffer aliasing in PBO."""

from __future__ import annotations

from rob974_features import MINUTE_MS, MinuteBar
from rob974_h4_contracts import WINDOW_START_MS

from app.services.rob974_h6b_materializer import ActualH4InputData, ActualMergedH4Runner


def test_actual_h3_pbo_config_chain_allocates_fresh_empty_candidate_buffers() -> None:
    """The corpus-free no-signal path must still allocate per-config buffers.

    H3 deliberately exposes immutable tuples.  CPython canonicalizes every
    empty tuple to the same singleton, so adapting an empty ``accepted`` tuple
    with ``tuple(...)`` aliases consecutive PBO configs even though the H3
    generator itself was invoked afresh.  Running the real H3 -> H2 adapter ->
    PBO fan-out over one incomplete minute per symbol isolates that condition
    without loading the empirical corpus.
    """

    rows = {
        symbol: (MinuteBar(WINDOW_START_MS, 1.0, 1.0, 1.0, 1.0, 1.0),)
        for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
    }
    data = ActualH4InputData.from_mapping(
        rows,
        corpus_end_ts=WINDOW_START_MS + MINUTE_MS,
        persisted_corpus_hash="a" * 64,
        persisted_feature_hash="b" * 64,
    )

    s3, s4 = ActualMergedH4Runner(data)._run_pbo()

    assert (s3.strategy, s3.config_count, s3.day_count, s3.slices) == (
        "S3",
        24,
        365,
        4,
    )
    assert (s4.strategy, s4.config_count, s4.day_count, s4.slices) == (
        "S4",
        24,
        365,
        4,
    )
