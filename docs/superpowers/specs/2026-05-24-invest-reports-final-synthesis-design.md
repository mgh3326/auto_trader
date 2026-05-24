# `/invest/reports` Final Synthesis Composer — Slice 2

- **Date**: 2026-05-24
- **Status**: Design approved, pending spec review
- **Branch**: `rob-308`
- **Linear**: [ROB-308](https://linear.app/mgh3326/issue/ROB-308) (parent direction: ROB-306)

## Context

Slice 2 of the TradingAgents-style `/invest/reports` program. What already exists:
- **Market dimension report** (`investment_dimension_reports`, ROB-306).
- **Per-symbol intermediate reports** (`investment_symbol_intermediate_reports`, `decision_bucket`/`verdict`, ROB-301).
- **Final report machinery** (ROB-287): Hermes pushes `HermesCompositionResult` (title/summary/risk_summary/thesis_text/no_action_note + items) → `HermesCompositionIngestService` → `review.investment_reports` + `investment_report_items`. Items already carry `side` (buy/sell) + `intent` + `apply_policy="requires_user_approval"` (advisory-only). Composition already cites `symbol_intermediate_report_uuids`.

So buy/sell is *expressible* today — but the synthesis loop is **not closed**:

1. The Hermes **context export** (`HermesContextPayload`) does **not** include the persisted dimension/symbol reports — only raw stage artifacts + a lightweight `dimension_evidence.market` dict. Hermes cannot synthesize the final report *from* the analyst reports it authored.
2. Composition cannot cite **dimension reports** (`symbol_intermediate_report_uuids` exists; no dimension equivalent).
3. Items have **no explicit "held action vs new buy candidate" classification** — both use `intent=buy_review`. The target output ("매수/매도 + 신규 후보") needs them distinguished.

## Goal

Close the loop: Hermes synthesizes the final report **from** the dimension + symbol intermediate reports; the composition cites the dimension reports; final items explicitly classify held-action vs new-candidate with per-item source citations.

## Decisions locked (with the user)

- Reuse the **5-value `decision_bucket`** vocabulary (ROB-301 — `new_buy_candidate`/`open_action`/`completed_or_existing`/`deferred_no_action`/`risk_watch`) on final items. `new_buy_candidate` = 신규 후보; `open_action`/`risk_watch` = 보유 액션. **No new vocab.**
- **First-class item fields** (not metadata stash) — clean-cut, no backward compat.
- Hermes authors the composition (push); auto_trader validates + persists. **No in-process LLM** (ROB-287). Items stay **advisory-only**.
- ROB-287 composition contract is extended **additively** only.

## Architecture / scope

### C1. Context export — feed the analyst reports to Hermes
Extend `HermesContextPayload` (`app/schemas/hermes_composition.py`) + the exporter (`app/services/investment_stages/hermes_context.py`):
- `dimension_reports: list[DimensionReportSummary]` — read `investment_dimension_reports` by `run_uuid` (dimension, market, symbol, stance, confidence, key_findings, report_text, dimension_report_uuid).
- `symbol_intermediate_reports: list[SymbolReportSummary]` — read `investment_symbol_intermediate_reports` by `run_uuid` (symbol, decision_bucket, verdict, confidence, summary, symbol_report_uuid).

Read-only reads in the exporter. This is the loop-closer: Hermes now synthesizes from its own analyst reports, not just raw stage artifacts.

### C2. Composition contract — citations + item classification
`HermesCompositionResult`:
- add `dimension_report_uuids: list[UUID] = []` (report-level; validated on ingest like `symbol_intermediate_report_uuids`).

`IngestReportItem` (`app/schemas/investment_reports.py`):
- `decision_bucket: str | None` — validated against the ROB-301 `DECISION_BUCKETS` tuple (single source of truth).
- `cited_symbol_report_uuid: UUID | None`.
- `cited_dimension_report_uuids: list[UUID] = []`.

### C3. Item model + migration
`investment_report_items` (`app/models/investment_reports.py`): additive columns `decision_bucket` (Text, nullable, CHECK from `DECISION_BUCKETS`), `cited_symbol_report_uuid` (UUID nullable), `cited_dimension_report_uuids` (UUID[] default `{}`). One alembic migration, **operator-applied** (`alembic upgrade head`).

### C4. Composition ingest
`HermesCompositionIngestService`: validate `dimension_report_uuids` existence + run membership (mirror the existing symbol-report-UUID validation); persist the new item fields. Advisory-only invariants (`requires_user_approval`, `operation=review`, no broker mutation) unchanged. Store `dimension_report_uuids` in `report_metadata.hermes_composition`.

### C5. Read exposure (minimal)
Final-report view-model groups items by `decision_bucket` → **"매수/매도 (보유 액션)"** vs **"신규 매수 후보"**, with cited-report links (symbol/dimension). Heavy UI deferred. (Locate the existing final-report read surface during planning; extend its view-model.)

## Non-goals / boundaries

- No in-process LLM (ROB-287 import guard must pass). No broker/order/watch/order-intent mutation; items remain advisory-only.
- No deterministic candidate ranking in this slice (considered + deferred — auto_trader could rank candidates by screener score later).
- ROB-287 composition contract extended additively only (existing fields/behavior unchanged; legacy compositions with empty new lists still validate).

## Migration

One additive migration on `investment_report_items`. Ships in PR, operator runs `alembic upgrade head` separately (production cutover gate).

## Testing strategy

- Context export: seed a run with a dimension report + symbol reports → assert `HermesContextPayload` carries both summary lists.
- Composition schema: `dimension_report_uuids` accepted; item `decision_bucket` validated against the tuple; `extra="forbid"` holds.
- Ingest: `dimension_report_uuids` existence + run-membership validation (404/409 on bad refs); item classification + citations persisted; advisory-only invariants intact.
- Read: items grouped held-action vs new-candidate with citations.
- Guards: ROB-287 no-internal-LLM import guard passes; no broker mutation reachable; legacy composition (no new fields) still ingests.

## PR split

- **PR1** — C1 context export (additive; feeds Hermes the analyst reports). Shippable alone.
- **PR2** — C2/C3/C4 contract + item model + migration + ingest, + C5 read grouping.

## Assumptions to verify during implementation

- The `DECISION_BUCKETS` tuple in `app/models/investment_symbol_intermediate_reports.py` is importable into the item model + schema without a cycle (it is a plain tuple).
- The existing composition-ingest UUID-validation helper (symbol reports) can be generalized/mirrored for dimension reports.
- The final-report read surface exists (router + view-model) and can be extended for grouping; if none renders items today, add a minimal grouped view.

## Program order

Parent: ROB-306. This = slice 2. Next: News dimension (research_reports ingestion), Fundamentals, Sentiment, crypto Market evidence (after ROB-282).
