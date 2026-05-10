# ROB-176 options/community value spike for stock detail

Run time: 2026-05-10 19:22 KST
Task: t_4971c299
Linear: ROB-176 child of ROB-173
Scope: research-only; no broker/order/watch mutations; no gated/community scraping.

## Recommendation summary

1. Options: DEFER for stock-detail MVP.
   - Do not include an options chain, options execution, or options strategy UI in ROB-174/ROB-175.
   - Keep the frozen MVP contract flag as `capabilities.options = { supported: false, reason: "out_of_mvp_scope" }`.
   - Revisit only after the stock-detail page has stable read-only equity/crypto data and after choosing a licensed US options data vendor.

2. Community: DROP public/community copy for MVP; INCLUDE an internal note/research strip.
   - Toss's `커뮤니티` tab should not be copied literally because auto_trader has no approved community ingestion/moderation pipeline, and the task explicitly disallows gated/community scraping.
   - Map the user value to private/internal artifacts already in the repo:
     - `latestAnalysis`: latest internal AI analysis summary from `StockAnalysisService.get_latest_analysis_by_symbol(...)`.
     - `researchReports`: broker research report citations from `ResearchReportsQueryService.find_relevant(symbol=...)`, citations only.
     - `tradeJournal`: user's own thesis/strategy/notes from `review.trade_journals`, read-only on stock detail.
   - UI label suggestion: `메모/리서치` or `투자 메모`, not `커뮤니티`.

3. Follow-up issue recommendation:
   - Create exactly one follow-up if product wants more than MVP: a read-only `메모/리서치` stock-detail panel that composes trade journal excerpts + research report citations + latest analysis.
   - Do not create an options follow-up now; there is not enough low-risk/high-value read-only options data in the current stack.

## Options decision

### Why defer

Options create a large surface area that is orthogonal to the stock-detail MVP:

- Market coverage mismatch: options are only relevant to US optionable equities/ETFs, not KR equities or crypto.
- Data complexity: useful options UX needs expirations, strikes, bid/ask, volume/open interest, greeks/implied volatility, delayed vs realtime provenance, and strong stale-data warnings.
- Safety complexity: the repo already has strict read-only MVP guardrails. Even read-only chains visually invite execution/strategy decisions unless clearly isolated from broker order flows.
- No current provider integration: repo search found no existing options chain service, schema, storage, or `/invest` endpoint. The only in-repo option reference in the likely path is a generic yfinance `Ticker.option_chain(date=None, tz=None)` capability after installing project deps locally, not a production contract.

### Possible read-only sources, if revisited later

| Source | Fit | Caveats | Recommendation |
|---|---|---|---|
| yfinance `Ticker.option_chain` | Fast prototype for US optionable symbols. Local installed `yfinance` 1.2.1 exposes `Ticker.options` and `Ticker.option_chain(date=None, tz=None)`. | Unofficial Yahoo surface, freshness/licensing ambiguity, likely unsuitable as core production UX. | Prototype only, not MVP. |
| Polygon Options API | Proper market-data product for US options. Public page advertises options market data coverage. | Paid/vendor setup; needs license review and API key; no current repo integration. | Best candidate if options become a real product. |
| Alpaca options | Existing user preference has Alpaca paper context, and Alpaca docs expose options trading capabilities. | Trading/broker surface, not just read-only; adding it now risks crossing the no-execution boundary. | Do not use for MVP stock-detail data. |
| Cboe public pages | Useful for broad market statistics/delayed quote browsing. | Not a clean app data contract for symbol option chains; scraping would be fragile and likely not worth it. | Do not build against for MVP. |
| ORATS / Nasdaq Data Link / Tradier / Tastytrade | Potential data vendors. | Contract, cost, licensing, and integration review required. | Vendor spike only after MVP. |

Minimum future acceptance criteria before any options panel:

- Read-only endpoint only; no order intent, paper order, live order, watch alert, or broker mutation imports.
- Explicit `market == "us"` and optionable-symbol gating.
- Strong `delayed`/`asOf`/`source` labels and missing-data fallback.
- Separate lazy endpoint; never above-the-fold MVP critical path.
- Safety tests extending `tests/test_invest_view_model_safety.py` and capability contract tests.

## Community vs internal memo/research mapping

### Recommendation

Do not clone Toss community behavior in MVP. Implement a private, read-only internal context panel instead:

- Header tab/copy: `메모/리서치` or `투자 메모`.
- First slice: show latest AI analysis summary already planned by ROB-173.
- Second slice: show broker research report citations if available.
- Third slice: show user's trade journal thesis/strategy/notes for the symbol if available.

### Repo evidence

- Latest AI analysis lookup exists at `app/services/stock_info_service.py:209-220` and is already included in the ROB-173 response contract.
- Research report citations are explicitly read-only and body-free: `docs/runbooks/research-reports-integration.md` says the response is `ResearchReportCitationListResponse`, never full body; `app/services/research_reports/query_service.py:54-101` supports `find_relevant(symbol=...)` and returns citation-shaped DTOs.
- Trade journal persistence already models user thesis/strategy/notes under `review.trade_journals`: `app/models/trade_journal.py:79-116` has `thesis`, `strategy`, `target_price`, `stop_loss`, `hold_until`, `notes`, and Paperclip linkage fields.
- Portfolio action DTOs already reason about `journal_status` (`present|missing|stale`) at `app/schemas/portfolio_actions.py:7-10,32-34`, so a stock-detail panel can reuse this concept rather than inventing social/community semantics.

### Suggested stock-detail contract shape for a follow-up

```jsonc
"notesAndResearch": {
  "supported": true,
  "latestAnalysis": { /* existing latestAnalysis or a reference to that block */ },
  "researchReports": {
    "count": 3,
    "citations": [
      {
        "source": "naver_research",
        "title": "...",
        "analyst": "...",
        "publishedAt": "...",
        "detailUrl": "...",
        "pdfUrl": "...",
        "excerpt": "...",
        "attributionPublisher": "...",
        "attributionCopyrightNotice": "..."
      }
    ]
  },
  "journal": {
    "status": "present",
    "latestId": 123,
    "thesisExcerpt": "...",
    "strategy": "...",
    "targetPrice": 82000,
    "stopLoss": 65000,
    "updatedAt": "..."
  }
}
```

Keep it read-only in stock detail. If editing journals is desired, route to the existing journal workflow rather than adding inline mutations to `/invest/stocks/:market/:symbol`.

## Follow-up Linear issue draft

Create only if the team wants ROB-176 to become implementation work:

Title: `ROB-176 follow-up: read-only stock detail 메모/리서치 panel`

Body:

- Parent: ROB-173. Evidence: `docs/superpowers/plans/2026-05-10-rob-176-options-community-spike.md`.
- Add a read-only `/invest` stock-detail panel that maps Toss `커뮤니티` value to internal/private `메모/리서치`.
- Reuse existing sources only: latest analysis, research report citations, and trade journal read model.
- No public/gated community scraping, no full report body/PDF ingestion, no broker/order/watch mutations, no inline journal writes.
- Acceptance:
  - stock-detail response or lazy endpoint returns citation-shaped research reports only;
  - journal content is excerpted and clearly private/internal;
  - empty states explain `분석/메모가 아직 없어요`;
  - safety tests prove no order/broker/watch imports from the view-model path;
  - `/invest` canonical route behavior remains unchanged.

## Final recommendation for ROB-176

- Mark options as `defer` for MVP and keep the capability flag unsupported.
- Translate Toss community into private `메모/리서치`, not public community.
- Highest-value read-only follow-up is a small notes/research panel; options should wait for a vendor/licensing decision.
