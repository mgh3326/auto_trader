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

import pytest


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


def test_run_wrong_hash_fails_before_session_factory_constructed(monkeypatch):
    """Captain test-matrix review (2026-07-18): the hash-mismatch branch
    specifically (distinct from the campaign-run-id-mismatch branch above)
    must ALSO fail closed before any session is constructed."""
    import app.core.db

    def _poisoned_session_factory(*args, **kwargs):
        raise AssertionError(
            "AsyncSessionLocal must never be constructed when the hash gate fails"
        )

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", _poisoned_session_factory)

    from run_rob940_empirical_materializer import main

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = main(
            [
                "--run",
                "--expected-full-campaign-hash",
                "0" * 64,  # deliberately wrong
                "--campaign-run-id",
                "irrelevant-since-hash-check-fires-first",
                "--output-dir",
                "/tmp/rob960-cli-test-should-not-be-created-3",
            ]
        )
    assert exit_code == 4
    assert "full_campaign_hash mismatch" in buf.getvalue()


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


# ---------------------------------------------------------------------------
# Captain pre-verify convergence gate (2026-07-18): H4 gate-order parity
# (24-unique-experiment-ID cross-check before any session), H6 typed-error
# classification (exit 4, never swallowed into the generic exit-6 bucket),
# and an explicit orchestration-order/failure spy matrix. Every DB
# interaction below is a pure in-memory fake/spy -- zero real connection/
# query/write.
# ---------------------------------------------------------------------------


def test_run_preflight_checks_24_unique_experiment_ids_before_session_factory_constructed(
    monkeypatch,
):
    import app.core.db

    def _poisoned_session_factory(*args, **kwargs):
        raise AssertionError(
            "AsyncSessionLocal must never be constructed when the 24-experiment-ID "
            "gate fails"
        )

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", _poisoned_session_factory)

    import run_rob944_campaign

    monkeypatch.setattr(
        run_rob944_campaign, "_derive_experiment_ids", lambda rows: ["forged"] * 24
    )

    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from run_rob940_empirical_materializer import main
    from run_rob944_campaign import _derive_primary_campaign_run_id

    envelope = build_production_frozen_campaign_envelope()
    full_hash = envelope.full_campaign_hash()
    campaign_run_id = _derive_primary_campaign_run_id(full_hash)

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
                "/tmp/rob960-cli-test-should-not-be-created-2",
            ]
        )
    assert exit_code == 4


class _OrderSpySession:
    def __init__(self, events):
        self._events = events

    async def rollback(self):
        self._events.append("rollback")

    async def commit(self):
        self._events.append("commit")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeCompleteOutcome:
    class _FakeReport:
        def model_dump(self):
            return {"verdict": "complete"}

    report = _FakeReport()
    attempt_evidence = []
    walkforward_results = {"S1": object(), "S2": object()}
    strategies_evidence = {"S1": {}, "S2": {}}
    empirical_success = True


def _wire_common_fakes(monkeypatch, events, *, session):
    import app.core.db

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", lambda: session)

    async def _fake_orch(session_arg, controller, **kwargs):
        return _FakeCompleteOutcome()

    import rob960_empirical_orchestrator

    monkeypatch.setattr(
        rob960_empirical_orchestrator,
        "run_empirical_campaign_with_capture",
        _fake_orch,
    )

    import run_rob944_campaign

    monkeypatch.setattr(
        run_rob944_campaign, "_import_campaign_controller", lambda: object()
    )
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")


def _pinned_run_args(output_dir):
    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from run_rob944_campaign import _derive_primary_campaign_run_id

    envelope = build_production_frozen_campaign_envelope()
    full_hash = envelope.full_campaign_hash()
    campaign_run_id = _derive_primary_campaign_run_id(full_hash)
    return [
        "--run",
        "--expected-full-campaign-hash",
        full_hash,
        "--campaign-run-id",
        campaign_run_id,
        "--output-dir",
        str(output_dir),
    ]


def test_successful_run_orders_build_scorecard_stage_commit_publish_correctly(
    monkeypatch, tmp_path
):
    """build_scorecard exactly once; render+stage both happen BEFORE commit;
    publish happens AFTER commit; success exits 0 (empirical_success=True)."""
    events = []
    session = _OrderSpySession(events)
    _wire_common_fakes(monkeypatch, events, session=session)

    def _fake_build_scorecard(**kwargs):
        events.append("build_scorecard")
        return {"scorecard_artifact_hash": "hash", "scorecard_payload": {}}

    def _fake_render_markdown(envelope):
        events.append("render_markdown")
        return "# md"

    def _fake_stage(envelope, markdown, output_dir):
        events.append("stage")
        return output_dir.parent / ".staging-fake"

    def _fake_publish(staging_dir, output_dir):
        events.append("publish")
        return output_dir / "scorecard.json", output_dir / "scorecard.md"

    import rob945_scorecard

    monkeypatch.setattr(rob945_scorecard, "build_scorecard", _fake_build_scorecard)
    monkeypatch.setattr(rob945_scorecard, "render_markdown", _fake_render_markdown)

    import rob960_scorecard_writer

    monkeypatch.setattr(rob960_scorecard_writer, "stage_scorecard_files", _fake_stage)
    monkeypatch.setattr(
        rob960_scorecard_writer, "publish_staged_scorecard", _fake_publish
    )

    from run_rob940_empirical_materializer import main

    output_dir = tmp_path / "out"
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        exit_code = main(_pinned_run_args(output_dir))

    assert exit_code == 0
    assert events.count("build_scorecard") == 1
    assert events == [
        "build_scorecard",
        "render_markdown",
        "stage",
        "commit",
        "publish",
    ]
    assert "rollback" not in events


def test_scorecard_stage_failure_rolls_back_never_commits(monkeypatch, tmp_path):
    events = []
    session = _OrderSpySession(events)
    _wire_common_fakes(monkeypatch, events, session=session)

    import rob945_scorecard

    monkeypatch.setattr(
        rob945_scorecard,
        "build_scorecard",
        lambda **kwargs: {"scorecard_artifact_hash": "h", "scorecard_payload": {}},
    )
    monkeypatch.setattr(rob945_scorecard, "render_markdown", lambda envelope: "# md")

    import rob960_scorecard_writer

    def _failing_stage(envelope, markdown, output_dir):
        raise OSError("simulated staging failure")

    monkeypatch.setattr(
        rob960_scorecard_writer, "stage_scorecard_files", _failing_stage
    )

    from run_rob940_empirical_materializer import main

    output_dir = tmp_path / "out"
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
        exit_code = main(_pinned_run_args(output_dir))

    assert exit_code == 6
    assert "commit" not in events
    assert events.count("rollback") == 1
    assert not output_dir.exists()


def test_commit_failure_rolls_back_exit_7_no_final_files(monkeypatch, tmp_path):
    events = []

    class _CommitFailingSession(_OrderSpySession):
        async def commit(self):
            events.append("commit-attempt")
            raise RuntimeError("simulated commit failure")

    session = _CommitFailingSession(events)
    _wire_common_fakes(monkeypatch, events, session=session)

    import rob945_scorecard

    monkeypatch.setattr(
        rob945_scorecard,
        "build_scorecard",
        lambda **kwargs: {"scorecard_artifact_hash": "h", "scorecard_payload": {}},
    )
    monkeypatch.setattr(rob945_scorecard, "render_markdown", lambda envelope: "# md")

    import rob960_scorecard_writer

    publish_calls = []

    monkeypatch.setattr(
        rob960_scorecard_writer,
        "stage_scorecard_files",
        lambda envelope, markdown, output_dir: output_dir.parent / ".staging-fake",
    )
    monkeypatch.setattr(
        rob960_scorecard_writer,
        "publish_staged_scorecard",
        lambda staging_dir, output_dir: publish_calls.append(1),
    )

    from run_rob940_empirical_materializer import main

    output_dir = tmp_path / "out"
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
        exit_code = main(_pinned_run_args(output_dir))

    assert exit_code == 7
    assert "commit-attempt" in events
    assert events.count("rollback") == 1
    assert publish_calls == []  # publish never reached
    assert not output_dir.exists()


def test_publish_failure_after_commit_does_not_roll_back_exit_6_staging_preserved(
    monkeypatch, tmp_path
):
    """The DB is ALREADY durable once commit succeeds -- a publish failure
    must NOT roll back (there's nothing to undo), must exit via the
    generic-unexpected-error bucket (6, never falsely claiming a
    rollback), and must leave the staged pair in place for forensic
    recovery."""
    events = []
    session = _OrderSpySession(events)
    _wire_common_fakes(monkeypatch, events, session=session)

    import rob945_scorecard

    monkeypatch.setattr(
        rob945_scorecard,
        "build_scorecard",
        lambda **kwargs: {"scorecard_artifact_hash": "h", "scorecard_payload": {}},
    )
    monkeypatch.setattr(rob945_scorecard, "render_markdown", lambda envelope: "# md")

    import rob960_scorecard_writer

    staging_marker = tmp_path / ".staging-fake"
    staging_marker.mkdir()

    monkeypatch.setattr(
        rob960_scorecard_writer,
        "stage_scorecard_files",
        lambda envelope, markdown, output_dir: staging_marker,
    )

    def _failing_publish(staging_dir, output_dir):
        events.append("publish-attempt")
        raise OSError("simulated publish failure after a successful commit")

    monkeypatch.setattr(
        rob960_scorecard_writer, "publish_staged_scorecard", _failing_publish
    )

    from run_rob940_empirical_materializer import main

    output_dir = tmp_path / "out"
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
        exit_code = main(_pinned_run_args(output_dir))

    assert exit_code == 6
    assert "commit" in events
    assert "rollback" not in events  # nothing to roll back -- commit already durable
    assert "publish-attempt" in events
    assert staging_marker.exists()  # preserved for forensic recovery
    assert "rolled back" not in buf_err.getvalue()  # never falsely claim a rollback
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "exc_name",
    [
        "CampaignHashDriftError",
        "CampaignBatchValidationError",
        "CampaignAccountingIncompleteError",
        "CampaignRunIdDerivationError",
        "RunIdentityMismatchError",
    ],
)
def test_h6_typed_validation_exceptions_roll_back_exit_4_never_generic_6(
    monkeypatch, tmp_path, exc_name
):
    """Captain typed-error-classification correction: H6's own 5 typed
    exceptions must roll back and exit 4 (H4's own preflight/orchestration-
    failure code), never fall through to the generic exit-6 bucket."""
    import app.services.rob944_campaign_controller as controller_mod

    exc_class = getattr(controller_mod, exc_name)

    events = []
    session = _OrderSpySession(events)

    import app.core.db

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", lambda: session)

    async def _raising_orch(session_arg, controller, **kwargs):
        raise exc_class("simulated H6 typed validation failure")

    import rob960_empirical_orchestrator

    monkeypatch.setattr(
        rob960_empirical_orchestrator,
        "run_empirical_campaign_with_capture",
        _raising_orch,
    )

    import run_rob944_campaign

    monkeypatch.setattr(
        run_rob944_campaign, "_import_campaign_controller", lambda: object()
    )
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")

    from run_rob940_empirical_materializer import main

    output_dir = tmp_path / "out"
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
        exit_code = main(_pinned_run_args(output_dir))

    assert exit_code == 4
    assert events == ["rollback"]
    assert exc_name in buf_err.getvalue()
    assert not output_dir.exists()


# ---------------------------------------------------------------------------
# Captain same-pass checklist follow-up (2026-07-18): the 24-ID gate's
# len/duplicate branch (distinct from the recomputation-divergence branch
# already covered), bridge-unavailable/opt-in-false session-factory-zero
# evidence at the CLI layer specifically, and empirical_success=False exit-5
# preservation through a full (non-empirical-success) publish.
# ---------------------------------------------------------------------------


def test_run_preflight_rejects_duplicate_experiment_ids_before_session_factory_constructed(
    monkeypatch,
):
    """Exercises len(experiment_ids) != 24 or len(set(...)) != 24 --
    distinct from the recomputation-divergence branch already covered by
    test_run_preflight_checks_24_unique_experiment_ids_before_session_factory_constructed."""
    import app.core.db

    def _poisoned_session_factory(*args, **kwargs):
        raise AssertionError(
            "AsyncSessionLocal must never be constructed when the 24-unique-ID "
            "count/duplicate gate fails"
        )

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", _poisoned_session_factory)

    from rob944_frozen_campaign import build_production_frozen_campaign_envelope

    real_envelope = build_production_frozen_campaign_envelope()
    real_hash = real_envelope.full_campaign_hash()
    real_plain = real_envelope.to_dict()
    rigged_ids = list(real_plain["experiment_ids"])
    rigged_ids[1] = rigged_ids[0]  # force a duplicate -> 24 total, 23 unique
    rigged_plain = dict(real_plain)
    rigged_plain["experiment_ids"] = rigged_ids

    class _FakeEnvelope:
        def full_campaign_hash(self):
            return real_hash

        def to_dict(self):
            return rigged_plain

    import rob944_frozen_campaign

    monkeypatch.setattr(
        rob944_frozen_campaign,
        "build_production_frozen_campaign_envelope",
        lambda: _FakeEnvelope(),
    )

    from run_rob940_empirical_materializer import main
    from run_rob944_campaign import _derive_primary_campaign_run_id

    campaign_run_id = _derive_primary_campaign_run_id(real_hash)

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = main(
            [
                "--run",
                "--expected-full-campaign-hash",
                real_hash,
                "--campaign-run-id",
                campaign_run_id,
                "--output-dir",
                "/tmp/rob960-cli-test-should-not-be-created-4",
            ]
        )
    assert exit_code == 4
    assert "24 unique experiment IDs" in buf.getvalue()


def test_run_opt_in_not_set_fails_before_session_factory_constructed_at_cli(
    monkeypatch,
):
    import app.core.db

    def _poisoned_session_factory(*args, **kwargs):
        raise AssertionError(
            "AsyncSessionLocal must never be constructed when opt-in is unset"
        )

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", _poisoned_session_factory)
    monkeypatch.delenv("ROB944_RESEARCH_WRITE_OPT_IN", raising=False)

    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from run_rob940_empirical_materializer import main
    from run_rob944_campaign import _derive_primary_campaign_run_id

    envelope = build_production_frozen_campaign_envelope()
    full_hash = envelope.full_campaign_hash()
    campaign_run_id = _derive_primary_campaign_run_id(full_hash)

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
                "/tmp/rob960-cli-test-should-not-be-created-5",
            ]
        )
    assert exit_code == 2


def test_run_bridge_unavailable_fails_before_session_factory_constructed_at_cli(
    monkeypatch,
):
    import app.core.db

    def _poisoned_session_factory(*args, **kwargs):
        raise AssertionError(
            "AsyncSessionLocal must never be constructed when the H6 bridge is "
            "unavailable"
        )

    monkeypatch.setattr(app.core.db, "AsyncSessionLocal", _poisoned_session_factory)
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")

    import run_rob944_campaign

    def _raise_import_error():
        raise ImportError("simulated H6 bridge unavailable")

    monkeypatch.setattr(
        run_rob944_campaign, "_import_campaign_controller", _raise_import_error
    )

    from rob944_frozen_campaign import build_production_frozen_campaign_envelope
    from run_rob940_empirical_materializer import main
    from run_rob944_campaign import _derive_primary_campaign_run_id

    envelope = build_production_frozen_campaign_envelope()
    full_hash = envelope.full_campaign_hash()
    campaign_run_id = _derive_primary_campaign_run_id(full_hash)

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
                "/tmp/rob960-cli-test-should-not-be-created-6",
            ]
        )
    assert exit_code == 2


def test_empirical_success_false_still_completes_publish_and_exits_5(
    monkeypatch, tmp_path
):
    """H4's own 0/5 empirical-success convention (accounting complete AND
    scorecard published, but not every primary attempt completed) must
    survive unchanged: commit + publish both happen, no rollback, exit 5."""
    events = []
    session = _OrderSpySession(events)

    class _FakeIncompleteOutcome(_FakeCompleteOutcome):
        empirical_success = False

    _wire_common_fakes(monkeypatch, events, session=session)

    import rob960_empirical_orchestrator

    async def _fake_orch(session_arg, controller, **kwargs):
        return _FakeIncompleteOutcome()

    monkeypatch.setattr(
        rob960_empirical_orchestrator, "run_empirical_campaign_with_capture", _fake_orch
    )

    import rob945_scorecard

    monkeypatch.setattr(
        rob945_scorecard,
        "build_scorecard",
        lambda **kwargs: {"scorecard_artifact_hash": "h", "scorecard_payload": {}},
    )
    monkeypatch.setattr(rob945_scorecard, "render_markdown", lambda envelope: "# md")

    import rob960_scorecard_writer

    monkeypatch.setattr(
        rob960_scorecard_writer,
        "stage_scorecard_files",
        lambda envelope, markdown, output_dir: output_dir.parent / ".staging-fake",
    )
    monkeypatch.setattr(
        rob960_scorecard_writer,
        "publish_staged_scorecard",
        lambda staging_dir, output_dir: (
            output_dir / "scorecard.json",
            output_dir / "scorecard.md",
        ),
    )

    from run_rob940_empirical_materializer import main

    output_dir = tmp_path / "out"
    buf_out = io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(io.StringIO()):
        exit_code = main(_pinned_run_args(output_dir))

    assert exit_code == 5
    assert "commit" in events
    assert "rollback" not in events
    summary = json.loads(buf_out.getvalue())
    assert summary["empirical_success"] is False
