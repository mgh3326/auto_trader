from __future__ import annotations

from research.kr_corpus.pacing import RequestProjection


def test_projection_includes_conservative_pykrx_session_refresh_allowance():
    projection = RequestProjection(
        requests_already_observed=11,
        session_count=2,
        markets_count=2,
        lifecycle_master_upper_bound=3,
        max_wall_clock_hours=12,
    )

    assert projection.membership_requests == 4
    assert projection.ohlcv_requests == 3
    assert projection.maximum_session_refresh_requests == 36
    assert projection.total == 54
