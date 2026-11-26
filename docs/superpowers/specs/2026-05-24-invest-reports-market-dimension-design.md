# `/invest/reports` TradingAgents-style Dimension Reports — Market Dimension (Slice 1)

- **Date**: 2026-05-24
- **Status**: Design approved, pending spec review
- **Branch**: `rob-306`
- **Linear**: [ROB-306](https://linear.app/mgh3326/issue/ROB-306)

## Context & problem

The user wants `/invest/reports` to work like the [TradingAgents](https://github.com/tauricresearch/tradingagents) project: per-dimension **analyst reports** (Market, News, Fundamentals, Sentiment) as the *intermediate* layer, synthesized into a *final* report that decides buy/sell + surfaces new buy candidates.

Investigation (2026-05-24) found the intermediate layer **does not work — not one dimension produces a meaningful report**, for two compounding reasons:

1. **Empty/missing evidence** — market_events (ROB-128) and research_reports (ROB-140/207) ingestion are not scheduled; crypto screener snapshots are empty (ROB-282 unshipped); there is no fundamentals or sentiment dimension at all.
2. **No analyst layer** — the deterministic stages (`market`, `news`, `candidate_universe`, `portfolio_journal`, `watch_context`) emit thin threshold verdicts (e.g. `"KOSPI change_percent=+0.50%"`), not prose analysis. The LLM composer that synthesized dimensions was removed in ROB-287. Hermes today produces only per-symbol intermediate reports (ROB-301) + final composition — **no per-dimension analyst reports**.

A separate TradingAgents integration exists (ROB-9, `app/services/trading_decisions/`) but is advisory-only, writes to `trading_decisions`, and is disconnected from `/invest/reports`.

## Decisions locked (with the user)

- **Reasoning engine = Hermes-as-analyst.** Hermes plays the TradingAgents analyst role. The ROB-9 TradingAgents runner is **not** used for this pipeline. auto_trader stays deterministic (ROB-287 boundary: no in-process LLM).
- **Approach = one dimension end-to-end first**, then replicate. First dimension = **Market** (data most ready: KR/US `invest_screener_snapshots` populated; reuses `screener_evidence` from PR1/PR2; it is the discovery entry point and where the user already invested via the screener data work).
- **No backward compatibility required.** Existing reports may disappear; data may be re-ingested; the thin `market` stage content may be replaced outright. No transitional shims.

## Goal

Prove the reusable **Hermes dimension-report pattern** by making the Market dimension flow end-to-end:

```
auto_trader: Market evidence bundle (deterministic, populated)
  → Hermes context export (dimension-scoped)
  → Hermes writes the "Market analyst report"  (new dimension-report contract)
  → auto_trader persists it (new investment_dimension_reports table)
  → human-readable GET surface
```

Establishing this contract is the deliverable; News/Fundamentals/Sentiment replicate it later.

## Non-goals / boundaries

- **No in-process LLM** — Market evidence assembly is deterministic; Hermes authors the prose. ROB-287 static import guard must continue to pass.
- **No broker/order/watch/order-intent mutation.**
- **No final synthesis in this slice** — the final report (dimensions → buy/sell + new candidates) is the *next* slice. You cannot synthesize dimensions until ≥1 dimension works. The removed composer is rebuilt separately.
- **No crypto Market dimension** — crypto screener snapshots are empty until ROB-282; this slice covers KR/US where data exists. The contract is crypto-ready (market discriminator) but crypto evidence is deferred.
- **No new ingestion scheduling** — Market reuses already-populated `invest_screener_snapshots` (KR/US, ROB-281). News/Fundamentals will need ingestion work in their own slices.

## Architecture

### M1. Market evidence bundle (auto_trader, deterministic, populated)

New service `app/services/investment_dimensions/market_evidence.py` assembles a structured Market evidence package from already-populated sources (reusing `screener_evidence`):

- **breadth** — advancers/decliners ratio, count of consecutive-up-day streaks / new highs (from `invest_screener_snapshots` via repository aggregates).
- **top_movers / candidates** — top gainers via `screener_evidence.build_candidate_evidence` + `list_top_candidates` (PR1), with new-vs-held split (PR2).
- **regime / indices** — index level + change% where available (the existing thin `market` snapshot's KOSPI value is folded in here; clean-cut: the thin stage is subsumed).
- **held_in_market** — held symbols currently moving (PR2 held cross-check).
- **freshness / data_health** — snapshot dates, fresh/stale counts, source coverage.

Output: a deterministic JSON bundle keyed by `market` (kr/us). No prose. This is the analyst's raw material.

### M2. `investment_dimension_reports` table (Hermes-authored, new)

Mirrors the ROB-301 symbol-intermediate-report pattern, but on the **dimension** axis (and supports market-wide via nullable symbol):

| column | notes |
|---|---|
| `run_uuid` | FK → `investment_stage_runs.run_uuid` (CASCADE) |
| `dimension` | locked enum: `market` \| `news` \| `fundamentals` \| `sentiment` |
| `market` | `kr` \| `us` \| `crypto` |
| `account_scope` | nullable |
| `symbol` | **nullable** — null = market-wide (Market); set = per-symbol (future News/Fundamentals) |
| `report_text` | Hermes prose (the analyst report) |
| `key_findings` | JSONB `list[str]` |
| `signals` | JSONB dict (e.g. `{regime, breadth, leadership}`) |
| `stance` | nullable: `bullish` \| `neutral` \| `bearish` (Hermes-provided) |
| `confidence` | 0–100, **freshness-capped by auto_trader** (reuse PR1 cap policy) |
| `cited_snapshot_uuids` | JSONB array |
| `source_evidence_refs` | JSONB (which bundle parts were used) |
| `freshness_summary` | JSONB |
| `missing_data` | JSONB `list[str]` |
| `content_hash` | SHA256 of canonical fields → idempotent upsert |
| `artifact_version` | bumps on content change |

- Unique: `(run_uuid, dimension, market, symbol, artifact_version)`.
- All writes via a service layer (no direct SQL), mirroring ROB-301.

### M3. Hermes dimension-reports ingest contract (new, push-only)

- `POST /trading/api/investment-reports/hermes/dimension-reports` — token-authed under the existing `retired-hermes-ingest-path-prefix` branch; push-only; no in-process LLM. Mirrors the ROB-301 `/symbol-reports` route + ingest service shape.
- Request: `{run_uuid, dimension, market, account_scope?, symbol?, report_text, key_findings[], signals{}, stance?, confidence?, cited_snapshot_uuids[], ...}` with `extra="forbid"`.
- auto_trader validates run membership, caps confidence by freshness, computes content_hash, upserts.
- **Context export**: extend the Hermes context (`HermesContextPayload` / `hermes_context.py`) to include the Market evidence bundle (M1) so Hermes has the material to write the report. Gated by the existing `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` env flag.

### M4. Read surface (human-facing)

- `GET /trading/api/investment-reports/runs/{run_uuid}/dimension-reports?dimension=market` — read-only, service-layer, reuses existing investment-reports auth.
- Korean view-model (following the `screener_service` view-model pattern): report prose + key findings + signals + freshness chip + stance badge + confidence.

## Clean-cut notes (no backward compat)

- The thin deterministic `market` stage's role is **subsumed** by M1's richer evidence bundle; the old single-metric summary may be removed rather than preserved.
- No migration-compat shims; the new table ships clean.
- Re-ingesting / re-running to populate fresh data is acceptable.

## Migration

- **One new table** (`investment_dimension_reports`) via alembic. Per repo convention the migration ships in the PR but the operator runs `alembic upgrade head` separately (production cutover gate).

## Testing strategy

- `market_evidence` assembler: fixture `invest_screener_snapshots` rows (kr/us) → expected breadth/movers/held/freshness bundle; empty-partition + stale edges.
- `investment_dimension_reports` model + ingest service: validation (run membership, dimension enum, market-wide null symbol), freshness confidence cap, idempotent upsert via content_hash.
- Hermes ingest route: token auth (403 unset / 401 wrong), push happy-path, `extra="forbid"` rejection.
- Read surface: view-model shape, dimension filter, empty-run behavior.
- Guards: ROB-287 no-internal-LLM import guard still passes; no broker mutation reachable.

## Decomposition / follow-ups (the broader program)

This slice is **Slice 1 of a program**:
1. **Market dimension report** (this spec).
2. **Final synthesis** — rebuild the composer (Hermes) consuming dimension reports → buy/sell + new candidates.
3. **News dimension** — schedule research_reports ingestion (ROB-140/207) → news analyst report.
4. **Fundamentals dimension** — new collector from KIS/Yahoo fundamentals + earnings → fundamentals analyst report.
5. **Sentiment dimension** — net-new.
6. **Crypto Market evidence** — once ROB-282 (24/7 crypto screener refresh) ships.

## Assumptions to verify during implementation

- Hermes context export (`hermes_context.py`) and the symbol-reports ingest (`retired-investment-hermes-http.py`, ROB-301) shapes can be mirrored for dimension reports without disrupting existing surfaces.
- `invest_screener_snapshots` (KR/US) has enough populated rows in the target environment to compute non-trivial breadth/movers.
- `investment_stage_runs` is the right FK parent (a dimension report belongs to a run, like symbol intermediate reports).
