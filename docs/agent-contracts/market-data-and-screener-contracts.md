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
- KR 1분봉 이벤트 수집기 — Toss → research.kr_candles_1m_toss (#1086)
- KR foreign net-flow ranking unit + provisional source (#1029)
- KRX 애프터마켓 세션×거래소 적격 (#925)
- crypto-universe-v1 — 상폐 포함 PIT 유니버스·일봉 재구축 (ROB-1432)
- 실험 레지스트리·manifest·heartbeat (ROB-1429)
- KR 상폐·종료결과 복구 + 인과 일봉 export (ROB-1430)
- US 상장 PIT 유니버스 — 상폐 포함 (ROB-1431)
- PIT 데이터 플레인 — universe·일봉·기업행위 일별 파티션 (ROB-1443)
- KR 부스팅 베이스라인·동결 대조군 (ROB-1441)

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
`buy_candidate_fanout_registration` records to `review.screener_pick_log`.
Fail-open. Prices are exact decimal text, never
float. Additive migration only — operator runs `alembic upgrade head`.
No scheduler. Not a policy/weight input.

Since task #884 (`collection_version="funnel-a1"`) the log is a full A-record:
one row per (call, source, symbol) for every source top-10 row that entered
the bounded slice — admitted or not — carrying `family`, `rank` (ordinal in
the source emission), `admission` ∈ `admitted | not_admitted |
dropped_preselection`, `admission_reason`, `selection_seq`, `source_status`,
`data_asof`, `fetched_at`, `raw_row`, `gate_features` (conditional funnel
features for revalidated candidates), and `call_context` (policy stamp,
selection method, per-source call statuses). Pre-884 rows keep NULL
`collection_version`; they are never backfilled. Full-analysis admission is
per-source-family round-robin (declared family order, one new symbol per
family per round, empty families yield) so a dominant family cannot take the
whole 10-slot pool.

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
`rsi_14`, `rsi_source`, `bars_used`, `supports`, `resistances`,
`decision_history`, `earnings`,
ROB-1048 freshness envelope 및 `halt_suspect`다. 뉴스/profile/consensus/
recommendation/holdings와 provider 기반 earnings는 quick에서 실행하지 않는다.
`quick=False` 는 기존 `analysis_screening._analyze_stock_impl`
→ `analysis_analyze.analyze_stock_impl` 호출과 full output을 유지한다.

ROB-1382 — RSI-14 단일 원천·단일 산식: 모든 MCP 표면의 RSI-14는
`market_data_indicators._calculate_rsi_series`(Wilder ewm `alpha=1/14`,
`min_periods=14`, `adjust=False`) 하나만 사용한다. quick는 `daily_candles`
최신 250봉(`QUICK_CANDLE_COUNT`) 위에서 계산하고 `rsi_source="daily_candles_db"`,
`bars_used=<사용 봉 수>`를 담는다. `get_ohlcv(period="day", include_indicators=True)`는
같은 `daily_candles` 창(`fetch_recent_batch`, 같은 market/partition/count)을 읽어
행 `date`로 매핑하며 canonical 행이 없는 날(형성 중 봉)은 `null`을 보고한다.
canonical을 읽지 못하면 provider 프레임 위에서 같은 Wilder 산식으로 계산하고
`rsi_source`는 provider 이름(`yahoo`/`kis`/`toss`/`upbit`)을, `bars_used`는 그
프레임 행 수를 담는다. `end_date` 지정 또는 `day` 외 period는 canonical을 건드리지
않는다. zero-loss 구간(avg_loss=0)은 quick과 마찬가지로 `null`이다.

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


### KRX 애프터마켓 세션×거래소 적격 (#925)

KRX 애프터마켓(2026-09-14 개장, 16:00-20:00 KST, 약 2,000 종목, ETF/ETN 등은 향후 도입)
이후 야간 적격은 `nxt_tradable` 단일 기준이 아니라 **세션×거래소**다. 근거:
hk:doc `strategy-lab/2026-09-29/krx-aftermarket-vs-nxt`(8157, 더구루 2026-09-27 기사 +
375500 실측).

- **적격 소스**: `krx_after_market_eligibility` — 운영자가 KRX 공시 목록을
  `scripts/import_krx_after_market_eligibility.py`(dry-run 기본, `--source` 인용 필수,
  전체 스냅샷 교체)로 적재. 유일한 writer 는
  `kr_symbol_universe_service.replace_krx_after_market_list`. 스케줄러 없음.
  런북: `docs/runbooks/krx-after-market-eligibility.md`
- **판정**: `app/services/krx_after_market.KrxAfterTradability` — 목록 등재 AND
  KOSPI/KOSDAQ AND `security_type == "STOCK"` AND `krx_trading_suspended is False` AND
  목록 as-of 7일 이내일 때만 true. 🔴 ETF/ETN·분류 미상·목록 없음/stale·조회 실패는 전부
  **false**(null 아님, true 기본값 금지). "주식이면 가능" 같은 규칙으로 목록을 대신하지 말 것
- **`get_quote`**: KR 응답에 `krx_after_tradable` + `_source/_list_source/_asof/_stale/_reason`.
  조회 실패는 시세를 깨지 않고 false + `lookup_failed`
- **승인 창** (`approval_window._resolve_kr_session`): 08:00-08:50 은 NXT 전용
  (`nxt_tradable` 만). 애프터는 NXT 증명 종목이면 기존 통합 창 그대로, 아니면 KRX 목록 증명
  종목에 한해 `krx_after_window`(Toss 통합 afterMarket ∩ 16:00-20:00)만 허용,
  `allowed_sessions=("regular","krx_after")`. NXT 미상 + KRX 증명은 KRX 창 안에서만 허용하고
  그 밖(15:30-16:00, 프리)은 기존대로 `CALENDAR_UNKNOWN`
- **만료** (`live_order_expiry`): `nxt_tradable=False` 를 넘긴 호출자만 `krx_after` 세션
  (20:00, `krx_after_close_20_00`)과 정규장 15:30 사멸(`krx_regular_close_15_30`, #876 과
  정합 — 야간은 새 주문)을 받는다. 미지정(None)은 ROB-671 동작 불변
- **Toss 주문 도구 preflight** (#969, `nxt_preflight.evaluate_nxt_preflight` +
  `nxt_preflight_krx_after.evaluate_nxt_preflight_with_krx_after`): 승인 창과 **같은 규칙**.
  `nxt_after` 세션의 비-NXT 종목은 `kr_krx_after_session_for`(위 창) 안이고 승인 창의
  `resolve_krx_after_capability` 가 허용할 때만 통과(`reason=krx_after_tradable`,
  `session=krx_after`). 🔴 목록 없음/조회 실패/유니버스 부재는 `krx_after_capability_unknown`,
  7일 초과는 `krx_after_capability_stale` 로 **차단**(fail-closed) — `not_nxt_eligible` 와
  구분. 목록은 읽혔으나 부적격이면 기존 NXT 사유 유지 + `krx_after_detail`. 08:00-08:50·
  15:30-16:00 과 NXT 종목 판정은 불변(NXT 종목은 KRX 조회 자체를 하지 않음). Toss
  preview/place/modify 와 `suggest_order_account` 는 같은 함수를 호출한다. 모드 의미 불변:
  `required` 만 place/modify 를 막고 `warn`/`optional` 은 로그 후 진행, preview 는 표시만,
  `off` 는 생략
- **미변경**: 운영 프롬프트 문언은 이 범위 밖

### KR foreign net-flow ranking unit + provisional source (#1029)

`get_top_stocks(market="kr", ranking_type=foreign_net_buy|foreign_net_sell|foreigners)`
는 KIS `FHPTJ04400000` (`/uapi/domestic-stock/v1/quotations/foreign-institution-total`,
국내기관_외국인 매매종목가집계) 를 읽는다. KIS 스펙은 `frgn_ntby_tr_pbmn ~ etc_corp_ntby_tr_pbmn`
을 **단위 백만원, 수량*현재가** 로 문서화한다.

- 🔴 `foreign_net_amount` 는 **KRW** 다. 백만원→KRW 변환(x1,000,000)은
  `analysis_screening._map_kr_foreign_row` 에서 `foreigners_liquidity.kis_million_krw_to_krw`
  로 **정확히 한 번**만 한다. 비유한값(NaN/Inf)·파싱 불가는 None 이고 유동성 필터에서 제외된다.
- 유동성 하한 `FOREIGNERS_MIN_NET_AMOUNT_KRW`(기본 1억 KRW = 100 백만원)는 변환된 KRW 값에 적용된다.
  #1029 이전에는 백만원 원값을 KRW 로 읽어 모든 행이 1억 미만으로 탈락하고 `status=degraded` 를 반환했다.
- 🔴 시가총액 하한 `FOREIGNERS_MIN_MARKET_CAP_KRW`(기본 300억 KRW, #1105)는 **정규화된
  `market_valuation_snapshots` 의 `naver_finance` 값(원화 KRW)에만** 적용된다. 행의
  `market_cap`(KIS 페이로드·`invest_kr_fundamentals_snapshots`/TradingView 백필·
  shares×price)은 단위가 정규화되지 않아 적격 증거로 쓰지 않는다. 정규화 커버리지가 없는
  심볼은 `market_cap_unknown` 으로 **fail-closed 제외**되고 다른 소스로 대체 판정하지 않는다.
  행별 제외 사유(`net_amount_missing`/`net_amount_below_floor`/`market_cap_below_floor`/
  `market_cap_unknown`)는 `liquidity_filter.excluded_rows`/`excluded_reasons` 에 싣고
  `degraded_reason` 에 사유별 건수를 표기한다. `include_illiquid=true` 는 전체 필터를 우회한다.
- 🔴 이 소스는 **가집계(잠정)** 다: 증권사 직원 입력 누계, 외국인 입력 시각 약 09:30/11:20/13:20/14:30 KST(±10분).
  확정값으로 대체되지 않으며 ~14:30 입력이 이 소스의 최종 상태다. 그래서 KR 외국인 랭킹 응답(행 반환·
  유동성 degraded·호출자 품질 하한 degraded·장외 fake-0 억제 전부)은 `source_state="provisional"`,
  `source_state_reason="kis_foreign_institution_total_provisional_tally"`, `source_state_note`,
  `foreign_net_amount_unit="KRW"` 를 싣는다. 확정 일별 수급은 장 마감 후 확정 투자자 수급
  시리즈(`investor_flow_snapshots` 18:10 KST job, `get_intraday_investor_flow` confirmed 블록)에서 읽는다.
- 순위 정렬은 `FID_DIV_CLS_CODE="0"`(수량정렬)이다 — 금액 기준 순위(Toss 등)와 순서가 다를 수 있다.

### KR 1분봉 이벤트 수집기 — Toss → research.kr_candles_1m_toss (#1086)

#1054 대상 유형 DART 공시(공급계약 의무·자율, 자사주 직접취득·신탁체결, 잠정실적,
유상증자 — r3 `cohort_of` 이식)가 들어오면 해당 종목의 D0(공시일 이후 첫 KRX 세션)·D+1
1분봉을 Toss `GET /api/v1/candles`(1m, `MARKET_DATA_CHART`)로 받아 저장한다.

- **클라이언트**: `TossReadClient.minute_candles`/`collect_minute_candles` (strict DTO
  `parse_minute_candle_page`; 기존 loose `candles` 는 불변). 커서는 spec 상 `before`/`nextBefore`
  뿐(after 없음), non-advancing·충돌 중복 = `TossResponseContractError`, 페이지 상한 =
  `TossPaginationCapExceeded`
- **수집기/트리거**: `app/services/research_candles/{toss_minute_collector,dart_minute_trigger}.py`
- **CLI**: `scripts/backfill_kr_candles_1m_toss.py` (dry-run 기본, `--commit` 시 INSERT 권한 사전확인)
- **런북**: `docs/runbooks/kr-candles-1m-toss-collector.md`

🔴 **대상 테이블은 `research.kr_candles_1m_toss`** — Toss 는 KRX+NXT 합산 상품이라
venue 키를 가진 `research.kr_candles_1m` 에 쓰지 않는다. KIS 폴백 없음(`source='TOSS'` CHECK),
Toss 무응답은 gap 기록(JSONL, 침묵 skip 금지).
🔴 **`time_utc` = Toss 봉 종료시각(raw)**. 봉은 `[timestamp-1분, timestamp)`. 시작시각 라벨을
쓰는 연구(#1054 N4 `bar_start >= rcept_dt + 6분`)는 반드시
`time_utc - interval '1 minute' AS bar_start` 로 읽는다. `session_segment` 는 Phase-2 와 같은
KST 시각 라벨(venue 아님). 가격은 `adjusted=false`. 쓰기는 `ON CONFLICT DO NOTHING`(기존 행 우선,
불일치는 `existing_conflict` 로 보고만).
🔴 **스케줄 등록 없음.** 배선(hk 1084 Q-122=A)은 머지 후 desk 가 등록한다(런북 §5).

### crypto-universe-v1 — 상폐 포함 PIT 유니버스·일봉 재구축 (ROB-1432)

survivor-only 였던 crypto-corpus-v1(상폐 비중 0%)을 대체하는 연구 전용 패키지
`research/crypto_universe/`. 공개 무인증 데이터만 쓰며 `app/`·DB·브로커·자격증명·
스케줄러 import 가 없다(AST 테스트). `public_http.check_url` 이 허용 목록
(data.binance.vision spot 아카이브·S3 목록, data-api.binance.vision exchangeInfo,
api.upbit.com market/all·candles/days) 밖의 URL 을 소켓 전에 거부한다.

- **CLI(수동)**: `uv run python -m research.crypto_universe.cli snapshot|build|pit --root DIR`
- **소스**: Binance 일봉 = 월별 1d zip(공개 `.CHECKSUM` SHA-256 검증, 2025+ µs 타임스탬프 정규화).
  Upbit 은 공개 아카이브가 없고 상폐 마켓에 404 를 주므로 상폐 쌍은 probe 가 캔들을
  돌려줄 때만 확보(그 경우 delisted 로 적재) — probe 결과(상태·본문 해시)를 coverage 에 기록한다.
  exchangeInfo 스냅샷 날짜가 `coverage_end` 이후가 아니면 build 를 거부한다(신규 상장을
  상폐로 오분류하는 것 방지).
- 🔴 **PIT 규칙**: live 쌍은 첫 거래일부터, 그 외는 listing episode(7일 초과 무거래 공백에서
  분할 — 재상장·심볼 재사용 구분) 안에서만 포함. `coverage_end` 이후 질의는
  `OutsideCoverageError` 로 거부(fail-closed). 상태는 exchangeInfo 스냅샷으로만 정한다
  (`TRADING`=live, `BREAK`/부재=delisted, 그 외=`not_trading_other`).
- 🔴 **스냅샷**: `snapshots/<UTC 날짜>/<source>.json.gz` 는 임시 파일 fsync 후 `os.link` 로
  게시(부분 파일·덮어쓰기 불가)하고 읽기 전용이다. `manifest.jsonl` 은 append-only, 실행은 `flock`
  으로 직렬화. 같은 UTC 날짜 재실행은 네트워크 0·쓰기 0, 파일만 있고 manifest 줄이 없으면 검증을
  통과한 디스크 바이트로 줄을 보수한다(이전 날짜 포함). 모든 소스를 먼저 받아 검증한 뒤 게시하며 UTC 자정을
  넘긴 실행은 아무것도 쓰지 않는다. build 는 staging 후 표식 기반 교체(충돌 시 다음 build 가 복구). 스케줄러 등록 금지.
- **산출물**: 원자료(캔들·스냅샷)는 git 밖 artifact root, 커밋되는 것은
  `research/crypto_universe/data/`(PIT 표·coverage·manifest)뿐. 캔들은 crypto-corpus-v1 holdout
  경계(2025-01-01)로 exploration/holdout 분리.
- **테스트**: `tests/research/crypto_universe/`. 런북 대신 `research/crypto_universe/README.md`.

### 실험 레지스트리·manifest·heartbeat (ROB-1429)

전략 실험(백테스트·부스팅 베이스라인·TFT 도전자·forward shadow)은 테스트 데이터를 읽기 전에
`research/experiments/registry.json` 에 등록한다. 형식·규칙 정본: `research/experiments/README.md`,
분할·누수 규칙: `docs/research/experiment-split-leakage-rules.md`.

- **manifest** `experiment-manifest/v1`(`research/experiments/manifests/*.json`): hashes·features(`available_at`)·
  labels(세션 지평)·calendar·splits(공통 날짜 walk-forward, purge ≥ 최장 지평, gap ≥ 5 세션, 테스트는 봉인
  기간 또는 prospective seal)·candidate_policies·costs_fills·tuning_budget·seeds·metrics·stopping_rules·
  cumulative_k. 정체성 = canonical JSON sha256. 바뀐 manifest 는 새 version 이고 옛 hash 는
  `previous_manifest_sha256` 로 남긴다.
- 🔴 **test-set look 마다 k 증가**: 테스트를 열기 전에 `test_looks` 에 추가(리포트 `report_sha256` 고정)하고 같은 커밋에서 `k = len(test_looks)`.
  `k` 감소·이전 look 수정·가설 삭제 금지(CI 가 base 브랜치 registry 와 대조). 리포트는
  `Experiment-Manifest-Sha256:` 줄로 등록된 hash 를 인용해야 하며, 미등록 hash·미계수 look 은 CI 실패
  (`scripts/ci/experiment_registry_lint.py`, `lint` 잡 + `tests/ci/test_experiment_registry_lint.py`).
- **heartbeat**: `uv run python -m research.experiments.heartbeat --path ID=PATH` — 읽기 전용(파일만, DB·네트워크·
  app import 0). epoch 별 마지막 advance 와 그 뒤 마감된 세션 수, 3 세션 초과 idle 이면 RED(종료 코드 1).
  ROB-1391(원장 `events.jsonl`)·H5(weekly-score snapshot `nav_samples[].observed_at`) 등록. 비공개 경로는
  레포에 넣지 않는다. 🔴 스케줄러·알림 연결 없음(운영자 결정).
- 등록(2026-11-01, k=0, draft): `ROB-1411`, `H-entry-price`, `H-admit`. 정리 대상 목록:
  `docs/research/at-retirement-inventory-2026-11-01.md`(권고만, hk/Linear 상태 변경 0).

### KR 상폐·종료결과 복구 + 인과 일봉 export (ROB-1430)

`research/kr_delist_repair/` — kr-corpus-v1 main scope(2015–2024) 심볼 중 오늘의 KIS 마스터
(`kr_symbol_universe` 동기화 원천)에 없는 심볼의 상폐일·사유·종료결과를 공개 KIND 상장폐지현황과
KRX `상장폐지` 공시 본문에서 **정확한 단축코드로만** 해석한다(회사명 매칭 금지). 연구 전용:
`app` import·DB·브로커·스케줄러 0, 봉인 holdout 읽기 금지(`HoldoutAccessError`).

- 🔴 **`kr_symbol_universe` 로 상폐 수명주기를 조인하지 말 것** — 동기화 시작 전 상폐 심볼은 행이
  아예 없어 inner join 이 생존편향을 되살린다. 수명주기는 export 경로의 `delist_resolution.v1.csv`(해시·행수는 `data/export_manifest.json`)를 심볼 키로 쓴다
  (`data/join_audit.md`).
- 🔴 corpus 안에서 membership 이 끝난 심볼은 상폐일 = 마지막 membership 다음 세션이어야 하며,
  어긋나면 고치지 않고 unresolved 로 남긴다.
- 🔴 **available_at**: 봉 = 세션 16:00 KST, 종료 이벤트 = 공시 접수시각(본문 사용 시) 또는 상폐일
  00:00 KST. `CausalTable.lookup`/`causal_join` 은 이른 사용 시 `AvailabilityViolation` 으로 실패한다.
- 🔴 종료 행은 심볼당 마지막 membership 세션 1행뿐, forward-fill 금지, 상폐일 이후 행 0
  (`validate_panel` 이 `ExportConflict`). 미해결 종료는 `terminal_outcome=unresolved` 로 표시, 삭제 금지.
- `payout_semantics` 는 라벨링 힌트이며 지급액을 주장하지 않는다.
- 산출 export(parquet)는 레포 밖 아티팩트 경로에 두고 해시는 `data/export_manifest.json`.
- **런북**: `research/kr_delist_repair/README.md`. 스케줄 없음(수동 `collect`/`build`).

### US 상장 PIT 유니버스 — 상폐 포함 (ROB-1431)

`research/us_corpus`(SURVIVORSHIP_BIASED=TRUE)의 생존 편향을 메우기 위한 연구용 US
상장 이력. NASDAQ Trader 심볼 디렉터리(`nasdaqlisted.txt`/`otherlisted.txt`)의
Internet Archive 캡처(2008→) + 자체 일별 스냅샷으로 상장 구간 표
(`symbol, listed_from, listed_to, status, status_source, delisted_by, listed_after, …`)를 만든다.

- **패키지**: `research/us_universe/` (stdlib, `app/` 중 `app.core.symbol` 만 import — 테스트가 강제)
- **명령(수동, 스케줄러 없음)**: `python -m research.us_universe.snapshot --root …`(일 1회 멱등,
  append-only), `python -m research.us_universe.rebuild --root … --out … [--offline]`
- **산출물**: 레포의 `research/us_universe/data_manifests/` 는 빌드 기록만 둔다
  (manifest.json·inputs.jsonl·coverage.md·build_info.json). PIT CSV 는 git 에 넣지 않고
  (`.gitignore`) 데스크톱 호스트 `~/work/herdr-artifacts/us-universe-v1/pit-v1/` 에 둔다.
  manifest 가 CSV 의 sha256·행수·생성 명령을 고정하며, `--offline` 재빌드는 build_info 를
  제외하고 바이트 동일하다
- 🔴 **날짜는 증거 구간이다**: 상장은 `(listed_after, listed_from]`, 상폐는
  `(listed_to, delisted_by]` 안 어딘가. `universe_as_of(t)` 기본 규칙은
  `listed_from <= t < delisted_by` — 처음 관측 전은 제외(룩어헤드 금지), 마지막 관측 뒤
  부재 관측 전까지는 포함(생존 편향 금지). 이 규칙을 `t <= listed_to` 로 좁히지 마라
  (상폐 직전 종목만 빠지는 생존 편향).
- 🔴 **불완전 입력은 상폐 증거가 아니다**: 헤더·`File Creation Time` 푸터·1,000행 미만·
  Test Issue 가 Y/N 이 아닌 행·같은 심볼의 상충 행이 있는 파일은 관측으로 쓰지 않는다. 이미
  캐시/스냅샷에 있는 파일이면 manifest 의 거부 입력으로 기록하고, 새로 받은 Wayback 본문이면
  캐시에 쓰지 않고 fetch gap(stderr)으로 보고해 다음 실행에서 재시도한다(manifest 는 캐시의
  순수 함수라 실행마다 달라지는 fetch 결과를 담지 않는다). 한 시리즈 최신 관측이 다른
  시리즈보다 7일 넘게 뒤처지면 rebuild 가 거부한다. 이 가드를 완화하지 마라.
- 브로커 API·자격증명·DB·스케줄러 없음. Alpaca paper inactive 자산 목록은 자격증명이 필요하고
  날짜가 없어 미사용(런북 = `research/us_universe/README.md`).

### PIT 데이터 플레인 — universe·일봉·기업행위 일별 파티션 (ROB-1443)

`research/data_plane/` — U7/U8(부스팅·TFT) 입력을 KR·US·crypto 별로 하루 한 번 쌓는 연구 전용
수집기. 계약 정본: `docs/research/data-plane-contract-v1.md`, 수집기 인벤토리·keep/fix/rebuild/retire
판정: `docs/research/data-plane-collector-inventory-2026-11-01.md`, 브로커 소스 인벤토리(ROB-1448):
`docs/research/data-plane-broker-sources-inventory-2026-11-01.md`. 공개 소스 읽기는
`public_http.check_url` 허용 목록 밖을 소켓 전에 거부하고, DB·스케줄러 import 가 없다
(`tests/research/data_plane/test_data_plane_guards.py`).

- **브로커 일봉 (ROB-1448)**: KR/US `bars_1d`(raw)·`bars_1d_adjusted`(KR/US 전용, 최근 N세션 조정 vintage)는
  `--bar-sources`(기본 `kis,toss,yahoo`) 순서로 커버리지 하한을 넘는 첫 소스가 파티션 전체를 서빙하고
  공개 Yahoo 가 폴백이다. manifest 가 `served_by`·`source_attempts`·`adjustment_basis` 를 기록하며 한 파티션에
  소스를 섞지 않는다. 🔴 앱 브로커 import 는 `broker_adapters.py` 한 곳, 함수 안 지연 import 뿐이고
  `KISClient`/`TossReadClient` 는 `ALLOWED_METHODS`(KIS 일봉 2종·Toss `candles`·close)만 답하는 읽기 전용 view 로만
  닿는다 — 주문·계좌·잔고 메서드 도달 불가(`test_data_plane_broker_guards.py`). 이 allowlist 를 넓히지 마라.
  🔴 연구 호출은 브로커별 키 한도의 고정 몫만 쓴다(`budget.py`, 기본 KIS 2/1s·Toss 1/1s, 25% 초과 거부,
  `<root>/.budget/` flock 으로 같은 root 의 수집기끼리 공유, 슬롯 1개 = HTTP 요청 1개) — 상한을 올리지 마라.
  연구 몫의 상한만 보장한다: 서빙 프로세스 limiter 는 KIS 20/1s 전부를 쓰므로 서빙 만부하 시 KIS 키 한도
  초과가 가능하며, 하드 보장은 서빙 limiter 를 몫만큼 낮추는 운영자 결정이 필요하다(이 PR 범위 밖). 앱 변경은 KIS 해외 일봉 `adjusted`(`MODP`)·국내 일봉 `max_pages` 선택 파라미터
  추가뿐(기본값·기존 동작 불변). `bars_1d_adjusted` 는 그 날에만 수집 가능(늦은 실행은 거부). Kiwoom(모의 전용·stage 2 별도 승인)·NHPLUG 는 소스로 쓰지 않는다.
- **명령(수동)**: `uv run python -m research.data_plane.cli collect --root DIR --market kr|us|crypto`
  (universe→bars_1d→bars_1d_adjusted(KR/US)→corporate_actions), `... health --root DIR --market M --dataset D [--ping-url-env NAME]`
- 🔴 **파티션 날짜 = 가용 날짜**: `date=P` 에는 `available_at` 이 시장 현지일 `P` 인 행만 들어간다.
  `P` 보다 늦은 `available_at`·이른 `available_at`·`observed_at < available_at`(형성 중 봉)·오프셋 없는
  시각·해시 불일치 행은 거부되고 아무것도 쓰지 않는다(exit 3). crypto 파티션 `P` 는 UTC `P−1` 일봉이다.
- 🔴 **write-once + append-only manifest**: 파티션은 `os.link` 로 한 번만 게시, 같은 날 재실행은 fetch 0·쓰기 0.
  커버리지가 `--min-coverage`(기본 0.95) 미만이면 그 날을 쓰지 않는다(빈 날로 남겨 health 가 잡게 함).
- 🔴 bars/actions 의 종목 집합은 `P` 이하 날짜의 최신 universe 파티션 2개에서만 고른다 — 더 늦은 universe 금지.
  universe·corporate_actions 는 그 날에만 관측 가능(과거 날짜 실행 거부). 기업행위는 first-observed
  (`available_at` = 처음 본 시각, `fact_hash` 로 중복 제거).
- **health**: 기한(`봉 가용 시각 + grace`)이 지난 세션 파티션이 빠지면 `gap`/`stale`, 해시 불일치 `corrupt`,
  커버리지 미달 `low_coverage` → exit 1. ping URL 은 출력하지 않는다.
- **Dagu 제안**: `deploy/dagu/data-plane/*.yaml` — 🔴 **등록 안 됨**(운영자 승인 대상). 새 명령만 참조.
- 기존 `*_candles_1d`·`*_symbol_universe`·뉴스 테이블은 서빙용으로 유지하되 덮어쓰기라 학습 입력으로 쓰지 않는다.

### KR 부스팅 베이스라인·동결 대조군 (ROB-1441)

`research/kr_boosting/` — U7-KR. U2 인과 데이터셋으로 `H-admit`·`H-entry-price` 의 LightGBM 베이스라인을
돌려 U5 계약으로 대조군(변경 없는 정책·모멘텀·평균회귀·현금)과 비교하고 게이트 판정을 낸다. 연구 전용:
`app` import·DB·브로커·스케줄러 0. 판정과 수치는 `research/experiments/reports/rob-1441-kr-boosting-baseline.md`.

- 🔴 **ML 의존성은 `pyproject.toml` 에 넣지 않는다**: `research/kr_boosting/requirements-ml.txt` 별도 환경. LightGBM 은 `train.py` 안에서만 지연 import 한다(테스트가 강제).
- 🔴 **2023-2024 는 sealed test 가 아니다**: 노출 감사(`exposure_audit.py`) 결과 입증 불가라 진단 전용이고, 게이트에 쓰지 않는다. 그 읽기는 k 로 계수했다. 시험은 prospective seal 이며 미개봉이다. `read_rows` 는 2023 이후 읽기를 기본 거부하고, `run.diagnostic()` 만 함수 안에서 동결 번들을 확인하고 claim 을 남긴 뒤 1회 읽는다(행이 아니라 경로를 받으므로 claim 전에 호출자가 봉인 행을 쥘 수 없다). claim 을 지우고 다시 읽으려면 look 을 먼저 계수한다. `validate` 는 claim 이나 diagnostic 결과가 있는 root 를 거부한다.
- 🔴 **`feature_join` 이 모델 피처의 유일한 입력 경로다**: `available_at` 이 결정 시각보다 늦은 행은 거부, 없는 행은 제외·계수. terminal 컬럼은 피처 입력이 아니다. 대조군 순위와 지정가는 세션 t 봉을 직접 읽어 이 가드를 타지 않는다(U2 봉은 세션 16:00 KST 가용, 결정은 t+1 08:30).
- 🔴 **게이트가 STOP 인 가설은 U8 TFT 를 돌리지 않는다.** 게이트 수치를 사후에 바꾸지 말고 바꿀 일이 있으면 새 manifest 버전과 새 시험을 쓴다. 2026-11-02 판정은 두 가설 모두 STOP 이다(k=1).
- 🔴 **모든 단계는 등록된 데이터셋만 받는다**: export 해시가 등록 manifest 의 `dataset_*_kr` 와 다르면 행을 읽기 전에 거부한다. `report` 는 저장 결과의 manifest·동결 번들·export 가 서로 다르면 거부한다.
- 분할은 purge + gap 을 경계마다 **더해서** 적용한다(H-admit 5+5, H-entry-price 25+25). 설정 선택은 Y-1 년으로만 한다.
- 🔴 **종료 지급액은 가정이다**: `delist_resolution.v1` 감사(ROB-1437)는 심볼·상폐일·사건 유형만 확인했고 지급액은 미검증이다. 베이스는 마지막 종가 청산이고, 리포트의 지급액 민감도 표(`payout_exposure.py`)가 그 가정에 걸린 금액을 보인다.
- U5 KR 어댑터는 D3 엔진을 감싸 자체 순위만 쓰고 거래량이 없어서, `simulator.py` 가 같은 계약 타입·체결 모델로 후보 목록과 `VolumeShare` 를 돌린다.
- **런북**: `research/kr_boosting/README.md`. 스케줄 없음(수동 `validate`/`diagnostic`/`report`).

## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
