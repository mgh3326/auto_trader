from __future__ import annotations

from pathlib import Path

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


def test_main_refuses_empty_sentry_api_data_without_writing_or_leaking_token(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    token = "sentry-token-sentinel-must-not-leak"
    calls: list[tuple[object, object]] = []

    class EmptySentryResponse:
        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"data": [], "meta": {"fields": {}, "units": {}}}

    def fake_get(*args: object, **kwargs: object) -> EmptySentryResponse:
        calls.append((args, kwargs))
        return EmptySentryResponse()

    monkeypatch.setattr(script.requests, "get", fake_get)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", token)
    output = tmp_path / "invest-performance.md"

    assert script.main(["--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert "refusing incomplete report" in captured.err
    assert not output.exists()
    assert len(calls) == 4
    assert token not in captured.out
    assert token not in captured.err
