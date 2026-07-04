# ROB-692 — 종목상세 결정론 추천 카드 (action·신뢰도·진입/목표/손절 + R:R)

> Implementation plan only. Source untouched. Worktree:
> `/Users/mgh3326/work/auto_trader.rob-692` (branch
> `feature/ROB-692-stock-detail-reco-card`, based on `main` which already
> includes ROB-690 R:R helper + ROB-691 scoreboard).

## 1. Goal

Surface the **already-existing deterministic** recommendation
(`build_recommendation_for_equity`) on the `/invest/stocks/:market/:symbol`
web page as an **on-demand card**: action (buy/hold/sell), confidence,
buy_zones / sell_targets / stop_loss, reasoning — and, for a buy setup, a
**risk/reward (R:R) chip** derived by reusing ROB-690's now-merged
`risk_reward` helper (`resolve_direction` / `build_trade_setup`).

The recommendation is MCP-only today. ROB-692 adds a thin read endpoint +
frontend card. **No new judgment/model** — the endpoint composes existing
deterministic primitives. Wiring R:R onto this recommendation path also
**fulfills ROB-690's deferred "Step 4"** (attach R:R where the entry/stop/
target actually originate: `build_recommendation_for_equity`).

## 2. Verified current state (file:line, with corrections)

### Recommendation core — MCP-only (confirmed)
- `build_recommendation_for_equity(analysis, market_type)` —
  `app/mcp_server/tooling/shared.py:524-808`. Issue's "~524-808" is **exact**.
  Return shape confirmed (dict, or `None` when `quote`/price absent):
  ```
  {"action": "buy"|"hold"|"sell", "confidence": "high"|"medium"|"low",
   "rsi14": float|None, "buy_zones": [ {price,type,reasoning} ] (≤3, asc),
   "sell_targets": [ {price,type,reasoning} ] (≤3, asc, all > current_price),
   "stop_loss": float|None, "reasoning": str}
  ```
  - `sell_targets` sorted **ascending** → `[0]` is the **nearest** target
    above current price ("first sell_target" in the issue).
  - `stop_loss` derived from nearest support×0.98, else 52w-low, else
    current×0.92; KR is tick-snapped. Always **< current_price**.
- **Correction to issue wording**: the issue says the reco is "returned by
  `analyze_stock`". Precisely: `analyze_stock_impl`
  (`app/mcp_server/tooling/analysis_analyze.py:726`) runs the full fetch
  pipeline (250 OHLCV bars + quote + indicators + support_resistance +
  opinions + valuation + optional peers), then `_apply_recommendation(...)`
  (`analysis_analyze.py:683-723`) calls `build_recommendation_for_equity`
  (line 690), **floors** it via `insufficient_inputs`/`floored_action`, adds
  `insufficient_inputs`, and attaches `analysis["recommendation"]`. Only
  `equity_kr`/`equity_us` get a recommendation (line 687 early-return for
  crypto).
- **No FastAPI route exposes it** (confirmed): the only callers of
  `build_recommendation_for_equity` and `analyze_stock_impl` live under
  `app/mcp_server/tooling/**` (+ tests). `grep app/routers` → none.

### ROB-690 R:R helper — in main (confirmed, path correct)
- `app/services/investment_reports/risk_reward.py` — `resolve_direction`
  (:78), `compute_leg` (:147), `build_trade_setup` (:200). stdlib+`decimal`
  only; long-default / explicit-short; **fail-closed**
  (`direction_price_mismatch` / `degenerate_risk` → empty legs / `None`).
- `resolve_direction(side, intent, item_kind, explicit_direction)` returns
  `"long"|"short"|"exit"|"unknown"` — `side="buy"`→`long`, `side="sell"`→
  `exit` (skip R:R), else intent fallback, else `unknown` (skip).
- **Correction/clarification**: `_serialise_trade_setup` (Decimal→str JSON
  dict) is **not** in `risk_reward.py` — it lives at
  `app/services/investment_reports/ingestion.py:41-67`. ROB-692 will
  **mirror** that serialization shape (not import it) so the wire payload
  matches what the frontend already knows how to parse.
- Reference wiring precedent: `ingestion.py:561-585` — the exact
  `resolve_direction → (long/short only) → build_trade_setup → (status
  ==="computed") → _serialise_trade_setup` sequence to copy.

### Frontend — no reco/confidence/setup panel today (confirmed)
- `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx` — `market`/
  `symbol` from `useParams` (`:497-500`; `market` default `"us"`, type
  `StockDetailMarket = "kr"|"us"|"crypto"`). Page eagerly fires ~8 fetches in
  one `useEffect` (`:512-555`) and renders cards at `:586-609`
  (`HeaderCard`, `TradeGuardrail`, `ChartCard`, `OrderLedgerCard`,
  `WatchCard`, `RetrospectiveCard`, `NewsCard`, `InvestorFlowCard`…). **No
  recommendation / confidence / entry-target-stop card exists.**
- `frontend/invest/src/desktop/stock-detail/` today: `InvestorFlowCard`,
  `OrderLedgerCard`, `RetrospectiveCard`, `WatchCard`. (No reco card.)
- Confidence-chip style to match — `InvestmentReportBundleContent.tsx`
  (`frontend/invest/src/components/investment-reports/`): `formatConfidence`
  (:197), chip `Pill tone="accent" size="sm"` "신뢰도 {n}" (:310-314); R:R
  chip pattern `Pill tone={long?"gain":"warn"}` + "손익비 R:R … 리스크 …%
  리워드 …%" (:330-340). `Pill` is exported from the shared DS
  (`import { Card, Pill } from "../../ds"`; `ds/atoms.tsx` →
  `PillTone` includes `gain|warn|accent|paper`). ROB-692 reuses `Pill` from
  `ds` directly — it does **not** import from or edit
  `InvestmentReportBundleContent.tsx` (that file is ROB-693's; see §7).

### Read-endpoint pattern to mirror (confirmed)
- `app/routers/invest_api.py` — prefix `/trading/api/invest`;
  `StockDetailMarketParam = Literal["kr","us","crypto"]` (:430); existing
  siblings `GET /stock-detail/{market}/{symbol}` (:433) and
  `.../research-consensus` (:455). Auth: `Depends(get_authenticated_user)`
  + `Depends(get_db)`; `SymbolNotFound → HTTPException(404)`;
  **research-consensus 400s for `crypto`** (`:463-467`,
  `research_consensus_supports_kr_us_only`) — the exact equity-only guard
  ROB-692 mirrors.
- Frontend fetch base: `frontend/invest/src/api/stockDetail.ts:18`
  `stockDetailPath` → `/invest/api/stock-detail/{market}/{symbol}` (proxy
  rewrite of the `/trading/api/invest` prefix; existing fetchers all use it —
  no correction, just mirror).
- Service-layer precedent that imports MCP tooling into web code is
  established (e.g. `stock_detail_research_consensus_service.py` imports
  `app.mcp_server.tooling.fundamentals._valuation`; ~20 services import
  `app.mcp_server.tooling`). Reusing `analyze_stock_impl` from a view-model
  service is **in-pattern** and violates no boundary (it does deterministic
  fetch+scoring, **no in-process LLM** — the ROB-501 guard only forbids LLM
  providers).
- Market map (`shared.normalize_market`, :113): `kr→equity_kr`,
  `us→equity_us`, `crypto→crypto`. URL market feeds `analyze_stock_impl`
  directly.

## 3. Design decisions

### 3a. Endpoint shape
```
GET /trading/api/invest/stock-detail/{market}/{symbol}/recommendation
  auth: Depends(get_authenticated_user) + Depends(get_db)   # same as siblings
  market: StockDetailMarketParam = Literal["kr","us","crypto"]
  → 400 research_recommendation_supports_kr_us_only   when market=="crypto"
  → 404 symbol_not_found                              on ValueError/SymbolNotFound
  → 200 StockDetailRecommendationResponse
```
Response schema (`app/schemas/invest_stock_detail_recommendation.py`, NEW):
```
StockDetailRecommendationResponse:
  market, symbol, name: str|None
  as_of: datetime            # analysis derived_as_of / now(UTC)
  current_price: float|None
  action: Literal["buy","hold","sell"]
  confidence: Literal["high","medium","low"]
  rsi14: float|None
  reasoning: str
  insufficient_inputs: list[str]      # carried through from floor
  buy_zones:   list[RecoZone]         # {price, type, reasoning}  (≤3)
  sell_targets:list[RecoZone]         # {price, type, reasoning}  (≤3)
  stop_loss: float|None
  trade_setup: RecoTradeSetup | None  # R:R; None when not derivable (fail-closed)

RecoTradeSetup:  # mirrors ingestion._serialise_trade_setup headline shape
  direction: Literal["long","short"]
  entry, stop, target: str           # Decimal-as-string
  risk_pct, reward_pct, rr_ratio: str
```

### 3b. Equity-only scope
`build_recommendation_for_equity` is equity-specific and
`_apply_recommendation` no-ops for crypto. Mirror research-consensus:
**crypto → 400** at the router; the frontend gates the card to
`market !== "crypto"` (same guard the page already applies to
`ResearchConsensusCard`). No crypto fallback synthesized (honest omission).

### 3c. Service reuses `analyze_stock_impl` (no re-fetch reimplementation)
New `app/services/invest_view_model/stock_detail_recommendation_service.py`:
```
async def build_stock_detail_recommendation(*, market, symbol) -> ...:
    analysis = await analyze_stock_impl(symbol, market=market)   # MCP impl fn
    reco = analysis.get("recommendation")            # already floored
    quote = analysis.get("quote") or {}
    current = quote.get("price")
    setup = _derive_trade_setup(reco, current, market)  # §3d, may be None
    return StockDetailRecommendationResponse(... reco fields ..., trade_setup=setup)
```
Rationale for reuse over reimplementation: `analyze_stock_impl` is the *only*
place that assembles the full `analysis` dict (quote+indicators+S/R+opinions+
valuation) that `build_recommendation_for_equity` consumes, **and** applies
the ROB-486/insufficient-inputs floor. Re-deriving it would duplicate ~250
lines of fetch/floor logic and drift. It already carries ROB-638 fetch-cache
internally, so repeat calls are cheap-ish.

### 3d. R:R reuse (fulfills ROB-690 Step 4) + direction guard + fail-closed
`_derive_trade_setup(reco, current_price, market)`:
1. `direction = resolve_direction(side=_action_to_side(reco["action"]),`
   `intent=None-ish, item_kind="action", explicit_direction=None)`
   where `_action_to_side`: `buy→"buy"` (→`long`), `sell→"sell"` (→`exit`),
   `hold→None` (→`unknown`). **Only `long` proceeds**; `exit`/`unknown`/
   `short` → return `None` (no R:R chip). (No live position here, so an
   `exit`/sell reco has no realized frame to show — skip, per issue.)
2. Build the triple (long): `entry = current_price` (answers "R:R if bought
   at today's price"); `stop = reco["stop_loss"]`; `target =
   reco["sell_targets"][0]["price"]` (nearest above current). If `current` is
   missing but `buy_zones` exist, fall back `entry = buy_zones[-1]["price"]`
   (top/highest buy_zone, still < current historically) — the issue's
   "entry = current price **or** top buy_zone".
3. Feed `build_trade_setup(entry_levels=[entry], quantities=[None],`
   `stop=Decimal(stop), target=Decimal(target), direction="long")`. Any
   missing leg (no stop / no target) → skip. `status != "computed"`
   (`direction_price_mismatch` when the price ordering isn't
   `stop < entry < target`, or `degenerate_risk`) → return `None`.
   **Fail-closed**: the card simply omits the R:R chip rather than showing a
   misleading ratio.
4. Serialize headline → `RecoTradeSetup` (mirror
   `ingestion._serialise_trade_setup`: Decimal→str). Single-leg is the
   default; multi-leg over `buy_zones` is an **optional** follow-up (keep
   single-entry for clarity + simplest fail-closed surface).

Decimal hygiene: convert floats via `Decimal(str(x))` (the helper is
Decimal-typed; never pass raw float).

### 3e. On-demand, not eager
The card fetches **on user action** (a "추천 실행 / R:R 보기" button inside
the card), not in the page-load `useEffect`. Justification:
- `analyze_stock_impl` is the heaviest call on the page (multi-provider:
  OHLCV 250 bars + quote + indicators + opinions + valuation + optional
  peers). The page already fires ~8 eager fetches; adding this to load would
  measurably slow first paint and multiply provider/rate-limit pressure on
  every view.
- The recommendation is an explicit "compute me a fresh deterministic call"
  action, not passive context — on-demand matches intent and keeps the
  default page cheap. Card shows an idle CTA → loading → result/empty/error.

### 3f. migration-0
Read-only; no persistence, no new table/column. No caching row needed
(`analyze_stock_impl` already memoizes fetches via ROB-638). `alembic
heads` unchanged.

## 4. Step-by-step

### Backend
1. **Schema** — `app/schemas/invest_stock_detail_recommendation.py` (NEW):
   `RecoZone`, `RecoTradeSetup`, `StockDetailRecommendationResponse`
   (Pydantic v2, `from __future__ import annotations`).
2. **Service** —
   `app/services/invest_view_model/stock_detail_recommendation_service.py`
   (NEW): `build_stock_detail_recommendation(*, market, symbol)` calling
   `analyze_stock_impl` + `_derive_trade_setup` (uses
   `app.services.investment_reports.risk_reward.{resolve_direction,
   build_trade_setup}`). Map `analyze_stock_impl` `ValueError` (unsupported
   symbol) → raise `SymbolNotFound` (reuse the existing exception the router
   already catches).
3. **Router** — `app/routers/invest_api.py` (EDIT): add
   `GET /stock-detail/{market}/{symbol}/recommendation` mirroring
   `get_stock_detail_research_consensus` (crypto→400, SymbolNotFound→404).
   Add the schema + service imports at top.

### Frontend
4. **Types** — `frontend/invest/src/types/stockDetail.ts` (EDIT): add
   `StockDetailRecommendationResponse` (+ `RecoZone`, `RecoTradeSetup`).
5. **API** — `frontend/invest/src/api/stockDetail.ts` (EDIT): add
   `fetchStockDetailRecommendation({market, symbol})` using existing
   `stockDetailPath(...) + "/recommendation"`.
6. **Card** —
   `frontend/invest/src/desktop/stock-detail/RecommendationCard.tsx` (NEW):
   idle-CTA / loading / error / result states. Renders action badge,
   confidence `Pill tone="accent" size="sm"` (matching the report-bundle
   style), buy_zones / sell_targets / stop_loss list, reasoning, and — when
   `trade_setup` present — the R:R chip (`Pill tone="gain"` "롱" + "손익비
   R:R … · 리스크 …% · 리워드 …%", copying the bundle-card pattern).
   `insufficient_inputs` → a muted note. **Self-contained** parse/format
   (no import from `InvestmentReportBundleContent.tsx`). Import `Pill`,
   `Card` from `../../ds`.
7. **Page wiring** — `StockDetailPage.tsx` (EDIT): add
   `recommendation`/`recoLoading`/`recoErr` state + a `loadRecommendation()`
   callback (fetch on button click; reset on `market`/`symbol` change).
   Render `{market !== "crypto" ? <RecommendationCard … onLoad={…}/> : null}`
   in the center column near the top (e.g. after `TradeGuardrail`).

### Tests (§5)

## 5. Test plan
Backend (`tests/test_invest_stock_detail_recommendation.py`, NEW):
- Service: monkeypatch `analyze_stock_impl` to a canned `analysis` dict →
  assert response fields pass through (action/confidence/zones/targets/
  stop_loss/reasoning/insufficient_inputs).
- R:R happy path (buy, `stop < current < target`) → `trade_setup.direction
  == "long"`, `rr_ratio` matches `(target-entry)/(entry-stop)` quantized.
- Fail-closed: (a) `action="sell"` → `trade_setup is None`; (b)
  `action="hold"` → `None`; (c) buy but `sell_targets == []` → `None`;
  (d) buy but `stop_loss >= current` (degenerate/mismatch) → `None`.
- Router: crypto → 400 `research_recommendation_supports_kr_us_only`;
  unknown symbol (service raises `SymbolNotFound`) → 404; unauthenticated →
  401/403 (reuse existing auth-fixture pattern from sibling stock-detail
  tests).
- Reuse the real `risk_reward` helper in the R:R assertions (don't mock it)
  to prove the ROB-690 reuse.

Frontend (optional, if a sibling card test exists):
`RecommendationCard.test.tsx` — idle→click→result render; R:R chip shows only
when `trade_setup` present; crypto card not rendered.

Commands: `uv run pytest tests/test_invest_stock_detail_recommendation.py -v`;
`make lint`; (frontend) `cd frontend/invest && npm run test -- RecommendationCard`.

## 6. Migration note
**migration-0.** No ORM/schema/table changes. Pure read composition over
existing deterministic functions. `uv run alembic heads` unchanged. No
persistence, no new caching table (ROB-638 fetch-cache is internal to
`analyze_stock_impl`).

## 7. Risks / out-of-scope
- **Parallel-safety vs ROB-693** — ROB-693 edits `app/schemas/
  research_pipeline.py`, `app/schemas/investment_reports.py`,
  `app/services/investment_stages/hermes_ingest.py`, and
  `frontend/.../InvestmentReportBundleContent.tsx`. ROB-692 touches **none**
  of those (see §8). Overlap-avoidance: ROB-692 puts its schema in a **new**
  `invest_stock_detail_recommendation.py` and creates a **new**
  `RecommendationCard.tsx` rather than editing the bundle card — fully
  disjoint. Both may add to `app/schemas/` but never the same file.
- **Latency / provider load** — mitigated by on-demand fetch (§3e); if even
  on-demand is too heavy, a future follow-up could add a lighter "reco-only"
  fetch path, but that's out of scope.
- **Do NOT resurrect deprecated pages** — `/analysis-json`, `/stock-latest`
  are 410 tombstones (`app/routers/deprecated_pages.py:16-17`). This card
  lives only under the SPA `/invest/stocks/:market/:symbol`; the tombstone
  router is untouched.
- **Floor semantics** — the response carries `insufficient_inputs` and the
  already-floored action/confidence verbatim; ROB-692 does not re-score or
  re-floor. R:R never contradicts the floor (skips on non-`long`).
- **Out of scope**: crypto recommendation, multi-leg R:R over all buy_zones
  (single representative entry only), persisting the reco, any broker/order/
  watch mutation (none reachable — read-only).

## 8. Exact files touched (for ROB-693 disjointness check)
**New (backend):**
- `app/schemas/invest_stock_detail_recommendation.py`
- `app/services/invest_view_model/stock_detail_recommendation_service.py`
**Edit (backend):**
- `app/routers/invest_api.py`  (add one GET route + 2 imports)
**New (frontend):**
- `frontend/invest/src/desktop/stock-detail/RecommendationCard.tsx`
**Edit (frontend):**
- `frontend/invest/src/types/stockDetail.ts`  (add response types)
- `frontend/invest/src/api/stockDetail.ts`  (add fetcher)
- `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx`  (state + render)
**New (tests):**
- `tests/test_invest_stock_detail_recommendation.py`
- (optional) `frontend/invest/src/__tests__/RecommendationCard.test.tsx`

**Disjoint from ROB-693** (`app/schemas/research_pipeline.py`,
`app/schemas/investment_reports.py`,
`app/services/investment_stages/hermes_ingest.py`,
`frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`)
— zero shared files.
