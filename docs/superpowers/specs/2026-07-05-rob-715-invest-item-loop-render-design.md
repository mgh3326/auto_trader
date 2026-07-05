# ROB-715 — /invest item-level learning-loop render

Design spec. Generated 2026-07-05. Branch: `rob-715`. Stage 5 (final) of the
판단→결과 learning-loop roadmap (ROB-711~715). Office-hours design:
`~/.gstack/projects/mgh3326-auto_trader/mgh3326-main-design-20260705-161950.md` §5단계.

## Problem

/invest report-item rows render `rationale` / `confidence` / `linkedOrders`
(one hop) and the ROB-692/693 `tradeSetup` R:R pill — but the item→forecast
해소→회고 chain is invisible. Forecasts and retrospectives exist only as the
market/symbol aggregates on /insights. This is the audit/trust human surface
(design premise P2 — the LLM analysis session is the *primary* consumer via
decision_history injection; the web is the secondary audit면).

Several item fields are stored but never rendered: `trigger_checklist`,
`max_action`, `structured_evidence`, and `decision_bucket` (surfaced only
indirectly via the ROB-308/322 section projection, never as an item-row badge).

**Success criterion:** from a report item, reach "이 판단의 결과
(체결→forecast 해소→회고)" within 2 clicks.

## Decisions (2026-07-05, user-confirmed)

- **D1 — Scope = Core (a+b+c), defer d.** This PR ships: (a) forecast-resolution
  status + retrospective link on item rows, (b) render the already-normalized raw
  fields, (c) plan-vs-actual (frontend-only juxtaposition). **Deferred to a
  follow-up**: (d) `analysis_artifacts` links via correlation_id — needs a new
  read endpoint (service-layer `list_artifacts(correlation_id=...)` exists but is
  unexposed) and carries the least success-criterion value.
- **D2 — Delivery = batch maps on the bundle.** Attach
  `forecasts_by_item_uuid` + `retrospectives_by_item_uuid` to the
  investment-reports bundle response, mirroring the existing
  `linked_orders_by_item_uuid` / `decisions_by_item_uuid`. One fetch, no
  per-item N+1 (ROB-713/717 fanout perf lesson).
- **D3 — Join = exact `report_item_uuid` only.** An audit surface must show
  *this item's own* forecast/retro, not any symbol match (symbol-window would
  mis-attribute another item's forecast). Empty renders as "해소 대기 / 미연결".
  Adoption of `report_item_uuid` on forecasts/retros is near-zero today (measured
  forecasts 0/2, retros 0/5); ROB-714 place-time auto-forecast + operator
  discipline fill it going forward. This is a forward-looking surface.

## Grounding (verified in code)

- **Join key.** `list_linked_orders_for_item_uuids(db, item_uuids)`
  (`app/services/investment_reports/linked_orders.py:105`) queries each live
  ledger `WHERE report_item_uuid.in_(item_uuids)` and keys the result by
  `str(row.report_item_uuid)`. The router sets
  `resp.linked_orders = linked_by_uuid.get(str(it.item_uuid))`
  (`app/routers/investment_reports.py:64`). So the item's `item_uuid` **is** what
  downstream tables store in their `report_item_uuid` column. The same join
  applies to forecasts and retrospectives.
- **`TradeForecast`** (`app/models/review.py:1101`) carries `report_item_uuid`,
  `status` ('open'|'closed'), `outcome`, `review_date`, `resolution_source`,
  `brier_score`, `probability`, and the `forecast_target` JSONB (direction /
  target_price). `serialize_forecast` (`forecast_service.py:190`) already
  projects all of these.
- **`TradeRetrospective`** (`app/models/review.py:969`) carries
  `report_item_uuid`, `outcome`, `lesson`, `result_summary`, `next_strategy`,
  `root_cause_class`, `trigger_type`, `pnl_pct`, `next_actions`, `created_at`.
- **Bundle batch-map pattern.** `query_service.py:160-177` builds
  `linked_orders_by_item_uuid` + `decisions_by_item`; `_serialise_bundle`
  (`investment_reports.py:56-115`) folds `decisions_by_item_uuid` onto the
  response. New maps slot into the same two seams.
- **Frontend item render.** `ItemRow`
  (`frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx:276`)
  already renders confidence, rationale, `tradeSetup` (planned entry/stop/target,
  R:R pill), `invalidationTriggers`, `linkedOrders` (actual fills). The TS layer
  (`api/investmentReports.ts`, `types/investmentReports.ts`) already **normalizes
  but does not render** `triggerChecklist`, `maxAction`, `decisionBucket`; carries
  both `itemUuid` and `reportItemUuid`.
- **ROB-716 reuse boundary.** ROB-716 built `build_decision_context(db, symbol,
  market)` (`app/services/decision_history.py`) — symbol-scoped, embedded in
  stock-detail. **ROB-717 owns edits to `decision_history.py` / `aggregates.py`.**
  ROB-715 does **not** touch them: it queries forecasts/retros directly by
  `report_item_uuid`, which is item-scoped and avoids that ownership overlap.

## Architecture

Thin read-only backend (additive schema, migration 0) + frontend render. No
broker/order/watch/order-intent mutation is reachable. ROB-501: all deterministic
reads, no in-process LLM import.

### Backend

1. **New file** `app/services/investment_reports/item_loop_links.py` — modeled on
   `linked_orders.py`:
   - `list_forecasts_for_item_uuids(db, item_uuids) -> dict[str, list[ForecastLinkView]]`
     — one batched `SELECT ... WHERE report_item_uuid IN (item_uuids)` on
     `TradeForecast`, `id desc`, keyed by `str(report_item_uuid)`. Projects
     `forecast_id`, `status`, `outcome`, `review_date`, `direction`,
     `target_price`, `probability`, `brier_score`, `resolution_source`.
   - `list_retrospectives_for_item_uuids(db, item_uuids) -> dict[str, list[RetrospectiveLinkView]]`
     — same shape on `TradeRetrospective`. Projects `outcome`, `lesson`,
     `result_summary`, `root_cause_class`, `trigger_type`, `pnl_pct`,
     `created_at`.
   - Items with no rows are absent from the dict (caller treats missing as
     "미연결").
   - `report_item_uuid` is stored as text on these two tables (vs `PG_UUID` on the
     ledgers) — pass `str(uuid)` values in the `IN` clause. Verify column type in
     implementation and coerce accordingly.
2. `app/services/investment_reports/query_service.py` — call both new loaders
   alongside `list_linked_orders_for_item_uuids`; add `forecasts_by_item_uuid`
   and `retrospectives_by_item_uuid` to the returned bundle dict.
3. `app/schemas/investment_reports.py` — additive:
   - `ForecastLinkResponse`, `RetrospectiveLinkResponse` (projection models).
   - `forecasts_by_item_uuid: dict[str, list[ForecastLinkResponse]]` and
     `retrospectives_by_item_uuid: dict[str, list[RetrospectiveLinkResponse]]`
     on `InvestmentReportBundle` (default empty dict — legacy-safe).
   - `structured_evidence_summary: str | None` on the item response — a
     deterministic backend-derived summary of `structured_evidence` so the
     frontend displays a string without parsing the nested structure
     ("백엔드 전용 — 프론트 미파싱"). If `structured_evidence` is a subtree of
     `evidence_snapshot` rather than a distinct column, derive from there;
     confirm the field's home during implementation.
4. `app/routers/investment_reports.py::_serialise_bundle` — build
   `forecasts_by_item_uuid` and `retrospectives_by_item_uuid` from the bundle
   dict exactly as `decisions_by_item_uuid` is built, and pass them to
   `InvestmentReportBundle`.

### Frontend

5. `frontend/invest/src/types/investmentReports.ts` — add `ForecastLink`,
   `RetrospectiveLink` interfaces + the two bundle map fields +
   `structuredEvidenceSummary` on the item type.
6. `frontend/invest/src/api/investmentReports.ts` — normalize
   `forecasts_by_item_uuid` / `retrospectives_by_item_uuid` (snake→camel, same as
   `decisions_by_item_uuid`) and `structured_evidence_summary`.
7. `InvestmentReportBundleContent.tsx::ItemRow` — the render:
   - **(a) Loop section (2-click success path).** For the row's forecasts: show
     `status` (open/closed), `outcome` (hit/miss/pending), `brier_score` when
     scored, `review_date`. For retrospectives: show `outcome` + `lesson`
     (expandable to `result_summary` / `root_cause_class`). When **both** maps
     are empty for the item → muted "해소 대기 / 미연결".
   - **(b) Raw fields.** `triggerChecklist` → chips; `maxAction` → compact
     exec-plan summary (side / qty|notional / limit / ladder_level);
     `decisionBucket` → **small inline row-level badge only** (must not
     re-project the ROB-308/322 sections); `structuredEvidenceSummary` → text.
   - **(c) Plan-vs-actual.** Juxtapose parsed `tradeSetup` planned
     entry/stop/target (already rendered) with the actual fill price already
     present on `linkedOrders`. Pure frontend — extends the existing R:R pill
     block, no new data.

## Testing

- **Backend (pytest).** Unit tests for `item_loop_links.py`: exact join returns
  only matching `report_item_uuid` rows, correct string key, empty → key absent,
  multiple forecasts/retros per item ordered. Query-service test that the bundle
  dict carries both maps. Serializer test that `_serialise_bundle` folds them onto
  `InvestmentReportBundle` and legacy bundles (missing keys) default to empty.
  **Shared-DB xdist discipline (bit ROB-711/713/705 three times): per-test unique
  symbols + `flush()`-only, never `commit()`; no autouse global-DELETE fixture.**
- **Frontend (vitest).** `ItemRow` renders forecast status + retrospective,
  renders the "해소 대기 / 미연결" empty state, renders trigger_checklist chips /
  max_action / decision_bucket badge / structured_evidence summary, and renders
  the plan-vs-actual juxtaposition. Follow existing
  `InvestmentReportBundleContent` test conventions.
- **Guards regression.** ROB-501 no-internal-LLM-import guard stays green;
  invest_view_model import-boundary guard unaffected (this work is in the
  `investment_reports/` router surface, not the view-model read path).

## Constraints / guards

- migration 0 (read-path only; all schema additions are response-model fields).
- No broker/order/watch/order-intent mutation reachable from any new code.
- ROB-501: deterministic reads only, no in-process LLM provider import.
- One batched query per table — no per-item N+1.
- Do not edit `app/services/decision_history.py` or
  `app/services/trade_journal/aggregates.py` (ROB-717 ownership).
- `decision_bucket` badge is row-level auxiliary only — no UX duplication with
  the ROB-308/322 section projection.

## Deferred (follow-up issue)

- **(d) analysis_artifacts item links.** Expose `correlation_id` /
  `report_item` filter on the `invest_artifacts` router (service-layer
  `list_artifacts(correlation_id=...)` already exists) and render report/item →
  artifact links. Deferred from this PR per D1.

## Open questions

- Exact home of `structured_evidence` (distinct column vs `evidence_snapshot`
  subtree) — resolve at implementation; the summary derivation adapts either way.
- `max_action` render density — start with a one-line summary; expand only if the
  operator asks.
