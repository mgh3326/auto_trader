# ROB-62 — KR Preopen Hermes News Brief MVP — Implementation Plan

## Goal & Scope
Add a KR preopen "Hermes news brief" surface that is **advisory-only** end-to-end: surface news readiness gating, stale warnings, confidence caps, and sector/candidate impact/risk flags through the existing read-only preopen dashboard, and persist the brief as a `ResearchRun` (with candidates of kind `proposed`/`other`) using existing advisory-only invariants. TradingAgents output, when present, is consumed only as supporting evidence and as a confidence adjuster — never as a trade signal.

**Non-goals (hard guardrails):**
- No order placement, no dry-run orders, no watch list mutation, no order-intent records.
- No writes to production trading tables (`symbol_trade_settings`, `manual_holdings`, holdings/position tables, KIS/Upbit order tables).
- No new outbound API calls (KIS/Upbit/broker/Slack) on the preopen path.
- TradingAgents is **not** invoked synchronously from the preopen route; consumed only if a recent `ResearchRun` already exists.

---

## Files to Edit / Add

### New
- `app/services/kr_preopen_news_brief_service.py`
  - Pure assembly service: takes news readiness + latest news preview + (optional) latest TradingAgents-backed `ResearchRun` evidence and returns a `KRPreopenNewsBrief` schema. No I/O beyond the existing news/research_run services.
- `app/schemas/preopen_news_brief.py`
  - `KRPreopenNewsBrief`, `SectorImpactFlag`, `CandidateImpactFlag`, `RiskFlag`, `BriefConfidence` Pydantic models.
- `tests/services/test_kr_preopen_news_brief_service.py`
- `tests/test_router_preopen_news_brief.py`
- `tests/services/test_research_run_service_news_brief_safety.py`

### Edited
- `app/schemas/preopen.py`
  - Add optional `news_brief: KRPreopenNewsBrief | None` field to the KR preopen dashboard response.
- `app/services/preopen_dashboard_service.py`
  - After existing news-readiness merge, build the brief via `kr_preopen_news_brief_service`, attach to response. Apply confidence cap + warning when readiness is `stale`/`degraded`. Gate on news readiness `ok | stale | unavailable` (never raises; gracefully omits or marks unavailable).
- `app/routers/preopen.py`
  - No new endpoints; keep route read-only. Ensure the brief is included in existing KR preopen response model.
- `app/services/research_run_service.py`
  - Add `record_kr_preopen_news_brief(...)` helper that persists a `ResearchRun` (`kind="kr_preopen_news_brief"` or existing `proposed` kind per current schema), with `market_brief`, `source_freshness`, `source_warnings`, `advisory_links` populated, and candidates of kind `proposed`/`other`. Reuses existing advisory-only validators (`advisory_only=True`, `execution_allowed=False`). No new writes elsewhere.
- `app/services/llm_news_service.py`
  - No behavior change; only ensure `get_news_readiness` / `get_latest_news_preview` return shapes the brief service expects (add a thin typed accessor if needed).

---

## Data / API Shape

### Schema: `KRPreopenNewsBrief` (new)
```python
class RiskFlag(BaseModel):
    code: Literal["news_stale", "news_unavailable", "ingestion_partial",
                  "low_evidence", "tradingagents_unavailable"]
    severity: Literal["info", "warn", "block_advisory_only"]
    message: str

class SectorImpactFlag(BaseModel):
    sector: str                      # e.g. "반도체", "2차전지"
    direction: Literal["positive", "negative", "mixed", "unclear"]
    confidence: int                  # 0-100, capped by readiness
    sources: list[NewsRefRef]        # references to NewsArticle ids
    reasons: list[str]               # max 3

class CandidateImpactFlag(BaseModel):
    symbol: str                      # KR symbol (DB '.' format)
    name: str
    direction: Literal["positive", "negative", "mixed", "unclear"]
    confidence: int                  # 0-100
    sector: str | None
    reasons: list[str]               # max 3
    research_run_candidate_id: int | None  # link to ResearchRunCandidate

class BriefConfidence(BaseModel):
    overall: int                     # 0-100
    cap_reason: Literal["news_stale", "news_unavailable",
                        "no_tradingagents_evidence", "ok"]

class KRPreopenNewsBrief(BaseModel):
    generated_at: datetime
    news_readiness: Literal["ok", "stale", "degraded", "unavailable"]
    news_max_age_minutes: int | None
    confidence: BriefConfidence
    sector_flags: list[SectorImpactFlag]      # max ~5
    candidate_flags: list[CandidateImpactFlag] # max ~10, advisory-only
    risk_flags: list[RiskFlag]
    research_run_id: int | None               # latest backing ResearchRun, if any
    advisory_only: Literal[True] = True       # invariant
```

### Confidence cap rules
| Readiness     | Max overall confidence | Required risk flag             |
|---------------|------------------------|--------------------------------|
| `ok`          | 90                     | none                           |
| `stale`       | 60                     | `news_stale` (warn)            |
| `degraded`    | 40                     | `ingestion_partial` (warn)     |
| `unavailable` | 0 (brief omitted or marked unavailable) | `news_unavailable` (warn) |

Per-flag `confidence` is also clamped to `min(flag.confidence, brief.confidence.overall)`.

### Persistence (advisory-only)
- One `ResearchRun` per preopen brief generation:
  - `market_brief`: human-readable Korean summary string.
  - `source_freshness`: `{ "news": {...readiness payload...}, "tradingagents": {...} }`.
  - `source_warnings`: list mirroring `risk_flags`.
  - `advisory_links`: only entries with `advisory_only=True, execution_allowed=False`.
  - `kind`: prefer `proposed` (existing) — confirm against current enum; otherwise reuse `other`.
- `ResearchRunCandidate` rows for each `CandidateImpactFlag`:
  - `kind = "proposed"` (or `"other"`).
  - `payload`: candidate flag JSON (sector, direction, reasons, sources).
  - `warnings`: per-candidate warnings (e.g., `low_evidence`).
  - `confidence`: capped value.
  - **Never** populated with order quantity, price, side, or any execution field.

---

## Logic Flow

1. `preopen_dashboard_service.build_kr_dashboard()` (existing) computes source freshness + news readiness as today.
2. Call `kr_preopen_news_brief_service.build_brief(...)`:
   - Read news readiness via `get_news_readiness`.
   - Read latest news preview via `get_latest_news_preview` (already used by service).
   - Read most recent `ResearchRun` of relevant kind via `research_run_service.get_latest_for_kr_preopen(...)` (read-only). If TradingAgents evidence exists in its `advisory_links`, use it to bump `confidence.overall` (subject to readiness cap) and to enrich candidate `reasons`. Absence ⇒ `tradingagents_unavailable` info flag, no failure.
   - Aggregate sector + candidate flags purely from news + research_run payloads (no LLM call introduced here in MVP — deterministic extraction; LLM enrichment is out of scope and explicitly deferred).
   - Apply confidence cap + risk flags.
3. Attach `news_brief` to the dashboard response. If readiness is `unavailable`, attach a brief with empty flag lists, `confidence.overall=0`, and a `news_unavailable` warn flag — the route still returns 200.
4. Persistence is opt-in via a guarded `record=True` parameter (default off in the read-only dashboard request path). Persistence is performed only by an explicit caller (e.g., a scheduled job or admin endpoint, **not added in this MVP**); MVP only wires the assembly + response surface and the persistence helper with safety tests. This keeps the GET dashboard truly read-only.

---

## Tests

### Unit — `tests/services/test_kr_preopen_news_brief_service.py`
- Readiness `ok` ⇒ `confidence.overall ≤ 90`, no stale flag, full sector/candidate flags returned.
- Readiness `stale` ⇒ `confidence.overall ≤ 60`, `news_stale` warn flag present, per-flag confidence clamped.
- Readiness `degraded` ⇒ `confidence.overall ≤ 40`, `ingestion_partial` flag present.
- Readiness `unavailable` ⇒ `confidence.overall == 0`, empty flag lists, `news_unavailable` flag.
- TradingAgents evidence absent ⇒ `tradingagents_unavailable` info flag, no exception.
- TradingAgents evidence present ⇒ `confidence.overall` increased (still under cap), reasons enriched.
- All produced `CandidateImpactFlag` payloads contain **no** keys named `quantity`, `price`, `side`, `order_type`, `dry_run`, `watch`, `order_intent` (asserted explicitly).

### Router — `tests/test_router_preopen_news_brief.py`
- GET KR preopen returns 200 with `news_brief` populated for each readiness state (parametrized).
- Response is read-only: assert no DB writes occur during the request (use SQLAlchemy event hooks or assert via spy on session `add/commit`).
- News readiness `unavailable` ⇒ response still 200, brief marked unavailable.

### Safety — `tests/services/test_research_run_service_news_brief_safety.py`
- `record_kr_preopen_news_brief(...)` persists `ResearchRun` with `advisory_only=True`, `execution_allowed=False`; rejects payload if any candidate carries forbidden execution keys (`quantity`, `price`, `side`, `order_type`, `dry_run`, `watch`, `order_intent`).
- Persisting with an `advisory_link` whose `execution_allowed=True` raises (reuses existing validator).
- No row is inserted into `symbol_trade_settings`, `manual_holdings`, or any holdings/order table during persistence (asserted via table-watcher fixture, mirroring `tests/services/test_research_run_service_safety.py`).
- No outbound HTTP / KIS / Upbit / Slack call is made during brief assembly or persistence (assert via mocked client classes raising on call).

### Existing tests — extend, don't break
- `tests/test_preopen_dashboard_service.py`: add cases asserting the new `news_brief` field is present and conformant; existing assertions on `source_freshness` / `source_warnings` remain green.
- `tests/test_router_preopen.py`: smoke-assert `news_brief` key in JSON for KR.
- `tests/services/test_research_run_service_safety.py`: extend forbidden-keys list to include `dry_run`, `watch`, `order_intent` if not already present.

---

## Verification

```bash
uv run pytest tests/services/test_kr_preopen_news_brief_service.py -v
uv run pytest tests/test_router_preopen_news_brief.py -v
uv run pytest tests/services/test_research_run_service_news_brief_safety.py -v
uv run pytest tests/test_preopen_dashboard_service.py tests/test_router_preopen.py -v
uv run pytest tests/services/test_research_run_service_safety.py -v
make lint
make typecheck
```

Manual:
- `make dev`, hit `GET /preopen/kr` and confirm `news_brief` shape across readiness states (toggle by adjusting `NewsIngestionRun` fixtures or env-controlled `max_age_minutes`).
- Confirm DB unchanged after dashboard GET (compare row counts on `research_runs`, `research_run_candidates`, `symbol_trade_settings`, `manual_holdings` before/after).

---

## Risks & Mitigations

- **Risk:** Confidence cap drift between schema, service, and tests.
  **Mitigation:** Centralize cap table as a constant in the service module; tests parametrize over it.
- **Risk:** A future caller persists the brief on every GET, breaking read-only invariant.
  **Mitigation:** Keep `record_kr_preopen_news_brief` out of the GET path; document with module-level comment + safety test asserting no writes during dashboard GET.
- **Risk:** TradingAgents evidence schema changes.
  **Mitigation:** Treat absence/parse failure as `tradingagents_unavailable` info flag; never raise.
- **Risk:** Candidate payload accidentally contains execution keys via copy-paste.
  **Mitigation:** Forbidden-key validator in `research_run_service.record_kr_preopen_news_brief` + dedicated test.
- **Risk:** News readiness signal misinterpreted (`degraded` vs `stale`).
  **Mitigation:** Reuse `get_news_readiness` enum directly; do not re-derive freshness in the brief service.

---

## Non-Goals (explicit)

- No order placement, no dry-run order simulation, no `watch` list mutation, no `order_intent` records — enforced by tests.
- No new LLM calls in the brief assembly (deterministic aggregation only); LLM-driven sector/candidate enrichment is a follow-up.
- No new outbound API integrations (KIS/Upbit/Slack/etc.).
- No production DB schema changes; reuse existing `ResearchRun` / `ResearchRunCandidate` / news tables.
- No changes to `production` branch deploy artifacts.

---

## Rollout

1. Land schema + service + tests behind no flag (read-only surface; safe by construction).
2. Verify on `main` via dashboard inspection.
3. Persistence helper remains unused by any scheduled job in MVP — wiring into a scheduled job is a separate ticket.

<!-- AoE:BEGIN ROB-62-kr-preopen-hermes-news-brief-plan -->
PLAN_VERSION: 1
PLAN_ID: ROB-62
PLAN_STATUS: ready-for-review
PLAN_SAFETY: advisory-only; no-orders; no-dry-run; no-watch; no-order-intent; no-prod-db-writes
<!-- AoE:END ROB-62-kr-preopen-hermes-news-brief-plan -->

AOE_STATUS plan_ready
AOE_ISSUE ROB-62
AOE_ROLE planner
AOE_PLAN_PATH docs/plans/ROB-62-kr-preopen-hermes-news-brief-plan.md
AOE_NEXT start_implementer_same_session
