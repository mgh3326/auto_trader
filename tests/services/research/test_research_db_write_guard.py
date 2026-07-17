"""ROB-946 (H6) — research DB write guard: RED-first coverage.

The guard is the FIRST of two independent conditions ROB-946 §7 requires
before any registry write primitive fires: (1) explicit default-off opt-in,
(2) a typed/authority-owned non-production research DB target. Neither
condition is satisfied by ``ENVIRONMENT != "production"``, a URL substring, a
bare database-name allowlist, or an empty/unknown/malformed/deceptive target
— every one of those must fail closed.

R1 Critical-1 remediation: a bare database name (e.g. ``test_db``) cannot
distinguish a disposable local fixture from a production cluster sharing the
same name. ``ResearchDbTarget`` now binds host+database together, and
authorization requires a positive EXACT match against a ``ResearchDbPolicy``
— a closed, validated set of real (host, database) pairs, not a caller-
supplied ``frozenset[str]`` a bridge caller could self-issue.
"""

from __future__ import annotations

import pytest

from app.services.research_db_write_guard import (
    ResearchDbPolicy,
    ResearchDbTarget,
    ResearchDbTargetRejected,
    ResearchWriteDisabled,
    assert_research_write_authorized,
    default_research_db_policy,
    research_write_opt_in_enabled,
    resolve_research_db_target,
)

_DISPOSABLE_TARGET = ResearchDbTarget(host="localhost", database_name="test_db")
_POLICY = ResearchDbPolicy.of(_DISPOSABLE_TARGET)


def test_opt_in_disabled_rejects_even_with_authorized_target():
    with pytest.raises(ResearchWriteDisabled):
        assert_research_write_authorized(
            opt_in_enabled=False, target=_DISPOSABLE_TARGET, policy=_POLICY
        )


def test_opt_in_enabled_and_positively_matched_target_is_authorized():
    assert_research_write_authorized(
        opt_in_enabled=True, target=_DISPOSABLE_TARGET, policy=_POLICY
    )  # must not raise


def test_production_host_with_allowlisted_bare_database_name_is_rejected():
    # R1 Critical-1 reproduction: the database is named identically to the
    # disposable target, but the HOST is a production cluster — must reject.
    production_target = ResearchDbTarget(
        host="prod-primary.internal.example", database_name="test_db"
    )
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True, target=production_target, policy=_POLICY
        )


def test_staging_host_with_allowlisted_database_name_is_rejected():
    staging_target = ResearchDbTarget(
        host="staging-db.internal.example", database_name="test_db"
    )
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True, target=staging_target, policy=_POLICY
        )


def test_unknown_host_and_database_never_defaults_to_authorized():
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(host="some-unknown-host", database_name="some_db"),
            policy=_POLICY,
        )


def test_correct_host_but_wrong_database_is_rejected():
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(host="localhost", database_name="production"),
            policy=_POLICY,
        )


def test_correct_database_but_missing_host_is_rejected():
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(host="", database_name="test_db"),
            policy=_POLICY,
        )


def test_deceptive_lookalike_host_is_not_accepted_by_substring_match():
    # "localhost.evil.example" and "notlocalhost" both relate textually to
    # "localhost"; only an EXACT match may pass.
    for deceptive_host in ("localhost.evil.example", "notlocalhost", "LOCALHOST."):
        with pytest.raises(ResearchDbTargetRejected):
            assert_research_write_authorized(
                opt_in_enabled=True,
                target=ResearchDbTarget(host=deceptive_host, database_name="test_db"),
                policy=_POLICY,
            )


def test_host_case_and_whitespace_are_normalized_for_a_true_match():
    messy_target = ResearchDbTarget(host="  LOCALHOST  ", database_name="test_db")
    assert_research_write_authorized(
        opt_in_enabled=True, target=messy_target, policy=_POLICY
    )  # must not raise -- normalization, not a security hole


def test_policy_cannot_be_constructed_empty():
    with pytest.raises(ValueError):
        ResearchDbPolicy.of()


def test_policy_rejects_malformed_targets_at_construction():
    with pytest.raises(ValueError):
        ResearchDbPolicy.of(ResearchDbTarget(host="localhost", database_name=""))
    with pytest.raises(ValueError):
        ResearchDbPolicy.of(ResearchDbTarget(host="", database_name="test_db"))


def test_default_research_db_policy_only_authorizes_the_one_known_local_target():
    policy = default_research_db_policy()
    assert policy.authorizes(
        ResearchDbTarget(host="localhost", database_name="test_db")
    )
    assert not policy.authorizes(
        ResearchDbTarget(host="prod-primary.internal.example", database_name="test_db")
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
        host = "localhost"

    class _FakeBind:
        url = _FakeURL()

    class _FakeSession:
        def get_bind(self):
            return _FakeBind()

    target = resolve_research_db_target(_FakeSession())
    assert target == ResearchDbTarget(host="localhost", database_name="test_db")


def test_resolve_research_db_target_on_a_production_looking_bind_carries_that_host_through():
    class _FakeURL:
        database = "test_db"
        host = "prod-primary.internal.example"

    class _FakeBind:
        url = _FakeURL()

    class _FakeSession:
        def get_bind(self):
            return _FakeBind()

    target = resolve_research_db_target(_FakeSession())
    assert target.host == "prod-primary.internal.example"
    with pytest.raises(ResearchDbTargetRejected):
        assert_research_write_authorized(
            opt_in_enabled=True, target=target, policy=_POLICY
        )


def test_error_messages_never_contain_dsn_or_password_shaped_text():
    try:
        assert_research_write_authorized(
            opt_in_enabled=True,
            target=ResearchDbTarget(host="prod.example", database_name="production"),
            policy=_POLICY,
        )
    except ResearchDbTargetRejected as exc:
        message = str(exc)
        assert "://" not in message
        assert "password" not in message.lower()
