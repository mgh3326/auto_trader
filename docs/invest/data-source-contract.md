# /invest 데이터 소스·리포트 API/MCP 계약 (ROB-340)

`/invest`의 데이터가 **어느 DB/read-model에서, 어떤 신뢰도로, 어떤 freshness 정책으로** 흐르는지 고정하는 계약 문서다. 목적은 리포트 생성 시점에 Toss/Naver/browser를 매번 새로 긁는 구조를 끝내고, 평소 축적된 read-model을 보고 필요한 근거만 snapshot bundle로 freeze하는 것이다.

> **단일 소스**: 아래 matrix 표는 `app/services/invest_data_source_contract.py`에서 자동 렌더된다. 표는 손으로 고치지 말 것 — diff-test(`tests/test_invest_data_source_contract.py`)가 표와 registry의 불일치를 CI에서 차단한다. 이 문서의 난라티브(설명)는 수기로 유지한다.

## 핵심 모델

- **surface** = `/invest` 제품 페이지: `news` / `screener` / `stocks` / `my` / `reports`.
- **authority_tier** = 신뢰도: `primary`(권위) / `supplementary`(보강·교차검증) / `low_trust_attention`(리테일 관심 신호; Toss/Naver).
- **fetch_policy** = 생성 경로 기준 언제 가져오나: `pre_collected`(평소 축적, 경로에선 DB-only) / `report_time_on_demand`(리포트 시점 bounded fetch 후 freeze) / `never_request_path`(요청/리포트 경로에서 절대 fetch 금지) / `frozen_in_bundle`(이미 freeze된 근거 소비).
- **reusable DB/read-model** = 제품 페이지용 mutable/upserted latest 상태. **investment snapshot bundle** = immutable-ish 리포트 근거 freeze. 둘은 다르다 — `investment_snapshots`를 Naver/Toss 범용 캐시로 쓰지 않는다.

## 공식 리포트 생성 flow (이슈 질문 5)

"순차 활용"은 auto_trader가 MCP를 명령형으로 줄세워 호출하는 게 아니다. 결정적 prepare → Hermes pull/compose/push의 비대칭 흐름이다. auto_trader는 compose를 오케스트레이션하지 않는다(in-process LLM import guard 유지).

```
Phase 0  축적 (생성 경로 밖, 평소 pre-collect)
  news-ingestor → news_articles / *_related_symbols / *_analysis_results
  screener job  → invest_screener_snapshots / investor_flow_snapshots / invest_crypto_screener_snapshots
  KIS sync      → holdings / cash / pending orders (account truth)

Phase 1  prepare (auto_trader, 결정적, in-process LLM 없음)
  1. investment_report_prepare_bundle        (SnapshotBundleEnsureService)
  2. production_collector_registry 실행       (fetch_policy 적용 — 아래 표)
  3. bundle freeze                            (investment_snapshots + bundle_items)

Phase 2  4. investment_report_get_hermes_context   (frozen bundle을 pull 대상으로 노출)

Phase 3  [Hermes, out-of-process LLM] pull → compose

Phase 4  push (ingest)
  5. investment_stage_artifacts_ingest_from_hermes
  6. investment_report_create_from_hermes_composition   (final report + items)
```

- **공식 생성 MCP** (canonical): 위 4개 (`prepare_bundle` / `get_hermes_context` / `..._ingest_from_hermes` / `..._create_from_hermes_composition`).
- **보조 MCP** (`get_quote` / `get_orderbook` / `get_indicators` / `get_valuation` / `get_financials` / `screen_stocks` / `get_top_stocks` / `get_news` / `search_news` / `get_holdings` / `get_cash_balance` / `get_order_history` / …)는 **read-only 진단**이다. UI/diagnostics 용도이며, 그 직접 호출 결과는 리포트 근거가 아니다.

## 보조 MCP는 언제 collector로 흡수되나 (이슈 질문 6)

| tier | 생성 경로 |
|------|-----------|
| **canonical-collector** | `production_collector_registry`에 등록된 collector. bundle에 freeze. |
| **live-diagnostic** | 위 보조 MCP. UI/진단만. 리포트 근거 아님. |
| **absorb-target** | live-diagnostic 결과가 리포트 근거로 **반복적으로** 필요해지면, durable 테이블 + collector로 승격해야 흡수된다. |
| **never-in-report** | broker mutation. read-only 어댑터(`_UpbitOpenOrdersAdapter`, `_KISDomesticQuoteOrderbookAdapter`)가 물리적으로 차단 — 계약상 도달 불가. |

흡수 규칙: 보조 MCP 직접 호출 결과를 리포트가 근거로 쓰려는 순간이 흡수 시점이다. 흡수는 (1) durable read-model 테이블 + (2) `production_collector_registry`에 등록된 collector를 의미하며, 그래야 drift-guard·freshness·freeze 계약 안으로 들어온다. request-path scraping으로 우회하지 않는다.

## 데이터 소스 matrix

`collector` 열이 채워진 행은 런타임 collector에 연결된다(stub 포함). 양방향 drift-guard가 이 집합 == `production_collector_registry(...).list_kinds()`임을 보장한다. `collector`가 `—`인 행은 durable read-model/fill source로, 아직 전용 collector가 없다(follow-up).

<!-- BEGIN GENERATED: data-source-matrix (rendered from app/services/invest_data_source_contract.py; do not hand-edit) -->
| surface | source | authority | table | fetch_policy | freshness_ttl | may_affect_ranking | unavailable | collector |
|---|---|---|---|---|---|---|---|---|
| my | kis_live | primary | — | report_time_on_demand | — | no | 확인 불가 | pending_orders |
| my | kis_live | primary | — | report_time_on_demand | — | yes | 확인 불가 | portfolio |
| my | toss_screen | low_trust_attention | — | report_time_on_demand | — | no | 확인 불가 | toss_remote_debug |
| my | trade_journal_db | primary | trade_journal | pre_collected | — | no | unavailable | journal |
| my | watchlist_db | primary | watch_context | pre_collected | — | no | unavailable | watch_context |
| news | naver_finance | supplementary | news_articles | pre_collected | — | no | 확인 불가 | — |
| news | news_ingestor | primary | news_articles | pre_collected | — | yes | unavailable | news |
| reports | browser_probe | low_trust_attention | — | report_time_on_demand | — | no | 확인 불가 | browser_probe |
| reports | invest_page_db | supplementary | — | pre_collected | — | no | unavailable | invest_page |
| reports | investment_snapshots | primary | investment_snapshots | frozen_in_bundle | — | no | unavailable | — |
| reports | market_events_db | primary | market_events | pre_collected | — | no | unavailable | market |
| screener | invest_screener_snapshots | primary | invest_screener_snapshots | pre_collected | — | yes | stale | candidate_universe |
| screener | investor_flow_snapshots | supplementary | investor_flow_snapshots | pre_collected | — | yes | stale | — |
| screener | upbit_live | primary | invest_crypto_screener_snapshots | pre_collected | — | yes | stale | — |
| stocks | kis_live | primary | — | report_time_on_demand | — | yes | 확인 불가 | symbol |
| stocks | naver_finance | low_trust_attention | — | report_time_on_demand | — | no | 확인 불가 | naver_remote_debug |
| stocks | stock_info | primary | stock_info | pre_collected | — | no | unavailable | — |
<!-- END GENERATED: data-source-matrix -->

## 이슈 질문에 대한 답

1. **뉴스는 어떤 DB에서 보여주나?** `news_articles`(+ `*_related_symbols` / `*_analysis_results`). `news_ingestor`가 primary source.
2. **뉴스가 부족하면 어디서 채우나?** `naver_finance` 어댑터를 **ingestion source**로 추가해 `news_articles`에 insert/upsert한다(supplementary). request-path scraping이 아니라 축적 경로다. (Naver article body 저장 여부는 follow-up에서 확정 — PR1은 title/snippet/url metadata 가정.)
3. **국내주식 screener는 어떤 source/type?** durable: `invest_screener_snapshots`(primary), `investor_flow_snapshots`(supplementary). type: top_gainers/losers/volume/trade_value, foreign/institution net-buy, theme/sector leader, risk-excluded/watch-only. crypto는 `invest_crypto_screener_snapshots`(`upbit_live`).
4. **Toss screener/popular/watchlist를 보나?** `low_trust_attention` `retail_attention` 시드로만. **buy rationale 금지, primary market-data 권위 금지, account/order truth 금지.** `may_affect_ranking=False` 고정. stale screener는 "오늘 신규 후보"에서 제외하되 stale context로는 표시.
5. **공식 리포트 flow?** 위 "공식 리포트 생성 flow" 참조 (`prepare_bundle` → `get_hermes_context` → Hermes compose → `create_from_hermes_composition`).
6. **보조 MCP 직접 호출은 언제 흡수?** 위 "보조 MCP는 언제 collector로 흡수되나" 참조.

## Freshness 정책 (이슈 §4)

`fetch_policy` 열이 카테고리별 정책을 고정한다. 구체 TTL(초/분) 값은 PR1에서 **deferred** — `freshness_ttl=None`은 "정책 lock, 값 TBD"를 의미하며 비차단이다. 실제 stale/coverage 판정은 `app/services/action_report/common/{stale_gate,diagnostics,snapshot_bundle}.py`가 enforce하며(ROB-323 core-aware `bundle_status`), 이 계약은 정책 의도만 기술한다(재정의하지 않는다).

## Follow-up 구현 대상 (이 PR 밖)

- Naver ranking ingestion → `invest_market_rank_snapshots`(gainers/volume/trade_value/foreign·institution rank/theme leader).
- Naver news ingestion 어댑터(질문 2의 fill 경로 실 구현).
- Toss account-screen cross-check 저장 → `account_screen_snapshots` / `_holdings` / `_pending_orders`(short-TTL, user/account scope).
- per-symbol 메트릭/valuation/risk-flag durable 테이블(`symbol_metric_snapshots` / `symbol_valuation_snapshots` / `symbol_risk_flag_snapshots`).
- read-only 진단 엔드포인트 `/invest/api/report-source-contract`(이 계약을 JSON으로 노출).
- 카테고리별 `freshness_ttl` 구체 값 튜닝.
- absorb-target(`—` collector 행)의 전용 collector 승격: `investor_flow_snapshots`, `upbit_live` crypto, `stock_info` 메타/valuation.
