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
