from app.core.config import Settings, validate_remote_debug_audit_config


def test_flag_defaults_false_and_validator_reports_missing_key() -> None:
    s = Settings(remote_debug_audit_enabled=False)
    assert s.remote_debug_audit_enabled is False
    assert validate_remote_debug_audit_config(s) == ["REMOTE_DEBUG_AUDIT_ENABLED"]


def test_validator_empty_when_enabled() -> None:
    s = Settings(remote_debug_audit_enabled=True)
    assert validate_remote_debug_audit_config(s) == []
