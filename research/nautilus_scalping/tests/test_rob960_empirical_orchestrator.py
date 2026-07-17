"""ROB-960 -- capture-wrapped H4+H6 empirical orchestrator tests.

Captain plan-gate G4: every test in this file uses an injected FAKE
controller (an object exposing an async run_full_campaign matching
app.services.rob944_campaign_controller.run_full_campaign's own
signature/contract) and a sentinel `session` object -- no AsyncSessionLocal,
no localhost/test_db, no real asyncpg connection anywhere. This proves
ROB-960's OWN new orchestration logic (capture-wrapping, gap-empty gate,
strategies_evidence assembly, commit-vs-rollback signal) independently of
H6's own (already-tested, untouched, out-of-scope-to-re-test-here) DB
persistence internals.
"""

from __future__ import annotations

import pytest
from rob944_frozen_campaign import build_production_frozen_campaign_envelope
from rob960_empirical_orchestrator import run_empirical_campaign_with_capture
from run_rob944_campaign import RunPreflightError, _derive_primary_campaign_run_id


class _FakeReport:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        return dict(self.__dict__)


def _complete_report(**overrides):
    base = {
        "verdict": "complete",
        "expected_total": 24,
        "actual_registrations": 24,
        "primary_attempts": 24,
        "total_attempts": 24,
        "retry_attempts": 0,
        "status_counts": {"completed": 0, "rejected": 0, "crashed": 24, "timeout": 0},
        "missing_experiment_ids": [],
        "extra_experiment_ids": [],
        "mismatch_experiment_ids": [],
        "duplicate_or_gap_experiment_ids": [],
    }
    base.update(overrides)
    return _FakeReport(**base)


class _FakeController:
    def __init__(self, *, report_factory=_complete_report):
        self.calls = []
        self._report_factory = report_factory

    async def run_full_campaign(self, session, **kwargs):
        self.calls.append(kwargs)
        specs = kwargs["specs"]
        experiment_id_by_key = {
            (s.strategy_key, s.params.get("config_id")): f"exp-{i}"
            for i, s in enumerate(specs)
        }
        kwargs["build_attempt_evidence"](experiment_id_by_key)
        return self._report_factory()


def _pinned_identity():
    envelope = build_production_frozen_campaign_envelope()
    full_hash = envelope.full_campaign_hash()
    campaign_run_id = _derive_primary_campaign_run_id(full_hash)
    return full_hash, campaign_run_id


@pytest.mark.asyncio
async def test_global_corpus_failure_falls_back_to_h6_crashed_batch_with_no_strategies_evidence(
    monkeypatch,
):
    monkeypatch.delenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", raising=False)
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    full_hash, campaign_run_id = _pinned_identity()
    fake_controller = _FakeController()
    outcome = await run_empirical_campaign_with_capture(
        session=object(),
        controller=fake_controller,
        expected_full_campaign_hash=full_hash,
        campaign_run_id=campaign_run_id,
    )
    assert outcome.report.verdict == "complete"
    assert outcome.walkforward_results is None
    assert outcome.strategies_evidence is None
    assert outcome.empirical_success is False
    assert fake_controller.calls  # non-vacuous: the fake was actually invoked


@pytest.mark.asyncio
async def test_preflight_fails_before_any_controller_call(monkeypatch):
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    fake_controller = _FakeController()
    with pytest.raises(RunPreflightError):
        await run_empirical_campaign_with_capture(
            session=object(),
            controller=fake_controller,
            expected_full_campaign_hash="0" * 64,  # deliberately wrong
            campaign_run_id="not-a-real-campaign-run-id",
        )
    assert fake_controller.calls == []


@pytest.mark.asyncio
async def test_opt_in_env_not_set_fails_before_any_controller_call(monkeypatch):
    monkeypatch.delenv("ROB944_RESEARCH_WRITE_OPT_IN", raising=False)
    full_hash, campaign_run_id = _pinned_identity()
    fake_controller = _FakeController()
    with pytest.raises(RunPreflightError):
        await run_empirical_campaign_with_capture(
            session=object(),
            controller=fake_controller,
            expected_full_campaign_hash=full_hash,
            campaign_run_id=campaign_run_id,
        )
    assert fake_controller.calls == []


@pytest.mark.asyncio
async def test_gap_nonempty_fails_closed_same_as_corpus_failure(monkeypatch):
    monkeypatch.setenv("ROB944_RESEARCH_WRITE_OPT_IN", "true")
    monkeypatch.setenv(
        "AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", "/tmp/rob960-does-not-matter"
    )
    full_hash, campaign_run_id = _pinned_identity()

    class _FakeManifestEntry:
        def __init__(self, symbol, gap_ranges):
            self.symbol = symbol
            self.gap_ranges = gap_ranges

    class _FakeManifest:
        klines = [
            _FakeManifestEntry("BTCUSDT", ((100, 200),)),
            _FakeManifestEntry("XRPUSDT", ()),
            _FakeManifestEntry("DOGEUSDT", ()),
            _FakeManifestEntry("SOLUSDT", ()),
        ]

    import rob941_manifest

    real_load = rob941_manifest.CorpusManifest.load
    call_count = {"n": 0}

    def _patched_load(path):
        # First call is build_production_frozen_campaign_envelope's own
        # (unrelated) manifest hash check -- must stay real. Only the
        # SECOND call (inside this module's own corpus-loading preamble)
        # gets the fake, gap-carrying manifest.
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_load(path)
        return _FakeManifest()

    monkeypatch.setattr(
        rob941_manifest.CorpusManifest, "load", staticmethod(_patched_load)
    )

    import rob941_offline_loader

    # Gap check happens AFTER load_corpus in the real preamble (mirrors H4's own
    # ordering) -- provide a real-shaped (empty) corpus; the important proof is
    # that a non-empty gap still collapses to the SAME fallback path as any
    # other corpus-stage failure.
    monkeypatch.setattr(
        rob941_offline_loader,
        "load_corpus",
        lambda manifest, root: {"klines": {}, "funding": {}},
    )

    fake_controller = _FakeController()
    outcome = await run_empirical_campaign_with_capture(
        session=object(),
        controller=fake_controller,
        expected_full_campaign_hash=full_hash,
        campaign_run_id=campaign_run_id,
    )
    assert outcome.walkforward_results is None
    assert outcome.strategies_evidence is None
    assert (
        outcome.report.verdict == "complete"
    )  # H4's own fallback still accounts cleanly
