import uuid

import pytest

from app.core import config
from scripts import remote_debug_audit_smoke as cli


def test_parser_requires_mode() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["--mode", "preflight"])
    assert args.mode == "preflight"


def test_preflight_reports_missing_key_names_only_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings, "remote_debug_audit_enabled", False)
    out = cli.run_preflight()
    assert out["step"] == "preflight"
    assert out["ok"] is False
    assert out["missing_env_keys"] == ["REMOTE_DEBUG_AUDIT_ENABLED"]
    # No values, only key names.
    assert all("=" not in k for k in out["missing_env_keys"])


def test_preflight_ok_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "remote_debug_audit_enabled", True)
    out = cli.run_preflight()
    assert out["ok"] is True
    assert out["missing_env_keys"] == []


def test_audit_mode_requires_a_uuid_arg() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["--mode", "audit"])
    # Neither bundle nor report uuid -> validated in _amain, surfaced as ValueError.
    with pytest.raises(ValueError):
        cli.require_target(args)


def test_build_smoke_output_echoes_target_kind_and_counts() -> None:
    rid, bid = uuid.uuid4(), uuid.uuid4()
    audit = {
        "checked_symbols": 3,
        "symbols_resolved": 2,
        "source": "naver_remote_debug",
    }
    out = cli.build_smoke_output("report", rid, bid, audit)
    assert out["target_kind"] == "report"
    assert out["report_uuid"] == str(rid)
    assert out["bundle_uuid"] == str(bid)
    assert out["checked_symbols"] == 3
    assert out["symbols_resolved"] == 2
    assert out["audit"] is audit


def test_build_smoke_output_bundle_kind_has_null_report_uuid() -> None:
    bid = uuid.uuid4()
    out = cli.build_smoke_output(
        "bundle", bid, bid, {"checked_symbols": 1, "symbols_resolved": 1}
    )
    assert out["target_kind"] == "bundle"
    assert out["report_uuid"] is None
    assert out["bundle_uuid"] == str(bid)


def test_audit_exit_code_zero_only_when_a_symbol_resolved() -> None:
    assert cli.audit_exit_code({"checked_symbols": 5, "symbols_resolved": 1}) == 0
    # Every symbol unresolved -> non-zero so the operator sees it failed,
    # categorized as env/external-page-change (not a report-generation failure).
    assert cli.audit_exit_code({"checked_symbols": 5, "symbols_resolved": 0}) == 3
    assert cli.audit_exit_code({"checked_symbols": 0, "symbols_resolved": 0}) == 3
