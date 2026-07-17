# ROB-960 Empirical Materializer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, same session — the cross-module trust-boundary contracts here are too dense for a fresh zero-context subagent per task; continuity of context is required). Steps use checkbox (`- [ ]`) syntax for tracking.

## CAPTAIN PLAN-GATE CORRECTION (2026-07-18 08:09 KST) — fully applied, self-reviewed

Source: `/Users/mgh3326/work/herdr-inbox/strategy-captain-rob960-plan-gate-20260718-080900.md`, status `CHANGES_REQUIRED_BEFORE_FIRST_RED`, verify-round cost 0. This is the SECOND pass over this document — the first pass added only a preamble resolution table while Task 1–5 bodies still described the pre-gate design; per the captain's follow-up order, this revision rewrites every affected Task body in place so the document itself (not just a summary above it) is gate-compliant.

| # | Gate | Correction applied in this document |
|---|------|---------------------------------------|
| G1 | No funding/engine reimplementation | Task 1 calls `rob944_walkforward._run_scenario` directly — no local funding-gate/ordering/engine reassembly. `fold_id=None` passed at runtime despite the frozen `str` annotation; proven safe by a dedicated focused test reusing `test_rob944_walkforward.py`'s own proven `_run_scenario` fixture shape. S2 pre-execution rejections go through the existing `run_rob944_campaign._s2_rejections_to_no_trade_records`. **Already implemented and green** (4/4 tests) before this document was corrected — code matches this table, not the stale text that used to follow it. |
| G2 | Full-window/gap authority | Task 1's `build_evaluate_config_callback` takes a REAL `gap_ranges` mapping (never hardcoded). Task 3's orchestrator asserts every symbol's H1-manifest `gap_ranges` is empty BEFORE invoking PBO evaluation; a non-empty gap fails the whole run closed (no commit, no scorecard) via the same "no `strategies_evidence`" signal G9 uses. |
| G3 | No synthetic PBO placeholder | Task 2 never catches `PboGridError`/`Rob945PboBuilderError`. They propagate up through Task 3 as a materialization abort. `pbo_valid` is never set to `False` by this materializer's own code (H5's own default, `True`, is what a genuine PBO success always yields here). |
| G4 | Zero DB connection/query/write in worker/verify | Task 3's tests inject a fake/spy controller module (`_import_campaign_controller` monkeypatched to a stub exposing an async `run_full_campaign`) and a sentinel `session` object — no `AsyncSessionLocal`/real `test_db` touched by any ROB-960 test. A dedicated spy test proves the session factory is never even constructed when `--run` preflight fails. |
| G5 | No new schema/reason/exit code | Task 4's plan echo does NOT add `materializer_schema_version` — only `scorecard_output_filenames` (pure operator metadata) on top of H4's unchanged `build_plan()` fields. Task 5's CLI uses only H4's existing exit codes 0/2/4/5/6/7 — no code 8 anywhere. Every new failure branch (pre-commit evidence-computation failure, post-commit publish failure, no-strategies-evidence rollback) maps onto the existing generic "unexpected error, rolled back" bucket (6) or the existing "commit failed, rolled back" bucket (7), each with an accurate (never falsely claiming rollback when none occurred) sanitized message — no new taxonomy. |
| G6 | Exact CLI filename | The CLI file is `research/nautilus_scalping/run_rob940_empirical_materializer.py` (the Linear/worker-prompt-fixed name) everywhere in this document — never `run_rob960_materializer.py`. |
| G7 | No half-published artifact pair | Task 4 splits writing into `stage_scorecard_files` (both files into a fresh staging dir, fsync'd, never touches `output_dir`) and `publish_staged_scorecard` (destination absent → single atomic `os.replace(staging_dir, output_dir)`; destination present with identical bytes → idempotent no-op, staging discarded; destination present with different bytes → fail-closed, existing pair untouched, staging preserved). No two-separate-file-rename path exists. |
| G8 | Scorecard finished before H6 commit | Fixed order in Task 5: (1) run H4/H6 orchestration to an in-memory uncommitted `report` + compute raw H5 evidence (walk-forward, gap-empty proof, PBO, concurrency) — Task 3's whole job; (2) `build_scorecard`/`render_markdown`; (3) `stage_scorecard_files`; (4) `session.commit()`; (5) `publish_staged_scorecard`. 1–3 fail → rollback, no final files, exit 6. 4 fails → rollback, no final files, exit 7 (H4's own existing commit-failure code, unchanged meaning). 5 fails → DB already durable, staging preserved, exit 6 with an accurate (no "rolled back" claim) message. |
| G9 | Global fallback must not bypass scorecard authority | If corpus/walk-forward genuinely fails, H4's own unchanged 24-crashed fallback evidence still lets `run_full_campaign` reach `verdict="complete"`, but Task 3 leaves `strategies_evidence=None`; Task 5's CLI then ROLLS BACK instead of committing — "H6 accounting complete, no scorecard" never becomes durable. Reuses exit 6, no new code. |

Every Task section below is the corrected, self-reviewed design — there is no longer any stale pre-gate text anywhere in this document.

**Goal:** Wire the already-frozen, already-merged ROB-940 H4 (`research/nautilus_scalping/run_rob944_campaign.py`), H6 (`app/services/rob944_campaign_controller.py` + `app/services/research_campaign_bridge.py`), and H5 (`research/nautilus_scalping/rob945_scorecard.py`) pure/gated APIs into ONE new operator CLI entrypoint (`--plan`/`--run`) that, from a single approved empirical `--run`, produces both H6's committed trial-accounting rows AND H5's hash-pinned `scorecard.json`/`scorecard.md` files — as one atomically-published unit, never one without the other. No new metric, strategy contract, reason code, schema field, or exit-code taxonomy is introduced anywhere.

**Architecture:** Five new, additive-only pure/thin-IO modules under `research/nautilus_scalping/` (no existing H1–H6/H5 byte is ever touched — proven by an extended byte-freeze test). The empirical `--run` path reuses H4's own private corpus-loading/evidence-building helpers and `rob944_walkforward._run_scenario`/`run_walkforward` directly — never reimplemented — wraps H4's `ConfigSpec`s with H5's `rob945_capture.wrap_config_specs_for_oos_capture` observer (byte-identical to unwrapped, per H5's own `test_rob945_capture.py`), and reuses H6's `run_full_campaign` unchanged. PBO evidence (Task 1) is the one genuinely new orchestration composition — built entirely from existing frozen H1–H4 primitives, calling `_run_scenario` itself rather than reassembling its internals. H6 commit and H5 file publication are sequenced so that EITHER both become durable together OR NEITHER does (G7–G9).

**Tech Stack:** Python 3.13, pytest (no `pytest-cov` installed in this environment, so `-p no:cacheprovider` only — `--no-cov` is not a valid flag here and is omitted), argparse CLI mirroring `run_rob944_campaign.py`'s exact `--plan`/`--run` shape. Task 3's tests use fakes/spies exclusively — no real DB session anywhere in ROB-960's own test suite (G4).

## Global Constraints

- `PYTHONDONTWRITEBYTECODE=1`, pytest `-p no:cacheprovider`; cache/temp under `/tmp`.
- No empirical `--run` of either H4's or the new materializer's CLI is ever executed in this session. No real corpus load/download. No production/staging DB access, no real DB connection/query/write of any kind in this worker's own tests (G4).
- Every existing byte under `research/nautilus_scalping/`, `app/services/rob944_campaign_controller.py`, `app/schemas/research_campaign_bridge.py`, `app/services/research_campaign_bridge.py`, `app/services/research_db_write_guard.py`, `research_contracts/` stays byte-identical — additions only.
- Every new pure module lives under `research/nautilus_scalping/` with an `rob960_` prefix, EXCEPT the CLI itself, whose filename is fixed by Linear/worker-prompt authority (G6): `run_rob940_empirical_materializer.py`. No new `app/services/*` file — H6 is reused unchanged.
- `campaign_run_id`/`full_campaign_hash` derivation is NEVER re-implemented independently — always obtained by importing `rob944_frozen_campaign.build_production_frozen_campaign_envelope` / `run_rob944_campaign._derive_primary_campaign_run_id` (already bit-for-bit duplicated authorities), never a fourth copy.
- `--plan` output must be provably consistent with H4's own `--plan` for every shared field — verified by a test that calls both and asserts subset equality. No new schema/version key is added (G5) — only pure operator metadata (output filenames).
- No new exit code anywhere (G5) — every new failure branch reuses one of H4's existing 0/2/4/5/6/7 with an accurate, non-misleading sanitized message.

---

## File Structure

```
research/nautilus_scalping/
  rob960_pbo_evaluator.py                 # DONE: full-window per-config EvaluateConfigCallback impl (reuses _run_scenario)
  rob960_strategy_evidence.py             # NEW pure: WalkForwardResult+capture+pbo -> build_scorecard's `strategies[strategy]`
  rob960_empirical_orchestrator.py        # NEW: H4(capture-wrapped)+H6 wiring; gap-empty proof; no commit inside
  rob960_scorecard_writer.py              # NEW: pure plan echo + stage/publish (pair-atomic) file writer
  run_rob940_empirical_materializer.py    # NEW: CLI --plan/--run entrypoint (fixed filename, G6)
  tests/
    test_rob960_pbo_evaluator.py                          # DONE, 4/4 green
    test_rob960_strategy_evidence.py
    test_rob960_empirical_orchestrator.py                 # fakes/spies only, G4
    test_rob960_scorecard_writer.py
    test_rob960_cli_plan.py
    test_rob960_h1_through_h5_frozen_bytes_unchanged.py   # extends the ROB-945 byte-freeze proof, base = 72b75e3c
```

No existing file is modified. `tests/` here is the existing `research/nautilus_scalping/tests/` package (same conftest/path-setup as the H4/H5 suites).

---

## Task 1: PBO full-window evaluator (`rob960_pbo_evaluator.py`) — DONE

Implemented and green (`build_evaluate_config_callback`, `compute_pbo_evidence_for_strategy`, both in `research/nautilus_scalping/rob960_pbo_evaluator.py`), 4/4 tests passing in `research/nautilus_scalping/tests/test_rob960_pbo_evaluator.py`:
- `test_zero_signal_config_returns_completed_response_with_empty_trades`
- `test_s2_config_also_returns_completed_response_with_empty_trades`
- `test_compute_pbo_evidence_for_strategy_returns_evidence_for_all_zero_grid`
- `test_run_scenario_accepts_fold_id_none_at_runtime_despite_str_annotation` (the G1 focused proof)

Design (as implemented, matches G1/G2): `build_evaluate_config_callback(*, bars_1m, funding_sidecars, gap_ranges=None, strategy)` returns a closure that, per `(config, symbol)`, generates signals via `generate_s1_signals`/`generate_s2_signals` (with `fold_id=None`), converts S2 pre-execution rejections via `run_rob944_campaign._s2_rejections_to_no_trade_records`, and calls `rob944_walkforward._run_scenario(bars_slice, signals, COST_SCENARIO_PRIMARY_STRESS, sidecar, gap_ranges.get(symbol, ()), strategy=..., config_id=..., symbol=..., fold_id=None, pre_execution_rejections=...)` unchanged — no local funding/ordering/engine reassembly anywhere. `compute_pbo_evidence_for_strategy` composes this with `rob945_pbo_builder.build_pbo_daily_grid` + `rob945_pbo_grid.compute_pbo_auxiliary_evidence`, raising (never swallowing) on structural invalidity.

**Remaining for this task:** none — proceed to Task 2. (An additional golden-path "real trade through the full `_evaluate` closure via 1m→15m aggregation" test was considered and deliberately deferred: the `_run_scenario`-level focused test already proves the funding/engine/ordering composition with a real trade, and `aggregate_complete`'s own bucket semantics are independently tested elsewhere — adding a second, harder-to-construct fixture here would duplicate coverage without proportionate new risk reduction. Noted as a known limitation in the worker report, not hidden.)

---

## Task 2: Strategy evidence assembler (`rob960_strategy_evidence.py`)

**Files:**
- Create: `research/nautilus_scalping/rob960_strategy_evidence.py`
- Test: `research/nautilus_scalping/tests/test_rob960_strategy_evidence.py`

**Interfaces:**
- Consumes: a real `rob944_walkforward.WalkForwardResult` (per strategy); a finalized `rob945_capture.OosSignalCaptureSink` (per strategy); `rob945_scenario_metrics.{compute_scenario_metrics, compute_fold_stability}`; `rob945_capture.CaptureInvalidError`; Task 1's `compute_pbo_evidence_for_strategy` (propagates `PboGridError`/`Rob945PboBuilderError` — NOT caught here, per G3).
- Produces: `def build_strategy_evidence(*, strategy: str, walkforward_result, capture_sink, signal_concurrency_evidence, bars_1m, funding_sidecars, gap_ranges=None) -> dict[str, Any]` — the exact per-strategy inner mapping `build_scorecard`'s `strategies[strategy]` argument expects: `scenarios`, `fold_stability`, `signal_concurrency`, `pbo`, `capture_valid`. **No `pbo_valid` key is ever set** (G3) — PBO failure is not a degraded state this function represents; it's an exception that propagates to the caller (Task 3), which treats it identically to a corpus/walk-forward failure (no `strategies_evidence`, whole run rolls back). `capture_valid` stays a legitimate degraded-but-real state (H5's own existing, tested contract) since `CaptureInvalidError` reflects a genuine observed anomaly, never a fabrication.

- [ ] **Step 1: Write the failing test — capture-invalid sink yields `capture_valid=False` without raising**

```python
# research/nautilus_scalping/tests/test_rob960_strategy_evidence.py
from __future__ import annotations

import rob941_frozen_scope as frozen
from rob940_bars_agg import Bar1m
from rob941_funding_sidecar import FundingSidecar
from rob944_folds import Fold
from rob944_selection import ConfigSelectionOutcome, FoldSelectionTrace
from rob944_walkforward import ConfigAttemptResult, FoldWalkForwardResult, WalkForwardResult
from rob945_capture import OosSignalCaptureSink
from rob945_signal_concurrency import StrategyConcurrencyEvidence
from rob960_strategy_evidence import build_strategy_evidence


def _flat_bars_1m():
    bars = tuple(
        Bar1m(ts=frozen.WINDOW_START_MS + i * 60_000, open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0)
        for i in range(5)
    )
    return {s: bars for s in frozen.UNIVERSE}


def _flat_funding_sidecars():
    return {s: FundingSidecar.from_rows(s, ()) for s in frozen.UNIVERSE}


def _empty_selection_outcome(config_id: str) -> ConfigSelectionOutcome:
    return ConfigSelectionOutcome(
        config_id=config_id, eligible_symbols=(), excluded_symbols=(),
        equal_weight_expectancy_bps=None, pooled_expectancy_bps=None, profit_factor=0.0,
        rejected=True, rejection_reason="insufficient_eligible_symbols",
        train_input_hash="0" * 64, no_trade_reason_counts={},
    )


def _walkforward_result_no_selection(strategy: str) -> WalkForwardResult:
    config_ids = tuple(f"{strategy}-{i:02d}" for i in range(12))
    fold_results = []
    for i in range(8):
        fold = Fold(
            fold_id=f"fold-{i:02d}", fold_index=i,
            train_start_ms=0, train_end_ms=1, embargo_start_ms=1, embargo_end_ms=1,
            oos_start_ms=1, oos_end_ms=2,
        )
        candidates = tuple(_empty_selection_outcome(cid) for cid in config_ids)
        trace = FoldSelectionTrace(strategy=strategy, candidates=candidates, selected_config_id=None)
        fold_results.append(FoldWalkForwardResult(fold=fold, selection_trace=trace, oos_outcomes=()))
    attempts = tuple(
        ConfigAttemptResult(
            strategy=strategy, config_id=cid, status="completed", reason_code=None,
            selected_in_folds=(), crash_log=(), gap_rejection_log=(),
        )
        for cid in config_ids
    )
    return WalkForwardResult(
        strategy=strategy, folds=tuple(fold_results), config_attempts=attempts,
        concatenated_oos_ledgers={},
    )


def test_invalid_capture_sink_marks_capture_valid_false_without_raising():
    sink = OosSignalCaptureSink()
    sink.mark_invalid("unsupported_batch_shape")
    sink.finalize(set())
    wf_result = _walkforward_result_no_selection("S1")
    concurrency = StrategyConcurrencyEvidence(
        strategy="S1", numerator=0, denominator=0, rate=None,
        reason="no_entry_signal_minutes", distinct_symbol_count_histogram={1: 0, 2: 0, 3: 0, 4: 0},
    )
    evidence = build_strategy_evidence(
        strategy="S1", walkforward_result=wf_result, capture_sink=sink,
        signal_concurrency_evidence=concurrency,
        bars_1m=_flat_bars_1m(), funding_sidecars=_flat_funding_sidecars(),
    )
    assert evidence["capture_valid"] is False
    assert "pbo_valid" not in evidence
```

- [ ] **Step 2: Run test to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `rob960_strategy_evidence.py`**

```python
"""ROB-960 -- assembles ONE strategy's build_scorecard `strategies[strategy]`
input entirely from ALREADY-COMPUTED evidence. Wiring only: every compute_*
call is an existing H5 pure function, called exactly once. Per captain
plan-gate G3, a PboGridError/Rob945PboBuilderError is NEVER caught here --
it propagates as a materialization abort; this module never fabricates or
degrades PBO evidence."""
from __future__ import annotations

from rob945_capture import CaptureInvalidError
from rob945_scenario_metrics import compute_fold_stability, compute_scenario_metrics
from rob960_pbo_evaluator import compute_pbo_evidence_for_strategy

_SCENARIOS = ("base", "primary_stress", "upward_stress")


def _fold_selected_config(walkforward_result) -> dict:
    return {
        fwr.fold.fold_id: fwr.selection_trace.selected_config_id
        for fwr in walkforward_result.folds
    }


def build_strategy_evidence(
    *, strategy, walkforward_result, capture_sink, signal_concurrency_evidence,
    bars_1m, funding_sidecars, gap_ranges=None,
):
    fold_selected_config = _fold_selected_config(walkforward_result)
    try:
        captured_signals = capture_sink.snapshot()
        capture_valid = True
    except CaptureInvalidError:
        captured_signals = ()
        capture_valid = False

    scenarios = {
        scenario_name: compute_scenario_metrics(
            strategy=strategy, scenario_name=scenario_name,
            ledger=walkforward_result.concatenated_oos_ledgers.get(scenario_name, ()),
            captured_signals=captured_signals, fold_selected_config=fold_selected_config,
        )
        for scenario_name in _SCENARIOS
    }

    fold_stability = compute_fold_stability(
        ledger=walkforward_result.concatenated_oos_ledgers.get("primary_stress", ()),
        fold_selected_config=fold_selected_config,
    )

    # No try/except: a PboGridError/Rob945PboBuilderError here is a genuine
    # materialization-abort condition (G3/G9), not a degraded evidence state
    # this function represents.
    pbo = compute_pbo_evidence_for_strategy(
        strategy=strategy, bars_1m=bars_1m, funding_sidecars=funding_sidecars,
        gap_ranges=gap_ranges,
    )

    return {
        "scenarios": scenarios,
        "fold_stability": fold_stability,
        "signal_concurrency": signal_concurrency_evidence,
        "pbo": pbo,
        "capture_valid": capture_valid,
    }
```

- [ ] **Step 4: Run test to verify it passes.**

- [ ] **Step 5: PBO-failure-propagates test** — pass `bars_1m`/`funding_sidecars` missing a required symbol key (triggers a `KeyError`/downstream `Rob945PboBuilderError` from `compute_pbo_evidence_for_strategy`) and assert `build_strategy_evidence` raises (does not swallow, does not return a placeholder).

- [ ] **Step 6: Golden-path test** — a `WalkForwardResult` with `capture_sink` validly finalized (not invalidated); assert `capture_valid is True`, `scenarios` has exactly the 3 canonical keys, `fold_stability` has exactly 8 rows, `"pbo_valid" not in evidence`.

- [ ] **Step 7: Commit**

```bash
git add research/nautilus_scalping/rob960_strategy_evidence.py research/nautilus_scalping/tests/test_rob960_strategy_evidence.py
git commit -m "feat(ROB-960): per-strategy build_scorecard evidence assembler (no PBO placeholder, G3)"
```

---

## Task 3: Empirical orchestrator (`rob960_empirical_orchestrator.py`)

**Files:**
- Create: `research/nautilus_scalping/rob960_empirical_orchestrator.py`
- Test: `research/nautilus_scalping/tests/test_rob960_empirical_orchestrator.py`

**Interfaces:**
- Consumes (direct import, never reimplemented): `run_rob944_campaign.{RunPreflightError, _import_campaign_controller, _run_precheck_bridge_and_opt_in, _derive_primary_campaign_run_id, _s2_rejections_to_no_trade_records, _normalize_and_capture_summaries, _build_fallback_evidence_and_capture, _is_empirical_success, H1_MANIFEST_PATH, PRODUCTION_S1_STRATEGY_KEY, PRODUCTION_S2_STRATEGY_KEY}`; `rob944_frozen_campaign.build_production_frozen_campaign_envelope`; `rob945_capture.{wrap_config_specs_for_oos_capture, expected_oos_calls_from_walkforward_result, OosSignalCaptureSink}`; `rob944_walkforward.{ConfigSpec, GeneratedSignalBatch, run_walkforward, summarize_config_attempts_for_h6}`; `rob945_signal_concurrency.compute_signal_concurrency`; Task 2's `build_strategy_evidence`.
- **G4: no `app.core.db`/real session anywhere in this module's OWN construction.** `run_empirical_campaign_with_capture` takes `session` and `controller` as **injected parameters** (the CLI, Task 5, is the only place that ever imports `app.core.db.AsyncSessionLocal`/`app.services.rob944_campaign_controller` for real) — this makes every orchestrator test a pure fake/spy test with zero DB coupling, and lets Task 5's CLI construct the real session/controller exactly once, outside this module.
- **G2: gap-empty proof.** Before calling Task 2's `build_strategy_evidence` (which internally invokes PBO), assert every symbol's `gap_ranges` (from the loaded `CorpusManifest`) is empty; a non-empty gap raises `RunPreflightError` fail-closed (caught by the SAME outer machinery that produces `strategies_evidence=None` — see below), never proceeding to PBO/commit.
- Produces: `class EmpiricalRunOutcome` (dataclass: `report`, `attempt_evidence: list`, `walkforward_results: dict | None`, `strategies_evidence: dict | None`, `empirical_success: bool`). `async def run_empirical_campaign_with_capture(session, controller, *, expected_full_campaign_hash: str, campaign_run_id: str) -> EmpiricalRunOutcome`. Never commits — mirrors H4's own convention (`run_full_campaign` raises, never commits; the caller commits). `walkforward_results`/`strategies_evidence` are `None` exactly when the global corpus-load/gap/PBO fallback path was taken (real per-strategy evidence was never produced) — the ONLY signal Task 5 uses to decide commit vs. rollback (G9).

- [ ] **Step 1: Write the failing test — fake controller/session, corpus loading forced to fail globally, assert `walkforward_results is None`/`strategies_evidence is None` while `report.verdict == "complete"` (H4's existing fallback behavior, reused unchanged) — zero real DB (G4)**

```python
# research/nautilus_scalping/tests/test_rob960_empirical_orchestrator.py
"""G4: every test in this file uses an injected FAKE controller (an object
exposing an async run_full_campaign matching app.services.
rob944_campaign_controller.run_full_campaign's signature/contract) and a
sentinel `session` object -- no AsyncSessionLocal, no localhost/test_db, no
real asyncpg connection anywhere. This proves ROB-960's OWN new orchestration
logic (capture-wrapping, gap-empty gate, strategies_evidence assembly,
commit-vs-rollback signal) independently of H6's own (already-tested,
untouched, out-of-scope-to-re-test-here) DB persistence internals."""
from __future__ import annotations

import pytest

from rob944_frozen_campaign import build_production_frozen_campaign_envelope
from run_rob944_campaign import _derive_primary_campaign_run_id
from rob960_empirical_orchestrator import run_empirical_campaign_with_capture


class _FakeReport:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        return dict(self.__dict__)


class _FakeController:
    def __init__(self):
        self.calls = []

    async def run_full_campaign(self, session, **kwargs):
        self.calls.append(kwargs)
        specs = kwargs["specs"]
        experiment_id_by_key = {
            (s.strategy_key, s.params.get("config_id")): f"exp-{i}"
            for i, s in enumerate(specs)
        }
        kwargs["build_attempt_evidence"](experiment_id_by_key)  # exercise the callback exactly like the real controller does
        return _FakeReport(
            verdict="complete", expected_total=24, actual_registrations=24,
            primary_attempts=24, total_attempts=24, retry_attempts=0,
            status_counts={"completed": 0, "rejected": 0, "crashed": 24, "timeout": 0},
            missing_experiment_ids=[], extra_experiment_ids=[],
            mismatch_experiment_ids=[], duplicate_or_gap_experiment_ids=[],
        )


@pytest.mark.asyncio
async def test_global_corpus_failure_falls_back_to_h6_crashed_batch_with_no_strategies_evidence(monkeypatch):
    monkeypatch.delenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", raising=False)  # forces corpus load to fail
    envelope = build_production_frozen_campaign_envelope()
    full_hash = envelope.full_campaign_hash()
    campaign_run_id = _derive_primary_campaign_run_id(full_hash)
    fake_controller = _FakeController()
    outcome = await run_empirical_campaign_with_capture(
        session=object(),  # never touched by real DB code -- a sentinel proves it
        controller=fake_controller,
        expected_full_campaign_hash=full_hash, campaign_run_id=campaign_run_id,
    )
    assert outcome.report.verdict == "complete"
    assert outcome.walkforward_results is None
    assert outcome.strategies_evidence is None
    assert outcome.empirical_success is False
    assert fake_controller.calls  # proves the fake was actually invoked, not vacuous
```

- [ ] **Step 2: Run test to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `rob960_empirical_orchestrator.py`**

Structural decisions (G1/G2/G4/G9-compliant):
1. Corpus loading + capture-wrapped walk-forward happens **inside** the `build_attempt_evidence` callback passed to `controller.run_full_campaign` — H6's registration/predeclaration happens first (frozen gate order), matching H4 exactly.
2. `_build_real_capture_wrapped_evidence` mirrors `run_rob944_campaign._build_real_attempt_evidence_inner`'s corpus-loading preamble and `_s1_gen_factory`/`_s2_gen_factory` closures verbatim (they are nested/private, not independently importable — reproduce, diff-check at review time), inserting `wrap_config_specs_for_oos_capture(...)` before each `run_walkforward` call and `sink.finalize(expected_oos_calls_from_walkforward_result(result))` after. **New in this pass (G2):** immediately after loading `manifest`/`gap_ranges` and before any walk-forward call, assert `all(not ranges for ranges in gap_ranges.values())`; a violation raises (caught by the SAME outer except as any other corpus-stage failure, per H4's own whole-function-fallback pattern).
3. On success for both strategies: stash `walkforward_results[strategy]`, `capture_sinks[strategy]`, and (new) `bars_1m`/`funding_sidecars`/`gap_ranges` into outer-scope dicts, needed later for Task 1's PBO call.
4. On ANY exception (corpus load, gap-nonempty assertion, walk-forward): whole-function try/except mirroring `run_rob944_campaign._build_real_attempt_evidence` exactly, falling back to the imported (unchanged) `_build_fallback_evidence_and_capture` — `walkforward_results`/etc. stay empty, which is what makes `strategies_evidence=None` afterward.
5. Never commits — returns the outcome; the CLI (Task 5) owns commit/rollback (G8/G9).
6. If `walkforward_results` has both strategies: compute `compute_signal_concurrency({"S1": ..., "S2": ...})` once, then Task 2's `build_strategy_evidence` per strategy (which itself calls PBO — G3: any PBO failure here propagates and is caught by an OUTER try/except in THIS function, again collapsing to `strategies_evidence=None`, never a partial/fabricated dict).

```python
"""ROB-960 -- wires H4's real corpus+walk-forward execution (capture-
wrapped) through H6's UNCHANGED run_full_campaign (injected, never
imported directly here -- G4), producing both the H6 accounting report AND
(only when real per-strategy evidence + a proven-empty-gap corpus + valid
PBO all succeeded) the strategies evidence Task 5 needs for H5's
build_scorecard. Never commits; never fabricates strategies_evidence."""
from __future__ import annotations

from dataclasses import dataclass

from rob945_capture import expected_oos_calls_from_walkforward_result, wrap_config_specs_for_oos_capture
from rob945_signal_concurrency import compute_signal_concurrency
from rob960_strategy_evidence import build_strategy_evidence
from run_rob944_campaign import (
    RunPreflightError,
    _build_fallback_evidence_and_capture,
    _derive_primary_campaign_run_id,
    _is_empirical_success,
    _normalize_and_capture_summaries,
    _run_precheck_bridge_and_opt_in,
    _s2_rejections_to_no_trade_records,
)


@dataclass
class EmpiricalRunOutcome:
    report: object
    attempt_evidence: list
    walkforward_results: dict | None
    strategies_evidence: dict | None
    empirical_success: bool


async def run_empirical_campaign_with_capture(
    session, controller, *, expected_full_campaign_hash, campaign_run_id
):
    from rob944_frozen_campaign import build_production_frozen_campaign_envelope

    envelope = build_production_frozen_campaign_envelope()
    actual_hash = envelope.full_campaign_hash()
    if actual_hash != expected_full_campaign_hash:
        raise RunPreflightError("full_campaign_hash mismatch")
    if campaign_run_id != _derive_primary_campaign_run_id(actual_hash):
        raise RunPreflightError("campaign_run_id derivation mismatch")
    _run_precheck_bridge_and_opt_in()

    plain = envelope.to_dict()
    from app.schemas.research_backtest import StrategyExperimentIdentity

    specs = [
        StrategyExperimentIdentity(
            strategy_key=row["strategy_key"], strategy_version=row["strategy_version"],
            hypothesis=row["hypothesis"], **row["components"],
        )
        for row in plain["rows"]
    ]

    walkforward_results: dict = {}
    capture_sinks: dict = {}
    corpus_cache: dict = {}  # {"bars_1m":..., "funding_sidecars":..., "gap_ranges":...}
    captured_summaries: list = []
    attempt_evidence_out: list = []

    def _build_attempt_evidence(experiment_id_by_key: dict) -> list:
        try:
            evidence = _build_real_capture_wrapped_evidence(
                experiment_id_by_key, full_campaign_hash=actual_hash,
                campaign_run_id=campaign_run_id, capture_summaries_into=captured_summaries,
                walkforward_results_out=walkforward_results, capture_sinks_out=capture_sinks,
                corpus_cache_out=corpus_cache,
            )
        except Exception:  # noqa: BLE001 -- mirrors run_rob944_campaign._build_real_attempt_evidence's own whole-function fallback exactly
            walkforward_results.clear()
            capture_sinks.clear()
            corpus_cache.clear()
            evidence = _build_fallback_evidence_and_capture(
                experiment_id_by_key, full_campaign_hash=actual_hash,
                campaign_run_id=campaign_run_id, capture_summaries_into=captured_summaries,
            )
        attempt_evidence_out.clear()
        attempt_evidence_out.extend(evidence)
        return evidence

    report = await controller.run_full_campaign(
        session, specs=specs, actual_full_campaign_hash=actual_hash,
        expected_full_campaign_hash=expected_full_campaign_hash, campaign_run_id=campaign_run_id,
        guard_opt_in_enabled=True, guard_policy=_default_research_db_policy(),
        build_attempt_evidence=_build_attempt_evidence,
        strategy_name="rob940_walkforward", timeframe="mixed_5m_15m", runner="rob940-empirical-materializer",
    )

    strategies_evidence = None
    if len(walkforward_results) == 2:
        try:
            s1_sink, s2_sink = capture_sinks["S1"], capture_sinks["S2"]
            s1_signals = () if s1_sink.is_invalid else s1_sink.snapshot()
            s2_signals = () if s2_sink.is_invalid else s2_sink.snapshot()
            concurrency = compute_signal_concurrency({"S1": s1_signals, "S2": s2_signals})
            strategies_evidence = {
                strategy: build_strategy_evidence(
                    strategy=strategy, walkforward_result=walkforward_results[strategy],
                    capture_sink=capture_sinks[strategy],
                    signal_concurrency_evidence=concurrency.per_strategy_by_name[strategy],
                    bars_1m=corpus_cache["bars_1m"], funding_sidecars=corpus_cache["funding_sidecars"],
                    gap_ranges=corpus_cache["gap_ranges"],
                )
                for strategy in ("S1", "S2")
            }
        except Exception:  # noqa: BLE001 -- G3/G9: any PBO/evidence-assembly failure here collapses to strategies_evidence=None, never a partial/fabricated dict
            strategies_evidence = None

    return EmpiricalRunOutcome(
        report=report,
        attempt_evidence=[e.model_dump() for e in attempt_evidence_out],
        walkforward_results=walkforward_results or None,
        strategies_evidence=strategies_evidence,
        empirical_success=_is_empirical_success(report),
    )


def _default_research_db_policy():
    from app.services.research_db_write_guard import default_research_db_policy

    return default_research_db_policy()
```

`_build_real_capture_wrapped_evidence` (new helper, same module): copy `run_rob944_campaign._build_real_attempt_evidence_inner`'s corpus-loading preamble + `_s1_gen_factory`/`_s2_gen_factory` closures verbatim; after building `gap_ranges = {k.symbol: k.gap_ranges for k in manifest.klines}`, add:

```python
    non_empty = {sym: ranges for sym, ranges in gap_ranges.items() if ranges}
    if non_empty:
        raise RunPreflightError(
            "H1 corpus manifest reports non-empty gap_ranges for one or more symbols -- "
            "PBO full-window evaluation requires a proven-empty-gap corpus (G2); refusing "
            "to proceed"
        )
```

then wrap each strategy's `ConfigSpec` tuple with `wrap_config_specs_for_oos_capture(specs, strategy=strategy, fold_schedule=fold_schedule, sink=sink)` before `run_walkforward`, `sink.finalize(expected_oos_calls_from_walkforward_result(result))` after, and write `walkforward_results_out[strategy] = result`, `capture_sinks_out[strategy] = sink`, and (once, not per-strategy) `corpus_cache_out.update(bars_1m=bars_1m, funding_sidecars=funding_sidecars, gap_ranges=gap_ranges)`.

- [ ] **Step 4: Run the Step-1 test, confirm PASS.**

- [ ] **Step 5: Preflight-fails-before-any-controller-call spy test (G4)** — a `_FakeController` whose `run_full_campaign` records a call; assert that when `expected_full_campaign_hash`/`campaign_run_id` don't match the fresh recomputation, `RunPreflightError` is raised and `fake_controller.calls == []` (the controller/session is never even reached).

- [ ] **Step 6: Gap-nonempty fail-closed test** — monkeypatch the corpus loader (inside `_build_real_capture_wrapped_evidence`'s import scope) to return a manifest with one symbol carrying a non-empty `gap_ranges`; assert the SAME fallback path is taken (`walkforward_results is None`, `strategies_evidence is None`), proving G2's gate fires before any PBO/commit path is reachable.

- [ ] **Step 7: Golden-path test** — monkeypatch the corpus loader to return a small synthetic in-memory corpus (reuse whatever fixture shape `test_rob944_walkforward.py` already uses for a full walk-forward pass — grep before inventing), with gap_ranges all empty; assert `outcome.walkforward_results is not None`, `outcome.strategies_evidence` has both `"S1"`/`"S2"` keys with Task 2's 5 expected inner keys (`scenarios`, `fold_stability`, `signal_concurrency`, `pbo`, `capture_valid`).

- [ ] **Step 8: Observer-effect proof test** — same synthetic corpus, run `run_walkforward` directly (unwrapped) vs. through `_build_real_capture_wrapped_evidence` (wrapped); assert the two `WalkForwardResult`s are `==` AND their canonical JSON bytes/SHA are identical (mirrors `test_rob945_capture.py`'s own pattern, non-vacuous — assert the two results are non-trivial, e.g. contain at least one real trade, so the equality isn't trivially satisfied by two empty results).

- [ ] **Step 9: Commit**

```bash
git add research/nautilus_scalping/rob960_empirical_orchestrator.py research/nautilus_scalping/tests/test_rob960_empirical_orchestrator.py
git commit -m "feat(ROB-960): capture-wrapped H4+H6 orchestrator, fake/spy-only tests (G4), gap-empty gate (G2)"
```

---

## Task 4: Scorecard plan echo + stage/publish writer (`rob960_scorecard_writer.py`)

**Files:**
- Create: `research/nautilus_scalping/rob960_scorecard_writer.py`
- Test: `research/nautilus_scalping/tests/test_rob960_scorecard_writer.py`

**Interfaces:**
- Consumes: `run_rob944_campaign.build_plan`.
- Produces:
  - `def build_materializer_plan() -> dict` — `run_rob944_campaign.build_plan()` verbatim plus ONLY `"scorecard_output_filenames": {"json": "scorecard.json", "md": "scorecard.md"}` (G5: no schema-version key). Proven subset-equal to H4's own plan on every H4-owned key.
  - `def stage_scorecard_files(envelope: dict, markdown: str, output_dir: Path) -> Path` — writes `scorecard.json`/`scorecard.md` into a fresh sibling staging directory (`tempfile.mkdtemp(dir=output_dir.parent, prefix=f".{output_dir.name}.staging-")`), fsyncs both files AND the staging directory itself, returns the staging directory path. Never touches `output_dir`.
  - `class ScorecardPublishConflictError(RuntimeError)`.
  - `def publish_staged_scorecard(staging_dir: Path, output_dir: Path) -> tuple[Path, Path]` (G7): if `output_dir` doesn't exist, `os.replace(staging_dir, output_dir)` — one atomic directory rename, done. If `output_dir` exists: compare `output_dir`'s two files' bytes against staging's; identical → `shutil.rmtree(staging_dir)`, return the existing `output_dir` paths (idempotent no-op). Different → raise `ScorecardPublishConflictError`, leave `output_dir` untouched, leave `staging_dir` in place (forensic/recovery). Never a state where `output_dir` has one file but not the other.

- [ ] **Step 1: Write the failing test — plan subset-equality with H4, no schema-version key**

```python
# research/nautilus_scalping/tests/test_rob960_scorecard_writer.py
from __future__ import annotations

from run_rob944_campaign import build_plan as h4_build_plan
from rob960_scorecard_writer import build_materializer_plan


def test_materializer_plan_never_diverges_from_h4_plan_on_shared_fields():
    h4_plan = h4_build_plan()
    materializer_plan = build_materializer_plan()
    for key in h4_plan:
        assert materializer_plan[key] == h4_plan[key], f"diverged on shared field {key}"
    assert "materializer_schema_version" not in materializer_plan
    assert materializer_plan["scorecard_output_filenames"] == {"json": "scorecard.json", "md": "scorecard.md"}
    assert "scorecard_output_filenames" not in h4_plan
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Implement `build_materializer_plan`**

```python
def build_materializer_plan() -> dict:
    from run_rob944_campaign import build_plan as h4_build_plan

    plan = dict(h4_build_plan())
    plan["scorecard_output_filenames"] = {"json": "scorecard.json", "md": "scorecard.md"}
    return plan
```

- [ ] **Step 4: Run test to verify it passes.**

- [ ] **Step 5: Write the failing test — first publish (destination absent) is a single atomic directory rename, both files present**

```python
def test_publish_staged_scorecard_first_publish_creates_output_dir_atomically(tmp_path):
    from rob960_scorecard_writer import publish_staged_scorecard, stage_scorecard_files

    envelope = {"schema_version": "rob945.v1", "scorecard_payload": {"a": 1}, "scorecard_artifact_hash": "x"}
    output_dir = tmp_path / "out"
    staging = stage_scorecard_files(envelope, "# stub", output_dir)
    assert not output_dir.exists()  # staging never touches the final dir
    json_path, md_path = publish_staged_scorecard(staging, output_dir)
    assert json_path.exists() and md_path.exists()
    assert not staging.exists()  # renamed away, not left behind on success
```

- [ ] **Step 6: Implement `stage_scorecard_files`/`publish_staged_scorecard`**

```python
import json
import os
import shutil
import tempfile
from pathlib import Path


def stage_scorecard_files(envelope: dict, markdown: str, output_dir: Path) -> Path:
    staging_dir = Path(tempfile.mkdtemp(dir=output_dir.parent, prefix=f".{output_dir.name}.staging-"))
    (staging_dir / "scorecard.json").write_text(json.dumps(envelope, indent=2, sort_keys=True))
    (staging_dir / "scorecard.md").write_text(markdown)
    for name in ("scorecard.json", "scorecard.md"):
        fd = os.open(staging_dir / name, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    dir_fd = os.open(staging_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return staging_dir


class ScorecardPublishConflictError(RuntimeError):
    pass


def publish_staged_scorecard(staging_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    json_path, md_path = output_dir / "scorecard.json", output_dir / "scorecard.md"
    if not output_dir.exists():
        os.replace(staging_dir, output_dir)
        return json_path, md_path
    staged_json = (staging_dir / "scorecard.json").read_bytes()
    staged_md = (staging_dir / "scorecard.md").read_bytes()
    if json_path.exists() and md_path.exists() and json_path.read_bytes() == staged_json and md_path.read_bytes() == staged_md:
        shutil.rmtree(staging_dir)
        return json_path, md_path
    raise ScorecardPublishConflictError(
        f"{output_dir} already contains a different published scorecard pair -- "
        f"refusing to overwrite; staging preserved at {staging_dir} for forensic inspection"
    )
```

- [ ] **Step 7: Run test to verify it passes.**

- [ ] **Step 8: Idempotent-replay test** — publish once, stage the SAME envelope/markdown again, publish again; assert both publishes return equal paths with identical bytes and the second staging dir is removed (not left behind).

- [ ] **Step 9: Conflict-fail-closed test** — publish once, stage a DIFFERENT envelope, attempt publish; assert `ScorecardPublishConflictError` is raised, the ORIGINAL `output_dir` files are byte-unchanged, and the second staging dir still exists on disk (forensic preservation).

- [ ] **Step 10: No-half-published-pair proof test** — monkeypatch `os.replace` to raise partway through a first-publish call (simulate a crash mid-rename); assert `output_dir` either doesn't exist at all afterward, or (if the OS-level rename is provably atomic and this monkeypatch fires before vs. after the syscall) contains both files — never exactly one. Document in the test docstring which of the two outcomes this specific monkeypatch point proves, since `os.replace` on a directory is itself a single syscall (no partial-rename state is possible at the Python level — this test exists to prove the CODE never does a two-step publish that COULD be partial, not to fuzz the OS's own rename atomicity).

- [ ] **Step 11: Commit**

```bash
git add research/nautilus_scalping/rob960_scorecard_writer.py research/nautilus_scalping/tests/test_rob960_scorecard_writer.py
git commit -m "feat(ROB-960): plan echo (no new schema key, G5) + pair-atomic stage/publish writer (G7)"
```

---

## Task 5: CLI entrypoint (`run_rob940_empirical_materializer.py`, G6 exact filename)

**Files:**
- Create: `research/nautilus_scalping/run_rob940_empirical_materializer.py`
- Test: `research/nautilus_scalping/tests/test_rob960_cli_plan.py`

**Interfaces:**
- Consumes: Task 4's `build_materializer_plan`/`stage_scorecard_files`/`publish_staged_scorecard`; Task 3's `run_empirical_campaign_with_capture`; `rob945_scorecard.{build_scorecard, render_markdown}`; H4's own gating imports (`app.core.db.AsyncSessionLocal`, `app.services.rob944_campaign_controller`, `research_db_write_guard`) — imported ONLY inside the `--run` branch, exactly mirroring H4's own lazy-import discipline, so `--plan` never touches `app.*` at all.
- `--plan`: prints `json.dumps(build_materializer_plan(), indent=2, sort_keys=True)`, exit 0. Pure — proven by a test that poisons `sys.modules["app.core.db"]` and confirms `--plan` still exits 0.
- `--run`: requires `--expected-full-campaign-hash`, `--campaign-run-id`, `--output-dir`. Gated identically to H4 (same `ROB944_RESEARCH_WRITE_OPT_IN` env var reused — no new env var). **Fixed order (G8):**
  1. Preflight (hash/run-id/opt-in/bridge — via `run_empirical_campaign_with_capture`'s own checks) — a `RunPreflightError` here means the session factory is NEVER constructed (proven by a spy test). Exit 2.
  2. Open the real session (`AsyncSessionLocal`), call `run_empirical_campaign_with_capture(session, real_controller, ...)`.
  3. If `outcome.strategies_evidence is None`: `await session.rollback()`, sanitized message ("H6 accounting evidence could not be paired with a real, complete scorecard input set — rolled back, no scorecard written"), exit 6 (G9 — reused, not a new code).
  4. Else: `build_scorecard(...)` + `render_markdown(...)` (in-memory, pre-commit) → `stage_scorecard_files(...)` (pre-commit). Any exception in this step: `await session.rollback()`, sanitized message, exit 6.
  5. `await session.commit()`. Exception here: `await session.rollback()`, sanitized message, exit 7 (reuses H4's own commit-failure code exactly).
  6. `publish_staged_scorecard(...)`. Exception here: DB already durable (no rollback attempted — nothing to roll back), sanitized message that does NOT claim a rollback occurred, exit 6.
  7. Success: print a JSON summary (`scorecard_artifact_hash`, paths, `empirical_success`), exit `0 if outcome.empirical_success else 5` (reuses H4's own 0/5 empirical-success convention exactly).

- [ ] **Step 1: Write the failing test — `--plan` is pure and matches Task 4's plan exactly, and never imports `app.core.db`**

```python
# research/nautilus_scalping/tests/test_rob960_cli_plan.py
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
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Implement `run_rob940_empirical_materializer.py`** — mirror `run_rob944_campaign.py`'s `_build_arg_parser`/`main` shape exactly, PLUS `--output-dir`. `--plan` branch imports ONLY `rob960_scorecard_writer`. `--run` branch implements the G8 6-step sequence above using `rob960_empirical_orchestrator.run_empirical_campaign_with_capture` with the REAL `AsyncSessionLocal()`/`app.services.rob944_campaign_controller` module as `controller` (imported lazily, inside the function, matching H4's own `_import_campaign_controller` pattern — reuse that helper directly rather than re-importing ad hoc).

- [ ] **Step 4: Run test to verify it passes.**

- [ ] **Step 5: `--run` preflight-fails-before-session-factory spy test (G4)** — monkeypatch `app.core.db.AsyncSessionLocal` (or wherever the CLI imports it from) with a spy that records calls and raises if invoked; run `--run` with a deliberately wrong `--campaign-run-id`; assert exit code 2 and the spy was never called.

- [ ] **Step 6: `strategies_evidence is None` → rollback, exit 6 test** — monkeypatch `run_empirical_campaign_with_capture` to return an outcome with `strategies_evidence=None`; use a fake session whose `.rollback()`/`.commit()` are spies; assert `rollback` was called, `commit` was NOT called, exit code 6, no files written to `--output-dir`.

- [ ] **Step 7: Commit**

```bash
git add research/nautilus_scalping/run_rob940_empirical_materializer.py research/nautilus_scalping/tests/test_rob960_cli_plan.py
git commit -m "feat(ROB-960): materializer CLI (G6 filename, G8 stage-before-commit order, G5 no new exit codes)"
```

---

## Task 6: Extended byte-freeze proof + full regression sweep

**Files:**
- Create: `research/nautilus_scalping/tests/test_rob960_h1_through_h5_frozen_bytes_unchanged.py`

Mirrors `test_rob945_h4_frozen_bytes_unchanged.py` exactly (`git diff --name-only --diff-filter=MDRCT` + its own non-vacuous companion), with `_REQUIRED_STARTING_HEAD` = the full 40-char SHA of the H5 merge commit `72b75e3c` (confirm via `git log --oneline | grep 72b75e3c` — do not truncate), and `_FROZEN_PATHS` = the same five `app/*`/`research_contracts` entries plus `research/nautilus_scalping` (whole dir, additions-only as before — this already covers every `rob945_*.py` file without needing to list them individually, since the existing test's own `research/nautilus_scalping` entry already does).

- [ ] **Step 1: Write the test.**
- [ ] **Step 2: Run it — must pass immediately** (hard gate; if it fails, Tasks 1–5 accidentally touched a frozen file — stop and fix before proceeding).
- [ ] **Step 3: Commit.**

```bash
git add research/nautilus_scalping/tests/test_rob960_h1_through_h5_frozen_bytes_unchanged.py
git commit -m "test(ROB-960): extend byte-freeze proof through the H5 merge base"
```

- [ ] **Step 4: Run the full verification sweep** (fresh, in order, exact pass counts recorded for the worker report):

```bash
export PYTHONDONTWRITEBYTECODE=1
uv run pytest research/nautilus_scalping/tests/ -k "rob960" -v -p no:cacheprovider
uv run pytest research/nautilus_scalping/tests/test_rob945_capture.py -v -p no:cacheprovider
uv run pytest research/nautilus_scalping/tests/ -k "rob944" -v -p no:cacheprovider
uv run pytest research/nautilus_scalping/tests/test_rob945*.py -v -p no:cacheprovider
uv run pytest research/nautilus_scalping/tests/ -k "rob941 or rob942 or rob943" -v -p no:cacheprovider
uv run ruff check research/nautilus_scalping/rob960_*.py research/nautilus_scalping/run_rob940_empirical_materializer.py research/nautilus_scalping/tests/test_rob960_*.py
uv run ruff format --check research/nautilus_scalping/rob960_*.py research/nautilus_scalping/run_rob940_empirical_materializer.py research/nautilus_scalping/tests/test_rob960_*.py
uv run ty check research/nautilus_scalping/rob960_*.py research/nautilus_scalping/run_rob940_empirical_materializer.py 2>&1 | tee /tmp/rob960_ty.txt
uv run python3 research/nautilus_scalping/run_rob944_campaign.py --plan | shasum -a 256   # must equal 18c7350350e5fbd055ed9e9284817a2eec271f53963aff5c9b74a7df279c9690
uv run python3 research/nautilus_scalping/run_rob940_empirical_materializer.py --plan | shasum -a 256
git status --short   # must show only Added files under research/nautilus_scalping/, docs/superpowers/plans/
```

(`tests/services/research/test_rob944_campaign_controller.py` — H6's own real-`test_db` suite — is explicitly NOT run by this worker per G4; it is pre-existing, untouched, out of this worker's DB-touching authority.)

- [ ] **Step 5: Note any pre-existing baseline failure (e.g. `ty`) unrelated to these new files explicitly, distinct from changed-scope results.**

---

## Task 7: PR

- [ ] **Step 1:** `git push -u origin rob-960`
- [ ] **Step 2:** `gh pr create` — title `[strategy] ROB-960 empirical materializer — frozen H4/H5/H6 scorecard wiring`, body covering scope, frozen-identity proof, the G1–G9 resolution matrix, safety (no empirical run ever executed, no real DB touched), test counts.
- [ ] **Step 3:** Do not merge. Write the `herdr-inbox` report per the task brief's §7 template, including literal `ultrathink`, the G1–G9 resolution matrix, and the explicit "worker did not execute an empirical RUN, and touched zero real DB connections" statement.
