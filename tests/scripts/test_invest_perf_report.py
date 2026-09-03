from __future__ import annotations

from scripts import invest_perf_report as script


def test_query_contracts_are_bounded_to_invest_traces_and_rum() -> None:
    window = script.parse_window("2026-09-01T00:00:00+00:00..2026-09-02T00:00:00+00:00")

    assert ("dataset", "spans") in script.latency_query_params(window)
    assert (
        "query",
        'transaction:"GET /invest/api/*" span.op:"http.server"',
    ) in script.latency_query_params(window)
    assert ("dataset", "errors") in script.rum_query_params(window)
    assert ("query", 'message:"invest.rum"') in script.rum_query_params(window)
    assert ("field", "tags[n_requests]") in script.rum_query_params(window)


def test_render_report_has_fanout_latency_and_component_shares() -> None:
    report = script.render_report(
        window=script.Window(start="start", end="end"),
        latency={"GET /invest/api/home": script.Latency(8, 120, 360)},
        db_ms={"GET /invest/api/home": 30},
        ext_ms={"GET /invest/api/home": 70},
        rum={"/invest": [2, 4, 6]},
    )

    assert "| /invest | 3 | 4.0 |" in report
    assert "| GET /invest/api/home | 8 | 120.0 | 360.0 | 30.0% | 70.0% |" in report


def test_report_refuses_empty_rum_samples() -> None:
    try:
        script.render_report(
            window=script.Window(start="start", end="end"),
            latency={"GET /invest/api/home": script.Latency(1, 1, 1)},
            db_ms={},
            ext_ms={},
            rum={},
        )
    except script.SentryQueryError as exc:
        assert "incomplete" in str(exc)
    else:  # pragma: no cover - assertion path
        raise AssertionError("empty RUM must fail closed")
