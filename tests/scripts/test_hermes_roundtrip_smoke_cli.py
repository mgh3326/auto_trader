"""ROB-287 Phase C — Hermes round-trip smoke CLI unit tests.

The CLI is exercised against a live auto_trader instance by operators
(see ``docs/runbooks/hermes-report-generation.md`` §Phase C smoke).
These tests stay unit-shaped: they cover the wire-shape glue (fixture
loading, placeholder substitution, token redaction, argparse defaults)
without spawning a real server.
"""

from __future__ import annotations

import json
import uuid

import pytest

from scripts.hermes_roundtrip_smoke import (
    _load_fixture,
    _parse_args,
    _redact_token,
    _substitute_placeholders,
)


def test_load_fixture_strips_comment() -> None:
    """Fixtures carry an ``_comment`` for operator readability; the
    CLI strips it before sending so the wire payload matches the
    Pydantic schema exactly."""
    parsed = _load_fixture("stage_artifacts_request.json")
    assert "_comment" not in parsed
    assert parsed["run_envelope"]["market"] == "kr"


def test_substitute_placeholders_replaces_uuids() -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    payload = _substitute_placeholders(
        _load_fixture("stage_artifacts_request.json"),
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle_uuid,
    )
    assert payload["run_envelope"]["run_uuid"] == str(run_uuid)
    assert payload["run_envelope"]["snapshot_bundle_uuid"] == str(bundle_uuid)
    # The fixture must not leave behind raw braces after substitution.
    serialised = json.dumps(payload)
    assert "{{" not in serialised
    assert "}}" not in serialised


def test_substitute_placeholders_works_on_composition() -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    payload = _substitute_placeholders(
        _load_fixture("composition_request.json"),
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle_uuid,
    )
    assert payload["composition"]["snapshot_bundle_uuid"] == str(bundle_uuid)
    # The composition metadata must reference the stage run for §D4
    # auto-finalize to kick in.
    assert payload["composition"]["metadata"]["investment_stage_run_uuid"] == str(
        run_uuid
    )


def test_substitute_placeholders_threads_report_uuids_into_composition() -> None:
    """ROB-309 — the composition fixture carries {{symbol_report_uuid}} +
    {{dimension_report_uuid}} placeholders, substituted in a second pass once
    the symbol-reports / dimension-reports POSTs return their server UUIDs."""
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    symbol_report_uuid = uuid.uuid4()
    dimension_report_uuid = uuid.uuid4()
    payload = _substitute_placeholders(
        _load_fixture("composition_request.json"),
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle_uuid,
        symbol_report_uuid=symbol_report_uuid,
        dimension_report_uuid=dimension_report_uuid,
    )
    composition = payload["composition"]
    assert composition["symbol_intermediate_report_uuids"] == [str(symbol_report_uuid)]
    assert composition["dimension_report_uuids"] == [str(dimension_report_uuid)]
    # The classified item threads both citations.
    candidate = next(
        it
        for it in composition["items"]
        if it.get("decision_bucket") == "new_buy_candidate"
    )
    assert candidate["cited_symbol_report_uuid"] == str(symbol_report_uuid)
    assert candidate["cited_dimension_report_uuids"] == [str(dimension_report_uuid)]
    # No placeholder braces survive the full substitution.
    serialised = json.dumps(payload)
    assert "{{" not in serialised
    assert "}}" not in serialised


def test_substitute_placeholders_works_on_symbol_and_dimension_fixtures() -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    for name in ("symbol_reports_request.json", "dimension_reports_request.json"):
        payload = _substitute_placeholders(
            _load_fixture(name),
            run_uuid=run_uuid,
            snapshot_bundle_uuid=bundle_uuid,
        )
        assert payload["run_envelope"]["run_uuid"] == str(run_uuid)
        assert payload["run_envelope"]["snapshot_bundle_uuid"] == str(bundle_uuid)
        serialised = json.dumps(payload)
        assert "{{" not in serialised
        assert "}}" not in serialised


@pytest.mark.parametrize(
    ("raw", "expected_substr"),
    [
        ("", "(empty)"),
        ("abcdef", "***"),  # length <= 8 → fully redacted
        ("abcdefghij", "abcd"),  # length > 8 → prefix shown, length disclosed
    ],
)
def test_redact_token_never_prints_full_value(raw: str, expected_substr: str) -> None:
    redacted = _redact_token(raw)
    if raw:
        # Full token must not appear in the redaction output.
        assert raw not in redacted, f"redact leaked the token: {redacted}"
    assert expected_substr in redacted


def test_parse_args_uses_env_token_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator-friendly default: pull HERMES_INGEST_TOKEN from env so
    the smoke command line stays free of the secret."""
    monkeypatch.setenv("HERMES_INGEST_TOKEN", "env-secret-zzz")
    bundle = uuid.uuid4()
    args = _parse_args(
        [
            "--base-url",
            "https://example",
            "--bundle-uuid",
            str(bundle),
        ]
    )
    assert args.token == "env-secret-zzz"
    assert args.token_header == "X-Hermes-Ingest-Token"
    assert args.bundle_uuid == bundle
    assert args.base_url == "https://example"


def test_parse_args_rejects_missing_required_args() -> None:
    """``--base-url`` and ``--bundle-uuid`` are required so the CLI
    never invents bundle UUIDs to keep behaviour predictable."""
    with pytest.raises(SystemExit):
        _parse_args(["--base-url", "https://example"])
    with pytest.raises(SystemExit):
        _parse_args(["--bundle-uuid", str(uuid.uuid4())])


def test_parse_args_defaults_fixture_set_to_kr() -> None:
    """Backwards compatibility — operators who omit --fixture-set keep
    getting the original KR payloads (PR #910 behaviour)."""
    args = _parse_args(
        [
            "--base-url",
            "https://example",
            "--bundle-uuid",
            str(uuid.uuid4()),
        ]
    )
    assert args.fixture_set == "kr"


def test_parse_args_accepts_us_fixture_set() -> None:
    """``--fixture-set us`` switches the CLI to the US narrow smoke
    fixtures pinned to market='us', account_scope='alpaca_paper',
    status='draft'."""
    args = _parse_args(
        [
            "--base-url",
            "https://example",
            "--bundle-uuid",
            str(uuid.uuid4()),
            "--fixture-set",
            "us",
        ]
    )
    assert args.fixture_set == "us"


def test_parse_args_rejects_unknown_fixture_set() -> None:
    """The fixture set is a closed enum — extending it requires a
    code change (deliberate friction)."""
    with pytest.raises(SystemExit):
        _parse_args(
            [
                "--base-url",
                "https://example",
                "--bundle-uuid",
                str(uuid.uuid4()),
                "--fixture-set",
                "kr_or_us_or_zz",
            ]
        )


def test_fixture_by_set_map_locks_payload_files() -> None:
    """Lock the fixture filenames bound to each set so an accidental
    rename can't silently change what the CLI sends on the wire. ROB-309
    extends each set with the symbol-reports + dimension-reports fixtures."""
    from scripts.hermes_roundtrip_smoke import _FIXTURE_BY_SET

    assert _FIXTURE_BY_SET["kr"] == {
        "stage_artifacts": "stage_artifacts_request.json",
        "symbol_reports": "symbol_reports_request.json",
        "dimension_reports": "dimension_reports_request.json",
        "composition": "composition_request.json",
    }
    assert _FIXTURE_BY_SET["us"] == {
        "stage_artifacts": "stage_artifacts_request_us.json",
        "symbol_reports": "symbol_reports_request_us.json",
        "dimension_reports": "dimension_reports_request_us.json",
        "composition": "composition_request_us.json",
    }


def test_parse_args_session_cookie_defaults_empty() -> None:
    """The read-surface GETs need a session cookie; it defaults to empty so
    the ingest chain still runs (read-surface assertions are then skipped)."""
    args = _parse_args(
        [
            "--base-url",
            "https://example",
            "--bundle-uuid",
            str(uuid.uuid4()),
        ]
    )
    assert args.session_cookie == ""


def test_parse_args_session_cookie_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SMOKE_SESSION_COOKIE", "session=abc123")
    args = _parse_args(
        [
            "--base-url",
            "https://example",
            "--bundle-uuid",
            str(uuid.uuid4()),
        ]
    )
    assert args.session_cookie == "session=abc123"
