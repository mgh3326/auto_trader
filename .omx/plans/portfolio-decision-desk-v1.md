# Portfolio Decision Desk V1 Plan

## 1. Recommended V1 product definition

Build an analysis-only "Decision Desk" page inside the existing portfolio UI. The V1 route should present one generated decision slate for the current portfolio and make each proposed action explainable by price distance, support/resistance context, journal context, position weight, and execution boundary.

Grounding in the current repo:
- `app/routers/portfolio.py:51` already owns the `/portfolio` router.
- `app/routers/portfolio.py:66` renders the existing portfolio dashboard HTML page.
- `app/routers/portfolio.py:99` exposes the existing overview API, which already merges journal snapshots into positions.
- `app/routers/portfolio.py:373` renders position detail pages under `/portfolio/positions/{market_type}/{symbol}`.
- `app/services/portfolio_overview_service.py:82` returns holdings grouped by market/account/symbol with prices, position values, facets, exchange rate, and warnings.
- `app/services/portfolio_dashboard_service.py:81` and `app/services/portfolio_dashboard_service.py:104` expose latest active/draft journal snapshots for one symbol or a batch.
- `app/mcp_server/tooling/fundamentals/_support_resistance.py:29` exposes internal support/resistance analysis without needing a new MCP tool.

V1 should include only held positions unless product explicitly asks for non-held opportunities. The prompt says the main unit is `Decision Item`, with symbols as grouping context; that maps naturally to a service response with `decision_run`, `summary`, `symbol_groups[]`, and each group containing `items[]`.

Acceptance criteria:
- `/portfolio/decision` renders an authenticated HTML page in the existing portfolio router.
- `/portfolio/api/decision-slate` returns a deterministic JSON payload for the authenticated user's current portfolio.
- Each symbol group can contain multiple action items, not just one row per symbol.
- Each item states current price, suggested/action price when known, current-to-action delta, nearest support/resistance context, rationale, and execution boundary.
- Missing support/resistance, journal, indicators, or price data produces warnings/unknown fields without failing the whole page.
- No live execution, dry-run execution button, Discord flow, Paperclip flow, n8n change, schema migration, new dependency, or new MCP tool is included in Phase 1.

## 2. Route and page structure

Recommendation: create a separate page at `/portfolio/decision`, linked from the existing portfolio dashboard. Do not make it a tab inside `portfolio_dashboard.html` for V1.

Reasoning:
- The existing dashboard template is already large and owns overview filtering, cash, rotation, AI advice, and responsive table/card rendering.
- The detail page pattern already supports a separate portfolio page with an HTML shell plus JSON-backed data sections.
- A separate route keeps V1 additive and reversible while still staying inside the `/portfolio` IA.

Route/API additions:
- Add `get_portfolio_decision_service(...)` dependency in `app/routers/portfolio.py`, mirroring `get_portfolio_position_detail_service(...)`.
- Add `@router.get("/decision", response_class=HTMLResponse)` that renders `portfolio_decision_desk.html`.
- Add `@router.get("/api/decision-slate", response_model=PortfolioDecisionSlateResponse)` with filters:
  - `market: Literal["ALL", "KR", "US", "CRYPTO"] = "ALL"`
  - `account_keys: Annotated[list[str] | None, Query()] = None`
  - `q: Annotated[str | None, Query(min_length=1)] = None`
  - optional `include_held_only: bool = True`, default true. Keep true-only behavior if non-held candidates are out of scope.

Template structure for `app/templates/portfolio_decision_desk.html`:
- Header:
  - title: `Decision Desk`
  - subtitle: analysis-only generated slate
  - badges: `analysis_only`, generated time, warning count
  - link back to `/portfolio/`
- Summary cards:
  - total positions evaluated
  - actionable item count
  - manual-review item count
  - auto-executable candidate count
  - missing-context count
- Filters:
  - market selector
  - account selector, reusing account facet data from the slate payload
  - symbol/name search
  - action filter: `all`, `buy_candidate`, `trim_candidate`, `sell_watch`, `hold`, `manual_review`
  - execution boundary filter: `all`, `auto_candidate`, `manual_only`, `analysis_only`
- Symbol-grouped sections:
  - symbol, name, market, current price, quantity, avg price, P/L, portfolio weight, detail-page link
  - compact support/resistance strip: nearest support, nearest resistance, data status
  - decision item rows/cards below the symbol header
- Decision item row:
  - action badge
  - suggested/action price and delta from current
  - price anchor: support, resistance, target, stop, avg price, current price
  - rationale bullets
  - confidence/source badges
  - execution boundary badge
  - warnings for missing journal/SR/price
  - link to `/portfolio/positions/{market}/{symbol}`
- Empty/fallback states:
  - no holdings match filters
  - holdings exist but all classify as hold
  - support/resistance unavailable
  - journal unavailable
  - current price unavailable

Dashboard integration:
- Add a single navigation affordance in `app/templates/portfolio_dashboard.html`, such as a header button/link to `/portfolio/decision`.
- Do not embed the decision desk into the dashboard table or rotation panel in V1.

## 3. Backend service / API design

Create `app/services/portfolio_decision_service.py`.

Constructor:
```python
class PortfolioDecisionService:
    def __init__(self, *, overview_service, dashboard_service) -> None:
        self.overview_service = overview_service
        self.dashboard_service = dashboard_service
```

Primary method:
```python
async def build_decision_slate(
    self,
    *,
    user_id: int,
    market: str = "ALL",
    account_keys: list[str] | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    ...
```

Inputs to reuse:
- `PortfolioOverviewService.get_overview(...)` for current portfolio positions and filters.
- `PortfolioDashboardService.get_journals_batch(...)` for active/draft journal snapshots with `target_distance_pct` and `stop_distance_pct`.
- Internal `_get_support_resistance_impl(symbol, market=...)` from `app.mcp_server.tooling.fundamentals._support_resistance` for nearest support/resistance.
- Optional `_get_indicators_impl` from the existing position-detail pattern for RSI only, if needed for conservative buy/hold heuristics.

Keep deterministic logic in the service:
- action classification
- price delta calculations
- nearest support/resistance selection
- execution boundary classification
- missing-context warnings

Keep optional AI text out of V1:
- `AiAdvisorService` is useful for user-triggered Q&A, but V1 decision item rationales should be deterministic and inspectable.
- Later phases can add an optional "ask why" action that uses the existing AI advice provider surface; V1 should not generate core action labels via LLM.

Support/resistance fetching strategy:
- Fetch SR concurrently for positions with valid `current_price`.
- Cap concurrent SR calls with an internal semaphore to avoid creating a slow page for large portfolios.
- Treat any returned payload containing `error` or missing `supports`/`resistances` as `support_resistance.status = "unavailable"` and add a warning to the item/group.
- Use the current overview price as the source of truth for current-vs-action deltas; SR's `current_price` can be included as `source_current_price` for diagnostics only.

Market mapping:
- Overview market types are `KR`, `US`, `CRYPTO`.
- Support/resistance helper accepts market aliases through existing resolution; pass lower-case `kr`, `us`, `crypto` or normalize through a small private helper in the decision service.
- Position detail URLs should use the existing `/portfolio/positions/{market_type.lower()}/{symbol}` path contract already tested in dashboard/router tests.

Do not reuse `PortfolioRotationService` as the main service:
- It is crypto-only and uses `_collect_portfolio_positions` plus separate screener buy candidates.
- It can inspire response-shape tests and conservative classifier organization, but a cross-market Decision Desk needs overview-position inputs and symbol grouping.

## 4. Data contract proposal

Add `app/schemas/portfolio_decision.py` with Pydantic models. Keep fields explicit enough for tests and UI, while allowing nested `dict[str, Any]` for raw context snapshots where volatility is high.

Proposed response:
```json
{
  "success": true,
  "decision_run": {
    "id": "runtime-2026-04-20T10:15:00+09:00",
    "generated_at": "2026-04-20T10:15:00+09:00",
    "mode": "analysis_only",
    "persisted": false,
    "source": "portfolio_decision_service_v1"
  },
  "filters": {
    "market": "ALL",
    "account_keys": [],
    "q": null
  },
  "summary": {
    "symbols": 3,
    "decision_items": 5,
    "actionable_items": 2,
    "manual_review_items": 1,
    "auto_candidate_items": 1,
    "missing_context_items": 1,
    "by_action": {
      "buy_candidate": 1,
      "trim_candidate": 1,
      "sell_watch": 0,
      "hold": 2,
      "manual_review": 1
    },
    "by_market": {
      "KR": 1,
      "US": 1,
      "CRYPTO": 1
    }
  },
  "facets": {
    "accounts": []
  },
  "symbol_groups": [
    {
      "market_type": "US",
      "symbol": "NVDA",
      "name": "NVIDIA Corp.",
      "detail_url": "/portfolio/positions/us/NVDA",
      "position": {
        "quantity": 3.0,
        "avg_price": 120.0,
        "current_price": 132.0,
        "evaluation": 396.0,
        "evaluation_krw": 540000.0,
        "profit_loss": 36.0,
        "profit_loss_krw": 49000.0,
        "profit_rate": 0.1,
        "portfolio_weight_pct": 9.8,
        "market_weight_pct": 24.5,
        "components": []
      },
      "journal": {
        "status": "active",
        "strategy": "trend",
        "target_price": 145.0,
        "stop_loss": 118.0,
        "target_distance_pct": 9.85,
        "stop_distance_pct": -10.61
      },
      "support_resistance": {
        "status": "available",
        "nearest_support": {
          "price": 128.5,
          "distance_pct": -2.65,
          "strength": "moderate",
          "sources": ["volume_poc", "bb_middle"]
        },
        "nearest_resistance": {
          "price": 145.0,
          "distance_pct": 9.85,
          "strength": "strong",
          "sources": ["fib_0.618", "bb_upper"]
        },
        "supports": [],
        "resistances": []
      },
      "items": [
        {
          "id": "US:NVDA:trim_candidate:target",
          "action": "trim_candidate",
          "label": "Trim candidate",
          "priority": "medium",
          "current_price": 132.0,
          "action_price": 145.0,
          "action_price_source": "journal_target",
          "delta_from_current_pct": 9.85,
          "anchor": {
            "type": "resistance",
            "price": 145.0,
            "distance_pct": 9.85,
            "strength": "strong"
          },
          "rationale": [
            "Journal target is within the next resistance zone.",
            "Position is profitable and near planned exit context."
          ],
          "execution_boundary": {
            "mode": "analysis_only",
            "broker": "kis",
            "auto_executable": false,
            "manual_only": false,
            "reason": "Phase 1 does not expose execution."
          },
          "badges": ["analysis_only", "near_resistance"],
          "warnings": []
        }
      ],
      "warnings": []
    }
  ],
  "warnings": []
}
```

Recommended model classes:
- `DecisionRunResponse`
- `DecisionSummaryResponse`
- `DecisionPositionContextResponse`
- `DecisionJournalContextResponse`
- `SupportResistanceLevelResponse`
- `SupportResistanceContextResponse`
- `ExecutionBoundaryResponse`
- `DecisionAnchorResponse`
- `DecisionItemResponse`
- `DecisionSymbolGroupResponse`
- `PortfolioDecisionSlateResponse`

State badges:
- Always include `analysis_only` in V1.
- Use `manual_only` when source/account/broker is not auto-executable through KIS or when market/source is manual-only.
- Use `dry_run_ready` only as a capability label for future readiness, not as a button or action in Phase 1. Prefer `execution_boundary.future_capability = "dry_run_ready"` if needed to avoid implying current execution.

## 5. V1 decision heuristics

All thresholds should be constants at the top of `portfolio_decision_service.py`, with tests naming expected behavior. Initial conservative constants:
- `NEAR_SUPPORT_PCT = 3.0`
- `NEAR_RESISTANCE_PCT = 3.0`
- `TARGET_NEAR_PCT = 5.0`
- `STOP_NEAR_PCT = 5.0`
- `HIGH_WEIGHT_PCT = 15.0`
- `PROFIT_TRIM_PCT = 8.0`
- `LOSS_WATCH_PCT = -6.0`
- `RSI_OVERSOLD = 30.0`
- `RSI_OVERBOUGHT = 70.0`

Classification should produce one or more decision items per symbol, in priority order:

1. `manual_review`
- Trigger when current price is missing, quantity is zero/invalid, avg price is invalid, or both journal and support/resistance context are unavailable.
- Also trigger if journal has no target/stop and no thesis/notes, matching the existing detail service's "저널 보강 필요" idea.
- `action_price = null`.
- Rationale should name the missing context.

2. `sell_watch`
- Trigger when journal stop is near: `stop_distance_pct >= -STOP_NEAR_PCT`.
- Trigger when unrealized loss is at or below `LOSS_WATCH_PCT` and no active journal exists.
- Anchor to `journal_stop` if present; otherwise nearest support below current.
- Keep this as watch/manual-review language, not an execution instruction.

3. `trim_candidate`
- Trigger when position weight is high: `portfolio_weight_pct >= HIGH_WEIGHT_PCT`.
- Trigger when journal target is near: `0 <= target_distance_pct <= TARGET_NEAR_PCT`.
- Trigger when nearest resistance is near: `0 <= nearest_resistance.distance_pct <= NEAR_RESISTANCE_PCT` and profit is positive.
- Suggested `action_price` preference: journal target, then nearest resistance, then current price.
- Reduce quantity/percent should be omitted or labeled `suggested_size = null` in V1 unless explicitly configured later. Do not import the crypto rotation service's `PARTIAL_REDUCE_PCT` as cross-market policy.

4. `buy_candidate`
- For held positions only in V1: trigger only when the user already holds the symbol, the position is not high weight, current price is near support, and either RSI <= 30 or journal thesis/strategy supports accumulation.
- Suggested `action_price` preference: nearest support, then journal entry/avg price context, never arbitrary "market now".
- If broker/source is manual-only, mark `execution_boundary.mode = "manual_only"` even if action is buy candidate.

5. `hold`
- Default when no stronger rule triggers.
- Include rationale from current weight, target/stop distance, and support/resistance spacing.
- Hold is still a decision item, because the product goal asks "why not trim/buy".

Execution boundary:
- Phase 1: every item has `execution_boundary.mode = "analysis_only"` as the dominant badge.
- Add `execution_boundary.channel = "kis_candidate"` only for KIS-backed KR/US components with broker/source data indicating KIS, but keep `auto_executable = false`.
- Add `execution_boundary.channel = "manual_review"` for Toss/manual components, crypto, missing account context, or mixed-account symbols.
- If a symbol has mixed KIS and manual components, prefer `manual_review` unless the item can specify component-level broker scope. V1 should avoid implicit partial-account execution assumptions.

## 6. Test plan

Add service tests:
- New file: `tests/test_portfolio_decision_service.py`.
- Test `build_decision_slate` returns top-level run/summary/groups/items shape from fake overview and fake journal data.
- Test `trim_candidate` when target or nearest resistance is near.
- Test `sell_watch` when stop loss is near.
- Test held-only `buy_candidate` when current price is near support and RSI is oversold.
- Test `hold` default when no action trigger fires.
- Test `manual_review` when current price or all context is missing.
- Test support/resistance helper errors degrade into warnings instead of exceptions.
- Test mixed KIS/manual components become manual-review or analysis-only boundary, not auto-executable.

Extend router tests:
- Extend `tests/test_portfolio_dashboard_router.py` to assert the dashboard links to `/portfolio/decision`.
- Add `tests/test_portfolio_decision_router.py` or extend dashboard router tests if keeping portfolio page tests together.
- Test `/portfolio/decision` renders HTML with `id="portfolio-decision-desk-page"`.
- Test `/portfolio/api/decision-slate` calls the injected service with `user_id`, `market`, repeated `account_keys`, and `q`.
- Test API response validates through the response model.

Template tests:
- Assert the new template includes:
  - `fetch("/portfolio/api/decision-slate`
  - `function escapeHtml(value)`
  - detail URL rendering
  - empty state text
  - badges for `analysis_only`, `manual_only`, and action types

Regression/fallback tests:
- No support/resistance: group still renders with `support_resistance.status = "unavailable"`.
- No journal: item contains a `journal_missing` warning but still classifies based on price/SR if possible.
- No current price: item is `manual_review`, not a 500.
- Unsupported market from SR helper: item/group warning, not response failure.

Suggested verification commands after implementation:
```bash
uv run pytest tests/test_portfolio_decision_service.py -q
uv run pytest tests/test_portfolio_decision_router.py tests/test_portfolio_dashboard_router.py -q
uv run pytest tests/test_portfolio_position_detail_router.py tests/test_portfolio_position_detail_service.py -q
make lint
```

## 7. Phase breakdown

Phase 1: analysis-only Decision Desk
- Add route, API, schema, service, template, and dashboard link.
- Use held portfolio positions only.
- Use deterministic heuristics only.
- No persistence. `decision_run.id` is runtime-generated from timestamp.
- No execution, no dry-run button, no workflow integrations.

Phase 2: dry-run / execution handoff integration
- Add explicit dry-run handoff only after Phase 1 is stable.
- Introduce component/account-scoped action sizing and broker-specific eligibility.
- Add stronger guardrails for live-vs-paper separation.
- Treat this as `high_risk_change` because it touches order approval boundaries.

Phase 3: Discord / Paperclip / approval workflow integration
- Persist decision runs if approvals/comments need stable references.
- Add approval states and audit trail.
- Integrate Discord/Paperclip/n8n only after approval and persistence requirements are explicit.
- Treat this as `high_risk_change + needs_stronger_model_review + hold_for_final_review` before merge/deploy.

## 8. Step-by-step implementation checklist

1. Add schema models.
- Create `app/schemas/portfolio_decision.py`.
- Export models from `app/schemas/__init__.py` only if this repo's schema exports are expected by local convention.
- Include `PortfolioDecisionSlateResponse` as the router response model.

2. Add service tests first.
- Create `tests/test_portfolio_decision_service.py`.
- Use `MagicMock`/`AsyncMock` patterns from `tests/test_portfolio_position_detail_service.py`.
- Patch support/resistance and indicator helpers at the service module import path.
- Cover response shape, each action classifier, and fallback paths.

3. Implement `PortfolioDecisionService`.
- Create `app/services/portfolio_decision_service.py`.
- Inject `overview_service` and `dashboard_service`.
- Fetch overview with the same filter parameters as `/portfolio/api/overview`.
- Fetch batch journals with current prices.
- Build portfolio/market weights using logic equivalent to `PortfolioPositionDetailService._build_weights`; consider extracting later, but duplicate narrowly in V1 to avoid broad refactor.
- Fetch support/resistance per symbol with graceful error handling.
- Build `symbol_groups[]` and `items[]`.
- Return warnings at item, group, and top-level scopes.

4. Add router dependency and endpoints.
- Modify `app/routers/portfolio.py`.
- Import `PortfolioDecisionSlateResponse` and `PortfolioDecisionService`.
- Add `get_portfolio_decision_service(...)`.
- Add HTML route `/decision`.
- Add JSON route `/api/decision-slate`.
- Keep `HTTPException(500)` handling consistent with existing overview endpoints only for unexpected service failures; service-level missing SR/journal should not raise.

5. Add the template.
- Create `app/templates/portfolio_decision_desk.html`.
- Reuse the visual language of portfolio templates: Bootstrap, bootstrap-icons, page shell/header, cards, responsive mobile layout, escape helpers.
- Fetch `/portfolio/api/decision-slate` on load and on filter changes.
- Render symbol sections from groups, not flat rows.
- Use guarded HTML escaping and URL handling like existing templates.

6. Link from the dashboard.
- Modify `app/templates/portfolio_dashboard.html`.
- Add a header/sidebar action link to `/portfolio/decision`.
- Keep this to one additive navigation link; do not restructure the dashboard.

7. Add router/template tests.
- Create `tests/test_portfolio_decision_router.py`.
- Extend `tests/test_portfolio_dashboard_router.py` for the new link.
- Reuse `FastAPI()`, `TestClient`, dependency override patterns from existing portfolio tests.

8. Run focused verification.
- Run the new service and router tests.
- Run existing portfolio dashboard/detail tests to catch template/router regressions.
- Run lint after tests pass.

9. Keep Phase 1 scoped.
- Do not add migrations.
- Do not persist decision runs.
- Do not wire MCP, Paperclip, Discord, n8n, or execution endpoints.
- Do not add dependencies.

## 9. Risks / open questions

Open questions:
- Should V1 include only held positions, or also non-held buy candidates? Recommendation: held-only for V1, because overview service already owns current portfolio context and non-held candidates introduce screener scope and ranking policy.
- Should decision runs be persisted? Recommendation: no for V1. Persist only when approval workflow, audit trail, or shareable run URLs become requirements.
- Should KIS/Toss separation be exposed on day one? Recommendation: yes as badges/context, but not as executable controls.
- Should component-level decisions be allowed for mixed-account symbols? Recommendation: no for V1 unless account scoping is explicit. Classify mixed symbols as manual-review or analysis-only to avoid hidden partial-execution assumptions.
- Should `PortfolioPositionDetailService._build_weights` be extracted to a shared helper? Recommendation: not in first pass unless duplication becomes awkward. A focused duplicate keeps the diff safer.

Risks and mitigations:
- Support/resistance calls can make the page slow for large portfolios. Mitigate with concurrency limits, warnings, and progressive frontend loading if needed.
- SR current price can differ from overview current price. Mitigate by calculating deltas from overview current price and keeping SR source price diagnostic-only.
- Deterministic heuristics can be misread as advice. Mitigate with strong `analysis_only` copy, no execution controls, and rationale that names data sources/uncertainty.
- Journal data may be stale or missing. Mitigate with item-level warnings and `manual_review` fallback.
- Future execution phases are high risk. Mitigate by requiring explicit design review and no live execution paths in V1.
