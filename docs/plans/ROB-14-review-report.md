# ROB-14 — Review Report

- **Linear:** ROB-14 — TradingAgents pre-proposal veto/synthesis for Trading
  Decision Workspace
- **Branch / worktree:** `feature/ROB-14-tradingagents-pre-proposal-synthesis`
  at `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-14-tradingagents-pre-proposal-synthesis`
- **Implementation commit reviewed:** `28235606 feat(rob-14): add tradingagents pre-proposal synthesis`
- **Reviewer:** Claude Opus 4.7 (read-only)
- **Plan file:** `docs/plans/ROB-14-tradingagents-pre-proposal-synthesis-plan.md`
- **Verdict:** **PASS** with non-blocking follow-ups recorded for a later PR.
- **Date:** 2026-04-28

---

## 1. Verdict summary

The minimal-safe implementation:

- Honors every safety constraint in the planner brief.
- Adds three production modules and two test modules behind clear advisory-only
  semantics; no broker / KIS / Upbit / watch / paper / `dry_run=False` /
  order-intent surface area is created or imported.
- Implements the Linear acceptance criteria: advisory-only invariants are
  pinned, the buy-candidate × `Underweight` downgrade is covered by both
  policy and test, the proposal payload reflects source / model / base_url /
  decision / risks / warnings / reflected action, and the persistence wrapper
  is unit-tested to confirm only `create_decision_session` and
  `add_decision_proposals` are touched.
- Deviates from the plan only in scope reductions (no real-DB integration
  test, no FE panel, narrower schema literals, simpler policy ladder). None
  of those reductions weaken safety; they trade richer policy / coverage for a
  smaller PR.

A single recommended follow-up (safety-test subprocess hardening) is not
blocking because the current modules are independently verified safe in a
clean subprocess (373 new modules, **0** forbidden). I'm flagging it because
the in-process variant of the safety test will give false negatives on any
future change that adds a forbidden import after another test in the run has
already loaded the synthesis package.

---

## 2. Files inspected

### 2.1 Production code
- `app/schemas/trading_decision_synthesis.py` (155 lines)
- `app/services/trading_decision_synthesis.py` (147 lines)
- `app/services/trading_decision_synthesis_persistence.py` (75 lines)

### 2.2 Tests
- `tests/services/test_trading_decision_synthesis.py` (183 lines, 9 tests)
- `tests/services/test_trading_decision_synthesis_safety.py` (122 lines, 3 tests)

### 2.3 Reference / not-modified
- `app/services/trading_decision_service.py` — re-used; **unmodified**.
- `app/services/tradingagents_research_service.py` — **unmodified** (ROB-9).
- `app/models/trading_decision.py` — **unmodified** (ROB-1).
- `app/routers/trading_decisions.py` — **unmodified** (ROB-2).
- `app/core/config.py` — **unmodified**.
- All broker / KIS / Upbit / order / watch / paper / task modules —
  **unmodified and unimported by the new code**.

### 2.4 Independently re-run (read-only verification)
- `uv run pytest tests/services/test_trading_decision_synthesis.py
  tests/services/test_trading_decision_synthesis_safety.py -v` →
  **12 passed**, 2 unrelated Pydantic deprecation warnings from
  `app/auth/schemas.py`.
- `uv run python -c "<clean-subprocess import diff for the three new
  modules>"` → 373 new modules loaded transitively, 0 violations against the
  full forbidden-prefix list.

---

## 3. Safety-constraint audit

| Constraint (from planner brief) | Result | Evidence |
|---|---|---|
| TradingAgents stays `advisory_only=True`, `execution_allowed=False` | ✅ | Pydantic `Literal[True]` / `Literal[False]` defaults on `AdvisoryEvidence`; `SynthesizedProposal._payload_preserves_advisory_invariants` model-validator re-asserts both at the proposal payload boundary; persisted `original_payload` and `market_brief` carry both flags |
| No live orders | ✅ | No broker / order / KIS / Upbit / paper module is imported by the three new modules (grep confirmed: `import` lines reference only stdlib, pydantic, sqlalchemy, and existing trading_decision modules) |
| No `dry_run=False` | ✅ | Token absent from new code |
| No watch registration | ✅ | `app.services.watch_alerts` not imported (grep + safety test) |
| No order intent | ✅ | No `place_order`, no order-intent helper, no broker contract import |
| No broker / KIS / Upbit / order / watch / task imports | ✅ | Two complementary checks: (a) forbidden-prefix safety test in pytest; (b) clean-subprocess re-import check by reviewer |
| No secrets / `.env` values | ✅ | No `os.environ` / `os.getenv` / settings reads in the new code; `secret`/`token`/`api_key`/`password` tokens absent |
| Synthesis writes only sessions+proposals | ✅ | Persistence wrapper calls only `create_decision_session` + `add_decision_proposals`; mock-based test asserts no other helpers are awaited; no `record_decision_action` / `create_counterfactual_track` / `record_outcome_mark` import |

## 4. Acceptance-criteria coverage (Linear)

| AC | Status | Evidence |
|---|---|---|
| Unit tests cover advisory-only invariant enforcement | ✅ | `test_advisory_pins_advisory_only_literals` (rejects `advisory_only=False` and `execution_allowed=True`) plus the `SynthesizedProposal` validator that requires `original_payload.advisory_only is True` and `original_payload.execution_allowed is False` |
| Unit tests cover buy candidate + TradingAgents `Underweight` downgrade/veto | ✅ | `test_buy_candidate_underweight_is_downgraded_to_no_side_watch`: buy + Underweight → `final_proposal_kind="pullback_watch"`, `final_side="none"`, `final_confidence ≤ 25`, `conflict=True`, policy `downgrade_buy_on_bearish_advisory` |
| Proposal payloads include TradingAgents source/model/base_url/decision/key risks/warnings/reflected action | ✅ | `_build_original_payload` writes `synthesis.tradingagents = advisory.model_dump(mode="json")` (model, base_url, decision_text, final_trade_decision_text, warnings, risk_flags, raw_state_keys, as_of_date) plus `synthesis.reflected_action`; asserted by `test_buy_candidate_underweight_…` (model + base_url + reflected_action) |
| Session generation tests verify no broker/order/watch side-effect APIs are called | ✅ | `test_persistence_composes_only_session_and_proposal_helpers` monkeypatches the only two DB helpers and asserts each is awaited exactly once with the synthesized payload; the surrounding safety test asserts no forbidden module is loaded by importing the persistence module |
| Approval page can display enough evidence for the operator to see the TradingAgents effect | ✅ (minimum-viable) | `original_rationale` is rendered verbatim by existing `ProposalRow` ("TradingAgents Underweight advisory downgraded NVDA buy candidate to pullback_watch/none."); `proposal_kind`/`side` chips already change. Detailed advisory data lives in `original_payload.synthesis` and `session.market_brief.synthesis_meta` for API consumers. A dedicated FE panel was deferred (see §6.3) |

## 5. Plan adherence

The implementation honors the plan's **safety contract** exactly. It deviates
in **scope** (intentionally, per the implementer's note) and in some **shape
choices**:

| Plan element | Impl choice | Impact |
|---|---|---|
| `extra="forbid"` on `CandidateAnalysis` / `AdvisoryEvidence` | `extra="allow"` | Loosens the contract slightly; callers can include extra keys. Safety-neutral because nothing in those keys can cross into broker code. |
| `CandidateAnalysis.side: Literal["buy","sell","hold","none"]` | `Literal["buy","sell","none"]` | Drops the `hold` side; `hold_passthrough` policy from the plan is not reachable. Acceptable given the AC focuses on buy-side veto. Caller would model a hold candidate as `side="none"`. |
| `AdvisoryEvidence.advisory_action: Literal[Buy, Overweight, Hold, Underweight, Sell, Unknown]` | `str` with `normalized_action` property | Less strict at the schema layer; matches the bearish set via lower-cased comparison (`{"underweight","sell","avoid","reduce","reduce_exposure"}`). This is more permissive of upstream phrasing variation; safety-neutral. |
| Plan policy ladder (60-thresholded buy→pullback_watch vs avoid; agreement +10; hold passthrough; risk_flag −15 / −10 stack) | One-line buy×bearish → `pullback_watch`+confidence-clamp ≤ 25; buy×{hold,neutral} → confidence ≤ 50; risk_flags OR warnings → −10 | Simpler ladder, same direction (downgrade or lower confidence on bearish/neutral advisory). All AC tests pass. |
| `synthesize_pre_proposals(candidates, advisory_by_symbol)` orchestrator | `synthesize_candidate_with_advisory(candidate, advisory)` (no list/map orchestrator) | Caller must zip candidates with advisories. Less ergonomic, no functional gap. |
| `build_synthesized_session(...)` taking `candidates` + `advisory_by_symbol` | `create_synthesized_decision_session(...)` taking pre-synthesized proposals | Layering choice; caller-side composition. Safety-neutral. |
| Plan Tasks 5/6/8: real-DB integration tests | Not implemented | See §6.1. |
| Plan Task 7: subprocess-based safety test | In-process `importlib.import_module` + `sys.modules` diff | See §6.2. |
| Plan Task 11: optional `SynthesisPanel.tsx` FE component | Not implemented | See §6.3. |

The behavior-relevant numeric thresholds in the plan (60 / 30 / 25 / 15 / 10)
are not all reproduced; the impl uses a different (simpler) downgrade math. AC
text says "downgrade from buy to `watch`, `review_required`, or `none` instead
of creating direct buy proposal" and "lower confidence and record affected
thresholds" — both are satisfied. **Not a must-fix.**

## 6. Findings

### 6.1 Non-blocking — No real-DB integration test

**Observation.** The plan's Tasks 5/6/8 wrote sessions/proposals to a real
Postgres DB and asserted the persisted `original_payload` and
`market_brief.synthesis_meta` shape. The shipped suite uses
`unittest.mock.AsyncMock` against `create_decision_session` /
`add_decision_proposals` and never exercises SQLAlchemy ENUM coercion or
JSONB roundtrip.

**Why it's non-blocking.** `InstrumentType` (`app/models/trading.py:19`) is a
`StrEnum`, so passing `candidate.instrument_type: str` (e.g., `"equity_us"`)
through `ProposalCreate` to a `Mapped[InstrumentType]` column is supported by
SQLAlchemy. ROB-9's `tradingagents_research_service` already exercises the
same write path with a JSONB `original_payload`, and ROB-13's prod smoke
proved the path is healthy.

**Follow-up.** Add a single integration test (the plan's Task 5/8 condensed)
or run the deployed-runtime smoke once the synthesis path has a public
trigger.

### 6.2 Non-blocking — Safety test is in-process; plan asked for subprocess

**Observation.** `tests/services/test_trading_decision_synthesis_safety.py:29`
captures `sys.modules` baseline, then `importlib.import_module(...)` the three
new modules, then diffs. `import_module` on an already-loaded package is a
no-op. In the file's own run order (it imports the synthesis modules at
collection of `test_trading_decision_synthesis.py`), the synthesis modules
are already in `sys.modules` when `baseline` is taken, so the test diff is
empty regardless of what the synthesis modules transitively imported in that
session. The plan's Task 7 used a subprocess to get a clean import
environment for exactly this reason.

**Why it's non-blocking.** Re-verified independently in a fresh subprocess: 0
forbidden modules are loaded by importing the three new modules. The
implementation is currently safe; only the **regression-detection** strength
of the test is weaker than intended.

**Follow-up.** Convert the test body to a subprocess invocation matching the
ROB-9 pattern in
`tests/services/test_tradingagents_research_service_safety.py:32`. Five-to-ten
line refactor.

### 6.3 Non-blocking — FE Task 11 deferred

**Observation.** Plan Task 11 added `SynthesisPanel.tsx` to surface
`proposal.original_payload.synthesis` (advisory action, applied policies,
conflict flag). It was not implemented.

**Why it's non-blocking.** Existing `ProposalRow` already renders
`proposal.original_rationale` verbatim, which the synthesizer populates with a
human-readable string ("TradingAgents Underweight advisory downgraded NVDA
buy candidate to pullback_watch/none."). The proposal `proposal_kind` and
`side` chips also visibly change. `MarketBriefPanel` renders
`session.market_brief` JSON (including `synthesis_meta`) under a `<details>`.
This satisfies the AC's "approval page can display enough evidence" with
minimum viable surface area.

**Follow-up.** Add `SynthesisPanel.tsx` per plan Task 11 in a UI-track PR if
operators want a richer breakdown without expanding the JSON `<details>`.

### 6.4 Non-blocking — `extra="allow"` on advisory schemas

`AdvisoryEvidence` and `CandidateAnalysis` both use `model_config =
ConfigDict(extra="allow")`. Stray fields will be silently retained on the
model, then `model_dump(mode="json")`'d into the persisted payload. No safety
risk because the payload never crosses into broker code, but it does mean a
caller can leak extra fields into JSONB. Recommend tightening to `"forbid"`
in a follow-up to match the ROB-1/ROB-9 convention.

### 6.5 Non-blocking — `SynthesizedProposal.advisory` is required

The plan modeled `advisory: AdvisoryEvidence | None` to support a
"deterministic candidate without TradingAgents advisory" path. The impl
requires `advisory` to be present. If auto_trader callers ever want a session
that contains some candidates without an advisory, they'll need to construct
a stub `AdvisoryEvidence` (e.g., `advisory_action="Unknown"`). Trade-off:
simpler invariants but less flexibility. Document for the next caller.

## 7. Lint / type / smoke status (read-only verification)

| Check | Result |
|---|---|
| `uv run pytest tests/services/test_trading_decision_synthesis.py tests/services/test_trading_decision_synthesis_safety.py -v` | 12 passed |
| Implementer's recorded sweep `…test_tradingagents_research_service.py …test_tradingagents_research_service_safety.py …test_smoke_tradingagents_db_ingestion.py` | 37 passed (no regression in ROB-9 / ROB-13 surface) |
| `uv run ruff check` (per implementer log) | clean |
| `uv run ruff format` (per implementer log) | clean after format |
| `make typecheck` (per implementer log) | clean |
| Reviewer clean-subprocess import audit of synthesis modules | 0 forbidden among 373 new transitive imports |
| Grep for banned tokens (`subprocess`, `os.environ`, `os.getenv`, `dry_run`, `place_order`, `watch_alert`, `broker`, `kis_trading`, `upbit`) in the three new modules | 0 hits outside docstrings |

## 8. Risk register

| Risk | Severity | Mitigation in this PR | Recommended later mitigation |
|---|---|---|---|
| ENUM/JSONB write path silently incompatible | Low | `InstrumentType` is `StrEnum`; existing ROB-9 path uses identical helpers | Add a real-DB integration test (§6.1) |
| Future PR adds forbidden import; safety test silently passes | Medium | None in this PR; reviewer-run subprocess audit confirms current state safe | Convert safety test to subprocess (§6.2) |
| Operator misses synthesis effect on approval page | Low | `original_rationale` text + `proposal_kind`/`side` chips visibly change | Add `SynthesisPanel.tsx` (§6.3) |
| Caller leaks extra fields into payload | Low | Allowed extras have no path into broker code | Tighten to `extra="forbid"` (§6.4) |
| Need-driven candidate-without-advisory caller | Low | Caller can stub `AdvisoryEvidence(advisory_action="Unknown")` | Loosen `advisory` to `Optional` (§6.5) |

## 9. Recommendation

Proceed to PR / CI / merge / deploy-smoke. Capture §6.1, §6.2, §6.3, §6.4,
§6.5 as Linear follow-ups under a single ROB ticket (e.g.,
"ROB-14-followup: synthesis hardening"). None of them blocks ROB-14
acceptance.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-14
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-14-review-report.md
AOE_NEXT: create_pr
