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
