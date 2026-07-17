"""ROB-960 materializer CLI tests.

--plan is proven pure (never touches app.*). --run's preflight is proven to
fail closed BEFORE any DB session factory is even constructed (captain
plan-gate G4 spy discipline), and a missing strategies_evidence is proven
to roll back rather than commit (captain plan-gate G9) -- zero real DB
connection/query/write anywhere in this file.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys


def test_plan_flag_is_pure_and_matches_materializer_plan():
    from rob960_scorecard_writer import build_materializer_plan
    from run_rob940_empirical_materializer import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = main(["--plan"])
    assert exit_code == 0
    assert json.loads(buf.getvalue()) == build_materializer_plan()


def test_plan_flag_never_imports_app_core_db(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.core.db", None)  # poison the import
    from run_rob940_empirical_materializer import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = main(["--plan"])
    assert exit_code == 0


def test_run_requires_all_three_flags():
    from run_rob940_empirical_materializer import main

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = main(["--run", "--expected-full-campaign-hash", "0" * 64])
    assert exit_code == 2
    assert "--campaign-run-id" in buf.getvalue()


def test_run_preflight_fails_before_session_factory_constructed(monkeypatch):
    """G4: a wrong --campaign-run-id must fail closed via
    _run_precheck_bridge_and_opt_in/hash-derivation checks BEFORE
    AsyncSessionLocal is ever imported/called."""
    import app.core.db

    def _poisoned_session_factory(*args, **kwargs):
        raise AssertionError(
            "AsyncSessionLocal must never be constructed when preflight fails"
        )

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", _poisoned_session_factory)

    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from run_rob940_empirical_materializer import main

    full_hash = build_production_frozen_campaign_envelope().full_campaign_hash()

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = main(
            [
                "--run",
                "--expected-full-campaign-hash",
                full_hash,
                "--campaign-run-id",
                "not-the-real-derived-value",
                "--output-dir",
                "/tmp/rob960-cli-test-should-not-be-created",
            ]
        )
    assert exit_code == 4


def test_run_with_missing_strategies_evidence_rolls_back_never_commits(
    monkeypatch, tmp_path
):
    """G9: when the orchestrator reports strategies_evidence=None (global
    corpus/gap/PBO failure), the CLI must roll back -- never commit, never
    write scorecard files."""

    class _FakeOutcome:
        strategies_evidence = None
        empirical_success = False

    class _SpySession:
        def __init__(self):
            self.rolled_back = False
            self.committed = False

        async def rollback(self):
            self.rolled_back = True

        async def commit(self):
            self.committed = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    spy_session = _SpySession()

    class _FakeSessionFactory:
        def __call__(self):
            return spy_session

    import app.core.db

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", _FakeSessionFactory())

    async def _fake_run_empirical_campaign_with_capture(session, controller, **kwargs):
        return _FakeOutcome()

    import rob960_empirical_orchestrator

    monkeypatch.setattr(
        rob960_empirical_orchestrator,
        "run_empirical_campaign_with_capture",
        _fake_run_empirical_campaign_with_capture,
    )

    class _FakeController:
        pass

    import run_rob944_campaign

    monkeypatch.setattr(
        run_rob944_campaign, "_import_campaign_controller", lambda: _FakeController()
    )

    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from run_rob940_empirical_materializer import main
    from run_rob944_campaign import _derive_primary_campaign_run_id

    envelope = build_production_frozen_campaign_envelope()
    full_hash = envelope.full_campaign_hash()
    campaign_run_id = _derive_primary_campaign_run_id(full_hash)

    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    output_dir = tmp_path / "out"

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = main(
            [
                "--run",
                "--expected-full-campaign-hash",
                full_hash,
                "--campaign-run-id",
                campaign_run_id,
                "--output-dir",
                str(output_dir),
            ]
        )

    assert exit_code == 6
    assert spy_session.rolled_back is True
    assert spy_session.committed is False
    assert not output_dir.exists()
