# ROB-318 — /invest/reports 보강: reference-site audit + 리포트 생성 버그 수정

- **Date**: 2026-05-26 (KST)
- **Branch**: `rob-318`
- **Author**: Claude Code (operator: 광현)
- **Scope of this doc**: Phase 1 browser reference audit (read-only), Phase 2 bug
  root-cause + fix, and the cut line for Phase 3/4 (enrichment schema + UI).

> **Safety**: every browser observation below is read-only. No order / watch /
> broker / order-intent mutation was performed. External sites (Naver / Toss /
> Upbit / TradingView) are **reference / benchmark only**, never promoted to
> source-of-truth. `kis_live` = broker_authority, `auto_trader_db` =
> product_authority. No cookies, tokens, session URLs, or the operator's
> private portfolio figures are recorded here.

---

## 0. How the audit was run

The operator pre-logged-in a Chrome remote-debug profile
(`~/.hermes/chrome-toss-debug`) on `127.0.0.1:9222`. Per explicit operator
choice for this task, I attached **read-only** to the already-open tabs over CDP
(`Runtime.evaluate` extracting visible `innerText` only — no navigation, no
clicks). CDP websocket attach succeeded without the `--remote-allow-origins`
403 because the client sends no `Origin` header.

Reader script (throwaway, not committed): `/tmp/rob318_cdp_read.py`.

Surfaces actually verified on 2026-05-26: `stock.naver.com/`,
`stock.naver.com/market/crypto`, `tossinvest.com/screener`,
`tossinvest.com/screener/18`, `upbit.com/exchange`, `upbit.com/trends`,
`kr.tradingview.com/`, `kr.tradingview.com/crypto-coins-screener/`,
`trader.robinco.dev/invest/reports`, `trader.robinco.dev/invest/screener`.

---

## 1. Reference-site audit matrix

Columns: provider / surface → useful decision data → source role → can affect
**new buy**? → can affect **held strategy**? → current auto_trader coverage →
gap → proposed fix (and where it lands).

| provider / surface | useful decision data (verified 2026-05-26) | source role | new buy? | held strat? | auto_trader coverage today | gap | proposed fix / owner |
|---|---|---|---|---|---|---|---|
| **Naver** `/` | KR/US indices (KOSPI +3.42%, KOSDAQ, DOW, NDQ, S&P), USD/KRW, gold, WTI −5.6%; 뉴스·리서치 + 토론 tabs | reference / source_candidate | indirect (regime) | yes (catalyst/regime) | market snapshot exists in bundle (`market` kind) | no per-symbol news/disclosure/research catalyst in report | `held_strategy_change_signals` evidence container (deterministic; news/disclosure flags w/ source+time+`확인 불가` when absent). **ROB-318 Phase 3** |
| **Naver** `/market/crypto` | crypto broad context for KR audience | crypto_market_reference | indirect | indirect | partial (crypto report mode) | not wired as reference card | crypto `external_reference_checks` card. **ROB-304** (crypto enrichment) |
| **Toss** `/screener` | 11 presets (연속 상승세, 쌍끌이 매수, 저평가 탈출…); **674 results** on 쌍끌이 매수; columns: 현재가/등락률/카테고리/시가총액/거래량/애널리스트 분석 | benchmark / reference candidate | **yes** (candidate universe) | partial | auto_trader mirrors preset *names* (연속 상승세, 쌍끌이 매수, etc.) | auto_trader screener returned **0 results** (stale snapshot) vs Toss 674; missing columns (애널리스트 분석, 카테고리) | candidate `external_reference_checks` w/ `candidate_source=toss_reference`; column parity is screener-side. **ROB-318 Phase 3 (evidence tag only)** + **ROB-280/277 (screener parity)** |
| **Toss** `/screener/18` | 쌍끌이 매수 = 기관+외국인 동시 순매수; filters: 주가등락률 / 외국인 순매수 비교 / 기관 순매수 비교 | benchmark | yes | no | not exposed as condition provenance | report can't say "this candidate came from a 쌍끌이-style condition" | `screener_condition_provenance` field. **ROB-318 Phase 3** |
| **Upbit** `/trends` | 종합/알트/BTC/ETH group indices, USD/KRW, 시황 뉴스 + 휴장 캘린더 (메모리얼데이) | crypto_market_reference / exchange_benchmark | indirect | yes (TP/SL regime) | crypto report has Upbit live account path | no Upbit-style market-context card; holiday calendar not surfaced | crypto report `external_reference_checks` market-context card. **ROB-304** |
| **Upbit** `/exchange` | KRW market top volume / gainers / orderbook | crypto_market_reference | yes | yes | live account + Binance Demo scalping separate | KRW-market candidate context distinct from Binance Demo | keep Upbit (KRW benchmark) vs Binance Demo (execution) separated. **ROB-304 / ROB-307** |
| **TradingView** `/` | multi-asset watchlist: KOSPI/KOSDAQ/SPX/NDQ/NI225, BTCUSD/BTCKRW, USDKRW/EURUSD/JPY, 국내주식, 해외선물 (CL −5.85%) | screener_ux_benchmark / technical_reference | indirect | yes (regime gate) | `market` snapshot kind | no cross-asset regime gate panel | regime evidence in `report_quality_summary` (deterministic). **ROB-318 Phase 3 (light)** |
| **TradingView** `/crypto-coins-screener` | technical screener UX: market cap / volume / performance / technical rating columns | screener_ux_benchmark | yes | yes | auto_trader screener has fewer columns/filters | technical-screening evidence schema undefined | standardize technical evidence schema (trend/momentum/volume/volatility/RSI/MA/freshness). **ROB-280 (screener) + ROB-318 Phase 3 (report side)** |
| **auto_trader** `/invest/reports` | 20 recent reports, **all draft/advisory_only, 0 published**; top KR report 08:13 explicitly: "portfolio snapshot **unavailable** stale gate로 published 전환 보류"; 06:20 Hermes report: portfolio **partial** (KRW cash + buying-power from nested payload) | product_authority | n/a | n/a | reports render; provenance partial | generic "포지션 데이터 확인 불가"; no source-specific diagnostic; no real-vs-stale-gated no-action split | **Phase 2 bug fix (done)** + `report_quality_summary`/`why_no_action`/`data_sufficiency_by_source`. **ROB-318 Phase 2 + 3** |
| **auto_trader** `/invest/screener` | 연속 상승세 = **0 results**; "화면 갱신 **방금**" while "데이터 기준 **2026.05.22** 장마감"; "스크리너 스냅샷 업데이트가 필요해 …표시하지 못했습니다" | product_authority | yes (candidate universe) | no | screener live, snapshot stale | "방금 갱신" vs 4-day-old snapshot date is misleading; freshness source unclear | freshness semantics already owned by **ROB-277 / ROB-280 / ROB-281**; ROB-318 only consumes provenance |

### Headline findings

1. **Bug corroborated end-to-end.** `/invest/reports` 08:13 KR report is stuck in
   draft because `portfolio` = `unavailable`, while `/invest/screener`'s right
   panel renders the full KIS live portfolio (holdings + KRW/USD cash + NAV).
   The data IS retrievable through the canonical read path → confirms the gate
   is a wiring bug, not a KIS connectivity issue. (Root cause §2.)

2. **Two report paths diverge.** The Hermes path (`prepare_bundle`, user_id
   supplied per ROB-314) produced a 06:20 report with portfolio = `partial`
   (cash + buying-power present). The `generate_from_bundle` path (ROB-273) at
   08:13 produced `unavailable`. The difference is exactly the missing user_id
   on `generate_from_bundle`.

3. **Screener freshness confusion is real but already owned elsewhere.**
   "방금 갱신" vs "2026.05.22 장마감 기준" with 0 results is the ROB-277/280/281
   freshness-semantics problem. ROB-318 should not re-fix it; it should consume
   the provenance those issues expose.

4. **Toss/TradingView are strong candidate/column benchmarks**, not sources.
   Toss returned 674 쌍끌이-매수 candidates with analyst-rating/category/volume
   columns; auto_trader's mirrored preset returned 0 (stale). The *parity* is a
   screener concern; ROB-318 only needs the deterministic **evidence tag**
   (`candidate_source=toss_reference`, `screener_condition_provenance`).

---

## 2. Phase 2 — report generation bug: root cause + fix (DONE)

### Root cause (confirmed by code trace, not inference)

`investment_report_generate_from_bundle_impl` (ROB-273, MCP tool) **did not
expose a `user_id` parameter**, so it was never placed in the payload →
`ReportGenerationRequest.user_id` defaulted to `None`.

Everything downstream was already wired correctly (ROB-278 / ROB-314):

```
generate_from_bundle_impl (NO user_id)            ← the only gap
  → ReportGenerationRequest.user_id = None
    → SnapshotBackedReportGenerator.generate()    generator.py:188,202 forward user_id
      → EnsureBundleRequest(user_id=None)
        → CollectorRequest(user_id=None)            snapshot_bundle.py:297
          → PortfolioSnapshotCollector._collect_kis_live()  portfolio.py:243-279
            → user_id is None → fail-closed freshness_status="unavailable"
              → derive_generator_constraints()      generator_constraints.py:84-95
                → "포지션 데이터 확인 불가 — 매수/매도 권고 불가"  (advisory_only / draft)
```

The Hermes path (`prepare_bundle`) already threaded `user_id` (ROB-314), which
is why its bundle got portfolio = `partial` instead of `unavailable`.

### Fix

`app/mcp_server/tooling/investment_reports_handlers.py`:
- Added `user_id: int | None = None` to `investment_report_generate_from_bundle_impl`.
- Added `"user_id": user_id` to the request payload.
- Updated the MCP tool description so callers (Hermes/operator) know to pass it;
  omitting it keeps broker collectors fail-closed (ROB-278 invariant preserved).

Tests (`tests/test_investment_reports_mcp.py`):
- `test_generate_from_bundle_threads_user_id_to_request` — user_id reaches the
  request (was a TypeError before the fix → TDD red confirmed).
- `test_generate_from_bundle_user_id_defaults_to_none_fail_closed` — omitting it
  stays `None` (fail-closed default documented).

Verified: `163 passed` across `tests/test_investment_reports_mcp.py` +
`tests/services/action_report/snapshot_backed/`; ruff clean.

### Operator-gated remaining step (cannot be done from here)

Live publish still requires the deployment to (a) have
`SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true`, and (b) the caller
(Hermes/operator) to actually pass `user_id` to `generate_from_bundle`. The
code fix removes the wiring gap; the live round-trip is operator-gated.

---

## 3. Cut line — what ships in ROB-318 vs follow-up issues

### Ships in ROB-318

- **PR 1 (Slice A, ready): user_id bugfix.** §2 above. Surgical, standalone,
  unblocks published transition. High priority per operator.
- **PR 2 (Slice B, this doc): reference audit matrix.** §1. Docs-only.
- **PR 3 (Phase 3, scoped): deterministic report evidence containers** — see §4.
  Includes the source-specific diagnostics (folded in as `data_sufficiency_by_source`).

### Deferred / belongs to existing issues (cut)

- **/invest/screener freshness semantics** ("방금 갱신" vs snapshot date, 0-result
  empty state, market-aware refresh) → **ROB-277 / ROB-280 / ROB-281** already
  own this. ROB-318 only *consumes* the provenance they expose.
- **Crypto Upbit/Naver market-context cards** → **ROB-304** (browser-backed
  crypto screener enrichment).
- **Screener column parity with Toss/TradingView** (애널리스트 분석, 카테고리,
  technical rating columns) → **ROB-280** (screener), not the report.
- **Per-dimension analyst reports / Hermes-as-analyst direction** → the broader
  TradingAgents-style dimensions track; ROB-318 Phase 3 only lays the
  deterministic evidence-container groundwork it will consume.

---

## 4. Phase 3 design notes — deterministic evidence containers (no in-process LLM)

Per operator answers + ROB-287 invariant: these fields are **deterministic
evidence containers** populated by collectors/assembler from verifiable facts
(source, freshness, reason_code). The human-readable narrative is composed by
**Hermes** out-of-process. A fail-closed fallback string may be a deterministic
template.

Proposed payload additions (names from the issue; final shape during PR 3):

| field | populated by | content (deterministic) | narrative owner |
|---|---|---|---|
| `data_sufficiency_by_source` | bundle assembler | per source: `{status, reason_code, as_of, origin}` — folds in the source-specific diagnostic (e.g. `portfolio: {status:unavailable, reason_code:user_id_missing}`) | — (structured) |
| `why_no_action` | deterministic pipeline | `{kind: real_no_action \| stale_gated \| data_insufficient, blocking_sources:[...]}` | Hermes (prose); deterministic fallback template allowed |
| `report_quality_summary` | assembler | coverage %, fresh/stale/unavailable counts, regime flag | — |
| `new_candidate_reference_signals` | candidate collector | per candidate: `{source_role, condition_provenance, freshness, 확인 불가 flags}` | Hermes |
| `held_strategy_change_signals` | journal/watch/news collector | per holding: `{signal_type, source, as_of, evidence_ref}` (journal_expired, stop_loss_proximity, catalyst_changed…) | Hermes |
| `external_reference_checks` | reference collector (optional, fail-open) | `{provider, surface, observed_at, available: bool, summary}`; unavailable → `확인 불가`, never inferred | Hermes |
| `screener_condition_provenance` | screener handoff | `{condition_name, filters, snapshot_date, source}` from /invest/screener | — |

Key invariant to enforce in PR 3 (matches PR #898 import guard): the assembler
must not call any in-process LLM provider; all narrative text comes from the
Hermes compose contract or a deterministic template. The diagnostics improvement
(source-specific reason instead of generic "포지션 데이터 확인 불가") is delivered as
the `data_sufficiency_by_source` structured field, surfaced through the report
detail + `PublishBlockedByStaleGateError`.

---

## 5. Acceptance-criteria status

- [x] AC#1 — logged-in remote-debug session used; surfaces documented (§0, §1).
- [x] AC#2 — audit matrix with source roles + redacted evidence (§1).
- [x] AC#3 — KR/kis_live generic "포지션 데이터 확인 불가" root-caused + fixed (§2).
- [~] AC#4 — precise source-specific diagnostics: structured field designed (§4),
  implemented in PR 3.
- [~] AC#5 — real vs stale-gated no-action: `why_no_action` designed (§4), PR 3.
- [~] AC#6 — candidate / held-strategy provenance without source-of-truth
  promotion: designed (§4), PR 3.
- [ ] AC#7 — /invest/screener provenance handoff: consumes ROB-277/280/281 (cut, §3).
- [x] AC#8 (partial) — targeted tests for the bug fix (§2); Phase 3 tests in PR 3.
- [x] AC#9 — no broker/order/watch mutation introduced; import/side-effect guards green.
- [~] AC#10 — handoff (branch/PR/tests/screenshots/flags) compiled at PR time.
