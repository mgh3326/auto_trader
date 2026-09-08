# 시장 데이터·스크리너 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- KR/US Category Normalization & Lazy Fill (ROB-512)
- Market Events Ingestion Foundation (ROB-128)
- Research Reports Integration (ROB-140)
- Invest Screener US Activation (ROB-204)
- Screener pick log (prospective bakeoff)
- screen_stocks_snapshot / screen_stocks_enrich MCP 도구 분리 (ROB-1309)
- Negative-class(기각 코호트) 기록 — decision_bucket (ROB-1283)
- 정지 종목 오염 차단 — halted_suspect (ROB-1236)
- analyze quick fast projection (ROB-1311)

## 기준 원문 계약

### KR/US Category Normalization & Lazy Fill (ROB-512)

KR Naver 업종과 US Yahoo Finance Industry/Sector를 `symbol_sectors` 테이블로 통합 관리합니다.

- **마스터 모델**: `app/models/symbol_sectors.SymbolSector` (`source_key`가 식별자)
- **Lazy Fill**: 스크리너 조회 시(enrichment) 섹터가 없는 종목은 실시간 fetch 후 DB에 저장합니다.
- **서비스**: `app/services/symbol_sectors_service.py` (쓰기 전용), `app/services/us_sector_korean_map.py` (US 한글 매핑)
- **적용 로더**: `investor_flow`, `consecutive_gainers`, `double_buy`, `fundamentals` 등 주요 스크리너 로더에 JOIN 배선 완료.
- **표시 규칙**: `SymbolSector.name_kr` ?? `SymbolSector.name_en` ?? "-" (US는 한글 매핑 우선).

### Market Events Ingestion Foundation (ROB-128)

시장 이벤트 (US earnings, KR DART 공시, 향후 crypto/economic) 수집·저장·조회 foundation.

- **모델**: `app/models/market_events.py` — `MarketEvent`, `MarketEventValue`, `MarketEventIngestionPartition`
- **서비스**: `app/services/market_events/` — `repository`, `ingestion`, `query_service`, `normalizers`, `taxonomy`
- **라우터**: `app/routers/market_events.py` — GET `/trading/api/market-events/today`, `/range` (read-only)
- **CLI**: `scripts/ingest_market_events.py` — `--source finnhub|dart --category earnings|disclosure --market us|kr --from-date --to-date [--dry-run]`
- **런북**: `docs/runbooks/market-events-ingestion.md`

**안전 경계**: read-mostly 마켓 데이터, 브로커/주문/감시 mutation 없음. `raw_payload_json` 은 저장 전 `_redact_sensitive_keys` 적용. 모든 DB 쓰기는 `MarketEventsRepository` 경유. Prefect 배포는 후속 작업.

### Research Reports Integration (ROB-140)

브로커 리서치 리포트 (Naver Research / KIS Research 등) `research-reports.v1` 페이로드의 thin ingest/read-layer 통합.

- **모델**: `app/models/research_reports.py` — `ResearchReport`, `ResearchReportIngestionRun`
- **스키마**: `app/schemas/research_reports.py` — `ResearchReportIngestionRequest`, `ResearchReportCitation`, copyright 가드
- **서비스**: `app/services/research_reports/` — `repository`, `ingestion`, `query_service`
- **라우터**: `app/routers/research_reports.py` — GET `/trading/api/research-reports/recent`
- **CLI**: `scripts/ingest_research_reports.py` — `--file path/to/payload.json [--dry-run]`
- **런북**: `docs/runbooks/research-reports-integration.md`

**안전 경계**: 풀 PDF 본문 / 전체 추출 텍스트는 스키마 단계에서 거부 (`full_text_exported`/`pdf_body_excluded=true` 페이로드는 reject). `summary_text` 1000자, `detail.excerpt` 500자로 트렁케이트. 모든 DB 쓰기는 `ResearchReportsRepository` 경유. 브로커/주문/감시 mutation 없음.

### Invest Screener US Activation (ROB-204)

US `consecutive_gainers` 스크리너는 `invest_screener_snapshots`를 통해 스냅샷 기반 결과를 제공합니다. 첫 번째 US 프로덕션 write는 다음을 요구합니다:

- **additive 컬럼**: `us_symbol_universe.is_common_stock` (nullable Boolean, alembic: `1a2b3c4d5e6f`)
- **분류 CLI**: `scripts/sync_us_common_stock_flags.py` — NASDAQ Trader 파일 기반, dry-run 기본값
- **bounded commit**: `scripts/build_invest_screener_snapshots.py --market us --all --common-stocks-only --commit` (dry-run 증거 + 리뷰어 승인 후에만)
- **user-facing warning**: `app/services/invest_view_model/screener_service.py` — `dataState ∈ {"missing", "stale"}`이면 `"미국 스크리너 데이터 준비중 — 일부 결과만 표시됩니다."` 경고 추가
- **프론트엔드 chip**: `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` — non-fresh `dataState`에 freshness chip 렌더링
- **Prefect flow**: `app/flows/invest_screener_snapshots_us_flow.py` — `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` 환경 변수로 게이트 (기본 `False` → dry-run). **배포 등록은 이 PR에 포함되지 않음.**

**운영 활성화 절차**: `docs/runbooks/invest-screener-snapshots.md` §7 (US 활성화) 및 §8 (Prefect 배포, 연기됨) 참고.

**안전 경계**: TaskIQ 반복 스케줄 없음. 브로커/주문/감시 mutation 없음. DB write는 `InvestScreenerSnapshotsRepository.upsert`만 허용.

### Screener pick log (prospective bakeoff)

`discover_buy_candidates_fanout` itself still performs no writes. When
`SCREENER_PICK_LOG_ENABLED=true` (default false), an outer observer in
`buy_candidate_fanout_registration` records returned per-source picks to
`review.screener_pick_log`. Fail-open. Prices are exact decimal text, never
float. Additive migration only — operator runs `alembic upgrade head`.
No scheduler. Not a policy/weight input.

### screen_stocks_snapshot / screen_stocks_enrich MCP 도구 분리 (ROB-1309)

Sentry 실측(p50 38.11s / p95 54.73s, 호출당 ~214 HTTP call, 120s 예산 내 타임아웃 8건)에서
`screen_stocks_snapshot`이 기본 경로에서 매 호출마다 섹터 lazy-fill + 애널리스트 컨센서스 +
실시간가 fetch를 무조건 수행하던 것이 원인으로 확인되어, 두 도구로 분리했습니다.

| | `screen_stocks_snapshot` (기존 이름, 계약 변경) | `screen_stocks_enrich` (신규) |
|---|---|---|
| 기본 동작 | **DB-only** — `invest_screener_snapshots`/`invest_crypto_screener_snapshots` 읽기 + 필터/페이지네이션만 | 동일 preset/filter/pagination 파이프라인 실행 후 라이브 enrichment |
| HTTP 호출 | **0회** (KR/US/crypto 전부) — 섹터 lazy-fill 없음, 애널리스트 컨센서스 fetch 없음, quoteSummary/timeseries/crumb 없음, 실시간가 fetch 없음 | 심볼당 섹터(KR Naver/US yfinance) + 애널리스트 컨센서스(KR/US 둘 다 Redis 캐시-어사이드, 아래 참고) + 실시간가 fetch |
| `min_analyst_count`/`min_analyst_buy_count` | **거부** — `{"error": ..., "redirectTool": "screen_stocks_enrich"}` fail-closed (무시하거나 네트워크 호출하지 않음) | 지원 — 페이지네이션 전에 컨센서스 COUNT를 해석해 필터링 |
| 응답의 `analysisContext`/`analystLabel` | 없음 | 있음 (`enrich_snapshot_page` 결과) |
| write 부작용 | 없음 — `screen_stocks_snapshot`은 KIS-live holdings lookup을 포함해 어떤 external HTTP call도 만들지 않는다(ROB-1309 회귀수정: `isHeld`는 항상 `false`, `exclude_held`는 `screen_stocks_enrich`로 fail-closed redirect) | 섹터 lazy-fill이 기존 `symbol_sectors_service`(ROB-512) 경로로 씀. KIS-live 보유종목 조회 1회(`isHeld`/`exclude_held` 표시용, ROB-1309 이전부터 존재하던 bounded call)도 이제 이 도구에서만 수행됨. `invest_screener_snapshots` 자체에 대한 쓰기는 없음(그 테이블의 유일한 writer는 여전히 `InvestScreenerSnapshotsRepository.upsert`이며, 오프라인 스냅샷 빌더/flow 전용 — 이 두 MCP 도구 어느 쪽도 호출하지 않음) |
| 음성 캐시 | 해당 없음 | 있음 — `app/services/invest_view_model/enrichment_negative_cache.py` (재시도 차단 TTL 30분 + 실패 이력/카운트 보존 TTL 24시간 분리, 에러 분류, 연속 실패 카운트). 실패/스킵은 `meta.enrichment_excluded`에 항상 명시적으로 보고(조용히 사라지지 않음). **연속 3회 이상 실패한 chronic 후보는 그 호출의 `results`에서는 제외되고 `meta.chronic_failure_candidates`에 별도로 non-silent 보고된다** — "절대 제거 안 됨"이 아니라 "제거는 항상 명시적으로 보고되고, 두 TTL 중 어느 쪽이든 경과하거나 성공 시 자동 회복되어 영구 은닉이 불가능"이 정확한 계약이다. **유니버스 mutation은 의도적으로 구현하지 않음** — 아래 "negative cache = universe cleanup 범위" 참고 |
| 공유 로직 | `app/mcp_server/tooling/screener_snapshot_tool.py::_build_snapshot_page` → `build_screener_results(..., snapshot_only=True)` (DB-only 빌드/필터/페이지네이션; 스냅샷이 없거나/stale이거나 스냅샷 분기 자체가 없는 preset/market 조합은 generic `ScreenerService.list_screening`으로 fall-through하지 않고 빈 결과 + 명시적 warning으로 fail-closed) | 동일 `_build_snapshot_page`를 `snapshot_only=False`(기본값)로 재사용 — snapshot이 없는 일부 preset(예: `consecutive_gainers`/`growth_expectation`(US)/`crypto_high_volume` 파티션 미적재)에 한해 live discovery로 fall-through하는 pre-existing 동작을 유지한다. live fallback은 오직 이 explicit 도구를 통해서만 노출된다 |

**구현**: `app/mcp_server/tooling/screener_snapshot_tool.py`(DB-only 도구 + 공유 빌더),
`app/mcp_server/tooling/screener_enrich_tool.py`(신규 enrichment 도구),
`app/services/invest_view_model/enrichment_negative_cache.py`(신규 음성 캐시).

**US 애널리스트 컨센서스 캐시(ROB-1309 완결)**: `analyst_consensus_cache.py`는 이제 KR(Naver, KST-date
bucket)뿐 아니라 US(yfinance, `analyze_cache.PROVIDER_YFINANCE` US/Eastern-date bucket)도 동일한
Redis cache-aside로 캐싱한다 — `_PROVIDER_BY_MARKET = {"kr": PROVIDER_NAVER, "us": PROVIDER_YFINANCE}`.
캐시 hit 시 US도 KR과 동일하게 전체 yfinance opinion fetch(`analyst_price_targets` +
`recommendations` + `upgrades_downgrades` + `info`)를 건너뛰고, upside 재계산용 실시간가만
`app.services.brokers.yahoo.client.fetch_fast_info`(가벼운 `fast_info` 단일 호출)로 갱신한다.

**negative cache = "universe cleanup" 범위 (의도적 설계 결정)**: 원 요구사항은 "실패/상장폐지 심볼에
대한 negative cache + universe cleanup"이었다. 이 레포는 `kr_symbol_universe`/`us_symbol_universe`
행을 실제로 mutate하는 "universe cleanup"을 구현하지 **않았다** — 대신 요구사항 자체가 명시한
제약("must not permanently hide valid symbols")과 이 레포의 기존 관례(ROB-1236 `halted_suspect`가
정지 의심 종목을 DB mutation 없이 **탐지+보고**만 하는 것과 동일 패턴)를 따라, "universe cleanup"을
**TTL 경과 시 자동 회복되는 활성-fetch 대상에서의 일시 제외**로 해석했다: `enrichment_negative_cache`는
재시도 차단 TTL(`NEGATIVE_CACHE_TTL_SECONDS`, 30분)과 실패 이력/카운트 보존 TTL
(`NEGATIVE_CACHE_HISTORY_TTL_SECONDS`, 24시간)을 분리해 둔다(bounded) — 실패를 분류하고
(error_class), `meta.enrichment_excluded`로 항상 보고하며(non-silent), 성공 시
`record_success`가 즉시 엔트리를 지워 자동 해제된다. **연속 3회 이상 실패(`_CHRONIC_FAILURE_THRESHOLD`)한
chronic 후보는 그 호출의 `screen_stocks_enrich` `results`에서는 제외되지만**(DB/영구 mutation은
아님 — 다음 호출에서 재시도 TTL이 지나 있거나 성공하면 즉시 복귀), **그 제외는 항상
`meta.chronic_failure_candidates`(및 `meta.enrichment_excluded`)로 non-silent 보고된다** —
"조용히 사라짐"이 아니라 "제거는 항상 보고되고, 두 TTL 중 어느 쪽이든 지나거나 성공하면 자동
회복되어 영구 은닉이 불가능"이 정확한 계약이다. `meta.chronic_failure_candidates`는 운영자가
직접 `kr_symbol_universe`/`us_symbol_universe`를 검토할 수 있는 advisory 신호이기도 하며, 이
신호 자체가 자동 삭제/비활성화를 트리거하지는 않는다. 실제 DB 테이블 mutation(예: soft-delete
플래그, 별도 정리 스크립트)이 필요하다고 판단되면 별도 Linear 이슈로 분리해야 한다 — 이 PR
범위에서는 스키마 변경/마이그레이션을 추가하지 않았다.

**주의**: `halted_suspect`(ROB-1236) 시맨틱은 이 분리와 무관합니다 — `screen_stocks_snapshot`/
`screen_stocks_enrich` 어느 쪽도 `halt_filter.py`/`analysis_analyze.py`/`buy_candidate_fanout.py`를
import하거나 건드리지 않습니다(halted_suspect 판정은 `screen_stocks`/`analyze_stock` 전용 경로).

**ROB-207 activation:** `POST /trading/api/research-reports/ingest/bulk` is the news-ingestor → auto_trader bridge (token-authed via `RESEARCH_REPORTS_INGEST_TOKEN`). `GET /trading/api/research-reports/freshness` returns the readiness signal. A TaskIQ task `research_reports.ingest_bulk_smoke` is registered but ships **scheduleless**; production recurrence lives in `robin-prefect-automations` and remains `paused=true` until the unpause checklist in `docs/runbooks/research-reports-integration.md` is satisfied. Production cutover (`paused=false`) is approval-gated.

### Negative-class(기각 코호트) 기록 — `decision_bucket` (ROB-1283)

"매수 후보가 정말 없었나"에 정본 데이터로 답하기 위한 기각 기록 경로. 🔴 **관측 전용**
(주문·승인 경로 영향 0).

- **어휘 정본**: `app/models/decision_vocabulary.DECISION_BUCKETS` — 리포트 아이템 CHECK ·
  Pydantic 스키마 · `review.trade_forecasts` CHECK 가 **같은 튜플**에서 생성됨(3층 드리프트 불가)
- **기록**: `forecast_save(decision_bucket="deferred_no_action", ...)` — 세션이 실제로 부르는
  표면. bucket + resolvable target + review_date + outcome/brier 가 한 행이라 리포트 아이템이
  없어도 **채점 가능**(고아 아님)
- **조회**: `get_forecasts(decision_bucket=...)`; `summary.by_decision_bucket` 의
  `unclassified` 는 **사각지대 크기**이지 0 이 아님
- **가드**: `get_operating_briefing` → `negative_class_recording` 섹션. route-stamp
  (`operator-compliance/v1`)가 브리핑 응답을 통째로 박제하므로 **운영자 레포 변경 0으로**
  세션 준수 스탬프에 도달
- **진단**: `scripts/diagnose_negative_class_recording.py` (SELECT only, exit 1 = stalled)
- **런북**: `docs/runbooks/negative-class-recording.md`

**주의**:
- 🔴 2026-06-15~ 결손 구간은 **메우지 않는다**. `gap.backfilled=false` 로 명시 보고 —
  가짜 연속성 금지. 이 구간을 가로지르는 코호트는 정확히 그만큼 불완전
- 🔴 원인은 핸들러 파손이 아니라 **호출 지점 부재**(라이브 프롬프트 5종이 `deferred_no_action`
  어휘만 쓰고 그걸 기록하는 도구 이름을 대지 않음). 기록 재개는 **운영자 레포 프롬프트 수정**이
  선행돼야 함 — 이 레포 변경만으로는 완결되지 않는다
- 🔴 bucket 없는 일반 forecast 는 카운트 안 됨. 실제 정지가 숨은 방식이 정확히 이것
  (`trade_forecasts` 는 66일 내내 바빴다)
- `decision_bucket=NULL` = "미분류"이지 "기각 아님"이 아니다. 오타는 fail-closed(거부)

### 정지 종목 오염 차단 — `halted_suspect` (ROB-1236)

일봉이 **3거래일 연속 죽어 있으면**(`volume=0` **또는** `high==low==close` 이면서
직전 close 와 동일) `data_state: "halted_suspect"`. 🔴 **`fresh` 금지 · 지표 null ·
스크리너/정책표에서 제외.** 실사례 = 000880 한화(8거래일 동결인데 `fresh` + RSI 35.40 로
매수 후보 rank 2 랭킹).

- **판정기**: `app/services/halt_detection.py` (순수·stdlib, DB/네트워크/시계 없음)
- **소비자 3곳**: `analyze_stock_impl::_apply_halt_suspect`(indicators·support_resistance
  = None, quote/top-level 양쪽 data_state 덮어씀) · `screening/halt_filter.py`
  (`screen_stocks_unified` 단일 깔때기) · `scripts/policy_table/adapters/{kr,us,crypto}.py`
- **런북**: `docs/runbooks/halted-suspect-data-state.md`

**주의**:
- 🔴 **`halted_suspect` 는 확정이 아니라 의심.** KRX 거래정지 마스터가 레포에 **없다**
  (`krx_halt_master: "unavailable"` 를 응답에 명시). 확정 정지로 단정 금지.
- 🔴 **N=3 은 양방향 오차를 감수한 값** — 1~2일 정지는 못 잡고(위음성), 3일 연속 무거래
  초저유동 종목은 잡힌다(위양성). 그래서 **제외는 항상 심볼·근거와 함께 보고**된다
  (`meta.halted_suspect_excluded` / `universe.halted_suspect`). 조용한 삭제 금지.
- **상한가/하한가 잠김**은 `open=high=low=close` 지만 직전 close 와 가격이 다르므로
  잡히지 않는다 — 0-변동 조건의 "직전 close 동일" 절을 제거하지 말 것.
- 봉 이력 조회 실패는 **fail-open**(행 유지 + warning). DB 장애는 정지의 증거가 아니다.
- 🔴 스크리너는 **최신봉 거래량 > 0 이면 이력 조회를 건너뛴다**(장중 일봉 캐시 우회 →
  100행 스크린이 KIS 라이브 100회를 때리는 것 방지). 감수하는 구멍 = 거래량 있는
  0-변동 구간. analyze/정책표는 이력을 무조건 읽으므로 그쪽에서 잡힌다.

### analyze quick fast projection (ROB-1311)

`analyze_stock_batch(quick=True)` 는 full analyzer의 formatter-only 변형이 아니다.
MCP registration → `analysis_tool_handlers.analyze_stock_batch_impl` →
`analysis_quick.load_quick_projection_batch` 로 분기해 `daily_candles` DB에서 모든
요청 심볼의 일봉 이력을 batch read하고, 가격/OHLCV/RSI/지지·저항을 로컬 계산한다.
기존 compact consumer가 요구하는 `decision_history`와 `earnings` 의미도 각각
set-based review/market-events read model로 batch read해 canonical 심볼에 붙인다.
KR/US는 시장별 window query 1회씩, crypto는 instrument identity 1회와 candle query
1회를 사용하며, history/earnings read model을 포함한 batch당 DB execution 상한은
12회로 고정한다. 외부 HTTP는 0회다.

Quick allowlist는 `symbol`, `market_type`, `source`, `current_price`, 최신 `ohlcv`,
`rsi_14`, `supports`, `resistances`, `decision_history`, `earnings`,
ROB-1048 freshness envelope 및 `halt_suspect`다. 뉴스/profile/consensus/
recommendation/holdings와 provider 기반 earnings는 quick에서 실행하지 않는다.
`quick=False` 는 기존 `analysis_screening._analyze_stock_impl`
→ `analysis_analyze.analyze_stock_impl` 호출과 full output을 유지한다.

🔴 **`current_price`는 라이브 시세가 아니다.** quick의 `current_price`는
`daily_candles`의 가장 최근 **마감된** 일봉 종가이며 `data_state="stale"`,
`data_state_reason="db_only_projection"`로 항상 스탬프된다. 라이브 가격·세션
상태(NXT 체결가능 등)가 필요하면 반드시 `get_quote`를 호출할 것 — quick만으로
판단하지 말 것.

**PR #1915 축소 필드 (quick 전용, `quick=False`는 불변)**: 구 quick 요약이
가지고 있던 `nxt_tradable`/`nxt_tradable_source`/`nxt_tradable_asof`/
`nxt_tradable_stale`(ROB-668) · `price_source`/`session`/`session_state`/
`krx_prev_close`/`change_pct`(ROB-725/ROB-888) · `venue`/`quote_asof`/`delayed`
(quote provenance) · `price_data_state`(ROB-1048) · `fresh_artifact_exists`
(ROB-648) · `consensus`/`recommendation`/`position`/뉴스/profile/provider
earnings는 전부 quick allowlist에서 제거됐다. 라이브 가격/세션 provenance가
필요하면 `get_quote`를 사용할 것 — quick은 그 값들을 더 이상 담지 않는다.

🔴 ROB-1236: quick도 모든 심볼의 일봉 이력을 반드시 읽고 `classify_ohlcv_frame`을
적용한다. 동결 3세션이면 `data_state=halted_suspect`, RSI와 supports/resistances는
null이며 `halt_suspect` 근거를 보존한다. DB 장애는 해당 행을 `missing`으로 남기며
외부 provider로 우회하지 않는다.


## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
