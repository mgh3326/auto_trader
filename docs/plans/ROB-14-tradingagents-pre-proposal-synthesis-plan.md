# ROB-14 — TradingAgents Pre-Proposal Veto / Synthesis Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> (or superpowers:subagent-driven-development) to execute this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

- **Linear issue:** ROB-14 — TradingAgents pre-proposal veto/synthesis for Trading Decision Workspace
- **Linear URL:** https://linear.app/mgh3326/issue/ROB-14/tradingagents-pre-proposal-vetosynthesis-for-trading-decision
- **Branch / worktree:** `feature/ROB-14-tradingagents-pre-proposal-synthesis`
  (`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-14-tradingagents-pre-proposal-synthesis`)
- **Status:** Plan only. **No code begins until this plan is reviewed.**
- **Planner / reviewer:** Claude Opus
- **Implementer:** Codex (`codex --yolo`), scoped to this worktree
- **Depends on:** ROB-1 (#595), ROB-2 (#597), ROB-9 (#601, merged 55ecdb6e),
  ROB-13 (#604, merged 95eb2dee, prod smoke passed). All four are preconditions
  and **are not modified** by this PR.

**Goal:** Add a pre-proposal **synthesis layer** so TradingAgents advisory output
becomes risk-veto / confidence-adjuster / evidence for `TradingDecisionSession` +
`TradingDecisionProposal` rows **before** the trader.robinco.dev approval page is
shown. TradingAgents stays advisory-only; auto_trader keeps full authority over
the final proposal kind/side/confidence.

**Architecture:** One new pure synthesis module + one new persistence wrapper
that compose existing `trading_decision_service` helpers. The synthesis module
accepts (a) a list of normalized **deterministic candidates** from auto_trader
and (b) optional normalized **advisory evidence** (one per symbol), then returns
a list of synthesized proposal payloads with veto/downgrade/confidence policy
applied. A second module persists those payloads via the existing ROB-1
`create_decision_session` / `add_decision_proposals` API. The TradingAgents
subprocess call stays in `tradingagents_research_service` (ROB-9) and is wrapped
by a thin **adapter** that converts `TradingAgentsRunnerResult` →
`AdvisoryEvidence`. Synthesis is pure (no DB, no subprocess, no broker import).

**Tech Stack:** Python 3.13, dataclasses + `typing.Literal`, Pydantic v2 for the
shared pydantic-friendly types, SQLAlchemy async via existing
`trading_decision_service`, pytest (unit + integration + safety subprocess
import test).

---

## 1. Scope check

ROB-14 is **one** subsystem (synthesis + persistence orchestrator). It does not
introduce a scheduler, FastAPI route, screening pipeline integration, or UI
overhaul. Live screening → synthesis wiring is **deferred** (future ROB ticket).
The acceptance criteria are met by:

- a public Python entry point auto_trader callers can invoke once we wire it
  later;
- unit tests covering the synthesis policy;
- integration test covering DB persistence shape;
- subprocess-import safety test mirroring ROB-9;
- proposal payload + session market_brief embedding the advisory evidence so
  the existing `MarketBriefPanel` + rationale already render it on the approval
  page.

A small optional FE highlight component is included as Task 11; it is the only
SPA change and is gated on the unit tests for visibility.

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| `app/services/trading_decision_synthesis.py` (pure) | ✅ | — |
| `app/services/trading_decision_synthesis_persistence.py` (DB orchestrator) | ✅ | — |
| Adapter `TradingAgentsRunnerResult` → `AdvisoryEvidence` | ✅ inside `trading_decision_synthesis.py` (no subprocess import) | — |
| Pydantic schemas mirrored for cross-layer use | ✅ `app/schemas/trading_decision_synthesis.py` | — |
| Unit tests for synthesis policy | ✅ | — |
| Integration test for DB persistence shape | ✅ | — |
| Safety subprocess-import test | ✅ | — |
| Optional small FE panel highlighting `synthesis` block | ✅ Task 11 | — |
| FastAPI endpoint to trigger synthesis | ❌ | future ROB |
| Auto-wiring screening / KIS / Upbit candidates | ❌ | future ROB |
| Discord / Telegram notifications | ❌ | future ROB |
| `TradingDecisionAction` / `Counterfactual` / `Outcome` rows | ❌ — **forbidden** | — |
| Watch alert registration | ❌ — **forbidden** | — |
| Live or paper order placement, `dry_run=False` | ❌ — **forbidden** | — |
| Modifying `tradingagents_research_service.py`, `trading_decision_service.py`, models | ❌ | — |
| Modifying TradingAgents fork | ❌ | upstream-only |
| Reading or echoing API keys / env values | ❌ — **forbidden** | — |

## 3. Safety invariants this PR MUST enforce

1. The synthesis module imports **none** of: `app.services.kis*`,
   `app.services.upbit*`, `app.services.brokers`, `app.services.order_service`,
   `app.services.watch_alerts`, `app.services.paper_trading_service`,
   `app.services.openclaw_client`, `app.services.crypto_trade_cooldown_service`,
   `app.services.fill_notification`, `app.services.execution_event`,
   `app.services.redis_token_manager`, `app.services.kis_websocket*`,
   `app.tasks*`, `app.services.tradingagents_research_service` (no subprocess).
2. The persistence module additionally avoids `app.services.tradingagents_research_service`
   and writes only `TradingDecisionSession` + `TradingDecisionProposal` rows.
3. Every produced proposal payload has `advisory_only=True` and
   `execution_allowed=False` reflected at top level of `original_payload`.
4. `final_side ∈ {"buy", "sell", "none"}` and is fixed by synthesis, not by
   advisory text alone.
5. `final_proposal_kind ∈ ProposalKind` enum.
6. The persistence wrapper performs `await db.flush()` only and leaves
   `db.commit()` to the caller (matches existing `trading_decision_service`).
7. The persistence wrapper raises if any candidate has an
   `instrument_type` not in `InstrumentType` enum or a `symbol` empty/too long
   (>64).
8. Adapter from `TradingAgentsRunnerResult` accepts only payloads that already
   passed ROB-9 invariants (`status="ok"`, `advisory_only=True`,
   `execution_allowed=False`); it does not relax these literals.
9. Synthesis must never produce an `original_payload` that contains an OS env
   value or a value matching `(KEY|SECRET|TOKEN|PASSWORD|URL)$` (case-insensitive)
   unless it came from a **whitelisted** runner field (currently only
   `llm.base_url`). Enforced by an explicit allowlist and a unit test.
10. `tradingagents.base_url` may be persisted (it is the local OpenAI shim URL
    from settings; not a credential), but no other URL or token derived from
    `os.environ` is allowed in the payload.

## 4. Design

### 4.1 Data shapes

`app/schemas/trading_decision_synthesis.py` (new):

- `class CandidateAnalysis(BaseModel)` — input from auto_trader deterministic
  analysis. Fields:
  - `symbol: str` (1–64, must match `^[A-Za-z0-9._/-]{1,64}$`)
  - `instrument_type: InstrumentTypeLiteral` (re-uses ROB-2 literal)
  - `side: Literal["buy", "sell", "hold", "none"]`
  - `confidence: int` (`Field(ge=0, le=100)`)
  - `proposal_kind: ProposalKindLiteral` — initial proposed kind from analyzer
    (e.g. `enter`, `add`, `trim`, `exit`, `pullback_watch`, …)
  - `quantity: Decimal | None`, `quantity_pct: Decimal | None ge=0 le=100`,
    `amount: Decimal | None ge=0`, `price: Decimal | None ge=0`,
    `trigger_price: Decimal | None ge=0`, `threshold_pct: Decimal | None ge=0 le=100`
  - `currency: str | None` (≤8)
  - `rationale: str | None` (≤4000)
  - `extra_payload: dict = {}` — non-secret extra context (e.g.,
    technical indicators, holdings snapshot summary). Adapter rejects keys
    matching the secret-name regex.
- `class AdvisoryEvidence(BaseModel)` — normalized TradingAgents output.
  Fields:
  - `advisory_only: Literal[True]`
  - `execution_allowed: Literal[False]`
  - `advisory_action: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell", "Unknown"]`
  - `decision_text: str` (≤4000)
  - `final_trade_decision_text: str` (≤4000)
  - `provider: str`, `model: str`, `base_url: str`
  - `warnings: list[str]`
  - `risk_flags: list[Literal["failed_support", "failed_resistance", "trend_breakdown", "earnings_risk", "macro_risk", "liquidity_risk"]]`
  - `raw_state_keys: list[str]`
  - `as_of_date: date`
- `class SynthesizedProposal(BaseModel)` — output:
  - `candidate: CandidateAnalysis`
  - `advisory: AdvisoryEvidence | None`
  - `final_proposal_kind: ProposalKindLiteral`
  - `final_side: Literal["buy", "sell", "none"]`
  - `final_confidence: int` (0–100)
  - `conflict: bool`
  - `applied_policies: list[str]`
  - `evidence_summary: str` (≤4000)
  - `original_payload: dict` — already shaped for persistence
  - `original_rationale: str | None` — already shaped for persistence

### 4.2 Synthesis policy (pure function in
`app/services/trading_decision_synthesis.py`)

```python
def synthesize_pre_proposal(
    candidate: CandidateAnalysis,
    advisory: AdvisoryEvidence | None,
) -> SynthesizedProposal: ...

def synthesize_pre_proposals(
    candidates: Sequence[CandidateAnalysis],
    advisory_by_symbol: Mapping[str, AdvisoryEvidence],
) -> list[SynthesizedProposal]: ...
```

Rules (in order; first match wins for `applied_policies` ordering, but multiple
policies may stack):

1. **No advisory** (`advisory is None`): keep candidate values; `conflict=False`;
   `applied_policies=["no_advisory"]`; payload reflects candidate plus
   `synthesis.tradingagents = null`.
2. **Buy candidate + advisory `Underweight` or `Sell`**:
   - Downgrade: if `candidate.confidence >= 60` → `final_proposal_kind="pullback_watch"`,
     `final_side="none"`. If `candidate.confidence < 60` →
     `final_proposal_kind="avoid"`, `final_side="none"`.
   - `final_confidence = max(0, candidate.confidence - 30)`.
   - `conflict=True`, `applied_policies=["downgrade_buy_on_bearish_advisory"]`.
3. **Sell candidate + advisory `Overweight` or `Buy`**:
   - Keep `final_proposal_kind = candidate.proposal_kind`,
     `final_side = "sell"`.
   - `final_confidence = max(0, candidate.confidence - 25)`.
   - `conflict=True`, `applied_policies=["lower_confidence_on_sell_vs_bullish_advisory"]`.
4. **Agreement** (buy↔Buy/Overweight, sell↔Sell/Underweight):
   - Keep candidate kind/side; `final_confidence = min(100, candidate.confidence + 10)`.
   - `conflict=False`, `applied_policies=["confirm_with_advisory"]`.
5. **Hold candidate**: keep as-is; `applied_policies=["hold_passthrough"]`.
6. **Risk-flag adjustments** (stack on top of 1–5 unless already vetoed to
   `avoid`/`pullback_watch`):
   - `failed_support` or `trend_breakdown` present → subtract 15 from
     `final_confidence`, append policy `"risk_flag_minus_15"`.
   - `failed_resistance` for a sell → no change (consistent with sell intent).
   - `earnings_risk` or `macro_risk` or `liquidity_risk` → subtract 10,
     append `"risk_flag_minus_10"`.
7. **Advisory action `Unknown`**: do **not** veto, do **not** boost; record
   advisory as evidence only; `applied_policies=["advisory_unknown_evidence_only"]`.
8. **Clamp**: `final_confidence ∈ [0, 100]`.
9. `evidence_summary` is a single ≤4000-char string concatenating policy names,
   advisory action, and the first 200 chars of `decision_text` (for the
   approval-page rationale display).

### 4.3 Adapter `TradingAgentsRunnerResult` → `AdvisoryEvidence`

Function `advisory_from_runner_result(result, *, default_action="Unknown") ->
AdvisoryEvidence`:

- Re-asserts `result.advisory_only is True` and
  `result.execution_allowed is False`. Raises `AdvisoryInvariantViolation` if
  not (mirrors ROB-9 exception class — re-imported for parity).
- Extracts `advisory_action` via `parse_advisory_action(decision_text,
  final_trade_decision_text)`:
  - Case-insensitive keyword match prioritising `final_trade_decision_text`,
    falling back to `decision_text`.
  - Order: `"underweight"`→Underweight; `"overweight"`→Overweight;
    `"strong sell"|"sell signal"|" sell "`→Sell;
    `"strong buy"|"buy signal"|" buy "`→Buy;
    `"hold"|"neutral"`→Hold.
  - If multiple non-overlapping matches → prefer most specific
    (`underweight/overweight` over `buy/sell` over `hold`).
  - Otherwise → `"Unknown"`.
- Extracts `risk_flags` by scanning warnings + decision text for the literals
  in `AdvisoryEvidence.risk_flags`. Unknown phrases are ignored.
- Truncates `decision_text` and `final_trade_decision_text` to 4000 chars.

This adapter does **not** import `tradingagents_research_service`; it operates
on the already-validated `TradingAgentsRunnerResult` model.

### 4.4 Persistence orchestrator
(`app/services/trading_decision_synthesis_persistence.py`)

```python
async def build_synthesized_session(
    db: AsyncSession,
    *,
    user_id: int,
    market_scope: str,
    strategy_name: str,
    candidates: Sequence[CandidateAnalysis],
    advisory_by_symbol: Mapping[str, AdvisoryEvidence] | None = None,
    generated_at: datetime,
    notes: str | None = None,
) -> tuple[TradingDecisionSession, list[TradingDecisionProposal]]: ...
```

Behavior:

- Calls `synthesize_pre_proposals(candidates, advisory_by_symbol or {})`.
- Builds `market_brief = {"advisory_only": True, "execution_allowed": False,
  "synthesis_meta": {"candidates_count": …, "advisory_count": …,
  "applied_policies": <flat sorted unique list>, "tradingagents_models":
  <sorted unique list of advisory.model values>}}`.
- Calls `trading_decision_service.create_decision_session(source_profile=
  "auto_trader_synthesis", strategy_name=strategy_name, market_scope=…,
  market_brief=market_brief, generated_at=…, notes=…)` (existing helper, no
  changes).
- Calls `trading_decision_service.add_decision_proposals(session_id=…,
  proposals=[ProposalCreate(...) for s in synthesized])` building each from
  `s.original_payload` + `s.original_rationale` + numeric fields drawn from
  `s.candidate`.
- Returns `(session, proposals)`. Caller does `db.commit()`.
- **Does not** create `TradingDecisionAction`, `TradingDecisionCounterfactual`,
  or `TradingDecisionOutcome` rows. **Does not** import any forbidden module.
- All proposals have `user_response="pending"` (default).

### 4.5 Reflected payload shape (per proposal)

```json
{
  "advisory_only": true,
  "execution_allowed": false,
  "synthesis": {
    "auto_trader": {
      "side": "buy",
      "confidence": 65,
      "proposal_kind": "enter",
      "rationale": "<= 4000 chars",
      "extra": { "...non-secret extras...": "..." }
    },
    "tradingagents": {
      "advisory_only": true,
      "execution_allowed": false,
      "advisory_action": "Underweight",
      "model": "gpt-5.5",
      "provider": "openai-compatible",
      "base_url": "http://127.0.0.1:8796/v1",
      "decision_text": "...",
      "final_trade_decision_text": "...",
      "warnings": ["..."],
      "risk_flags": ["earnings_risk"],
      "raw_state_keys": ["market_report", "..."],
      "as_of_date": "2026-04-27"
    },
    "applied_policies": ["downgrade_buy_on_bearish_advisory", "risk_flag_minus_10"],
    "final_proposal_kind": "pullback_watch",
    "final_side": "none",
    "final_confidence": 25,
    "conflict": true,
    "evidence_summary": "Downgraded buy → pullback_watch ..."
  }
}
```

`session.market_brief.synthesis_meta` aggregates session-level info; the SPA's
existing `MarketBriefPanel` already renders both as JSON.

`proposal.original_rationale` carries the human-readable summary
(`evidence_summary`), so the existing `ProposalRow` "Original" panel surfaces
it without FE changes.

## 5. File map

### 5.1 New files

| File | Purpose |
|---|---|
| `app/schemas/trading_decision_synthesis.py` | Pydantic shapes: `CandidateAnalysis`, `AdvisoryEvidence`, `SynthesizedProposal` |
| `app/services/trading_decision_synthesis.py` | Pure synthesis policy + adapter from runner result |
| `app/services/trading_decision_synthesis_persistence.py` | DB orchestrator using existing `trading_decision_service` |
| `tests/services/test_trading_decision_synthesis.py` | Unit tests for the policy and the adapter |
| `tests/services/test_trading_decision_synthesis_persistence.py` | Integration test (real DB, stubbed inputs) |
| `tests/services/test_trading_decision_synthesis_safety.py` | Subprocess-import safety test (mirror ROB-9) |
| `frontend/trading-decision/src/components/SynthesisPanel.tsx` | (Task 11, optional) compact panel that reads `proposal.original_payload.synthesis` |
| `frontend/trading-decision/src/components/SynthesisPanel.module.css` | Styles for above |
| `frontend/trading-decision/src/__tests__/SynthesisPanel.test.tsx` | Vitest for above |

### 5.2 Files MODIFIED

| File | Change |
|---|---|
| `frontend/trading-decision/src/components/ProposalRow.tsx` | Mount `<SynthesisPanel synthesis={…} />` above existing `Original` panel when `proposal.original_payload.synthesis` exists. **No other changes.** |

### 5.3 Files NOT modified

| File | Reason |
|---|---|
| `app/services/trading_decision_service.py` | Re-used as-is; new orchestrator composes its existing helpers |
| `app/services/tradingagents_research_service.py` | Untouched; ROB-9 behavior preserved. The synthesis module imports only the **schemas** (`TradingAgentsRunnerResult`), not the subprocess service |
| `app/models/trading_decision.py` | No new columns or enum values |
| `app/routers/trading_decisions.py` | No new endpoint in this PR |
| `app/core/config.py` | No new settings |
| `scripts/smoke_tradingagents_db_ingestion.py` | Out of scope |
| `app/services/kis*`, `app/services/upbit*`, `app/services/brokers/*`, `app/services/order_service.py`, `app/services/watch_alerts.py`, `app/services/paper_trading_service.py`, `app/tasks/*` | Forbidden by §3 |

## 6. Self-review checklist (planner-verified before handing to Codex)

- [x] Each acceptance criterion maps to ≥1 task below (see §11 cross-ref).
- [x] No placeholders or "TODO later" steps.
- [x] Type names, method signatures, and field names are consistent across
  schemas, synthesis, and persistence sections.
- [x] Numeric thresholds (60, 30, 25, 15, 10) are stated literally in policy
  rules and re-asserted in unit-test assertions in Task 4.
- [x] All forbidden import prefixes from §3 are listed verbatim in the safety
  test in Task 9.

---

## 7. Pre-flight verification (planner)

- [x] `git status` clean on `feature/ROB-14-tradingagents-pre-proposal-synthesis`.
- [x] `git log --oneline -1` is `95eb2dee feat(rob-13): add TradingAgents
  advisory DB smoke harness (#604)`.
- [x] `app/services/tradingagents_research_service.py` exposes
  `ingest_tradingagents_research`, `run_tradingagents_research`,
  `AdvisoryInvariantViolation`, `TradingAgentsRunnerResult`.
- [x] `app/services/trading_decision_service.py` exposes
  `create_decision_session`, `add_decision_proposals`, `ProposalCreate`.
- [x] `app/models/trading_decision.py` enums: `ProposalKind`, `UserResponse`,
  `ActionKind`, `TrackKind`, `OutcomeHorizon`, `SessionStatus` — unchanged.
- [x] `app/schemas/trading_decisions.py` contains the literal types reused by
  the new schemas.
- [x] `frontend/trading-decision/src/components/ProposalRow.tsx` exposes a
  predictable mount point next to "Original" panel.

---

## 8. Tasks

### Task 1: Create the schemas module

**Files:**
- Create: `app/schemas/trading_decision_synthesis.py`
- Test: `tests/services/test_trading_decision_synthesis.py` (extended in later tasks)

- [ ] **Step 1.1: Write the failing test for schema shape**

Create `tests/services/test_trading_decision_synthesis.py` and add the file
header plus this test:

```python
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.trading_decision_synthesis import (
    AdvisoryEvidence,
    CandidateAnalysis,
    SynthesizedProposal,
)


def _candidate_kwargs(**over):
    base = {
        "symbol": "NVDA",
        "instrument_type": "equity_us",
        "side": "buy",
        "confidence": 65,
        "proposal_kind": "enter",
        "rationale": "auto_trader buy signal",
    }
    base.update(over)
    return base


def _advisory_kwargs(**over):
    base = {
        "advisory_only": True,
        "execution_allowed": False,
        "advisory_action": "Underweight",
        "decision_text": "Reduce exposure; macro risk elevated.",
        "final_trade_decision_text": "No execution authorized.",
        "provider": "openai-compatible",
        "model": "gpt-5.5",
        "base_url": "http://127.0.0.1:8796/v1",
        "warnings": ["macro liquidity risk noted"],
        "risk_flags": ["macro_risk"],
        "raw_state_keys": ["market_report"],
        "as_of_date": date(2026, 4, 27),
    }
    base.update(over)
    return base


def test_candidate_rejects_unknown_side():
    with pytest.raises(ValidationError):
        CandidateAnalysis(**_candidate_kwargs(side="strong_buy"))


def test_candidate_clamps_confidence_range():
    with pytest.raises(ValidationError):
        CandidateAnalysis(**_candidate_kwargs(confidence=101))


def test_advisory_pins_advisory_only_literals():
    with pytest.raises(ValidationError):
        AdvisoryEvidence(**_advisory_kwargs(advisory_only=False))
    with pytest.raises(ValidationError):
        AdvisoryEvidence(**_advisory_kwargs(execution_allowed=True))


def test_synthesized_proposal_payload_advisory_only_present():
    syn = SynthesizedProposal(
        candidate=CandidateAnalysis(**_candidate_kwargs()),
        advisory=AdvisoryEvidence(**_advisory_kwargs()),
        final_proposal_kind="pullback_watch",
        final_side="none",
        final_confidence=25,
        conflict=True,
        applied_policies=["downgrade_buy_on_bearish_advisory"],
        evidence_summary="Downgraded buy → pullback_watch.",
        original_payload={
            "advisory_only": True,
            "execution_allowed": False,
            "synthesis": {"final_proposal_kind": "pullback_watch"},
        },
        original_rationale="Downgraded buy → pullback_watch.",
    )
    assert syn.original_payload["advisory_only"] is True
    assert syn.original_payload["execution_allowed"] is False
```

- [ ] **Step 1.2: Run the failing test**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.trading_decision_synthesis'`.

- [ ] **Step 1.3: Implement the schemas**

Create `app/schemas/trading_decision_synthesis.py`:

```python
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.trading_decisions import (
    InstrumentTypeLiteral,
    ProposalKindLiteral,
    SideLiteral,
)

AdvisoryActionLiteral = Literal[
    "Buy", "Overweight", "Hold", "Underweight", "Sell", "Unknown"
]

RiskFlagLiteral = Literal[
    "failed_support",
    "failed_resistance",
    "trend_breakdown",
    "earnings_risk",
    "macro_risk",
    "liquidity_risk",
]

CandidateSideLiteral = Literal["buy", "sell", "hold", "none"]

AppliedPolicyLiteral = Literal[
    "no_advisory",
    "downgrade_buy_on_bearish_advisory",
    "lower_confidence_on_sell_vs_bullish_advisory",
    "confirm_with_advisory",
    "hold_passthrough",
    "risk_flag_minus_15",
    "risk_flag_minus_10",
    "advisory_unknown_evidence_only",
]


class CandidateAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._/-]{1,64}$")
    instrument_type: InstrumentTypeLiteral
    side: CandidateSideLiteral
    confidence: int = Field(ge=0, le=100)
    proposal_kind: ProposalKindLiteral
    quantity: Decimal | None = None
    quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    amount: Decimal | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    trigger_price: Decimal | None = Field(default=None, ge=0)
    threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    currency: str | None = Field(default=None, max_length=8)
    rationale: str | None = Field(default=None, max_length=4000)
    extra_payload: dict = Field(default_factory=dict)


class AdvisoryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advisory_only: Literal[True]
    execution_allowed: Literal[False]
    advisory_action: AdvisoryActionLiteral
    decision_text: str = Field(max_length=4000)
    final_trade_decision_text: str = Field(max_length=4000)
    provider: str
    model: str
    base_url: str
    warnings: list[str] = Field(default_factory=list)
    risk_flags: list[RiskFlagLiteral] = Field(default_factory=list)
    raw_state_keys: list[str] = Field(default_factory=list)
    as_of_date: date


class SynthesizedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: CandidateAnalysis
    advisory: AdvisoryEvidence | None
    final_proposal_kind: ProposalKindLiteral
    final_side: SideLiteral
    final_confidence: int = Field(ge=0, le=100)
    conflict: bool
    applied_policies: list[AppliedPolicyLiteral]
    evidence_summary: str = Field(max_length=4000)
    original_payload: dict
    original_rationale: str | None = Field(default=None, max_length=4000)
```

- [ ] **Step 1.4: Re-run test to verify pass**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v`
Expected: 4 passed.

- [ ] **Step 1.5: Commit**

```bash
git add app/schemas/trading_decision_synthesis.py \
        tests/services/test_trading_decision_synthesis.py
git commit -m "feat(rob-14): add CandidateAnalysis/AdvisoryEvidence/SynthesizedProposal schemas

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Implement the advisory text parser (pure)

**Files:**
- Create: `app/services/trading_decision_synthesis.py`
- Modify: `tests/services/test_trading_decision_synthesis.py`

- [ ] **Step 2.1: Write the failing tests for `parse_advisory_action`**

Append to `tests/services/test_trading_decision_synthesis.py`:

```python
from app.services.trading_decision_synthesis import parse_advisory_action


@pytest.mark.parametrize(
    "decision_text, final_text, expected",
    [
        ("market context bullish", "Strong Buy signal.", "Buy"),
        ("Reduce exposure", "Underweight is appropriate.", "Underweight"),
        ("Cautious overweight position warranted", "No execution authorized.", "Overweight"),
        ("Hold and reassess", "neutral stance recommended.", "Hold"),
        ("...", "Strong Sell.", "Sell"),
        ("Advisory research only.", "No execution authorized.", "Unknown"),
        ("buy or sell are both possible", "underweight tilt advised", "Underweight"),
    ],
)
def test_parse_advisory_action(decision_text, final_text, expected):
    assert parse_advisory_action(decision_text, final_text) == expected
```

- [ ] **Step 2.2: Run failing tests**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v -k parse_advisory_action`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.trading_decision_synthesis'`.

- [ ] **Step 2.3: Implement `parse_advisory_action` only (no synthesis yet)**

Create `app/services/trading_decision_synthesis.py`:

```python
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from app.schemas.trading_decision_synthesis import (
    AdvisoryActionLiteral,
    AdvisoryEvidence,
    CandidateAnalysis,
    SynthesizedProposal,
)
from app.schemas.tradingagents_research import TradingAgentsRunnerResult

_PRIORITISED_PATTERNS: tuple[tuple[re.Pattern[str], AdvisoryActionLiteral], ...] = (
    (re.compile(r"\bunderweight\b", re.IGNORECASE), "Underweight"),
    (re.compile(r"\boverweight\b", re.IGNORECASE), "Overweight"),
    (re.compile(r"\b(strong\s+sell|sell\s+signal|\bsell\b)\b", re.IGNORECASE), "Sell"),
    (re.compile(r"\b(strong\s+buy|buy\s+signal|\bbuy\b)\b", re.IGNORECASE), "Buy"),
    (re.compile(r"\b(hold|neutral)\b", re.IGNORECASE), "Hold"),
)


def parse_advisory_action(
    decision_text: str, final_trade_decision_text: str
) -> AdvisoryActionLiteral:
    """Map TradingAgents advisory free-form text to a normalized action label.

    Priority: most specific (under/overweight) > directional (buy/sell) > neutral
    (hold). `final_trade_decision_text` is searched first; falls through to
    `decision_text`. Anything else returns 'Unknown'.
    """
    for source in (final_trade_decision_text or "", decision_text or ""):
        for pattern, label in _PRIORITISED_PATTERNS:
            if pattern.search(source):
                return label
    return "Unknown"
```

- [ ] **Step 2.4: Run tests to verify pass**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v`
Expected: all 4 + 7 parametric = 11 passed.

- [ ] **Step 2.5: Commit**

```bash
git add app/services/trading_decision_synthesis.py \
        tests/services/test_trading_decision_synthesis.py
git commit -m "feat(rob-14): parse advisory action from TradingAgents text output

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Implement adapter `advisory_from_runner_result`

**Files:**
- Modify: `app/services/trading_decision_synthesis.py`
- Modify: `tests/services/test_trading_decision_synthesis.py`

- [ ] **Step 3.1: Write the failing tests**

Append to `tests/services/test_trading_decision_synthesis.py`:

```python
from app.schemas.tradingagents_research import TradingAgentsRunnerResult
from app.services.trading_decision_synthesis import advisory_from_runner_result


def _runner_payload(**over) -> dict:
    base = {
        "status": "ok",
        "symbol": "NVDA",
        "as_of_date": "2026-04-27",
        "decision": "Reduce exposure; macro liquidity risk noted.",
        "advisory_only": True,
        "execution_allowed": False,
        "analysts": ["market", "news"],
        "llm": {
            "provider": "openai-compatible",
            "model": "gpt-5.5",
            "base_url": "http://127.0.0.1:8796/v1",
        },
        "config": {
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "max_recur_limit": 30,
            "output_language": "English",
            "checkpoint_enabled": False,
        },
        "warnings": {
            "structured_output": [
                "earnings sensitivity noted",
                "macro liquidity risk noted",
            ]
        },
        "final_trade_decision": "Underweight is appropriate. No execution authorized.",
        "raw_state_keys": ["market_report", "news_report"],
    }
    base.update(over)
    return base


def test_advisory_from_runner_extracts_action_and_risk_flags():
    runner = TradingAgentsRunnerResult.model_validate(_runner_payload())
    ev = advisory_from_runner_result(runner)
    assert ev.advisory_only is True
    assert ev.execution_allowed is False
    assert ev.advisory_action == "Underweight"
    assert "earnings_risk" in ev.risk_flags
    assert "macro_risk" in ev.risk_flags
    assert "liquidity_risk" in ev.risk_flags
    assert ev.model == "gpt-5.5"
    assert ev.base_url == "http://127.0.0.1:8796/v1"


def test_advisory_from_runner_unknown_when_no_keywords():
    runner = TradingAgentsRunnerResult.model_validate(
        _runner_payload(
            decision="Advisory research only.",
            final_trade_decision="No execution authorized.",
            warnings={"structured_output": []},
        )
    )
    ev = advisory_from_runner_result(runner)
    assert ev.advisory_action == "Unknown"
    assert ev.risk_flags == []


def test_advisory_from_runner_truncates_long_text():
    runner = TradingAgentsRunnerResult.model_validate(
        _runner_payload(decision="x" * 9000, final_trade_decision="y" * 9000)
    )
    ev = advisory_from_runner_result(runner)
    assert len(ev.decision_text) == 4000
    assert len(ev.final_trade_decision_text) == 4000
```

- [ ] **Step 3.2: Run failing tests**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v -k advisory_from_runner`
Expected: FAIL — `advisory_from_runner_result` not defined.

- [ ] **Step 3.3: Implement adapter**

Append to `app/services/trading_decision_synthesis.py`:

```python
_RISK_FLAG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfailed[\s_-]+support\b", re.IGNORECASE), "failed_support"),
    (re.compile(r"\bfailed[\s_-]+resistance\b", re.IGNORECASE), "failed_resistance"),
    (re.compile(r"\btrend[\s_-]+breakdown\b", re.IGNORECASE), "trend_breakdown"),
    (re.compile(r"\bearnings\b", re.IGNORECASE), "earnings_risk"),
    (re.compile(r"\bmacro\b", re.IGNORECASE), "macro_risk"),
    (re.compile(r"\bliquidity\b", re.IGNORECASE), "liquidity_risk"),
)


def _extract_risk_flags(*texts: str) -> list[str]:
    found: list[str] = []
    for source in texts:
        if not source:
            continue
        for pattern, flag in _RISK_FLAG_PATTERNS:
            if pattern.search(source) and flag not in found:
                found.append(flag)
    return found


def advisory_from_runner_result(
    result: TradingAgentsRunnerResult,
) -> AdvisoryEvidence:
    """Convert a validated TradingAgents runner result to AdvisoryEvidence.

    The runner result is already pinned advisory_only=True / execution_allowed=False
    by the schema in app/schemas/tradingagents_research.py; this adapter restates
    the literals at the synthesis-layer boundary.
    """
    warnings_text = "\n".join(result.warnings.structured_output)
    risk_flags = _extract_risk_flags(
        result.decision, result.final_trade_decision, warnings_text
    )
    return AdvisoryEvidence(
        advisory_only=True,
        execution_allowed=False,
        advisory_action=parse_advisory_action(
            result.decision, result.final_trade_decision
        ),
        decision_text=result.decision[:4000],
        final_trade_decision_text=result.final_trade_decision[:4000],
        provider=result.llm.provider,
        model=result.llm.model,
        base_url=result.llm.base_url,
        warnings=list(result.warnings.structured_output),
        risk_flags=risk_flags,
        raw_state_keys=list(result.raw_state_keys),
        as_of_date=result.as_of_date,
    )
```

- [ ] **Step 3.4: Run tests to verify pass**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v`
Expected: all passing (≥14).

- [ ] **Step 3.5: Commit**

```bash
git add app/services/trading_decision_synthesis.py \
        tests/services/test_trading_decision_synthesis.py
git commit -m "feat(rob-14): adapter from TradingAgents runner result to AdvisoryEvidence

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Implement `synthesize_pre_proposal` policy (pure)

**Files:**
- Modify: `app/services/trading_decision_synthesis.py`
- Modify: `tests/services/test_trading_decision_synthesis.py`

- [ ] **Step 4.1: Write the failing tests covering all policy branches**

Append to `tests/services/test_trading_decision_synthesis.py`:

```python
from app.services.trading_decision_synthesis import (
    synthesize_pre_proposal,
    synthesize_pre_proposals,
)


def _candidate(**over) -> CandidateAnalysis:
    return CandidateAnalysis(**_candidate_kwargs(**over))


def _advisory(**over) -> AdvisoryEvidence:
    return AdvisoryEvidence(**_advisory_kwargs(**over))


def test_no_advisory_keeps_candidate():
    syn = synthesize_pre_proposal(_candidate(), advisory=None)
    assert syn.final_side == "buy"
    assert syn.final_proposal_kind == "enter"
    assert syn.final_confidence == 65
    assert syn.conflict is False
    assert "no_advisory" in syn.applied_policies
    assert syn.original_payload["advisory_only"] is True
    assert syn.original_payload["execution_allowed"] is False
    assert syn.original_payload["synthesis"]["tradingagents"] is None


def test_buy_underweight_high_confidence_downgrades_to_pullback_watch():
    syn = synthesize_pre_proposal(
        _candidate(side="buy", confidence=70, proposal_kind="enter"),
        advisory=_advisory(advisory_action="Underweight", risk_flags=[]),
    )
    assert syn.final_side == "none"
    assert syn.final_proposal_kind == "pullback_watch"
    assert syn.final_confidence == 40
    assert syn.conflict is True
    assert "downgrade_buy_on_bearish_advisory" in syn.applied_policies


def test_buy_underweight_low_confidence_downgrades_to_avoid():
    syn = synthesize_pre_proposal(
        _candidate(side="buy", confidence=55, proposal_kind="enter"),
        advisory=_advisory(advisory_action="Sell", risk_flags=[]),
    )
    assert syn.final_proposal_kind == "avoid"
    assert syn.final_side == "none"
    assert syn.final_confidence == 25


def test_sell_overweight_lowers_confidence_keeps_sell():
    syn = synthesize_pre_proposal(
        _candidate(side="sell", confidence=70, proposal_kind="exit"),
        advisory=_advisory(advisory_action="Overweight", risk_flags=[]),
    )
    assert syn.final_side == "sell"
    assert syn.final_proposal_kind == "exit"
    assert syn.final_confidence == 45
    assert syn.conflict is True
    assert "lower_confidence_on_sell_vs_bullish_advisory" in syn.applied_policies


def test_buy_buy_agreement_boosts_confidence_capped_at_100():
    syn = synthesize_pre_proposal(
        _candidate(side="buy", confidence=95, proposal_kind="enter"),
        advisory=_advisory(advisory_action="Buy", risk_flags=[]),
    )
    assert syn.final_side == "buy"
    assert syn.final_confidence == 100
    assert syn.conflict is False
    assert "confirm_with_advisory" in syn.applied_policies


def test_hold_passthrough_with_advisory_evidence_only():
    syn = synthesize_pre_proposal(
        _candidate(side="hold", confidence=50, proposal_kind="no_action"),
        advisory=_advisory(advisory_action="Buy", risk_flags=[]),
    )
    assert syn.final_side == "none"
    assert syn.final_proposal_kind == "no_action"
    assert syn.final_confidence == 50
    assert syn.conflict is False
    assert "hold_passthrough" in syn.applied_policies


def test_advisory_unknown_does_not_veto():
    syn = synthesize_pre_proposal(
        _candidate(side="buy", confidence=70, proposal_kind="enter"),
        advisory=_advisory(advisory_action="Unknown", risk_flags=[]),
    )
    assert syn.final_side == "buy"
    assert syn.final_proposal_kind == "enter"
    assert syn.final_confidence == 70
    assert "advisory_unknown_evidence_only" in syn.applied_policies


def test_risk_flag_failed_support_subtracts_15():
    syn = synthesize_pre_proposal(
        _candidate(side="buy", confidence=80, proposal_kind="enter"),
        advisory=_advisory(advisory_action="Hold", risk_flags=["failed_support"]),
    )
    assert "risk_flag_minus_15" in syn.applied_policies
    assert syn.final_confidence == 80 - 15  # Hold passthrough then risk-flag


def test_risk_flag_macro_subtracts_10_after_downgrade():
    syn = synthesize_pre_proposal(
        _candidate(side="buy", confidence=80, proposal_kind="enter"),
        advisory=_advisory(advisory_action="Underweight", risk_flags=["macro_risk"]),
    )
    assert "downgrade_buy_on_bearish_advisory" in syn.applied_policies
    assert "risk_flag_minus_10" in syn.applied_policies
    assert syn.final_confidence == max(0, 80 - 30 - 10)


def test_payload_includes_full_advisory_evidence():
    syn = synthesize_pre_proposal(
        _candidate(side="buy", confidence=70, proposal_kind="enter"),
        advisory=_advisory(advisory_action="Underweight", risk_flags=["earnings_risk"]),
    )
    ev = syn.original_payload["synthesis"]["tradingagents"]
    assert ev["advisory_only"] is True
    assert ev["execution_allowed"] is False
    assert ev["advisory_action"] == "Underweight"
    assert ev["model"] == "gpt-5.5"
    assert ev["base_url"] == "http://127.0.0.1:8796/v1"
    assert ev["risk_flags"] == ["earnings_risk"]


def test_synthesize_pre_proposals_routes_advisory_by_symbol():
    nvda = _candidate(symbol="NVDA", side="buy", confidence=70, proposal_kind="enter")
    aapl = _candidate(symbol="AAPL", side="hold", confidence=40, proposal_kind="no_action")
    advisory_by_symbol = {
        "NVDA": _advisory(advisory_action="Underweight"),
        # AAPL has no advisory entry
    }
    out = synthesize_pre_proposals([nvda, aapl], advisory_by_symbol)
    assert len(out) == 2
    assert out[0].candidate.symbol == "NVDA"
    assert "downgrade_buy_on_bearish_advisory" in out[0].applied_policies
    assert out[1].candidate.symbol == "AAPL"
    assert out[1].advisory is None
    assert "no_advisory" in out[1].applied_policies
```

- [ ] **Step 4.2: Run failing tests**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v -k synthesize`
Expected: FAIL — functions undefined.

- [ ] **Step 4.3: Implement `synthesize_pre_proposal` and `synthesize_pre_proposals`**

Append to `app/services/trading_decision_synthesis.py`:

```python
def _bearish(action: str) -> bool:
    return action in ("Underweight", "Sell")


def _bullish(action: str) -> bool:
    return action in ("Overweight", "Buy")


def _build_payload(
    candidate: CandidateAnalysis,
    advisory: AdvisoryEvidence | None,
    *,
    final_proposal_kind: str,
    final_side: str,
    final_confidence: int,
    conflict: bool,
    applied_policies: list[str],
    evidence_summary: str,
) -> dict:
    advisory_block: dict | None = None
    if advisory is not None:
        advisory_block = {
            "advisory_only": True,
            "execution_allowed": False,
            "advisory_action": advisory.advisory_action,
            "model": advisory.model,
            "provider": advisory.provider,
            "base_url": advisory.base_url,
            "decision_text": advisory.decision_text,
            "final_trade_decision_text": advisory.final_trade_decision_text,
            "warnings": list(advisory.warnings),
            "risk_flags": list(advisory.risk_flags),
            "raw_state_keys": list(advisory.raw_state_keys),
            "as_of_date": advisory.as_of_date.isoformat(),
        }
    return {
        "advisory_only": True,
        "execution_allowed": False,
        "synthesis": {
            "auto_trader": {
                "side": candidate.side,
                "confidence": candidate.confidence,
                "proposal_kind": candidate.proposal_kind,
                "rationale": candidate.rationale,
                "extra": dict(candidate.extra_payload),
            },
            "tradingagents": advisory_block,
            "applied_policies": list(applied_policies),
            "final_proposal_kind": final_proposal_kind,
            "final_side": final_side,
            "final_confidence": final_confidence,
            "conflict": conflict,
            "evidence_summary": evidence_summary,
        },
    }


def synthesize_pre_proposal(
    candidate: CandidateAnalysis,
    advisory: AdvisoryEvidence | None,
) -> SynthesizedProposal:
    applied: list[str] = []
    conflict = False

    if advisory is None:
        applied.append("no_advisory")
        final_kind = candidate.proposal_kind
        final_side: str = "none" if candidate.side in ("hold", "none") else candidate.side
        final_conf = candidate.confidence
    elif candidate.side == "buy" and _bearish(advisory.advisory_action):
        if candidate.confidence >= 60:
            final_kind = "pullback_watch"
        else:
            final_kind = "avoid"
        final_side = "none"
        final_conf = max(0, candidate.confidence - 30)
        conflict = True
        applied.append("downgrade_buy_on_bearish_advisory")
    elif candidate.side == "sell" and _bullish(advisory.advisory_action):
        final_kind = candidate.proposal_kind
        final_side = "sell"
        final_conf = max(0, candidate.confidence - 25)
        conflict = True
        applied.append("lower_confidence_on_sell_vs_bullish_advisory")
    elif (candidate.side == "buy" and _bullish(advisory.advisory_action)) or (
        candidate.side == "sell" and _bearish(advisory.advisory_action)
    ):
        final_kind = candidate.proposal_kind
        final_side = candidate.side
        final_conf = min(100, candidate.confidence + 10)
        applied.append("confirm_with_advisory")
    elif candidate.side == "hold":
        final_kind = candidate.proposal_kind
        final_side = "none"
        final_conf = candidate.confidence
        applied.append("hold_passthrough")
    elif advisory.advisory_action == "Unknown":
        final_kind = candidate.proposal_kind
        final_side = "none" if candidate.side in ("hold", "none") else candidate.side
        final_conf = candidate.confidence
        applied.append("advisory_unknown_evidence_only")
    elif advisory.advisory_action == "Hold":
        final_kind = candidate.proposal_kind
        final_side = "none" if candidate.side in ("hold", "none") else candidate.side
        final_conf = candidate.confidence
        applied.append("advisory_unknown_evidence_only")  # treated as no-veto
    else:
        # Defensive default
        final_kind = candidate.proposal_kind
        final_side = "none" if candidate.side in ("hold", "none") else candidate.side
        final_conf = candidate.confidence
        applied.append("advisory_unknown_evidence_only")

    if advisory is not None:
        if any(f in advisory.risk_flags for f in ("failed_support", "trend_breakdown")):
            final_conf = max(0, final_conf - 15)
            applied.append("risk_flag_minus_15")
        if any(
            f in advisory.risk_flags
            for f in ("earnings_risk", "macro_risk", "liquidity_risk")
        ):
            final_conf = max(0, final_conf - 10)
            applied.append("risk_flag_minus_10")

    final_conf = max(0, min(100, final_conf))

    advisory_action = advisory.advisory_action if advisory is not None else "n/a"
    decision_excerpt = (advisory.decision_text[:200] if advisory is not None else "")
    evidence_summary = (
        f"final={final_kind}/{final_side} conf={final_conf} "
        f"advisory={advisory_action} policies={','.join(applied)} "
        f"excerpt={decision_excerpt}"
    )[:4000]

    payload = _build_payload(
        candidate,
        advisory,
        final_proposal_kind=final_kind,
        final_side=final_side,
        final_confidence=final_conf,
        conflict=conflict,
        applied_policies=applied,
        evidence_summary=evidence_summary,
    )

    return SynthesizedProposal(
        candidate=candidate,
        advisory=advisory,
        final_proposal_kind=final_kind,
        final_side=final_side,
        final_confidence=final_conf,
        conflict=conflict,
        applied_policies=applied,
        evidence_summary=evidence_summary,
        original_payload=payload,
        original_rationale=evidence_summary,
    )


def synthesize_pre_proposals(
    candidates: Sequence[CandidateAnalysis],
    advisory_by_symbol: Mapping[str, AdvisoryEvidence],
) -> list[SynthesizedProposal]:
    return [
        synthesize_pre_proposal(c, advisory_by_symbol.get(c.symbol))
        for c in candidates
    ]
```

- [ ] **Step 4.4: Run tests to verify pass**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v`
Expected: all passing.

- [ ] **Step 4.5: Commit**

```bash
git add app/services/trading_decision_synthesis.py \
        tests/services/test_trading_decision_synthesis.py
git commit -m "feat(rob-14): synthesize_pre_proposal pure-function policy with veto/downgrade

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Add the persistence orchestrator

**Files:**
- Create: `app/services/trading_decision_synthesis_persistence.py`
- Test: `tests/services/test_trading_decision_synthesis_persistence.py`

- [ ] **Step 5.1: Write the failing integration test (uses real DB pattern from ROB-9)**

Create `tests/services/test_trading_decision_synthesis_persistence.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading_decision import (
    TradingDecisionAction,
    TradingDecisionCounterfactual,
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
)
from app.schemas.trading_decision_synthesis import (
    AdvisoryEvidence,
    CandidateAnalysis,
)
from app.services.trading_decision_synthesis_persistence import (
    build_synthesized_session,
)

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(
                text("SELECT to_regclass('trading_decision_sessions')")
            )
            if row.scalar_one_or_none() is None:
                pytest.skip("trading_decision tables are not migrated")
    except Exception:
        pytest.skip("database is not available for integration persistence checks")


async def _create_user() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:u, :e, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {"u": f"rob14_synth_{suffix}", "e": f"rob14_synth_{suffix}@example.com"},
            )
        ).scalar_one()
        await session.commit()
        return user_id


async def _cleanup(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :u"), {"u": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_synthesized_session_persists_advisory_block():
    await _ensure_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds, proposals = await build_synthesized_session(
                session,
                user_id=user_id,
                market_scope="us",
                strategy_name="auto_trader+tradingagents",
                candidates=[
                    CandidateAnalysis(
                        symbol="NVDA",
                        instrument_type="equity_us",
                        side="buy",
                        confidence=70,
                        proposal_kind="enter",
                        rationale="auto_trader buy signal",
                    )
                ],
                advisory_by_symbol={
                    "NVDA": AdvisoryEvidence(
                        advisory_only=True,
                        execution_allowed=False,
                        advisory_action="Underweight",
                        decision_text="Reduce exposure.",
                        final_trade_decision_text="No execution authorized.",
                        provider="openai-compatible",
                        model="gpt-5.5",
                        base_url="http://127.0.0.1:8796/v1",
                        warnings=["macro liquidity risk noted"],
                        risk_flags=["macro_risk"],
                        raw_state_keys=["market_report"],
                        as_of_date=datetime(2026, 4, 27, tzinfo=UTC).date(),
                    )
                },
                generated_at=datetime.now(UTC),
                notes="advisory-only synthesis",
            )
            await session.commit()

            assert ds.source_profile == "auto_trader_synthesis"
            assert ds.market_brief["advisory_only"] is True
            assert ds.market_brief["execution_allowed"] is False
            assert "synthesis_meta" in ds.market_brief
            assert len(proposals) == 1
            p = proposals[0]
            assert p.proposal_kind == "pullback_watch"
            assert p.side == "none"
            payload = p.original_payload
            assert payload["advisory_only"] is True
            assert payload["execution_allowed"] is False
            assert payload["synthesis"]["tradingagents"]["advisory_action"] == "Underweight"
            assert payload["synthesis"]["final_confidence"] == 30  # 70-30-10
            assert payload["synthesis"]["conflict"] is True

            # Zero side-effect rows
            assert (
                await session.scalar(
                    select(func.count(TradingDecisionAction.id)).where(
                        TradingDecisionAction.proposal_id == p.id
                    )
                )
            ) == 0
            assert (
                await session.scalar(
                    select(func.count(TradingDecisionCounterfactual.id)).where(
                        TradingDecisionCounterfactual.proposal_id == p.id
                    )
                )
            ) == 0
            assert (
                await session.scalar(
                    select(func.count(TradingDecisionOutcome.id)).where(
                        TradingDecisionOutcome.proposal_id == p.id
                    )
                )
            ) == 0
    finally:
        await _cleanup(user_id)
```

- [ ] **Step 5.2: Run failing test**

Run: `uv run pytest tests/services/test_trading_decision_synthesis_persistence.py -v`
Expected: FAIL — module not found.

- [ ] **Step 5.3: Implement persistence orchestrator**

Create `app/services/trading_decision_synthesis_persistence.py`:

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ProposalKind,
    TradingDecisionProposal,
    TradingDecisionSession,
)
from app.schemas.trading_decision_synthesis import (
    AdvisoryEvidence,
    CandidateAnalysis,
)
from app.services import trading_decision_service
from app.services.trading_decision_service import ProposalCreate
from app.services.trading_decision_synthesis import synthesize_pre_proposals


def _coerce_instrument_type(value: str) -> InstrumentType:
    return InstrumentType(value)


def _coerce_proposal_kind(value: str) -> ProposalKind:
    return ProposalKind(value)


async def build_synthesized_session(
    db: AsyncSession,
    *,
    user_id: int,
    market_scope: str,
    strategy_name: str,
    candidates: Sequence[CandidateAnalysis],
    advisory_by_symbol: Mapping[str, AdvisoryEvidence] | None = None,
    generated_at: datetime,
    notes: str | None = None,
) -> tuple[TradingDecisionSession, list[TradingDecisionProposal]]:
    """Compose deterministic candidates with TradingAgents advisory evidence into
    one TradingDecisionSession plus N TradingDecisionProposal rows.

    Caller controls db.commit(). No broker, watch, paper, or order side-effect
    rows are created. TradingAgents stays advisory-only (its evidence is mirrored
    in proposal.original_payload['synthesis'] and session.market_brief).
    """
    synthesized = synthesize_pre_proposals(candidates, advisory_by_symbol or {})

    applied_policies = sorted(
        {pol for s in synthesized for pol in s.applied_policies}
    )
    tradingagents_models = sorted(
        {s.advisory.model for s in synthesized if s.advisory is not None}
    )

    market_brief = {
        "advisory_only": True,
        "execution_allowed": False,
        "synthesis_meta": {
            "candidates_count": len(synthesized),
            "advisory_count": sum(1 for s in synthesized if s.advisory is not None),
            "applied_policies": applied_policies,
            "tradingagents_models": tradingagents_models,
            "conflicts": sum(1 for s in synthesized if s.conflict),
        },
    }

    session_obj = await trading_decision_service.create_decision_session(
        db,
        user_id=user_id,
        source_profile="auto_trader_synthesis",
        strategy_name=strategy_name,
        market_scope=market_scope,
        market_brief=market_brief,
        generated_at=generated_at,
        notes=notes,
    )

    proposal_creates: list[ProposalCreate] = []
    for s in synthesized:
        c = s.candidate
        proposal_creates.append(
            ProposalCreate(
                symbol=c.symbol,
                instrument_type=_coerce_instrument_type(c.instrument_type),
                proposal_kind=_coerce_proposal_kind(s.final_proposal_kind),
                side=s.final_side,
                original_quantity=c.quantity,
                original_quantity_pct=c.quantity_pct,
                original_amount=c.amount,
                original_price=c.price,
                original_trigger_price=c.trigger_price,
                original_threshold_pct=c.threshold_pct,
                original_currency=c.currency,
                original_rationale=s.original_rationale,
                original_payload=s.original_payload,
            )
        )

    proposals = await trading_decision_service.add_decision_proposals(
        db, session_id=session_obj.id, proposals=proposal_creates
    )
    return session_obj, proposals
```

- [ ] **Step 5.4: Run integration test**

Run: `uv run pytest tests/services/test_trading_decision_synthesis_persistence.py -v`
Expected: PASS (or `skip` if Postgres is not migrated locally — that is an
acceptable env state per the same pattern used by ROB-9 integration tests).

- [ ] **Step 5.5: Commit**

```bash
git add app/services/trading_decision_synthesis_persistence.py \
        tests/services/test_trading_decision_synthesis_persistence.py
git commit -m "feat(rob-14): persistence orchestrator for synthesized session+proposals

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: Add agreement / hold passthrough integration assertions

**Files:**
- Modify: `tests/services/test_trading_decision_synthesis_persistence.py`

- [ ] **Step 6.1: Add an integration test for the agreement path**

Append to `tests/services/test_trading_decision_synthesis_persistence.py`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_synthesized_session_agreement_keeps_buy():
    await _ensure_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds, proposals = await build_synthesized_session(
                session,
                user_id=user_id,
                market_scope="us",
                strategy_name="auto_trader+tradingagents",
                candidates=[
                    CandidateAnalysis(
                        symbol="NVDA",
                        instrument_type="equity_us",
                        side="buy",
                        confidence=60,
                        proposal_kind="enter",
                    )
                ],
                advisory_by_symbol={
                    "NVDA": AdvisoryEvidence(
                        advisory_only=True,
                        execution_allowed=False,
                        advisory_action="Buy",
                        decision_text="Strong Buy.",
                        final_trade_decision_text="No execution authorized.",
                        provider="openai-compatible",
                        model="gpt-5.5",
                        base_url="http://127.0.0.1:8796/v1",
                        warnings=[],
                        risk_flags=[],
                        raw_state_keys=[],
                        as_of_date=datetime(2026, 4, 27, tzinfo=UTC).date(),
                    )
                },
                generated_at=datetime.now(UTC),
            )
            await session.commit()

            assert proposals[0].proposal_kind == "enter"
            assert proposals[0].side == "buy"
            assert proposals[0].original_payload["synthesis"]["final_confidence"] == 70
    finally:
        await _cleanup(user_id)
```

- [ ] **Step 6.2: Run**

Run: `uv run pytest tests/services/test_trading_decision_synthesis_persistence.py -v`
Expected: 2 passed (or both skipped if DB unavailable; same as Step 5.4).

- [ ] **Step 6.3: Commit**

```bash
git add tests/services/test_trading_decision_synthesis_persistence.py
git commit -m "test(rob-14): integration coverage for agreement path

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Forbidden-import safety test for synthesis modules

**Files:**
- Create: `tests/services/test_trading_decision_synthesis_safety.py`

- [ ] **Step 7.1: Write the safety test (mirrors ROB-9 pattern)**

Create `tests/services/test_trading_decision_synthesis_safety.py`:

```python
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

_FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.tasks",
    "app.services.tradingagents_research_service",
]


@pytest.mark.integration
@pytest.mark.parametrize(
    "module_relpath",
    [
        "app/services/trading_decision_synthesis.py",
        "app/services/trading_decision_synthesis_persistence.py",
    ],
)
def test_synthesis_modules_do_not_import_execution_paths(module_relpath: str) -> None:
    project_root = str(pathlib.Path(__file__).parent.parent.parent)
    service_file = str(
        pathlib.Path(__file__).parent.parent.parent / module_relpath
    )
    module_name = (
        module_relpath.replace("/", ".").removesuffix(".py")
    )

    script = f"""
import sys
import types
import json
import importlib.util
import pathlib

project_root = {project_root!r}
service_file = {service_file!r}
module_name = {module_name!r}
sys.path.insert(0, project_root)

svc_stub = types.ModuleType("app.services")
svc_stub.__path__ = [str(pathlib.Path(project_root) / "app" / "services")]
svc_stub.__package__ = "app.services"
sys.modules.setdefault("app.services", svc_stub)

spec = importlib.util.spec_from_file_location(module_name, service_file)
mod = importlib.util.module_from_spec(spec)
sys.modules[module_name] = mod
spec.loader.exec_module(mod)

print(json.dumps(sorted(sys.modules.keys())))
"""

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"Subprocess import of {module_name} failed:\n{result.stderr}"
    )

    loaded: list[str] = json.loads(result.stdout)
    violations = [
        m
        for prefix in _FORBIDDEN_PREFIXES
        for m in loaded
        if m == prefix or m.startswith(prefix + ".")
    ]

    assert not violations, (
        f"Forbidden module(s) loaded from {module_name}:\n"
        + "\n".join(violations)
    )
```

- [ ] **Step 7.2: Run safety test**

Run: `uv run pytest tests/services/test_trading_decision_synthesis_safety.py -v`
Expected: 2 passed (parametrised).

- [ ] **Step 7.3: If a violation appears**, the failure message names the
  offending import. Inspect the suspect module and refactor to use the
  schema-only import (`from app.schemas.tradingagents_research import
  TradingAgentsRunnerResult`). Re-run.

- [ ] **Step 7.4: Commit**

```bash
git add tests/services/test_trading_decision_synthesis_safety.py
git commit -m "test(rob-14): forbid execution-path imports in synthesis modules

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 8: Advisory-only invariant test on persisted payload

**Files:**
- Modify: `tests/services/test_trading_decision_synthesis_persistence.py`

- [ ] **Step 8.1: Add a test that re-reads the persisted proposal and asserts
  every required field is present in `original_payload`**

Append to `tests/services/test_trading_decision_synthesis_persistence.py`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_persisted_payload_contains_all_required_advisory_fields():
    await _ensure_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            _, proposals = await build_synthesized_session(
                session,
                user_id=user_id,
                market_scope="us",
                strategy_name="auto_trader+tradingagents",
                candidates=[
                    CandidateAnalysis(
                        symbol="NVDA",
                        instrument_type="equity_us",
                        side="buy",
                        confidence=70,
                        proposal_kind="enter",
                    )
                ],
                advisory_by_symbol={
                    "NVDA": AdvisoryEvidence(
                        advisory_only=True,
                        execution_allowed=False,
                        advisory_action="Underweight",
                        decision_text="Reduce exposure.",
                        final_trade_decision_text="No execution authorized.",
                        provider="openai-compatible",
                        model="gpt-5.5",
                        base_url="http://127.0.0.1:8796/v1",
                        warnings=["macro liquidity risk noted"],
                        risk_flags=["macro_risk"],
                        raw_state_keys=["market_report"],
                        as_of_date=datetime(2026, 4, 27, tzinfo=UTC).date(),
                    )
                },
                generated_at=datetime.now(UTC),
            )
            await session.commit()

            ev = proposals[0].original_payload["synthesis"]["tradingagents"]
            for required in (
                "advisory_only",
                "execution_allowed",
                "advisory_action",
                "model",
                "provider",
                "base_url",
                "decision_text",
                "final_trade_decision_text",
                "warnings",
                "risk_flags",
                "raw_state_keys",
                "as_of_date",
            ):
                assert required in ev, f"missing {required}"
            assert ev["advisory_only"] is True
            assert ev["execution_allowed"] is False
    finally:
        await _cleanup(user_id)
```

- [ ] **Step 8.2: Run**

Run: `uv run pytest tests/services/test_trading_decision_synthesis_persistence.py -v`
Expected: 3 passed (or skipped per env).

- [ ] **Step 8.3: Commit**

```bash
git add tests/services/test_trading_decision_synthesis_persistence.py
git commit -m "test(rob-14): assert persisted advisory evidence is complete

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 9: Lint and type checks

- [ ] **Step 9.1: Ruff format**

Run: `uv run ruff format app/schemas/trading_decision_synthesis.py
app/services/trading_decision_synthesis.py
app/services/trading_decision_synthesis_persistence.py
tests/services/test_trading_decision_synthesis.py
tests/services/test_trading_decision_synthesis_persistence.py
tests/services/test_trading_decision_synthesis_safety.py`
Expected: 0 changes (formatted).

- [ ] **Step 9.2: Ruff lint**

Run: `uv run ruff check app/schemas/trading_decision_synthesis.py app/services/trading_decision_synthesis.py app/services/trading_decision_synthesis_persistence.py tests/services/test_trading_decision_synthesis*.py`
Expected: All checks pass.

- [ ] **Step 9.3: Type check**

Run: `make typecheck` (or `uv run ty check` if `make typecheck` is unavailable).
Expected: 0 new errors in the files above.

- [ ] **Step 9.4: Commit any auto-fixes**

```bash
git add -p app/schemas/trading_decision_synthesis.py \
            app/services/trading_decision_synthesis.py \
            app/services/trading_decision_synthesis_persistence.py
git diff --cached --quiet && exit 0 || git commit -m "chore(rob-14): ruff/ty fixes

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 10: Full test sweep

- [ ] **Step 10.1: Run unit tests**

Run: `uv run pytest tests/services/test_trading_decision_synthesis.py -v`
Expected: ≥21 passed.

- [ ] **Step 10.2: Run integration + safety tests**

Run: `uv run pytest tests/services/test_trading_decision_synthesis_persistence.py tests/services/test_trading_decision_synthesis_safety.py -v`
Expected: 3 + 2 passed (or skipped where DB is unavailable).

- [ ] **Step 10.3: Run the broader trading-decision tests to ensure no regression**

Run: `uv run pytest tests/services/test_tradingagents_research_service.py tests/services/test_tradingagents_research_service_integration.py tests/services/test_tradingagents_research_service_safety.py tests/models/test_trading_decision_service.py tests/test_trading_decisions_router.py tests/test_trading_decisions_router_safety.py -v`
Expected: all green (no regression).

---

### Task 11 (optional, FE-only): Highlight synthesis in `ProposalRow`

**Files:**
- Create: `frontend/trading-decision/src/components/SynthesisPanel.tsx`
- Create: `frontend/trading-decision/src/components/SynthesisPanel.module.css`
- Create: `frontend/trading-decision/src/__tests__/SynthesisPanel.test.tsx`
- Modify: `frontend/trading-decision/src/components/ProposalRow.tsx`

- [ ] **Step 11.1: Write the failing component test**

Create `frontend/trading-decision/src/__tests__/SynthesisPanel.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import SynthesisPanel from "../components/SynthesisPanel";

const samplePayload = {
  advisory_only: true,
  execution_allowed: false,
  synthesis: {
    auto_trader: {
      side: "buy",
      confidence: 70,
      proposal_kind: "enter",
      rationale: "auto_trader buy signal",
      extra: {},
    },
    tradingagents: {
      advisory_only: true,
      execution_allowed: false,
      advisory_action: "Underweight",
      model: "gpt-5.5",
      provider: "openai-compatible",
      base_url: "http://127.0.0.1:8796/v1",
      decision_text: "Reduce exposure.",
      final_trade_decision_text: "No execution authorized.",
      warnings: ["macro liquidity risk noted"],
      risk_flags: ["macro_risk"],
      raw_state_keys: ["market_report"],
      as_of_date: "2026-04-27",
    },
    applied_policies: ["downgrade_buy_on_bearish_advisory", "risk_flag_minus_10"],
    final_proposal_kind: "pullback_watch",
    final_side: "none",
    final_confidence: 30,
    conflict: true,
    evidence_summary: "Downgraded buy → pullback_watch.",
  },
};

describe("SynthesisPanel", () => {
  it("renders nothing when no synthesis block", () => {
    const { container } = render(<SynthesisPanel payload={{}} />);
    expect(container.firstChild).toBeNull();
  });

  it("highlights conflict and applied policies", () => {
    render(<SynthesisPanel payload={samplePayload} />);
    expect(screen.getByText(/Underweight/)).toBeInTheDocument();
    expect(screen.getByText(/downgrade_buy_on_bearish_advisory/)).toBeInTheDocument();
    expect(screen.getByText(/conflict/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 11.2: Run failing test**

Run: `cd frontend/trading-decision && npm test -- SynthesisPanel`
Expected: FAIL — component missing.

- [ ] **Step 11.3: Implement `SynthesisPanel`**

Create `frontend/trading-decision/src/components/SynthesisPanel.tsx`:

```tsx
import styles from "./SynthesisPanel.module.css";

interface SynthesisPanelProps {
  payload: Record<string, unknown> | null | undefined;
}

interface SynthesisBlock {
  auto_trader: {
    side: string;
    confidence: number;
    proposal_kind: string;
    rationale: string | null;
    extra: Record<string, unknown>;
  };
  tradingagents: {
    advisory_only: true;
    execution_allowed: false;
    advisory_action: string;
    model: string;
    provider: string;
    base_url: string;
    decision_text: string;
    final_trade_decision_text: string;
    warnings: string[];
    risk_flags: string[];
    raw_state_keys: string[];
    as_of_date: string;
  } | null;
  applied_policies: string[];
  final_proposal_kind: string;
  final_side: string;
  final_confidence: number;
  conflict: boolean;
  evidence_summary: string;
}

function readSynthesis(payload: SynthesisPanelProps["payload"]): SynthesisBlock | null {
  if (payload === null || typeof payload !== "object") return null;
  const value = (payload as Record<string, unknown>).synthesis;
  if (value === null || typeof value !== "object") return null;
  return value as SynthesisBlock;
}

export default function SynthesisPanel({ payload }: SynthesisPanelProps) {
  const synthesis = readSynthesis(payload);
  if (synthesis === null) return null;
  const advisory = synthesis.tradingagents;
  return (
    <section className={styles.panel} aria-label="Synthesis">
      <header className={styles.header}>
        <h3>Synthesis</h3>
        {synthesis.conflict ? <span className={styles.conflict}>conflict</span> : null}
      </header>
      <dl className={styles.values}>
        <div>
          <dt>auto_trader</dt>
          <dd>
            {synthesis.auto_trader.side} · {synthesis.auto_trader.proposal_kind} ·
            confidence {synthesis.auto_trader.confidence}
          </dd>
        </div>
        <div>
          <dt>TradingAgents</dt>
          <dd>
            {advisory === null
              ? "n/a"
              : `${advisory.advisory_action} (model ${advisory.model})`}
          </dd>
        </div>
        <div>
          <dt>final</dt>
          <dd>
            {synthesis.final_proposal_kind} · {synthesis.final_side} · confidence{" "}
            {synthesis.final_confidence}
          </dd>
        </div>
        <div>
          <dt>policies</dt>
          <dd>
            <ul>
              {synthesis.applied_policies.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </dd>
        </div>
        {advisory && advisory.risk_flags.length > 0 ? (
          <div>
            <dt>risk_flags</dt>
            <dd>{advisory.risk_flags.join(", ")}</dd>
          </div>
        ) : null}
      </dl>
      <p className={styles.advisoryOnly}>
        TradingAgents output is advisory only — it never authorizes a live trade.
      </p>
    </section>
  );
}
```

Create `frontend/trading-decision/src/components/SynthesisPanel.module.css`:

```css
.panel {
  border: 1px solid var(--color-border, #d4d4d4);
  border-radius: 8px;
  padding: 0.75rem 1rem;
  background: var(--color-surface, #fafafa);
  margin-bottom: 1rem;
}

.header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.5rem;
}

.header h3 {
  font-size: 0.95rem;
  margin: 0;
}

.conflict {
  background: #ffe4e1;
  color: #8b0000;
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.values {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.25rem 1rem;
  margin: 0;
  font-size: 0.85rem;
}

.values div {
  display: contents;
}

.values dt {
  font-weight: 600;
}

.values dd {
  margin: 0;
}

.advisoryOnly {
  margin: 0.5rem 0 0;
  font-size: 0.75rem;
  color: var(--color-muted, #666);
}
```

- [ ] **Step 11.4: Mount the panel in `ProposalRow.tsx`**

Edit `frontend/trading-decision/src/components/ProposalRow.tsx`:

Add the import next to the existing component imports:

```tsx
import SynthesisPanel from "./SynthesisPanel";
```

Insert `<SynthesisPanel payload={proposal.original_payload} />` immediately
above the existing `<div className={styles.panels}>` element (so it appears
above "Original" and "Your decision").

- [ ] **Step 11.5: Run FE tests**

Run: `cd frontend/trading-decision && npm test -- --run`
Expected: all green, including the new SynthesisPanel test and the existing
ProposalRow test (which renders without `synthesis` and must not break).

- [ ] **Step 11.6: Commit**

```bash
git add frontend/trading-decision/src/components/SynthesisPanel.tsx \
        frontend/trading-decision/src/components/SynthesisPanel.module.css \
        frontend/trading-decision/src/__tests__/SynthesisPanel.test.tsx \
        frontend/trading-decision/src/components/ProposalRow.tsx
git commit -m "feat(rob-14): SPA SynthesisPanel surfaces TradingAgents effect

Refs: ROB-14
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## 9. Acceptance criteria → task cross-reference

| AC | Where covered |
|---|---|
| Unit tests cover advisory-only invariant enforcement | Task 1 (literal-pin tests), Task 3 (adapter restates literals), Task 8 (persisted payload retains literals) |
| Unit tests cover buy + Underweight downgrade | Task 4 step 4.1 (`test_buy_underweight_high_confidence_…`, `test_buy_underweight_low_confidence_…`) |
| Proposal payloads include source/model/base_url/decision/key risks/warnings/reflected action | Task 4 step 4.1 (`test_payload_includes_full_advisory_evidence`), Task 8 |
| Session generation tests verify no broker/order/watch APIs called | Task 7 (subprocess import safety on both modules) |
| Approval page can display advisory effect | Existing `MarketBriefPanel` (raw JSON) + `ProposalRow.original_rationale` already render synthesis text; Task 11 adds a focused panel |

## 10. Linear / Discord progress checklist (planner posts)

- [ ] After plan written: post "ROB-14 plan ready, starting implementer" to
  Linear comment + Discord channel.
- [ ] After Codex finishes: review the diff (separate review report).
- [ ] After PR opened: post PR URL.
- [ ] After CI passes + merge: post merge SHA.
- [ ] After deploy-smoke passes: post smoke summary, close ROB-14.

## 11. Out-of-scope reminder

Anything beyond the synthesis policy + persistence orchestrator (e.g., live
screening pipeline integration, scheduler, notifications, watch-alert wiring,
order-intent creation, paper-order placement) is **not** part of this PR and
must be tracked as a separate Linear ticket.
