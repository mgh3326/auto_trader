from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import perf_compare_sentry as script


def _fixture() -> dict:
    return json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "sentry"
            / "perf_compare_events.json"
        ).read_text()
    )


def test_build_query_is_spans_tools_call_and_grouped_by_server() -> None:
    window = script.parse_window("--before", "2026-08-20..2026-09-02T23:48+09")
    params = script.build_query_params(window, "server_name")

    assert ("dataset", "spans") in params
    assert ("query", 'transaction:"tools/call *"') in params
    assert ("field", "server_name") in params
    assert ("field", "p50(span.duration)") in params
    assert ("field", "p95(span.duration)") in params


def test_main_renders_fixture_and_marks_small_samples_insufficient(
    monkeypatch, capsys
) -> None:
    fixture = _fixture()
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "test-token")

    def fake_get(_url: str, *, params, headers, timeout: int):
        assert headers == {"Authorization": "Bearer test-token"}
        assert timeout == script.REQUEST_TIMEOUT_SECONDS
        payload = (
            fixture["before"]
            if ("start", "2026-08-20T00:00:00+00:00") in params
            else fixture["after"]
        )
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: payload,
        )

    monkeypatch.setattr(script.requests, "get", fake_get)
    assert script.main([]) == 0

    report = capsys.readouterr().out
    assert "tools/call analyze_stock_batch" in report
    assert "n=8; p50=6100.0ms; p95=7300.0ms" in report
    assert "n=9; p50=2600.0ms; p95=3400.0ms" in report
    assert "+57.4%" in report
    assert "tools/call get_quote" in report
    assert "부족 (n=3)" in report


def test_main_fails_closed_without_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)

    assert script.main([]) == 2
    assert "SENTRY_AUTH_TOKEN is required" in capsys.readouterr().err


def test_order_path_group_is_supported() -> None:
    before = script.parse_window("--before", "2026-08-20..2026-09-02T23:48+09")
    after = script.parse_window("--after", "2026-09-02T23:48+09..now")
    rows = {
        ("tools/call place_order", "edge"): script.Summary(count=5, p50=10, p95=20),
    }

    report = script.render_report(
        before=before,
        after=after,
        before_rows=rows,
        after_rows=rows,
        group="order.path",
    )

    assert "| tools/call place_order | edge |" in report
    assert "+0.0%" in report
