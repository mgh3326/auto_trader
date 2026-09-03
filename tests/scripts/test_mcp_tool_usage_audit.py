from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from scripts import mcp_tool_usage_audit as audit


def test_classification_is_fail_closed_and_three_axis() -> None:
    empty_refs = {"prompt_refs": [], "runbook_refs": [], "code_refs": []}
    assert audit.classify({"measurement_status": "unknown"}, empty_refs) == "U"
    assert (
        audit.classify(
            {"measurement_status": "ok", "calls_total": 3, "calls_last_30d": 1},
            empty_refs,
        )
        == "A"
    )
    assert (
        audit.classify(
            {"measurement_status": "ok", "calls_total": 3, "calls_last_30d": 0},
            empty_refs,
        )
        == "B"
    )
    assert (
        audit.classify(
            {"measurement_status": "ok", "calls_total": 0, "calls_last_30d": 0},
            {**empty_refs, "prompt_refs": ["x:1"]},
        )
        == "C"
    )
    assert (
        audit.classify(
            {"measurement_status": "ok", "calls_total": 0, "calls_last_30d": 0},
            empty_refs,
        )
        == "D"
    )
    assert audit.mutation_kinds("investment_stage_artifacts_ingest_from_hermes") == [
        "report"
    ]
    assert audit.mutation_kinds("paper_validation_advance") == ["order"]


def test_exact_token_references_do_not_match_identifier_substrings(
    tmp_path: Path,
) -> None:
    operator = tmp_path / "operator"
    (operator / "prompts").mkdir(parents=True)
    (operator / "prompts" / "one.md").write_text("Call get_quote, not get_quotes.\n")
    repo = tmp_path / "repo"
    (repo / "docs" / "runbooks").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "app" / "flows").mkdir(parents=True)
    (repo / "app" / "tasks").mkdir()
    refs = audit.find_exact_references(
        ["get_quote"], operator_repo=operator, repo_root=repo
    )
    assert refs["get_quote"]["prompt_refs"] == [
        str(operator / "prompts" / "one.md") + ":1"
    ]


def test_sentry_failure_is_unknown_not_zero() -> None:
    def fail(*_args, **_kwargs):
        raise audit.requests.RequestException("network")

    result = audit.collect_usage_90d(
        ["get_quote"],
        token="redacted",
        now=datetime(2026, 9, 3, tzinfo=UTC),
        request_get=fail,
    )
    assert result["get_quote"]["measurement_status"] == "unknown"
    assert result["get_quote"]["calls_total"] is None


def test_sentry_rows_preserve_server_and_last_called() -> None:
    rows = [
        {
            "transaction": "tools/call get_quote",
            "server_name": "mbp-server",
            "count()": 2,
            "p50(span.duration)": 10,
            "max(timestamp)": "2026-09-01T00:00:00Z",
        },
        {
            "transaction": "tools/call get_quote",
            "server_name": "vm-naver",
            "count()": 3,
            "p50(span.duration)": 20,
            "max(timestamp)": "2026-09-02T00:00:00Z",
        },
    ]
    summary = audit.summarize_rows(rows)["get_quote"]
    assert summary["calls_total"] == 5
    assert summary["p50_ms"] == 16.0
    assert summary["last_called_at"] == "2026-09-02T00:00:00+00:00"
    assert summary["callers"] == [
        {"server_name": "mbp-server", "calls": 2},
        {"server_name": "vm-naver", "calls": 3},
    ]


def test_sentry_pagination_stops_when_next_cursor_has_no_results() -> None:
    class Response:
        status_code = 200
        headers = {
            "Link": '<https://example.test/?cursor=abc>; rel="next"; results="false"'
        }
        links = {"next": {"url": "https://example.test/?cursor=abc"}}

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, list[dict[str, object]]]:
            return {"data": []}

    calls = 0

    def get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response()

    assert (
        audit.fetch_sentry_rows(
            token="redacted",
            start=datetime(2026, 6, 1, tzinfo=UTC),
            end=datetime(2026, 6, 2, tzinfo=UTC),
            request_get=get,
        )
        == []
    )
    assert calls == 1


def test_lane_allowlist_records_prompt_sentry_and_both(
    tmp_path: Path, monkeypatch
) -> None:
    operator = tmp_path / "operator"
    (operator / "prompts").mkdir(parents=True)
    (operator / "CLAUDE.md").write_text("get_quote\n")
    registry = {
        "get_quote": {"profiles": ["default"]},
        "get_holdings": {"profiles": ["default"]},
    }
    usage = {
        "get_quote": {
            "classification": "A",
            "callers": [{"server_name": "vm-naver-20260820095006"}],
        },
        "get_holdings": {
            "classification": "A",
            "callers": [{"server_name": "vm-naver-20260820095006"}],
        },
    }
    monkeypatch.setattr(
        audit,
        "LANE_SPECS",
        (
            audit.LaneSpec(
                "test", ("default",), ("CLAUDE.md",), ("vm-naver-20260820095006",)
            ),
        ),
    )
    lanes, unlisted = audit.build_lane_allowlists(
        registry, usage, operator_repo=operator
    )
    assert lanes["test"] == {"get_holdings": "sentry", "get_quote": "both"}
    assert unlisted["default"] == []
