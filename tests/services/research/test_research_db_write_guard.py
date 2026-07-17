"""ROB-946 (H6) — research DB write guard: RED-first coverage.

The guard is the FIRST of two independent conditions ROB-946 §7 requires
before any registry write primitive fires: (1) explicit default-off opt-in,
(2) a typed/allowlisted non-production research DB target. Neither condition
is derived from ``ENVIRONMENT != "production"``, a URL substring, or an
unknown/empty/malformed/deceptive target — every one of those must fail
closed. The guard is pure/dependency-injected: it never reads ``os.environ``
or a live DB connection itself, so it is trivially testable and cannot be
fooled by ambient process state.
"""

from __future__ import annotations

import pytest

from app.services.research_db_write_guard import (
    ResearchDbTarget,
    ResearchDbTargetRejected,
    ResearchWriteDisabled,
    assert_research_write_authorized,
    research_write_opt_in_enabled,
    resolve_research_db_target,
)

_ALLOWLIST = frozenset({"test_db"})


def test_opt_in_disabled_rejects_even_with_allowlisted_target():
    with pytest.raises(ResearchWriteDisabled):
        assert_research_write_authorized(
            opt_in_enabled=False,
            target=ResearchDbTarget(database_name="test_db"),
            allowlist=_ALLOWLIST,
        )


def test_opt_in_enabled_and_allowlisted_target_is_authorized():
    assert_research_write_authorized(
        opt_in_enabled=True,
        target=ResearchDbTarget(database_name="test_db"),
        allowlist=_ALLOWLIST,
    )  # must not raise


def test_opt_in_enabled_but_unlisted_target_rejected():
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(database_name="auto_trader_production"),
            allowlist=_ALLOWLIST,
        )


def test_empty_database_name_rejected_even_when_opted_in():
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(database_name=""),
            allowlist=_ALLOWLIST,
        )


def test_deceptive_substring_target_is_not_accepted_by_substring_match():
    # "nottest_db" and "test_db_2" both CONTAIN the allowlisted name as a
    # substring; only an EXACT match may pass — proves the guard is not doing
    # substring/prefix matching that a deceptive name could slip through.
    for deceptive in ("nottest_db", "test_db_2", "TEST_DB"):
        with pytest.raises(ResearchDbTargetRejected):
            assert_research_write_authorized(
                opt_in_enabled=True,
                target=ResearchDbTarget(database_name=deceptive),
                allowlist=_ALLOWLIST,
            )


def test_unknown_target_never_defaults_to_authorized():
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(database_name="some_unknown_db"),
            allowlist=_ALLOWLIST,
        )


@pytest.mark.parametrize("raw", ["1", "true", "True", "TRUE", "yes", "on"])
def test_opt_in_truthy_values(raw):
    assert research_write_opt_in_enabled(raw) is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "False", "no", "off", "nope"])
def test_opt_in_falsy_values(raw):
    assert research_write_opt_in_enabled(raw) is False


def test_resolve_research_db_target_reads_actual_session_bind_not_a_caller_label():
    class _FakeURL:
        database = "test_db"

    class _FakeBind:
        url = _FakeURL()

    class _FakeSession:
        def get_bind(self):
            return _FakeBind()

    target = resolve_research_db_target(_FakeSession())
    assert target == ResearchDbTarget(database_name="test_db")


def test_error_messages_never_contain_dsn_or_password_shaped_text():
    try:
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(database_name="production"),
            allowlist=_ALLOWLIST,
        )
    except ResearchDbTargetRejected as exc:
        message = str(exc)
        assert "://" not in message
        assert "password" not in message.lower()
