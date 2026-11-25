# 시장 데이터·스크리너 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- KR/US Category Normalization & Lazy Fill (ROB-512)
- Market Events Ingestion Foundation (ROB-128)
- Benzinga calendar ingestion (hk 6513)
- Research Reports Integration (ROB-140)
- Invest Screener US Activation (ROB-204)
- Screener pick log (prospective bakeoff)
- screen_stocks_snapshot / screen_stocks_enrich MCP 도구 분리 (ROB-1309)
- Negative-class(기각 코호트) 기록 — decision_bucket (ROB-1283)
- 정지 종목 오염 차단 — halted_suspect (ROB-1236)
- analyze quick fast projection (ROB-1311)
- get_ohlcv end_date 컷오프 (hk 6030)
- KR 1분봉 이벤트 수집기 — Toss → research.kr_candles_1m_toss (#1086)
- KR foreign net-flow ranking unit + provisional source (#1029)
- KRX 애프터마켓 세션×거래소 적격 (#925)
- crypto-universe-v1 — 상폐 포함 PIT 유니버스·일봉 재구축 (ROB-1432)
- 실험 레지스트리·manifest·heartbeat (ROB-1429)
- KR 상폐·종료결과 복구 + 인과 일봉 export (ROB-1430)
- US 상장 PIT 유니버스 — 상폐 포함 (ROB-1431)
- PIT 데이터 플레인 — universe·일봉·기업행위 일별 파티션 (ROB-1443)
- KR 부스팅 베이스라인·동결 대조군 (ROB-1441)
- Upbit KRW 15분 봉 장중 신호 사전등록·과거 재생 (hk 6035)
- 세션 분봉 타임라인 — research 분봉 수집·커버리지 진단 (hk 6145)
- 미국 데이마켓 Toss 1분봉 — `TOSS_INTEGRATED` venue·달력 기반 `US_DAY` (hk 6322)
- research 분봉 HTTP ingest·대상 읽기 (hk 6343)
- 장중 급등·급락 관측 로그 (hk 6154)
- 미국 장중 급등·급락 관측 로그 (hk 6510)
- 선물 시세 읽기·분봉 수집 — KIS 실전 선물 시세 view + research 선물 분봉 (hk 6148)
- FnGuide 컨센서스·추정치·투자지표 일별 스냅샷 (hk 6508)
- KR 관리·시장경고·정리매매·거래정지 플래그 (hk 6503)
- Upbit 원화장 장중 그림자 신호·가상 원장 기록기 (hk 6036)

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

**DART 공시 — 아직 끝나지 않은 날 (hk 3845)**: 07:00 KST 실행은 대상 날짜가 오늘이고 DART 가 오늘
목록을 아직 올리지 않아 0건이 정상이다. 규칙은 하나: 대상 날짜 ≥ 실행 시점의 KST 오늘(`today_kst()`, 테스트는
`today=` 주입)이면 "미완료"다.
- 🔴 미완료 날짜가 **행 0건**이고 XKRX 가 **거래 세션**으로 확정한 경우만 실패가 아니다.
  `ingest_kr_disclosures_for_date` 는 `status="pending"` 을 반환하고 파티션을 `pending` 으로 남긴다
  (`mark_partition_pending`; `succeeded` 도 `failed` 도 아니다). 다음 실행(18:30, 다음 07:00)이 그 날을 다시 받는다.
- 🔴 지난 날짜의 확정 세션 0건은 지금처럼 `RuntimeError`(failed)다. 캘린더 unknown 분기도 오늘에 대해서 그대로 실패한다.
  행이 있었지만 전부 파싱 불가인 경우(#1085)는 오늘이어도 pending 이 아니다. 확정 비세션 0건은 오늘이어도 `succeeded` 다.
- 🔴 `fetch_dart_filings_for_date` 는 대상 날짜 ≥ 오늘이면 `use_cache` 값과 무관하게 `list_date_ex(cache=False)` 로 받는다.
  OpenDartReader 는 페이지 URL 마다 `docs_cache/` 에 한 번 쓰고 계속 되돌려 주므로, 이른 "검색된 자료가 없습니다" 페이지가
  이후 실행에서 늦게 올라온 공시를 가린다.
- CLI 요약 JSON 은 기존 키를 그대로 두고 `pending`(미완료 파티션 수)을 추가한다. `failed == 0` 이면 pending 이 있어도 종료 코드 0이다.
  `pending` 키는 날짜 단위 루프 경로(dart 등)에만 있고 finnhub 범위 경로는 바뀌지 않았다.

**DART 공시 심볼 연결 (hk 4591)**: `list_date_ex` 행에 `stock_code`가 없으면 공식 OpenDART `corpCode.xml`
레지스트리의 `corp_name`을 공백 정규화한 뒤 정확히 비교한다. 디스크 캐시는 7일 TTL이며 갱신은 원자적으로 수행한다.
정규화 이름에 해당하는 레지스트리 항목 중 유효한 6자리 숫자 `stock_code`를 가진 상장사가 정확히 하나일 때만
그 코드를 `symbol`로 사용한다. 정확한 이름이 없으면 `no_registry_match`, 이름에 해당하는 항목은 있으나
유효한 상장 코드가 없으면 `unlisted`, 상장사가 둘 이상이면 `ambiguous_corp_name`, 레지스트리를 읽을 수
없으면 `registry_unavailable`을 `raw_payload_json.symbol_resolution_reason`에 기록하고 `symbol`은 `null`로 둔다.
퍼지 매칭은 하지 않으며 `corp_code`는 심볼로 대체하지 않는다. 행에 기존 `stock_code`가 있으면 기존 정규화 동작을
유지한다. 레지스트리 실패는 공시 수집을 실패시키지 않는다.

### Benzinga calendar ingestion (hk 6513)

운영자 결정 `hk:doc decision/2026-11-25/at-benzinga-trial-front-use`: 체험 요금제 기간에는 Benzinga 캘린더를 결정
흐름 앞단에서 쓰고, 체험 종료 전에 자체 소스로 대체할 수 있도록 원본 증거를 남긴다.

- **표면**: `app/services/benzinga_http.py`(공유 HTTP·시각 헬퍼 — hk 6511 이 바꾸는 `app/services/benzinga_news.py`
  는 건드리지 않는다), `app/services/benzinga_calendars/`(`spec`·`client`·`normalizers`·`ingestion`·`compare`),
  CLI `scripts/ingest_benzinga_calendars.py`. 행은 `market_events` 에 `source="benzinga"`, `market="us"` 로
  들어가며 쓰기는 `MarketEventsRepository` 하나뿐이다(직접 SQL 금지).
- **게이트**: `BENZINGA_CALENDARS_ENABLED` 가 정확히 `"true"`(기본 false)이고 키는 env `BENZINGA_API_KEY` 에서만
  읽는다. 인증은 query 파라미터 `token=<key>` 뿐이다(헤더 토큰은 401 — 2026-11-25 실호출 확인). 게이트가 꺼져
  있으면 CLI 는 **네트워크 호출·DB 접근·키 읽기 전에 exit 2** 다. 키 값은 로그·에러·저장 payload 어디에도 남지
  않는다(`redact_token`).
- **카테고리 → 엔드포인트**(필드 이름은 아래 문서의 응답 예시 그대로):
  | category | endpoint | 문서 |
  |---|---|---|
  | earnings | `GET /api/v2.1/calendar/earnings` → `earnings[]` | https://docs.benzinga.com/api-reference/calendar-api/get-earnings |
  | analyst_rating | `GET /api/v2.1/calendar/ratings` → `ratings[]` | https://docs.benzinga.com/api-reference/calendar-api/get-ratings |
  | guidance | `GET /api/v2.1/calendar/guidance` → `guidance[]` | https://docs.benzinga.com/api-reference/calendar-api/get-guidance |
  | dividend | `GET /api/v2.2/calendar/dividends` → `dividends[]` | https://docs.benzinga.com/api-reference/calendar-api/get-dividends |
  | economic | `GET /api/v2.1/calendar/economics` → `economics[]` | https://docs.benzinga.com/api-reference/calendar-api/get-economics |
  | conference | `GET /api/v2.1/calendar/conference-calls` → `conference[]` | https://docs.benzinga.com/api-reference/calendar-api/get-conference-calls |
  | short_interest | `GET /api/v1/shortinterest` → `shortInterestData{}` | https://docs.benzinga.com/api-reference/market-data/get-short-interest-data |
  | wiim | `GET /api/v2/news?channels=WIIM` → 배열 | https://docs.benzinga.com/api-reference/news-api/wiims/get-wiims |

  캘린더 계열은 `page`/`pagesize` + `parameters[date_from]`/`parameters[date_to]`(선택 `parameters[tickers]`),
  short interest 는 `symbols`(필수)/`from`/`to` + `page`/`pageSize`, news/WIIM 은 `dateFrom`/`dateTo` +
  `page`/`pageSize`(최대 100)를 쓴다. `shortInterestData` 는 심볼별 `{data: []}` 맵이라 평탄화한다.
  `ok=false` 오류 봉투·키 누락·비객체·비리스트 행은 **페이지 전체 실패**이고, 읽기 종료는
  `len(rows) < pagesize` 가 아니라 **빈 페이지**로만 판정한다 — 제공자가 요청한 `pagesize` 를 조용히 낮게
  캡해도 페이지 0,1,2… 를 끝까지 읽으므로 잘린 첫 페이지를 완전한 읽기로 오인하지 않는다. 페이지 상한
  (기본 20)에 닿았는데 그 페이지가 비어 있지 않으면 잘린 읽기이므로 실패다 — 빈 날로 읽지 않는다.
  `ok=false` 는 **row key 가 함께 있어도** 실패다: `{"ok": false, "earnings": []}` 를 빈 날로 읽으면 파티션이
  `succeeded` 가 된다. 읽기를 끝내는 것은 오류 플래그 없는 `{"earnings": []}` 뿐이다.
- **게이트 범위**: `BENZINGA_CALENDARS_ENABLED` 는 **모든 모드**(수집·`--compare-earnings` 포함)에 걸린다.
  꺼져 있으면 어떤 모드도 네트워크·DB 세션·키 읽기 없이 exit 2 다(비교 모드도 예외가 아니다).
- **taxonomy 확장(추가형)**: `CATEGORIES` 에 `analyst_rating`·`guidance`·`dividend`·`short_interest`·`wiim`,
  `SOURCES` 에 `benzinga`. 컨퍼런스콜은 기존 `conference`, 경제지표는 기존 `economic`(미국 행만)을 쓴다.
  기존 값은 그대로이고 `market_events` 에 category/source CHECK 는 없다(코드가 정본).
- **시각 계약**: `first_received_at` 은 **우리 시계의 최초 관측**이며 재수집이 덮어쓰지 않는다(리포지토리
  갱신 열에서 제외). 제공자 시각은 `provider_created_at`/`provider_updated_at`(aware UTC)과
  `provider_created_raw`/`provider_updated_raw`(받은 그대로)로 함께 남긴다. 캘린더 계열 `updated` 는 Unix
  seconds, WIIM `created`/`updated` 는 RFC 2822(`-0400`)다. 해석 불가는 `null` 이며 0 으로 채우지 않는다.
  WIIM 의 `event_date`(파티션 날짜)는 제공자 오프셋대로 `America/New_York` 날짜다 — 20:00 ET 이후 기사는
  UTC 로는 다음 날이지만 ET 로는 같은 날이며, 그 시간대가 WIIM 의 주 모집단이다.
- **revision**: `market_event_revisions`(append-only, UPDATE/DELETE/TRUNCATE 는 **모든 역할**에 대해 트리거가
  거부). 레코드의 정규화 payload 해시가 최신 revision 과 같으면 아무것도 쓰지 않고(값·`first_received_at`·
  `updated_at` 불변), 다르면 새 revision 행을 추가한 뒤 현재 `market_events` 행을 갱신한다 — **덮어써서
  이전 상태를 잃지 않는다**. 유일키는 `(source, category, market, source_event_id, revision_seq)` **하나뿐**이다:
  A→B→A 처럼 같은 내용이 다시 관측되면 그대로 세 번째 revision 이고, 최신 행이 항상 최신 관측을 설명한다.
  `append_revision` 이 False(동시 writer 가 같은 seq 를 가져감)면 현재 행을 갱신하지 않고 파티션을 실패로
  남긴다. `source_event_id` 로 식별되는 행은 개정으로 **`event_date`·`symbol`·`fiscal_year`·`fiscal_quarter`
  가 움직일 수 있으므로** 갱신 열에 포함된다(자연키 행은 그 값들이 키 자체라 불변). 다만 `None` 은 저장된
  값을 지우지 않는다 — 해석 실패는 "이제 모른다"가 아니라 그 실행이 증명하지 못한 것이다.
  `market_event_values` 도 **제공자 행이 그 레코드의 현재 전체 상태**라는 규칙을 따른다
  (`replace_values=True`, Benzinga 경로만): 개정이 회계 기간을 옮기면 이전 기간의 값 행은 남기지 않고
  지운다(`period` 가 값 키의 일부라 그대로 두면 같은 지표의 옛 값이 새 값 옆에 남아 first-match 리더가
  낡은 값을 낸다). 다른 writer 의 기존 upsert 동작은 바뀌지 않는다.
- **raw payload**: `raw_payload_json` 은 2,000자 넘는 문자열을 절단하고(`…(truncated)`) 리포지토리에서
  redaction 을 거친다. 대체 비교에 필요한 증거는 남기되 무한 blob 은 남기지 않는다.
- **리더(union/prefer 규칙 명시)**: `get_earnings_calendar` US 성공 응답은 Finnhub 행에 **저장된 Benzinga
  earnings 행을 union** 한다. 적용 조건은 (a) 게이트 on, (b) 창 안에 저장된 `source="benzinga"` earnings 행이
  존재, (c) 그 (symbol, date) 를 Finnhub 이 보고하지 않을 때뿐이다. 즉 **Finnhub 이 이긴다** — 같은
  (symbol, date) 에 Benzinga 행을 겹쳐 넣지 않는다. 추가 행은 `source="benzinga"` 로 표시되고, 최상위에
  `sources`·`benzinga_union`(added_rows·rule)이 붙는다. 위 조건이 하나라도 없거나 저장소 읽기가 실패하면
  Finnhub payload 는 **바이트 동일**하게 그대로 나간다(게이트 off golden, fail-soft). Finnhub 실패 시의
  ROB-1384 fail-closed 계약(저장분 degraded 폴백·`earnings_lookup_failed`)은 변경 없고, 그 폴백 행은 이미
  `source` 별로 라벨링된다. market events 읽기 표면(`/trading/api/market-events/...`)도 `source` 필드로
  벤징가 행을 그대로 노출한다. 추가 행의 `hour` 는 소비자(`earnings_context._TIMING_MAP`)가 매핑하는
  **제공자 코드**(`bmo`/`amc`/`dmh`, 판단 불가는 `unknown`)이고 우리 닫힌 값은 `time_hint` 에 그대로 남는다 —
  `hour` 에 `after_close` 같은 닫힌 값을 넣으면 소비자가 시점을 unknown 으로 읽는다.
- **파티션·재실행**: `(benzinga, category, us, date)` 파티션. `--force` 없이는 `succeeded` 파티션을 건너뛰므로,
  개정을 잡아야 하는 날짜는 **매 실행 `--force` 로 다시 받는다**(같은 payload 는 payload 해시 비교로 0행).
  fetch 실패는 그 창의 처리 대상 파티션 전부를 `failed` 로 남긴 뒤 예외를 다시 올린다. 개정으로
  `event_date` 가 움직이면 새 날짜 파티션에 기록되고 이전 날짜 파티션에는 그 행이 없다(파티션은 그 날짜에
  관측된 행의 수를 센다). **`--symbols` 로 좁힌 읽기는 파티션을 완료시키지 않는다** — 그 부분집합만 덮으므로
  파티션은 `pending` 으로 남고(이미 완료된 파티션을 `pending` 으로 내리지는 않는다) 다음 전체 읽기가 그 날을
  다시 받는다. AAPL 전용 읽기가 `succeeded` 를 찍으면 이후 MSFT 전용·전체 읽기가 fetch 전에 건너뛰어
  결손이 영구화된다. `first_received_at` 은 **fetch 가 돌아온 뒤**의 시계다(요청 시각이 아니다).
- **CLI**: `scripts/ingest_benzinga_calendars.py` — `--category`(콤마/반복, 필수), `--date` 또는
  `--from-date/--to-date`, `--symbols`(short_interest 필수, 그 밖에는 선택 필터), `--commit`(없으면 dry-run,
  네트워크 0), `--force`(succeeded 파티션 재수집 — 개정 포착에 필요), `--max-pages`,
  `--compare-earnings`(읽기 전용, 게이트는 동일하게 적용).
- **비교 도우미**: `app/services/benzinga_calendars/compare.py::compare_earnings` — Finnhub 대비 coverage
  (심볼 공통/한쪽만, 항목 한쪽만)와 **양쪽이 모두 관측한 항목에 대해서만** 시간대 일치/불일치/unknown 을 낸다.
  대체 판정용 읽기이며 어떤 정책·게이트도 바꾸지 않는다.
- **DAG 사본**: `deploy/dagu/at-benzinga-calendars/`(장전 1회, 장중 30분). 둘 다 스케줄이
  `America/New_York` 이므로 **날짜도 `TZ=America/New_York date +%F`** 로 계산한다(KST 날짜는 10:00 ET 이후
  하루 앞서고, Benzinga 캘린더의 `date` 도 미국 날짜다). 개정을 잡기 위해 두 DAG 모두 `--force` 를 준다 —
  없으면 장전이 오늘 파티션을 `succeeded` 로 만들어 장중 틱이 fetch 전에 건너뛴다. **등록되지 않았다** —
  등록은 설계 세션 몫이고, `short_interest` 는 symbols 필수라 DAG 에 넣지 않는다(수동 CLI). 그래서
  `market_events/expected_sources` 의 `EXPECTED_SOURCES` 에도 넣지 않았다 — 스케줄이 없는데 기대 소스로 두면
  신선도·`data_gaps` 가 매일 누락을 보고한다. DAG 를 등록할 때 같은 PR 에서 함께 정한다.
- **마이그레이션** `20261125_hk6513_benzinga`: 추가형. `market_events` 에 5개 nullable 열과
  `market_event_revisions` 표를 더하고 `at_app` 에 조건부 GRANT(SELECT·INSERT, 시퀀스 USAGE·SELECT)를 준다.
  downgrade 는 revision 행이나 provider 증거 열이 채워진 `market_events` 행이 있으면 거부한다.
- **범위 밖**: 뉴스(hk 6511·후속), Benzinga Pro 웹 엔드포인트, short interest 의 기본 symbol 목록 자동 결정,
  스케줄 등록, 주문 경로.
- **테스트**: `tests/services/benzinga_calendars/`(픽스처 정규화·idempotency·revision·client 계약·비교),
  `tests/mcp_server/test_benzinga_earnings_union.py`(union·게이트 off golden), `tests/test_benzinga_calendars_cli.py`,
  DB 왕복 `tests/services/benzinga_calendars/test_ingestion_db.py`(CI).

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

The outer wrapper creates one `call_id` per fanout invocation and passes it to
the recorder. It returns that value as the optional top-level
`pick_log_call_id` only when the gate is enabled, at least one row is written,
and the write succeeds. The key is omitted when logging is disabled, there are
no rows, or the write fails. The discovery implementation remains write-free.

fanout 바깥 wrapper는 호출마다 `call_id`를 하나 만들고 recorder에 전달한다.
기록 게이트가 켜져 있고 행을 하나 이상 성공적으로 쓴 경우에만 같은 값을
최상위 선택 키 `pick_log_call_id`로 반환한다. 기록이 꺼져 있거나 행이 없거나
쓰기에 실패하면 이 키를 생략한다. 후보 발굴 구현은 계속 쓰기를 하지 않는다.

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

**`screen_stocks`·`screen_stocks_snapshot` 관측 기록 (hk 5622, `collection_version="screener-tools-v1"`)**:
KR/US 라이브 세션은 fanout이 아니라 이 두 도구로 후보를 본다(fanout은 H1/H3 파일럿 프롬프트뿐이라 파일럿이
멈춘 10/30 이후 로그가 비었다). 그래서 두 도구가 돌려준 후보도 같은 바깥 관측자 패턴으로
`review.screener_pick_log`에 기록한다 — `app/services/screener_tool_pick_log.py`, 호출은 MCP 등록 계층
`analysis_registration.py` 한 곳뿐이다(`test_only_the_registration_layer_wires_the_observer`).
🔴 `screen_stocks_impl`·`screen_stocks_snapshot_impl`·`screen_stocks_enrich_impl`·`buy_candidate_fanout`은 계속 쓰기가 없다 —
`screen_stocks_impl`은 fanout과 웹 스크리너가 재사용하므로 impl 안에서 기록하면 하드룰 13이 깨진다.
`SCREENER_PICK_LOG_ENABLED` 기본 false, fail-open(기록 예외는 도구 출력을 바꾸지 않는다), 스케줄러 금지, 과거 소급 없음.
- **행**: 반환된 후보마다 1행(`admission='admitted'`, `admission_reason='returned_to_session'`, `selection_seq` = 반환 목록 안 1-based 위치(offset 포함),
  `rank` = `screen_stocks`는 반환 순서, snapshot은 도구의 rank — 다중 preset 병합 행은 첫 매칭 preset에만 rank가 있고 나머지 preset 행은 null).
  도구 자신이 이미 아는 제거는 `not_admitted` + 닫힌 사유: `halted_suspect`(screen_stocks, `meta.halted_suspect_excluded`),
  `exclude_symbols`·`exclude_watched`·`below_min_market_cap`·`market_cap_missing`·`above_max_market_cap`·`invalid_market_cap_bound`(snapshot; `exclude_held`는
  snapshot이 fail-closed로 거부하므로 행이 생기지 않는다). `invalid_market_cap_bound`(hk 5763)는 `min_market_cap`/`max_market_cap_eok`가 NaN 또는 무한대일 때 그 경계로 빠진 행의 사유다. 비교가 정의되지 않으므로 `below_min_market_cap`/`above_max_market_cap`으로 표시하지 않는다. 페이지 내용은 바뀌지 않는다. 반환 페이지 밖의 행은 세션이 보지 못했으므로 기록하지 않는다(`call_context.pagination`에 총수만).
  `screen_stocks_enrich`의 만성 실패 제외·`isHeld`는 이 변경 범위 밖이며 enrich는 기록하지 않는다. 시장은 kr/us만 기록한다(kospi/kosdaq/konex/all → kr, crypto 미기록).
- **source**: `screen_stocks` / `screen_stocks_snapshot:<preset>`(다중 preset 스윕에서도 (call, source, symbol) 유일키 유지). `family`는 `screen_stocks_live`/`screener_snapshot`.
  US 심볼은 DB 형식(`BRK.B`, `app/core/symbol.py`)으로 저장해 `decision_evidence`의 candidate ref(`call_id`+정규 심볼)가 그대로 해석된다.
- **가격**: 정확한 decimal 문자열. `screen_stocks`는 도구의 `price` 필드(float는 `Decimal(str())`), snapshot은 `priceLabel`("75,000원", "$12.30")을
  파싱한 값이라 라벨 정밀도다(`call_context.price_basis`). 해석 불가·비유한 값은 null이다.
- **source_status/data_asof**: 기록 행은 모두 `ok`다 — snapshot 자체의 fresh/partial/stale 판정은 컬럼 어휘에 대응이 없어 `call_context.freshness.<preset>`에 둔다.
  `data_asof`는 snapshot은 snapshotDate(없으면 computedAt), `screen_stocks`는 응답 timestamp다. 원 행은 JSONB-safe(NaN/Inf→null)로 `raw_row`에 둔다.
  마이그레이션은 없다(기존 컬럼·CHECK로 모두 표현된다).
- **call id**: 도구 호출마다 `call_id` 하나. 게이트가 켜져 있고 행을 하나 이상 썼을 때만 응답 최상위에 선택 키 `pick_log_call_id`가 붙는다
  (꺼짐·0행·오류 응답·기록 실패는 키 생략, 출력은 바이트 동일). 세션은 이를 `decision_evidence` 후보 ref로 인용한다.
  🔴 `decision_evidence`는 후보 ref 없는 같은 KST 날짜 매수 place에서 `admitted` 행을 `inference`로 연결하므로, 세션이 반환받은 후보는 이제 그 추론 대상이 된다.
- **스냅샷 provenance**: 병합 행의 표시 가격은 첫 매칭 preset의 것이므로, 그 가격을 싣는 모든 행의 `data_asof`/`fetched_at`도 첫 매칭 preset 기준이다
  (`source_params.price_provenance_preset`). 행 자신의 preset freshness는 `source_params.matched_preset_freshness`에 따로 둔다.
- **쓰기 예산**: 기록은 `WRITE_TIMEOUT_SECONDS`(10초)로 제한된다 — MCP 도구 데드라인(120초) 안에서 이미 성공한 스크리닝을 DB 지연이 잡아먹지 못한다. 도구 호출 안에서는(hk 5763) 기록 예산이 `min(10초, 남은 도구 데드라인 − 2초)`로 줄어든다. 남은 예산이 1초 미만이면 기록을 시작하지 않고 태그 없는 결과를 그대로 반환한다. 남은 데드라인은 `app/core/tool_deadline.py`가 `ToolTimeoutMiddleware`의 호출별 데드라인에서 읽으며, 도구 호출 밖(데드라인 없음)에서는 기존 10초다. 호출자 취소는 그대로 전파된다.
  초과하면 태그 없는 원 응답을 돌려주고, 호출자 취소는 그대로 전파된다. `raw_row`/`gate_features`/`call_context`는 `JSONB(none_as_null=True)`라
  파이썬 None이 JSON `null`이 아니라 SQL NULL로 저장된다(`*_object` CHECK는 SQL NULL 또는 객체만 허용).
- **fanout 규칙은 그대로**: `funnel-a1` A-record(소스 top-10 전수·admission·`dropped_preselection`)는 `discover_buy_candidates_fanout` 전용이고 이 두 도구는 위 규칙을 따른다.
- 테스트: `tests/services/screener_pick_log/test_screener_tool_pick_log.py`(순수·fail-open·게이트 off 동일성·등록 배선·경계), `..._db.py`(CI DB 왕복).

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

**기각 판단의 '샀다면 넣었을' 지정가 (hk 5719)**: `forecast_save`에 `decision_bucket='deferred_no_action'`과 함께
`declined_limit_price`(정확한 decimal 문자열 또는 정수, 소수 JSON 숫자는 거부), `declined_quantity` 또는 `declined_notional`(같은 형식, 정확히 하나), 선택 `declined_quote_price`+`declined_quote_asof`(시각대 포함 ISO), 선택 `declined_account_mode`(`kis_live`·`toss_live`·`upbit`)를 주면 그 기각 판단의 `decision_evidence`에 BUY rung 0 의 `limit_price`·수량/금액과 `limit`(및 `quote_at_decision`) 참조가를 함께 기록한다. 🔴 이 값은 기각이며 주문이 아니다 — 판단 행은 `decision_kind='declined'`이고 `decision_order_links`를 만들지 않는다. retro/v1 은 계획 필드만 채우고 주문·체결로 세지 않는다. 기록은 `equity_kr`·`equity_us`·`crypto` 이고 `decision_bucket` 이 정확히 `deferred_no_action` 인 새 생성 행에만 된다. 입력 검증은 쓰기 이전에 끝나며 닫힌 코드(`declined_bucket_required`·`declined_market_unsupported`·`declined_limit_price_required`·`declined_size_required`·`declined_size_ambiguous`·`declined_quote_incomplete`·`declined_value_invalid`·`declined_account_mode_invalid`)로 실패한다. 생성되지 않은 갱신에 입력을 주면 `declined_not_created`로 롤백한다. 입력이 전혀 없으면 기록은 종전과 바이트 동일하다(evidence hash 고정 테스트). 실험 짝의 모집단은 `docs/design/5624-entry-ab-mock.md` §2 를 따른다.

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

지지/저항 단일 원천: `get_support_resistance` 와 quick 의 레벨 입력(피보나치 +
20-bin 거래량 프로파일 POC·value area + 볼린저)은
`market_data_indicators._collect_support_resistance_price_levels` 하나에서 만든다.
`volume_poc` 는 거래량 프로파일 POC 이며 거래량 가중 종가가 아니다. quick 은 같은
60봉 창을 쓰고, 쓸 수 있는 거래량이 없으면 volume 레벨만 빠진다.
`research/screener_bakeoff/indicators.support_resistance` 는 이 빌더의 미러이며
`tests/research/test_screener_bakeoff_contract.py` 가 동일성을 고정한다.

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


### get_market_index 추가 심볼·날짜 분리 (task 6301)

`get_market_index`는 `SOX`(Yahoo `^SOX`), `KOSPI200`(기존 Naver 지수 소스, 코드 `KPI200` [unverified]), `NQ=F`·`ES=F`(Yahoo 연속 최근월, `contract.kind=continuous`, 지연 여부는 미검증이라 `delayed=null`), `USDKRW`(Yahoo `KRW=X`)를 받는다. `KOSPI200_NIGHT`(코스피200 야간선물)는 항상 `unsupported=true`와 `unsupported_reason`을 준다: KIS 공식 샘플에 야간 선물 시세 TR이 없고, hk 6148 view의 현재가 TR은 정규 `F` 시장이라 낮 세션 값이므로 야간 심볼 아래에 올리지 않는다(`KIS_FUTURES_QUOTES_ENABLED`가 켜져 있어도 KIS를 호출하지 않는다). 모든 행은 `as_of`(UTC)와 `as_of_basis`(`quote_time`|`fetched_at`)를 가진다. KR 행은 현재값의 시세 날짜를 `session_date`에, OHLC의 날짜를 `ohlc_date`에 따로 둔다. OHLC가 이전 세션의 것이면 최상위 `open/high/low/volume`은 null이고 그 세션 값은 `previous_session`(자기 `date`)에만 있어 한 레코드가 두 세션을 섞지 않는다. 이전 세션 OHLC가 있을 때의 ROB-464 frozen 판정은 `previous_session.open`으로 비교한다. 미지의 심볼은 `Unknown index symbol '<X>'. Supported: …`(지원 목록 정렬)로 거부한다.

### get_ohlcv end_date 컷오프 (hk 6030)

`get_ohlcv`의 `end_date`는 모든 시장·period·데이터 경로(캐논컬 DB, OHLCV Redis 캐시 hit/miss, provider fetch)에서
**포함 상한**이다. 날짜만(`YYYY-MM-DD`) 주면 그 시장 로컬 달력일 전체를 포함하고(크립토·KR=`Asia/Seoul`,
US=`America/New_York`), 시각이 있으면 그 로컬 시각에서 자른다. 일/주/월 봉은 봉의 날짜 라벨로, 분/시간 봉은
타임스탬프로 비교하므로 컷오프 당일 봉은 반환되고 그 뒤 날짜의 봉은 절대 반환되지 않는다. `count`는 컷오프에서
뒤로 채우며, 데이터가 모자라면 더 적게 반환하고 이후 봉으로 메우지 않는다. `include_indicators=true`의 지표는
컷오프 이전 봉만으로 계산한다. 캐시 경로(`app/services/{upbit,yahoo}_ohlcv_cache.py`)는 `end_date`를 읽기 상한으로
받아 컷오프에서 끝나는 창을 서빙하고, 캐시의 신선도 커서(`last_closed_date`/`last_closed_bucket`)는 실제 최신
마감 봉에 남긴다. `end_date`가 없으면 기존 최신 창 동작은 바뀌지 않는다.

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
  ROB-1488: KR `get_operating_briefing`의 `staleness.krx_after_market_eligibility`와
  수동 `scripts.diagnose_krx_after_market_eligibility`는 count/min/max SELECT로
  결손·7일 초과·미래·판독 실패를 주의 상태로 드러낸다. 읽기 경로의 자동 갱신은 없고,
  KRX의 검증된 목록 feed 없이 일반 주식 목록·거래량으로 적격을 만들지 않는다.
  진단 CLI는 READ ONLY·8초 statement timeout, 정상 exit 0·주의 exit 1이며 스케줄 등록 없음.
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
- **Dagu**: `deploy/dagu/data-plane/*.yaml` — data-plane-kr/-us/-crypto 는 OCI Dagu coordinator 에 **등록되어 있고** pi worker 에서 실행된다(hk:doc `report/2026-11-10/ncp-schedule-inventory` §4; 등록 시점은 그 인벤토리에 날짜 없음). DAG 등록·수정은 운영자만 호스트에서 하며, 레포의 YAML 은 사본이라 어떤 deploy 단계도 적용하지 않는다. 새 명령만 참조.
- **venue 라벨 relabel (hk 3847)**: US 유니버스의 `otherlisted` 거래소 코드 `F`=Texas Stock Exchange, `M`=NYSE Texas 는 NASDAQ Trader 정의 페이지에 없어(A/N/P/Z/V 만 기재) 실제 파일 행에서 매핑했다. 축소 가드는 이전 파티션 카운트에 `symdir.LEGACY_VENUE_ALIASES`(검토된 옛 라벨→새 라벨, 현재 `UNKNOWN:TXSE`→Texas Stock Exchange)만 접어 비교한다 — 임계값·slack 불변, 알려진 venue 의 실제 감소는 그대로 거부한다. 미매핑 코드는 `UNKNOWN:<code>` 로 남고 universe manifest 의 `unmapped_venues` 로 보고된다.
- **Dagu 제안 (배치 1, hk 3848)**: `deploy/dagu/at-batch/*.yaml` 11개 — Prefect 배치 11개의 이전 후보. 🔴 **등록 안 됨**: 등록·Prefect pause 는 `docs/runbooks/dagu-batch1-cutover.md` 를 잡별로 따를 때만, 8·10·11번은 hk 3846/3845 수정 배포 후. 정적 검사 `tests/scripts/test_dagu_at_batch_dags.py`.
- **Dagu 배치 2 (hk 4896)**: `deploy/dagu/at-batch2/*.yaml` 4개 — at-scheduler(TaskIQ) 은퇴로 체결 원장 대사(`scripts.reconcile_execution_ledger`, KIS·Upbit 독립 두 단계, 한쪽이 실패하면 run 실패, 30분마다, `EXECUTION_LEDGER_COMMIT_ENABLED` 가 참일 때만 `--commit`)와 종목 목록 3종(`scripts.sync_{kr,us,upbit}_symbol_universe`, 태스크와 같은 시각)을 옮긴 것. 운영자 승인(2026-11-17 채팅 "새로 만들 Dagu 잡의 스케줄 등록 승인", hk:doc consult/2026-11-17/at-go-1117-fable52)으로 OCI Dagu 에 **등록됨**. 레포 파일은 사본이며 어떤 배포 단계도 적용하지 않는다. 명령 안에서 셸 변수를 쓰지 않는다(Dagu 가 `${VAR}` 를 먼저 펼칠 수 있다). `docker run --init` 으로 시간 초과 시 컨테이너도 멈춘다.
  KR 일봉 동기화(`at-batch2-daily-candles-kr`)는 아직 거래 중인 KST 당일 봉을 쓰지 않는다: `DailyCandleSyncService._sync_kr` 가 KIS·Toss 양쪽 행에서 KST 봉 날짜가 실행 날짜(`today_kst()`)와 같거나 늦은 행을 버리고 실행마다 한 번 로그를 남긴다. 16:30 KST 실행은 과거 날짜를 다시 쓰고, 화–토 07:10 KST 추가 실행이 전일 정산 봉을 확정한다. US·crypto 경로는 바뀌지 않았다. 이 컷오프는 `sync_one` 을 쓰는 모든 KR 경로(예약 실행·`scripts/backfill_daily_candles.py`·`forecast_service._backfill_daily_candles`)에 적용되며, 같은 날 해소는 다음 날 확정 봉까지 미뤄진다(운영자 결정 hk 6245 댓글).
- 기존 `*_candles_1d`·`*_symbol_universe`·뉴스 테이블은 서빙용으로 유지하되 덮어쓰기라 학습 입력으로 쓰지 않는다.

### KR 부스팅 베이스라인·동결 대조군 (ROB-1441)

`research/kr_boosting/` — U7-KR. U2 인과 데이터셋으로 `H-admit`·`H-entry-price` 의 LightGBM 베이스라인을
돌려 U5 계약으로 대조군(변경 없는 정책·모멘텀·평균회귀·현금)과 비교하고 게이트 판정을 낸다. 연구 전용:
`app` import·DB·브로커·스케줄러 0. 판정과 수치는 `research/experiments/reports/rob-1441-kr-boosting-baseline.md`.

- 🔴 **ML 의존성은 `pyproject.toml` 에 넣지 않는다**: `research/kr_boosting/requirements-ml.txt` 별도 환경. LightGBM 은 `train.py` 안에서만 지연 import 한다(테스트가 강제).
- 🔴 **2023-2024 는 sealed test 가 아니다**: 노출 감사(`exposure_audit.py`) 결과 입증 불가라 진단 전용이고, 게이트에 쓰지 않는다. 그 읽기는 k 로 계수했다. 시험은 prospective seal 이며 미개봉이다. `read_rows` 는 2023 이후 읽기를 기본 거부하고, `run.diagnostic()` 만 함수 안에서 동결 번들을 확인하고 claim 을 남긴 뒤 1회 읽는다(행이 아니라 export 디렉터리를 받으므로 claim 전에 호출자가 봉인 행을 쥘 수 없고, 등록되지 않은 export 는 look 을 쓰기 전에 거부된다). claim 을 지우고 다시 읽으려면 look 을 먼저 계수한다. `validate` 는 claim 이나 diagnostic 결과가 있는 root 와 2022 이후 세션이 있는 panel 을 거부한다.
- 🔴 **`feature_join` 이 모델 피처의 유일한 입력 경로다**: `available_at` 이 결정 시각보다 늦은 행은 거부, 없는 행은 제외·계수. terminal 컬럼은 피처 입력이 아니다. 대조군 순위와 지정가는 세션 t 봉을 직접 읽어 이 가드를 타지 않는다(U2 봉은 세션 16:00 KST 가용, 결정은 t+1 08:30).
- 🔴 **게이트가 STOP 인 가설은 U8 TFT 를 돌리지 않는다.** 게이트 수치를 사후에 바꾸지 말고 바꿀 일이 있으면 새 manifest 버전과 새 시험을 쓴다. 2026-11-02 판정은 두 가설 모두 STOP 이다(k=1).
- 🔴 **모든 단계는 등록된 데이터셋만 받는다**: export 해시가 등록 manifest 의 `dataset_*_kr` 와 다르면 행을 읽기 전에 거부한다. `report` 는 저장 결과의 manifest·동결 번들·export 가 서로 다르면 거부한다.
- 🔴 **pickle 산출물은 `artifacts.py` 로만 읽고 쓴다**: 쓸 때 기록한 sha256(`run_manifest.json`)과 바이트가 다르거나 기록이 없으면 unpickle 전에 거부한다. 변경 탐지이지 인증이 아니므로(둘 다 고칠 수 있는 쓰기 권한자는 통과) `--root` 는 운영자 소유 디렉터리여야 한다.
- 분할은 purge + gap 을 경계마다 **더해서** 적용한다(H-admit 5+5, H-entry-price 25+25). 설정 선택은 Y-1 년으로만 한다.
- 🔴 **종료 지급액은 가정이다**: `delist_resolution.v1` 감사(ROB-1437)는 심볼·상폐일·사건 유형만 확인했고 지급액은 미검증이다. 베이스는 마지막 종가 청산이고, 리포트의 지급액 민감도 표(`payout_exposure.py`)가 그 가정에 걸린 금액을 보인다.
- U5 KR 어댑터는 D3 엔진을 감싸 자체 순위만 쓰고 거래량이 없어서, `simulator.py` 가 같은 계약 타입·체결 모델로 후보 목록과 `VolumeShare` 를 돌린다.
- **런북**: `research/kr_boosting/README.md`. 스케줄 없음(수동 `validate`/`diagnostic`/`report`).

### Upbit KRW 15분 봉 장중 신호 사전등록·과거 재생 (hk 6035)

`research/upbit_intraday/` — 신규 추세 돌파·보유 코인 반등·하방 이탈 세 계열을 마감된 15분 봉만으로 동결 규칙에 따라 재생한다. 연구 전용: `app`·DB·브로커·자격증명·스케줄러·websocket·주문 import 가 없다(정적 시험). 공개 Upbit GET 은 `/v1/market/all`·`/v1/candles/minutes/15` 두 경로만 허용 목록에 있고 URL 을 소켓 전에 검사한다. 원 봉 파일은 해시와 함께 git 밖에 두고 레포에는 `data_manifest.json`·집계만 둔다.

- 🔴 `rules.py`·`evaluate.py` 는 registry 매니페스트(`HK-6035`)가 sha256 으로 핀한다. 바꾸면 새 매니페스트 버전이며 `test` 명령이 `frozen code changed` 로 거부한다. 시험 구간은 한 번만 열린다(`test_look.done`); 다시 열려면 registry look 을 먼저 추가한다.
- 🔴 봉 없는 슬롯(무거래)은 NaN 이며 채우지 않는다. 진입 봉이 없는 신호는 결과에서 빼고 건수를 센다. 생존 편향(상장폐지 마켓 없음)·경고 이력 부재는 보고서가 방향과 함께 밝힌다.
- 2026-11-22 결과: 세 계열 `NOT_SUPPORTED`(`docs/research/6035-upbit-intraday-signals.md`). 후속 hk 6036(라이브 shadow 기록기)은 신호 효과 가정 없이 발화·미발화·체결 가능 가격의 전향 기록 범위다. 이 연구는 어떤 주문·알림·정책도 바꾸지 않는다.

### Upbit KRW shadow 순수 규칙 라이브러리 (hk 5888, 5852 설계 §9 T1)

`app/services/upbit_krw_strategy/` — `docs/design/5852-upbit-krw-shadow-strategy.md` 의 유니버스 U1–U5, 두 신호 계열(추세·눌림목), 크기, 청산·익절 사다리, 일일 적격과 `candidate_set_sha256` 을 순수 함수로만 구현한다. 🔴 DB·브로커·네트워크·설정·스케줄러 import 가 없다(`tests/services/upbit_krw_strategy/test_static_boundary.py` 가 import 목록과 import 뒤 `sys.modules` 를 모두 검사). 허용되는 앱 밖 import 는 `app.core.upbit_tick`, `research.crypto_universe.universe`(`Dockerfile.api` 가 이미 싣는 두 파일) 둘뿐이다. 주문·게이트웨이 의도·테이블·일일 잡(T4)은 이 패키지 범위 밖이다.

- **Decimal**: 돈·가격·비율은 전부 `Decimal`(float 는 `TypeError`)이다. 호가 단위는 `app/core/upbit_tick.py` 만 쓰고 표를 다시 정의하지 않는다.
- **U2 제외**: `crypto_exclusion_reason(base, (), cutoff=…)` 의 정적 목록(레버리지·스테이블·래핑 중복)에 `USDT` 를 더한다(F10). 30일 peg 규칙은 USD 종가용이라 KRW 종가에 적용하지 않는다(`closes` 를 넘기지 않는다). `PAXG` 는 연구 함수와 같이 유지된다.
- **날짜**: `asof` = 09:10 KST 발행 슬롯의 UTC 날짜, 가장 최근 완결 일봉 `C_0` 의 시가 날짜 = `asof − 1일`. `C_k` 는 달력 일 기준이고 `C_0..C_60` 중 하루라도 없으면 `NO_SIGNAL`(앞 값 채우기 없음).
- **RSI14**: pandas 를 import 하지 않는 Decimal 구현이며 ROB-1382 `_calculate_rsi_series` 와 같은 재귀(첫 차분이 0 으로 바뀐 뒤 `ewm(alpha=1/14, min_periods=14, adjust=False)`, `avg_loss == 0` 이면 `None`)다. 같은 산식임은 `test_signals.py` 의 pandas 동치 테스트(1e-9)가 고정한다. 신호는 `C_60..C_0` 61개 창 위에서 계산한다(설계가 창 길이를 정하지 않아 R60 에 필요한 창을 그대로 썼다).
- **모르는 값은 0 이 아니다**: NULL `quote_volume`·30일 창의 빠진 날은 `liquidity_unknown`, 호가가 없으면 청산 판정은 `hard_stop_unverified`, 결측 R60 은 `trend_unverified` 로 보고하고 청산·진입하지 않는다.
- **`candidate_set_sha256`** (hk 5931): 마켓별 `{eligible, reason_code, turnover30_median_krw, turnover30_sum_krw, liquidity_rank, tie_break_key, last_close_krw}` 와 `asof`·`rules_version`·`u0_sha256` 의 정규 JSON sha256. 슬롯 선택이 읽는 30일 거래대금 합·순위(적격 마켓만, 아니면 null)·동률 키(마켓 코드)가 모두 봉인된다 — 합만 달라져도 해시가 바뀐다. `u0_sha256` 은 멤버십 해시이고 컷 순서는 `u0_ordered_sha256` 이 따로 고정한다. 설계 문서의 "중앙값·종가만" 문구와 코드가 다르면 dots 리뷰(5908 B3)를 따랐고 문서는 hk 5930 이 고친다.
- **경고·보유 상태 (hk 5931)**: `market_warning` 은 닫힌 어휘다 — `NONE` 만 적격, `CAUTION` 은 `market_warning_caution`, 빈 값·null·그 밖의 값은 `market_warning_unknown` 으로 fail-closed. `select_entries` 는 보유 마켓을 신호·적격 행이 없어도 결과에서 빼지 않는다(`held=True`, 적격하지 않으면 `held_not_eligible`, `eligibility_reason` 에 현재 U 판정). 보유 마켓의 매수 의도는 여전히 없다. 이 열은 hk 6026 이후 `market_event.warning` 의 유의 투영이며(주의는 `market_event.caution`), `upbit_krw_strategy` 의 판정 의미는 바뀌지 않았다.
- **`candidate_set_sha256` 골든**: 정규 JSON(키 정렬, 구분자 `,`/`:`, ASCII) 의 sha256, 십진수는 지수 없는 정확한 문자열. 골든은 `tests/services/upbit_krw_strategy/golden_candidate_set.json`(리터럴 JSON 의 해시를 코드와 독립적으로 대조).
- **Upbit 시장 경보 상태 — `market_event` (hk 6026)**: Upbit 는 2024-11-20 부로 `GET /v1/market/all` 의 `market_warning` 필드를 없애고 `market_event: {warning: bool, caution: {<TYPE>: bool, ...}}` 로 대체했다(근거: docs.upbit.com/kr/changelog/mytrade_market_warning_deprecated_11_20, 필드 정의 docs.upbit.com/kr/reference/list-trading-pairs 2026-07-29 판, 주의 의미 고정 docs.upbit.com/kr/changelog/market_event 2026-01-29: 주의/경고/위험 어느 단계든 경보가 있으면 true). `warning` 은 유의, `caution` 은 주의이며 타입 이름은 Upbit 가 보낸 그대로 보존한다(우리가 목록을 닫지 않는다).
- **경보 저장과 닫힌 상태**: `upbit_symbol_universe.market_event` JSONB + `market_event_observed_at` TIMESTAMPTZ(마이그레이션 `20261122_hk6026_market_event`, 추가형·백필 없음, 운영자가 적용). `market_warning` 열은 이제 `market_event.warning` 의 유의 투영일 뿐이다(warning→`CAUTION`, false→`NONE`, 없음/판독불가→`UNKNOWN`); 주의는 그 열에 들어가지 않는다. Upbit 가 말하지 않은 시장에 `NONE` 을 쓰지 않는다. `app/services/upbit_market_event.py` 가 시장마다 정확히 하나의 상태를 낸다 — `none` / `warning` / `caution:<TYPE,...>` / `unknown`(market_event 없음·판독 불가) / `stale`(관측이 `MARKET_EVENT_STALE_AFTER`=36시간보다 오래됨; 동기화는 매일 06:15 KST). 신선도는 `market_event_observed_at` 으로만 판정하며 행 `updated_at` 을 쓰지 않는다. `warning` 은 `caution` 보다 우선하고 타입은 표시용으로 남긴다.
- **경보 리더 계약**: 구매측 리더(암호화폐 스크리너·추천·보유 표시)는 `warning`·`unknown`·`stale` 시장을 신규 매수·추가 매수에서 제외하고 `filtered_by_warning`/`filtered_by_unknown_warning`/`filtered_by_stale_warning` 와 `warnings` 로 이유를 보고한다. `caution:<types>` 는 제외하지 않고 후보의 `market_event_state`·`caution_types` 로 표시한다. 비활성(상장폐지·거래정지) 마켓은 기존대로 제외한다. 경보 조회가 실패하면 대화형 스크리닝은 필터를 건너뛰고 `warnings` 에 남기며, 암호화폐 스크리너 스냅샷 빌드는 `market_warning=false` 를 지어내지 않고 실패한다(스냅샷 행의 유의 플래그는 이제 실제 상태에서 온다 — 이전에는 finalize 가 지워 항상 false 였다). 읽기 모델 `app/services/upbit_public_read_model/market_warnings.py` 는 DB 만 읽고(읽기 시점 HTTP 없음) 엔트리마다 `state`·`caution_types`·`observed_at` 과 레거시 `warning` 을, 블록 `meta.fetchedAt` 은 가장 최근 관측 시각을 낸다 — 오래된 관측은 `stale` 이며 fresh 로 읽히지 않는다(2026-02-21 데이터가 fresh 로 보이던 dots 관측의 원인). `get_crypto_catalysts` 의 `market_warnings` 블록은 유의와 주의를 함께 싣는다.
- **`market_events` 확인 결과**: 설계 §6 이 물은 "투자유의 세부" 는 `market_events` 에 없다. taxonomy 는 `source="upbit"`·`category="crypto_exchange_notice"` 를 허용하지만 이를 쓰는 normalizer·ingestion 이 레포에 없다.

### 종목 선별 연구 하네스 (ROB-1474)

`research/stock_selection/` — KR·US·Upbit KRW의 일별 횡단면 순위 연구. 공개 무인증
시세 GET과 해시 검증된 로컬 CSV만 사용하며 DB·계좌·주문·스케줄러 연결은 없다.
CLI `fetch`/`build`/`evaluate`/`forward`/`score`는 수동이다. ML 의존성은 별도
`requirements-ml.txt`, LightGBM은 학습 함수 안 지연 import만 허용한다.

- 피처는 마감 시각까지, 라벨은 공통 거래소 세션 t+h. 결측/기업행위 교차 결과는
  unknown이며 미래 결과의 유무로 오늘 유니버스를 고르지 않는다. purge 10세션 +
  embargo 10세션을 더하고 모델·기준선에 동일 평가 날짜/채점기를 쓴다.
- forward는 날짜 prefix만 읽고 파일을 덮어쓰지 않는다. score는 저장 당시 입력
  prefix 해시와 대조하여 과거 시세 수정이 있으면 거부한다.
- `prediction_at`·`data_cutoff`·`label_kind=statistical_close_to_close`와
  `execution_label=null`을 기록한다. close(t)는 통계 기준점이며 예측 후 최초 거래가능
  가격에서 시작하는 실행 라벨과 섞지 않는다. pending·상폐·결측 결과는 0으로 채우지 않는다.
- 공개 과거 스냅샷은 당시 관측 vintage가 아니다. 결과는 **survivor-only/bounded
  diagnostic**으로만 보고한다. KR/US close×volume은 실제 거래대금이 아닌 추정치다.
  시장 전체 readiness/실행 성과로 승격하지 않는다. 데이터·모델·행별 결과는
  gitignored cache, 실험 사전등록과 k는 `research/experiments/`에 둔다.
- 런북: `research/stock_selection/README.md`, 보고서:
  `research/experiments/reports/rob-1474-stock-selection.md`.

### KR get_quote 장전 응답 (hk 6300)

`get_quote`의 KR 응답은 KRX 정규장 개장(09:00 KST) 전(`data_state: premarket_unavailable`)에 전일 이월 봉을 쓴다. 이때 `open`/`high`/`low`는 `krx_ohlcv_basis: "carried_previous_close"`로 표시하고 `volume`/`value`는 `null`로 둔다(0 체결 수로 읽지 않는다). NXT 호가를 가격으로 쓰는 것은 `nxt_tradable: true`인 종목뿐이며, 그 경우 `nxt_trade_state: "no_trade_observed"`를 남긴다. 그 밖의 종목은 `price_usable: false`다. 날짜만 있는 `price_as_of`(KST 자정, 일봉 날짜)는 장전 경로에서 fresh가 아니며 `price_as_of_kind: "date_only"`와 `price_unavailable_reason: "date_only_price_asof"`를 준다. 정규장 KR 응답의 형태는 바뀌지 않았다. 이 필드를 읽는 앱 내부 소비자는 `order_preflight`(`price_freshness`가 `fresh`가 아니면 거부, 키움 모의 경로 `kiwoom_mock_place`·`orders_kiwoom_variants`가 공급)와 `auto_approve_price_fallback`(`is_stale_price`·`data_state`)이다.

### KR get_quote 가격 종류 (hk 6498)

KR `get_quote`의 대표가(`price`)는 `price_kind`로 어떤 근거인지 밝힌다: `last_trade`(세션 안 마지막 체결), `mid`(양쪽 호가가 모두 있는 중간가), `expected`(예상체결가, 동시호가 구간에서만), `unavailable`(판단 불가, 사유 `price_kind_reason`). 예상체결가는 **연속매매 중에는 대표가가 되지 않는다**(장전 NXT `nxt_pre`, NXT 애프터 `nxt_post`, KRX 애프터 포함). 한쪽 호가만 있으면 가격이 아니다. 세션을 알 수 없으면 `unavailable`이며 추측하지 않는다.

- 예상체결가는 항상 `expected_price`(와 `expected_price_venue`) 필드에 따로 남는다. 같은 값이 `price`로 복사되는 것은 `price_kind=expected`(동시호가)뿐이다.
- `venues`는 읽은 시장마다 `best_bid`·`best_ask`·`observed_at`(호가 시각)·`expected_price`와, 체결이 있으면 `last_trade_price`·`last_trade_at`을 싣는다. 현재 `get_quote`는 NXT 호가만 읽으므로 `venues`에는 `nxt`만 나온다.
- 체결은 KIS 분봉(`inquire_minute_chart`, NX, 1분)의 거래량이 있는 가장 최근 봉에서 읽는다. 봉 종가가 체결가, 봉 시작 시각이 체결 시각(분 단위)이다. `inquire-price`(FHKST01010100)는 체결 시각을 주지 않으므로 쓰지 않는다. 체결 시각은 세션 시작(`nxt_pre` 08:00, `nxt_post` 15:30 KST) 이후이면서 현재 시각 이전이고, 현재 시각보다 5분(`TRADE_MAX_AGE`, 호가 신선도 기준과 같음) 이내여야 세션 안 체결로 친다. 그 밖이면 `mid`로 내려간다. 읽기 실패도 체결 없음이다.
- `price_kind`가 `expected`이면 `is_stale_price: true`, `price_freshness: "unavailable"`, `price_usable: false`, `price_unavailable_reason: "expected_price_not_order_basis"`다. 주문 근거로 쓰지 않는다. `order_preflight`(키움 모의 `fetch_kr_quote_for_preflight`)와 `auto_approve_price_fallback`(`classify_kis_quote`)은 이 값을 거부한다.
- 정규장 KRX 기본 경로(`data_state: fresh`, NXT 오버레이 없음)는 `price_kind: "last_trade"`, `price_session: "regular"`를 더한다. 그 밖의 기본 경로(장전·장마감)는 `price_kind: "unavailable"`과 사유 `premarket_unavailable` 또는 `krx_market_closed`를 더한다. 다른 키의 값과 형태는 그대로다.
- KRX 동시호가 구간(08:30–09:00, 15:20–15:30 KST, 거래일)에는 `get_quote`가 KRX 호가를 한 번 더 읽어 `price_session: auction`으로 판정한다. 이때 예상체결가가 있으면 `price_kind: expected`이고 주문 근거가 아니다. 호가 읽기가 실패하면 기존 기본 경로를 그대로 둔다.
- 순수 선택 규칙은 `app/services/kr_quote_price.py`, 테스트는 `tests/services/test_kr_quote_price.py`·`tests/mcp_server/test_kr_quote_price_kind.py`다. 시장 개장 사실(동시호가 시각, NXT 경계)은 이 PR 에서 거래소 공지로 확인하지 않았다. 장후 NX 분봉의 시장 코드와 봉 시각 의미는 `[unverified]`이며 운영자 스모크로 확인한다. US 시세, Go 수집기, 주문 도구 자체는 이 계약의 범위 밖이다. 주문 가격 계산(`order_validation._premarket_nxt_price_for_kr`)은 아직 `expected_price`를 쓰며 별도 결정이 필요하다.

## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 루트 `AGENTS.md`의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.

### ROB-1474 paper-runner daily input producer

`python -m scripts.mock_data_refresh --market us|crypto --root DIR` is a manual,
GET-only file producer, separate from the research public-only fetch harness.
Alpaca active equities / Binance TRADING USDT spot are ranked using completed
20-session median estimated dollar volume / 30-day actual quote volume. Current
membership is observed prospectively; backfilled features never create historical
membership. Immutable generations and a single atomic market symlink publish
`read_bars` CSV/metadata plus candidate history and dated membership hashes.
No orders, DB writes, scheduler registration or deployment. Crypto's explicit
`--cache-dir` runner input retains the legacy `--panel-root` path. Details:
`docs/runbooks/rob1474-mock-data-refresh.md`.

ROB-1474 input maintenance: `scripts.mock_data_prune` previews manual retention
(default seven generations including the protected current target). `--apply`
requires all root readers stopped; refresh shares its market flock. No automatic
cleanup or scheduler. New membership dates omit full source lists; each generation
stores `source_symbols.json` with its manifest hash. US producer consumers require
universe cutoff equal to the prior signal session and record observation time.
Crypto cache consumers require cutoff equal to the requested completed candle.

ROB-1474 successful incremental fetches replace the overlap only for symbols
present in the returned bars, including individual candle withdrawals. A requested
symbol absent from the response retains cached history but is reported as
`refresh_data_unavailable` and excluded from current membership. HTTP/transport
exceptions abort publication; older history and immutable generations remain.

A symbol with any row that `validate_bars` rejects is excluded whole with reason
`malformed_bars` before whole-frame validation; its rows reach neither `bars.csv`
nor `candidates.csv`. Missing or blank numeric fields parse to NaN so they take
this path. A malformed benchmark bar aborts publication. `validate_bars` itself is
unchanged.

### US 지정가 깊이 연구 shadow 설계 (hk 3737)

`docs/design/3737-us-limit-depth-shadow.md`는 후속 연구의 제안이며 구현·실험 활성화가 아니다. ROB-1474의 기록된 US 매수 진입 episode를 읽기 전용으로 비교하고, 1분봉 GET 파일의 불변 세대와 출처를 고정한다. 매수 반사실은 `low < limit`일 때만 체결 가정, 동등 접촉은 별도 계수하며 결과는 journal·브로커 체결과 별도 라벨이다. 사전등록·미결 설계 확정 후에만 후속 구현한다. 주문·원장 쓰기·소급·스케줄러 및 기존 시장가 실험 변경은 없다.

### 스냅샷 일일 갱신 — dry-run 채움률과 스크리너 5거래일 보관 (hk 5752)

스크리너·컨센서스·재무제표 빌더의 dry-run 결과는 `FILL_RATE_JSON {...}` 한 줄을 출력한다(`app/services/snapshot_fill_rate.py`, 핵심 필드별 `non_null`·`share`, 행 0이면 `share=null`). 이 줄은 등록 판단용 읽기 값이며 쓰기 경로와 게이트를 바꾸지 않는다. 재무제표 빌더의 설정되지 않은 DAG 분기는 `--estimate-only`이고 DART 를 호출하지 않는다.

`--keep-trading-days N`(스크리너 CLI, 기본 생략)은 빌드 성공 뒤 같은 시장의 **최신 N개 서로 다른 snapshot_date** 밖 행을 지운다(`app/services/invest_screener_snapshots/retention.py`의 순수 컷오프, `app/jobs/invest_screener_snapshots.py::prune_snapshot_history`). dry-run 은 삭제 대상 개수만 세고, 커밋은 배치 삭제이며 컷오프를 삭제 전에 고정한다. 날짜가 N개 미만이면 아무것도 지우지 않는다. 읽기 경로(`latest_partition`)는 최신 날짜만 쓰므로 보관 뒤에도 같다. 보관은 `invest_screener_snapshots` 한 테이블에만 쓰며 다른 snapshot 테이블과 스케줄러 등록은 없다. DAG 등록과 채움률 기준은 `docs/runbooks/snapshot-refresh-dagu.md`를 따른다.
### 비가격 이력 정리 (hk 5751)

운영자 결정 `decision/2026-11-20/at-direction-daily-plan-nonprice-go` 항목 5에 따른 정리 계약이다.
CLI는 `scripts/prune_nonprice_history.py` 하나이며 정본 설명은 `docs/runbooks/nonprice-history-prune.md`다.

- **삭제 허용 목록은 닫혀 있다**: 뉴스 피드 20개(`NEWS_RETIRED_FEEDS`), 시장 이벤트 트리플 3개(`MARKET_EVENT_RETIRED_TRIPLES`),
  Toss `market_cap` 최신이 아닌 행. 남기는 목록(`naver_item_news`·`finnhub_company_news`·`finnhub_general_news`·
  `research_on_demand_naver`·`research_on_demand_finnhub`·`dart`·`finnhub`·`naver_finance`·`tvscreener`과 Toss 최신 행)과
  뉴스 피드 삭제 목록이 남기는 뉴스 피드와 겹치면 모듈 로드가 실패한다.
- **보존 링크가 있는 기사는 지우지 않는다**: 삭제 피드 기사라도 보존 피드(`naver_item_news` 등)의 `symbol_news_relevance` 연결이 있으면 보호되며 미리보기 `protected_by_retained_feed_link`로 보고된다. `symbol_news_store`가 URL 충돌 때 첫 기사 행을 유지하고 연결에 현재 피드를 기록하기 때문이다.
- **배치 상한**: `MAX_BATCH_SIZE`는 10,000이다(asyncpg 바인드 파라미터 상한 32,767 아래). 배치 행 수 불일치는 트랜잭션 안에서 검사해 그 배치를 롤백한다.
- **보류 피드 `http_naver_stock_aggregate`**: spike attribution 의 유일한 KR 정확 시각 근거(`KR_EXACT`)이고 backfill 대상이다.
  `--allow-held-feed` 로 명시하지 않으면 커밋에서 빠지고 결과에 `held_news_feeds_kept`로 보고된다.
  `--allow-held-feed`에는 보류 피드 이름만 받는다(오타·보존 피드는 입력 오류).
- **의존 행은 같은 배치 트랜잭션에서 먼저 지운다**: `symbol_news_relevance`, `news_article_related_symbols`,
  `news_analysis_results`(스키마상 CASCADE이지만 명시 삭제해 건수를 남긴다). 시장 이벤트는 `market_event_values`와
  `market_event_ingestion_partitions`를 같은 트리플로 지운다. 밸류에이션은 의존 행이 없다.
- **배치와 재실행**: 배치마다 자기 트랜잭션, 커서는 `id` 오름차순, 선택 수와 삭제 수가 다르면 그 배치는 롤백하고 실패한다.
  두 번째 실행은 아무것도 지우지 않는다.
- **읽기 경로 판정**: `get_news`의 정규 읽기는 `feed_source` 필터 없이 `symbol_news_relevance`를 최신순으로 읽는다.
  삭제 피드의 연결 행은 해당 종목의 `get_news`에서 사라진다(설계 세션 결정 대기, 미리보기 `by_feed` 건수로 확인).
  `get_latest_news_preview`는 시간 창 없이 최신 기사를 고르므로 삭제 피드만 남은 실행 소스에서는 빈 미리보기가 된다.
- **Toss 시가총액은 최신만**: `MarketValuationSnapshotsRepository.prune_superseded`가 커밋 때 같은 트랜잭션에서 써 넣은
  심볼의 더 오래된 `toss_openapi` 행을 지운다. 다른 소스의 행은 건드리지 않으며, `latest_for_symbols`·`coverage_counts`
  같은 최신 기준 읽기는 그대로 동작한다. 판정식은 CLI와 writer가 같은 `superseded_by_newer_row`다.
- **시장 이벤트 판정**: `forexfactory`·`wisefn`은 `expected_sources`의 수동 전용 트리플이라 freshness에 영향이 없다.
  `tradingview` 경제 이벤트는 2026-06-10 이후 수집이 멈췄다. 삭제 뒤 `economic/global` 캘린더 행은 비게 된다.

### 크립토 레짐 게이트 입력 — 김치 프리미엄·funding 정산 이력 (hk 5889, 5852 T3)

`crypto_insight_snapshots`에 두 provider를 기존 서비스 경유로만 쓴다. 새 writer 없음, 마이그레이션 없음.

- **`upbit_binance` / `kimchi_premium` (BTC, unit `%`)**: Upbit `KRW-BTC` 체결가, Binance `BTCUSDT` 현물 가격, open.er-api USD/KRW 세 다리가 모두 유한 양수로 읽혀야 행을 만든다. 하나라도 빠지면 행 없이 `warnings`만 남는다(unknown은 0이 아니다). USD/KRW 폴백(`USD_KRW_FALLBACK_RATE`)은 쓰지 않는다. `snapshot_at`은 세 다리를 읽은 실행 시각이다.
- **`binance_settlement` / `funding_settlement_rate` (BTCUSDT, unit `ratio`)**: Binance USD-M `fundingRate?limit=3`의 정산 3회를 정산 시각(`snapshot_at`)마다 한 행으로 쓴다. 정확히 3회·중복 없음·모든 필드 판독 가능일 때만 쓰고 하나라도 어긋나면 전부 생략한다(2회 평균을 3회처럼 쓰지 않는다). 기존 `funding_rate`(premiumIndex 현재값) 행과 metric 이름이 달라 서로 덮어쓰지 않는다.
- **기본 잡은 불변**: `DEFAULT_PROVIDERS`·`DEFAULT_FLOW_PROVIDERS`에 넣지 않았다. 이 두 provider는 `deploy/dagu/at-gate-inputs/at-t3-crypto-gate-inputs.yaml`(PROPOSAL, 미등록)에서 `CRYPTO_INSIGHT_PROVIDERS_JSON`으로만 켜진다. 커밋은 기존 `CRYPTO_INSIGHT_COMMIT_ENABLED`가 정한다.
- **테스트**: `tests/test_crypto_insight_external_adapters.py`(파싱·누락=unknown), `tests/scripts/test_dagu_gate_inputs_dag.py`(DAG 정적 검사, 기본 제공자 집합 고정).

**시각 분리와 cutoff 규칙 (hk 5932, dots 리뷰 B2)**: 두 provider 의 모든 행은 `raw_payload["timing"]`에 세 시각을 따로 싣는다 — `event_at`(값이 가리키는 경제 시각: funding=정산 시각, kimchi=입력 중 가장 늦은 시각), `source_at`(출처가 말한 생성 시각, 출처가 주지 않으면 null이며 우리 시계로 채우지 않는다), `first_available_at`(우리가 처음 받은 실제 시계; 주입 가능한 `now`가 아니므로 backfill 실행이 더 이른 시각을 주장할 수 없다). 🔴 값은 cutoff 에서 `event_at`과 `first_available_at`이 **둘 다** cutoff 이하일 때만 쓸 수 있다(`app/services/crypto_insight_snapshots/availability.py`, `list_usable_at`). timing 이 없거나 읽을 수 없는 행은 쓸 수 없다. timing 행은 저장소에서 first-write-wins(삽입 전용)이며 같은 식별키의 이후 기록은 값·시각을 바꾸지 못하고 값이 다르면 경고 로그만 남는다. 스키마·마이그레이션 변경은 없다(JSONB 안의 키; 컬럼 승격은 별도 결정).
- kimchi: 입력별 `inputs.<leg>.source_at`/`source_time_basis`를 남긴다(Binance 현물 가격은 시각을 주지 않아 요청 시각을 상한으로 쓰고 `source_at=null`). Upbit 마지막 체결이 120초보다 오래됐거나, FX 발행이 36시간(FX 휴장 금 22:00~일 22:00 UTC 구간에서는 84시간)보다 오래됐거나, 출처 시각이 수신보다 5초 넘게 미래이거나, 출처 시각을 읽을 수 없으면 행 없이 경고만 남는다. `timing`에 `max_skew_seconds`·`spot_skew_seconds`·`fx_age_seconds`·`fx_weekend_carry`와 가정 문구 `usdt_usd_assumption`(USDT=1 USD, USDT/USD 환산 없음)을 기록한다. FX 출처는 open.er-api(`time_last_update_unix`)다. `snapshot_at`은 주입된 `now`가 아니라 `event_at`이다.
- funding: 행마다 `settlement_period_seconds`를 기록하고, 정확히 3회가 단일 정산 주기(1·2·4·8시간, ±60초)로 연속일 때만 쓴다 — 주기가 창 안에서 바뀌면 행 없이 경고(`select_funding_window`는 저장된 행에서 같은 판정으로 `missing`/`period_changed` 사유를 돌려준다). 정산율 0 은 실제 값(`value_state="zero"`, 라벨 neutral)이고 필드 누락·빈 값·비수치는 행이 없다.
- **테스트**: `tests/test_crypto_gate_input_timing.py`(cutoff 이후 도착·누락 뒤 backfill·정산 주기 변경·stale 출처·0 대 누락·삽입 전용). 게이트 판정 로직·DAG 등록·주문은 이 변경의 범위 밖이다.

### 세션 분봉 타임라인 — research 분봉 수집·커버리지 진단 (hk 6145)

설계 `hk:doc design/2026-11-24/at-session-timeline` §4, 결정 `decision/2026-11-24/at-session-timeline`. KR·US 닫힌 1분봉을 세션 표지와 **최초 수신 시각**과 함께 쌓는 1단계다. 🔴 `public.*_candles_1m` 과 그 writer 는 건드리지 않는다(서빙 캐시: upsert 가 덮고 90일 보관). 스케줄러 등록 없음(하드룰 6): DAG 파일은 사본이고 등록은 머지·마이그레이션 적용 뒤 설계 세션이 한다.

- **표**: `research.kr_candles_1m`(기존, `session_segment` CHECK 에 `KRX_AFTER` 추가) · `research.us_candles_1m`(신규: `time_utc`=봉 시작, `session_date`=ET 거래일(20:00 ET 롤오버, `us_trading_day_window`), `symbol`=DB 점 표기, venue NASD|NYSE|AMEX|TOSS_INTEGRATED(hk 6322), `session_segment` US_PRE|US_REGULAR|US_POST|US_DAY|UNKNOWN, source KIS|TOSS|YAHOO|UNKNOWN, `retrieved_at`, `batch_id`, PK `(time_utc, symbol, venue)`). US 연속 집계 `research.us_candles_{5m,15m,30m,1h}`(bucket 시작, `GROUP BY bucket, symbol, venue, session_segment` 라 버킷이 세션을 넘지 않는다)는 timescaledb 가 있을 때만 생긴다. 보관 정책·refresh 정책 없음. 마이그레이션 `20261124_hk6145_session_minutes`(추가형; downgrade 는 us 행 또는 `KRX_AFTER` 행이 있으면 거부). 하드룰 18: `at_app` 조건부 GRANT(두 research 분봉 표 SELECT·INSERT, schema USAGE).
- **세션 표지**: `app/services/research_candles/session_segment.py::session_segment_of(market, venue, bar_start_utc)` — 시장 무관 순수 함수(hk 6148 이 재사용). KR 경계는 `live_order_expiry` 의 `KR_*_KST`/`KRX_AFTER_*_KST`, US 는 `market_session.us_market_session`(XNYS 정규장·조기 폐장, 04:00–20:00 ET), 거래일 판정은 `session_calendar.is_trading_session`(fail-closed). 시작 포함·끝 배타: KRX 09:00–15:30 `KRX_REGULAR`, 16:00–20:00 `KRX_AFTER`; NTX 08:00–08:50 `NXT_PRE`, 09:00–15:20 `NXT_OVERLAP`, 16:00–20:00 `NXT_POST`(hk 6298: 공식 시간. 15:20–15:40 은 NXT 연속매매가 없고 15:40–16:00 은 KRX 종가매매와 시각으로 구분되지 않으며 KIS 분봉 frame 에 체결 구분 필드가 없어 `UNKNOWN`); 그 밖·휴장일은 `UNKNOWN`(저장하되 추측하지 않는다). 예상 분 분모는 이 표지에서 나오므로 `UNKNOWN` 분은 결손으로 세지 않는다. 기존 저장 행은 재라벨하지 않는다. US 20:00–04:00 ET 는 달력 없이는 `UNKNOWN` 이다. 토스 US 달력(hk 6322)이 있을 때만 dayMarket 창 안의 봉이 `US_DAY` 가 된다(KIS 행 포함). 달력이 없으면 추측하지 않는다.
- **수집기**: `app/services/research_candles/session_minutes_collector.py` + 수동 CLI `scripts/collect_session_minutes.py --market kr|us [--symbols …] [--dry-run]`. KIS 분봉은 **시작 라벨·현지 시각**(KR=KST, US=ET)이고 형성 중인 분을 포함한다. 🔴 `bar_start + 1분 + 10초 <= 요청 직전 시각` 인 닫힌 봉만 쓴다. 삽입은 `ON CONFLICT (time_utc, symbol, venue) DO NOTHING` 뿐이라 재실행은 0행이고 `retrieved_at`(최초 수신)은 바뀌지 않는다. KR 은 KIS `J`(venue KRX)와 `NX`(venue NTX, `kr_symbol_universe.nxt_eligible` 종목만), US 는 해외 분봉(거래소는 `us_symbol_universe`). KIS 객체는 읽기 4종(`inquire_minute_chart`, `inquire_overseas_minute_chart`, `fetch_my_stocks`, `fetch_my_us_stocks`)만 쓰고 주문·원장·승인 모듈을 import 하지 않는다(정적 테스트). 출력 JSON: `inserted`/`already_present`/`skipped_forming`/`errors`(+`targets_by_origin`, `targets_truncated`); 오류가 있으면 종료 코드 1. 소급 채움 없음. KR `value`: KIS 분봉의 `acml_tr_pbmn` 은 그날의 **누적** 거래대금이라, 봉 단위 값은 **증명될 때만** 저장한다 — 같은 KST 일의 직전 행(같은 프레임)과의 차이이거나, 정확히 venue 의 그날 첫 분(KRX 09:00·NTX 08:00)의 누적값 자체. 그 밖에는 `value` 를 NULL 로 두고 봉은 유지한다(마이그레이션이 `research.kr_candles_1m.value` 의 NOT NULL 을 해제; 모르는 값은 0도 추정도 아니다). 프레임 길이로 장 시작 도달을 추론하지 않는다. 누적값이 줄어든 행과 그 다음 행, 유효하지 않아 버려진 행(또는 값을 읽을 수 없는 행) 바로 뒤 행도 NULL 이다. 수집 JSON 의 `null_value` 와 진단의 세션별 `value_null`/`value_null_ratio` 로 센다. 🔴 기존 KR 연속 집계(`research.kr_candles_{5m,15m,30m,1h}`)의 `SUM(value)` 는 NULL 을 무시하므로 1분봉 `value` 가 NULL 인 분이 있으면 그 버킷 값은 **부분합**이다(cagg 정의 변경은 이 PR 범위 밖). US `value` 는 봉 단위 그대로다. 수집기·진단 모듈의 import 폐쇄는 `execution_ledger` 패키지에 닿지 않는다(정적 테스트). `research.kr_candles_1m` 의 `session_segment` CHECK 는 deep-history 하이퍼테이블 스캔을 피하려 NOT VALID 로 넓힌다.
- **대상 선정 (hk 6299, 단계별 예산 — 우선순위 순)**: ① **고정** = KIS·수동 보유 전부 + 고정 목록 (`SESSION_MINUTES_FIXED_SYMBOLS_KR/US`, 기본 KR `069500,229200` · US `SPY,QQQ,SMH`, 예산 `SESSION_MINUTES_BUDGET_FIXED` 50). 고정 단계는 **절대 자르지 않는다**(예산보다 많아도 전부 받고 아래 단계가 줄어든다). ② **장중 승격** = 당일 `invest_momentum_event_snapshots` 최초 포착(hk 6154 KR, hk 6510 US — 시장별 `market`·거래일은 KR=KST 날짜, US=ET 거래일)을 최초 포착 시각 → 순위 순으로, 예산 `SESSION_MINUTES_BUDGET_INTRADAY` 40. 당일 분봉이 이미 있는 종목은 예산에서 보호되어 같은 날 내려가지 않는다. ③ **장전 스크리너** = `preset_definitions` 중 `availability == "active"` 프리셋마다 상위 `SESSION_MINUTES_SCREENER_TOP_N`(기본 20)의 합집합(최고 순위 순), `screen_stocks_snapshot_impl(limit=N)` — DB 전용·HTTP 0회 — 예산 `SESSION_MINUTES_BUDGET_SCREENER` 60. ④ **pick log** = 당일 `review.screener_pick_log` `admitted`(KR=KST 당일, US=최근 24시간)의 나머지. 쓰지 않은 예산은 다음 단계로 넘어가고, 전체는 `SESSION_MINUTES_MAX_SYMBOLS_KR/US`(기본 150)로 상한을 건다. US 에는 ② 가 없다. 어느 단계도 뽑지 않은 종목만 `targets_cuts`(symbol·tier·reason `budget_cut`)로 보고하고, 단계별 `targets_tiers`(budget·allowance·candidates·selected·cut)를 함께 낸다. 0행 요청 종목의 사유는 진단이 `fetch_error`·`no_trades_reported`·`cut_by_budget`·`not_selected` 네 닫힌 코드 중 하나로 붙인다(수집 JSON 을 `--collect-report` 로 주면 선택적으로 붙는다. 우선순위는 오류 > 예산 잘림 > 대상 > 미선택이며, `dry_run` 보고는 근거가 아니다). 🔴 스냅샷 호출은 impl 직접 호출이라 pick log 를 쓰지 않는다(하드룰 13: 관측자 배선은 등록 계층 `analysis_registration` 뿐, 테스트로 단언). `--symbols` 는 이 합집합을 대체한다.
- **진단**: `scripts/diagnose_session_coverage.py --market kr|us --date D [--symbols …]` — 읽기 전용 트랜잭션(`SET TRANSACTION READ ONLY`), 8초 `statement_timeout`, SELECT 뿐. 종목 × venue × session_segment 별 `rows`/`expected_minutes`(그 시각에 이미 닫힌 분)/`missing_minutes`/`retrieved_at − (time_utc + 1분)` p50·p95. `batch_id` 가 `%backfill%` 또는 `promote-%` 인 행(최초 수신이 아님)은 제외한다. 계산은 `session_coverage.py`(순수).
- **범위 밖**: 선물, `market_timeline` 이벤트, retro/v1, 급등 귀속, DART·뉴스 시각, KRX 애프터 적격 목록 갱신, 주문·승인·정책 경로, DAG 등록, 과거 백필.
- **DAG 사본**: `deploy/dagu/at-session-minutes/`(KR 월–금 08:00–20:10 KST 5분, US 월–금 04:00–20:10 ET 5분 — KST 로 환산하면 서머타임에 따라 17:00/18:00–09:10/10:10). **테스트**: `tests/services/research_candles/test_session_{segment,minutes_collector,minutes_targets,coverage,minutes_db}.py`, `tests/test_hk6145_session_minutes_migration_db.py`(DB 는 CI), `tests/scripts/test_dagu_session_minutes_dags.py`. 런북 `docs/runbooks/session-minutes.md`.

### 미국 데이마켓 Toss 1분봉 — `TOSS_INTEGRATED` venue·달력 기반 `US_DAY` (hk 6322)

런북은 `docs/runbooks/session-minutes.md` §3b 다. 🔴 토스 데이마켓은 통합 피드라 실행 venue 를 모르므로 `NASD`/`NYSE`/`AMEX` 로 쓰지 않고 venue `TOSS_INTEGRATED` 로 쓴다(`research.us_candles_1m` venue CHECK 추가형 마이그레이션 `20261125_hk6322_us_toss_venue` NOT VALID, 후속 `20261125_hk6322_us_venue_val` VALIDATE; `value` NOT NULL 해제). 토스 행의 `source` 는 `TOSS`, `value` 는 NULL 이다(봉별 거래대금 없음).

- **봉 시작**: 토스 `timestamp` 는 봉의 끝이므로 저장 시작은 `timestamp − 1분`이고 같은 닫힌 봉 규칙(`is_closed`)을 쓴다. 형성 중인 봉은 `skipped_forming` 으로 건너뛴다. 클라이언트 `TossReadClient.collect_minute_candles(adjusted=False)` 를 쓴다(수정주가 아님).
- **달력**: `get_toss_market_calendar("us", today_kst)` 를 실행당 한 번 받는다. `session_segment_of(..., us_calendar=…)` 는 달력 창(절대 시각)으로만 라벨을 붙인다. dayMarket 창 안 → `US_DAY`(모든 venue; KIS 행도 달력이 있으면 같다), 토스 행은 그 밖에 pre/regular/after 창이면 그 라벨, 어느 창에도 없으면 `UNKNOWN`(저장하되 추측하지 않는다). 달력이 없으면 `TOSS_INTEGRATED` 는 `UNKNOWN` 이고 KIS 라벨은 바이트 동일하다(`us_calendar=None` 기본값). 휴장일(모든 세션 null)은 창이 없으므로 `UNKNOWN`.
- **대상·예산**: 기존 US 단계(고정 SPY/QQQ/SMH·보유·스크리너)와 같다. 토스 읽기는 달력 창이 `[현재 − 3시간, 현재]` 와 겹칠 때만 하고, 그 밖이면 `toss: no_window` 로 보고한다. KIS 미국 읽기는 달력 dayMarket 창이 열린 뒤 15분(`KIS_DAY_MARKET_GRACE`)이 지나면 건너뛴다(`kis_skipped_day_market`). 그 전에는 KIS 가 마지막 봉을 읽는다.
- **재실행**: `ON CONFLICT (time_utc, symbol, venue) DO NOTHING` 이므로 같은 토스 봉의 재삽입은 0행이고 `retrieved_at`(최초 수신)은 바뀌지 않는다(CI DB 테스트 `tests/services/research_candles/test_us_toss_day_market_db.py`). 마이그레이션 `downgrade` 는 `TOSS_INTEGRATED` 행이나 NULL `value` 행이 있으면 거부한다.
- **확인**: `scripts/diagnose_session_coverage.py --market us --date D --first-last` 가 세션 세그먼트·출처·venue 별 첫 봉과 마지막 봉(봉 시작, UTC·KST)을 한 줄로 보인다(읽기 전용).
- **범위 밖**: `get_quote` 와 Factortrail, KR 세션, 선물. DAG 는 사본만 갱신하며 등록하지 않는다. 토스 행의 `expected_minutes` 계산은 없다(KIS 표지만 기대 분을 센다).
- **테스트**: `tests/services/research_candles/test_us_toss_day_market.py`(순수: 라벨·변환·달력·수집기 게이트·마이그레이션 형태, 로컬), `..._db.py`(마이그레이션 왕복·재실행 0행·하향 거부, CI).

### research 분봉 HTTP ingest·대상 읽기 (hk 6343, 설계 hk 6321 T2)

Go 시세 수집기는 DB 에 직접 쓰지 않는다. 닫힌 1분봉은 토큰 인증 HTTP 표면으로 보내고 서버가 기존 writer 로 쓴다. 표면은 `/trading/api/research-minutes/` 아래 둘이며 `app/routers/research_minutes_ingest.py`·`app/schemas/research_minutes_ingest.py`·`app/services/research_candles/minute_ingest.py` 에 있고, 게이트·토큰 검사는 `AuthMiddleware` 의 전용 분기다(게이트 `RESEARCH_MINUTES_INGEST_ENABLED` 는 정확히 `"true"` 일 때만 열리고, 꺼져 있으면 어떤 경로든 DB 접근 전에 404, 토큰 `RESEARCH_MINUTES_INGEST_TOKEN` 이 비면 403, 틀리면 401 이며 세션 쿠키는 대체가 아니다). `POST .../ingest` 는 한 시장의 배치(최대 1,000봉)를 받아 봉마다 자기 SAVEPOINT 로 `SqlMinuteWriter.insert_one` 을 실행한다 — `ON CONFLICT (time_utc, symbol, venue) DO NOTHING` 이므로 재전송은 0행이고 `retrieved_at`(최초 수신)은 먼저 도착한 쪽이 고정한다. 응답 `results[]` 는 요청 `bars[]` 와 위치로 대응하며 `inserted`/`already_present`/`rejected`(사유는 닫힌 어휘 또는 쓰기 실패의 예외 클래스 이름)만 담는다. 서버가 소유한 필드는 클라이언트에서 받지 않는다: `session_segment` 는 `session_segment_of`, `session_date`/`session_date_kst` 는 `time_utc` 에서 유도하고, `received_at` 은 시간대가 있어야 하고 서버 시계 +5초를 넘는 미래이거나 봉 시작보다 이르면 그 항목을 거부한다(재시도·백필이 더 이른 수신을 주장할 수 없다). 🔴 닫힌 봉만 저장한다 — 수집기와 같은 `is_closed(bar_start, min(received_at, now))`(시작 + 1분 + 10초)를 통과하지 못하면 `bar_not_closed` 로 항목 거부하고, `time_utc` 는 정렬된 분이어야 하며 2000-01-01 UTC 이전(Go zero time)·오버플로 시각(예: `9999-12-31T23:59:59-01:00`)은 항목 단위 `bar_time_invalid`/`received_at_invalid` 다(형성 중 봉이나 영구 partial 행을 남기지 않는다). `source` 는 시장별 닫힌 집합(KR `KIWOOM|KIS|TOSS|UNKNOWN`, US `KIS|TOSS|YAHOO|UNKNOWN`)이며 이는 두 표의 CHECK 와 같다 — 새 소스는 그 소스 티켓의 추가형 마이그레이션 없이는 받지 않는다(이 표면에는 마이그레이션이 없다). `GET .../targets?market=kr|us` 는 읽기 전용으로 `build_targets`(hk 6299 단계·예산)와 `targets_report` 를 그대로 돌려주므로 계층 규칙의 정본은 Python 하나다. 정적 테스트 `tests/routers/test_research_minutes_ingest_static_guard.py` 가 라우터(함수 내부 import 포함)와 모듈 수준 import 폐쇄에서 상대 import·부모 패키지를 해석해 주문·원장·승인·슬리브·판단근거 모듈의 도달을 검사하고, writer SQL 이 두 research 표만 건드림을 고정한다. 리뷰 예외 하나를 명시한다: targets 의 KIS 보유 읽기는 `app.services.brokers.kis` 패키지를 거치는데 그 `__init__` 이 주문 클라이언트를 import 하므로(hk 6145 `session_segment` 가 이미 같은 패키지의 `live_order_expiry` 에 의존한다) 그 루트를 경유한 도달만 허용 목록으로 인정하고, 라우터는 KIS 클라이언트를 `_HoldingsOnlyReader`(`fetch_my_stocks`/`fetch_my_us_stocks` 두 메서드만)로 감싼다. KIS 루트 밖에서 도달하는 주문 모듈은 테스트 실패다. 스케줄러·과거 백필·Go 클라이언트·주문 경로는 범위 밖이고, 테스트는 `tests/routers/test_research_minutes_ingest{,_auth,_db,_static_guard}.py`·`tests/services/research_candles/test_minute_ingest.py`(DB 는 CI)다.

**선물 market (hk 6424)**: `market="futures"` 항목은 같은 게이트·토큰·항목별 결과 모양으로 `research.kr_futures_candles_1m`에만 쓰며, 쓰기는 `ON CONFLICT (time_utc, symbol) DO NOTHING`이고 재전송은 0행이다(`retrieved_at`은 첫 수신). `symbol`은 `<PRODUCT>.<YYMM>` 정본이며 `product`·`contract_month`(YYMM)·`roll_state`·`session_segment`·`session_date`는 서버가 같은 Python 규칙(`parse_futures_contract_symbol`, `roll_state`, `session_segment_of("futures", …)`, `futures_session_date`)으로 다시 계산한다. 보내온 값은 선택 필드이며 서버 값과 다르면 항목별 `*_mismatch`로 거부하고, 서버는 그 값을 채택하지 않는다. `roll_state`의 앞/다음 판정에는 종목마스터의 상장 결제월이 필요하다: 공개 마스터(`fetch_master_rows`, 자격증명·선물 시세 게이트 없음)를 프로세스 안에서 1시간 캐시하며, 읽지 못하면 선물 항목은 `master_unavailable`로 거부한다(상태를 추측해 저장하지 않으며 재전송 가능). `venue`는 `KRX`만 받고 `value`는 없어야 한다(`field_not_applicable`). 주식 시장(kr/us) 항목에 선물 전용 필드가 실리면 같은 사유로 거부한다.

### 장중 급등·급락 관측 로그 (hk 6154)

잠들어 있던 ROB-222 순위 스냅샷(`invest_momentum_event_snapshots`)을 **삽입 전용 관측 로그**로 되살린다. 🔴 주문·승인·정책 경로와 무관하고, 스케줄러 등록은 없다(하드룰 6: DAG 는 `deploy/dagu/at-intraday-movers/` 사본, 등록은 설계 세션). 1단계 = KRX 단독. NXT(2단계)·테마 이벤트(`upsert_theme`)는 범위 밖이다. 미국(3단계)은 hk 6510 이 아래 절로 더했다. 런북 `docs/runbooks/intraday-movers.md`.

- **삽입 전용**: `InvestMomentumEventSnapshotsRepository.insert_momentum` 은 `ON CONFLICT DO NOTHING` 이다(전 이름 `upsert_momentum` 은 덮어썼다). 같은 `(surface, snapshot_at, trade_type, market_type, order_type, symbol)` 재삽입은 값·행 수를 바꾸지 않는다. 기존 읽기(`query_service`/`coverage_service`/`list_*`) 출력 형태는 그대로다(행이 덮이지 않을 뿐). 🔴 하락(`down`) 행은 `list_candidate_signals`(조기 포착 후보 점수)에서 제외한다(`NON_CANDIDATE_ORDER_TYPES`) — 최신 스냅샷 선택·이전 순위·점수 모두. 하락 종목이 후보로 오르거나 하락만 있는 최신 스냅샷이 이전 후보를 가리지 않게 한다.
- **면**: `stock:KRX:ALL:{up,down,priceTop}` = 상승·하락·거래대금. 하락 순위는 네이버 `orderType=down` 이다(2026-11-24 공개 GET 확인: HTTP 200, 같은 응답 모양, 음수 `prevChangeRate`; `low`/`lowTop`/`downTop`/`fall` 은 HTTP 400). 픽스처 `tests/fixtures/naver_stock/`.
- **상태 표 `invest_momentum_event_runs`** (마이그레이션 `20261124_hk6154_momentum_runs`, 추가형, `at_app` SELECT·INSERT 조건부 GRANT): (`run_id`, `surface`) 당 한 행, `status ∈ ok|empty|error` + 닫힌 `reason`(`empty`=`no_rows`; `error`= `http_4xx|http_5xx|timeout|transport|bad_response|unparseable|unexpected`), `row_count`. DB CHECK 가 조합을 강제한다. 🔴 `error`/`empty` 는 순위 행을 쓰지 않고, 예외 문자열은 저장하지 않는다. 한 면의 순위 행과 상태 행은 한 트랜잭션이며 상태 행을 먼저 넣고 이미 있으면(같은 `snapshot_at` 재실행) 그 면은 아무것도 쓰지 않는다. `run_id` 는 `snapshot_at` 에서 결정적으로 유도한다.
- **세션 가드**: KRX 정규장 `[09:00, 15:30)` KST AND XKRX 거래일(`is_trading_session`, fail-closed)일 때만 가져오고 쓴다. 아니면 `outside_session` 으로 끝난다. 쓰기는 CLI `--dry-run` 이 아니고 `INVEST_MOMENTUM_EVENTS_COMMIT_ENABLED`(기본 false, 이 PR 은 켜지 않는다)가 true 일 때만 일어난다.
- **최초 포착**: `intraday.first_captures(session, trading_date, market="kr")` 는 읽기 시점 계산(새 표 없음)이다. 종목별 그날 가장 이른 `snapshot_at` 의 면·순위·등락률·거래대금, 동률은 순위→order_type→trade_type→market_type 순. 후속(hk 6171 retro 타임라인)이 읽는 평범한 읽기 함수다.
- **6145 연동**: 수집 대상 출처 `intraday_movers`(KR 최초 포착 종목 — US 는 hk 6510 절 참조)가 `TARGET_ORIGIN_ORDER` 끝(pick log 다음)에 있어 상한에서 가장 먼저 잘린다. 조회 실패는 다른 출처처럼 `error_details` 에 보고되고 실행은 계속된다. 🔴 KIS `inquire_minute_chart` 는 `n=200` 이어도 호출당 약 30봉만 주므로, 오늘 분봉 행이 없는 새 종목은 `end_time`(`FID_INPUT_HOUR_1`, `HHMMSS`) 커서로 한 페이지를 더 읽어 합친다(`backfill_symbols`/`backfill_pages`). `end_time` 은 `inquire_minute_chart` 의 추가 선택 인자(기본 동작 불변)다. 추가 페이지 실패는 첫 페이지를 지우지 않는다.
- **금지**: 이 경로가 `review.screener_pick_log` 에 쓰는 것(하드룰 13), 주문·원장·승인·브로커 변경 모듈 import(정적 테스트), 스케줄러 등록, 상태 없는 빈 순위를 성공으로 기록하는 것.
- **테스트**: `tests/services/invest_momentum_events/test_intraday.py`(순수), `test_intraday_db.py`(CI DB), `tests/services/research_candles/test_session_minutes_{targets,earlier_page}.py`, `tests/scripts/test_dagu_intraday_movers_dag.py`.

### 미국 장중 급등·급락 관측 로그 (hk 6510)

DELL·URBN 이 프리마켓(04:xx ET) 급락 목록에 있었지만 그 세션의 분봉 수집 대상이 아니어서 나중에 재생할 수 없었다(dots, hk 6039 comment 33778). 미국판은 hk 6154 와 **같은 표·같은 삽입 전용 규율**을 쓰고 소스만 둘이다. 🔴 주문·승인·정책 경로와 무관하고, 스케줄러 등록은 없다(하드룰 6: DAG 사본 `deploy/dagu/at-intraday-movers/at-us-intraday-movers.yaml`, 등록은 설계 세션). 런북 `docs/runbooks/intraday-movers.md`.

- **소스(둘 다 읽고 면마다 어느 소스가 답했는지 남긴다)**: ① `alpaca_screener` — paper-data 자격증명(`ALPACA_DATA_API_KEY_ID`/`ALPACA_DATA_API_SECRET_KEY`, `scripts/mock_data_refresh` 와 같은 쌍)으로 `https://data.alpaca.markets` 의 `GET /v1beta1/screener/stocks/movers`(gainers/losers, `top`)·`GET /v1beta1/screener/stocks/most-actives`(`top`). 문서상 movers 는 **개장 시 리셋되어 그 전에는 전일 목록**을 준다(docs.alpaca.markets/us/reference/movers-1, 2026-11-25 확인) — 그래서 응답의 `last_updated` 를 행 `raw_payload.response.last_updated` 와 dry-run 요약 `source_updated_at` 에 그대로 남기고 프리마켓 움직임으로 주장하지 않는다. ② `kis_overseas_ranking` — 해외주식 상승률/하락률 TR `HHDFS76290000`(`/uapi/overseas-stock/v1/ranking/updown-rate`, 공식 샘플 `examples_llm/overseas_stock/updown_rate/updown_rate.py`; EXCD=NAS/NYS/AMS, NDAY=0, GUBN=1 상승·0 하락, VOL_RANG=1). 한 면(상승 또는 하락)은 세 거래소를 모두 읽고 **하나라도 실패하면 그 면 전체가 `error`**(부분 순위를 완전한 순위처럼 쓰지 않는다). Alpaca 호출은 정확한 호스트·`follow_redirects=False`·`application/json` 만 받는다. 🔴 사이트 스크레이핑 없음.
- **면·세션·행**: 면은 `us:<source>:<session>:<side>`, `session ∈ us_pre|us_regular|us_post`(04:00 ET–개장 / 개장–마감 / 마감–20:00 ET, `session_segment_of("us", …)` = 6145 와 같은 창), `side ∈ up|down|active`. 행은 `market='us'`, `trade_type='US'`, `market_type` = KIS 거래소 코드(`NAS`/`NYS`/`AMS`) 또는 Alpaca `US`, `order_type`=side, `trading_date` = **ET 거래일**. `source` 는 `alpaca_screener`/`kis_overseas_ranking`. 기존 표에 세 열을 더한다(추가형): `asset_type`(닫힌 `common_stock|etf|leveraged_etf|right_or_warrant|unknown`), `first_received_at`(그 행을 처음 받은 시각, 삽입 전용), `field_reasons`(제공자가 주지 않는 필드의 닫힌 사유). KR 행은 세 열이 NULL 이고 바이트 동일하다(마이그레이션 `20261125_hk6510_us_movers` + `_val`, market/source CHECK 확장은 NOT VALID 추가 후 VALIDATE, `at_app` grant 변경 없음, downgrade 는 us 행이 있으면 거부).
- **필드**: `price`·`change_rate`(전일 정규장 종가 대비)·`volume`·`trade_value`(달러 거래대금). 제공자가 달러 거래대금을 주지 않으면 **같은 행의 보고된 `price × volume`**(정확한 Decimal)을 쓰고 `raw_payload.trade_value_basis="price_times_volume"` 로 표시한다(둘 중 하나라도 없으면 NULL + `field_reasons`). `asset_type` 은 `us_symbol_universe`(`security_type`/`leverage_factor`/`is_common_stock`/`name_en`)에서만 유도한다 — 행이 없으면 `unknown`+`not_in_symbol_universe`, 마스터 읽기가 실패하면 `unknown`+`unavailable`, 변환 불가는 `unknown`+`unavailable`. 🔴 `security_type` 이 없을 때 `is_common_stock=False` 만으로 ETF 로 단정하지 않는다: 그 플래그는 우선주·예탁증서·채권·신탁에도 False 이므로 이름이 warrant/right 일 때만 `right_or_warrant`, 그 밖은 `unknown` 이다(fund 증거는 `security_type` 뿐). 어떤 NULL 도 0 으로 채우지 않는다.
- **상태 표**: hk 6154 와 같은 `invest_momentum_event_runs` 에 (`run_id`, `surface`) 한 행. `run_id` 는 `snapshot_at` 의 uuid5(6510 전용 namespace). 닫힌 reason 어휘에 `source_unavailable` 을 더한다(리더·자격증명이 없어 시도조차 못 한 소스 — 빈 순위로 보이면 안 된다). 한 면의 행과 상태 행은 한 트랜잭션이고 상태 행을 먼저 넣으므로 같은 `snapshot_at` 재실행은 아무것도 쓰지 않는다.
- **세션 가드**: 04:00–20:00 ET AND XNYS 거래일에서만 가져오고 쓴다(아니면 `outside_session`, fetch 0). 쓰기는 CLI `--dry-run` 이 아니고 `INVEST_MOMENTUM_EVENTS_COMMIT_ENABLED`(KR 과 같은 게이트, 기본 false)가 true 일 때만.
- **CLI**: `scripts/us_intraday_movers.py`(`--dry-run`). 자격증명이 없는 소스는 그 면들이 `source_unavailable` 이고 종료 코드 1 이다. 상태가 `error` 인 면이 하나라도 있으면 1, `empty` 는 실패가 아니다.
- **6145 연동**: `build_targets(market="us")` 도 `intraday_movers` 단계를 쓴다 — US 최초 포착(`first_captures(market="us")`, ET 거래일)을 최초 포착 시각 → 순위 순으로, 예산 `SESSION_MINUTES_BUDGET_INTRADAY`, 고정 단계는 자르지 않는다. 당일 US 분봉(`research.us_candles_1m.session_date`)이 이미 있는 종목은 예산에서 보호된다. 새로 승격된 종목의 첫 수집은 KIS 해외 분봉 `keyb` 커서로 **세션 시작(04:00 ET)까지** 최대 `US_BACKFILL_MAX_PAGES`(8) 페이지를 더 읽어 합친다(실패한 이전 페이지는 최신 페이지를 지우지 않고 `error_details` 에 남긴다). 🔴 backfill 완료 판정은 **행 존재가 아니라 오늘 그 종목의 가장 이른 봉 시각**이다(`load_earliest_rows_today` vs 04:00 ET): 이전 페이지가 실패해 최신 페이지만 저장된 종목은 다음 실행이 다시 페이지를 읽고, 가장 이른 봉이 04:00 ET 에 닿아야 후보에서 빠진다. `first_captures` 는 US 행에서 저장된 면 문자열을 그대로 돌려준다(세션은 열이 아니다) — KR 출력은 바뀌지 않는다.
- **금지**: `review.screener_pick_log` 쓰기(하드룰 13), 주문·원장·승인·브로커 변경 모듈 import(정적 테스트 — US CLI 만 KIS 읽기 클라이언트를 import 한다), 스케줄러 등록, 빈 순위를 성공으로 기록하는 것, 벤징가·뉴스·스크레이핑.
- **테스트**: `tests/services/invest_momentum_events/test_us_intraday.py`(순수·정적 가드), `test_us_intraday_db.py`(CI DB), `tests/test_hk6510_us_movers_migration.py`, `tests/services/research_candles/test_session_minutes_{tiers,targets,earlier_page}.py`, `tests/scripts/test_dagu_intraday_movers_dag.py`. 회귀 표본 픽스처: `tests/fixtures/invest_momentum_events/`(DELL/URBN + NVDA/AMD/QQQ/SMH).

### 선물 시세 읽기·분봉 수집 — KIS 실전 선물 시세 view + research 선물 분봉 (hk 6148)

설계 `hk:doc design/2026-11-24/at-session-timeline` §1(선물 행)·§5, 결정 `decision/2026-11-24/at-session-timeline` 항목 2·3. 🔴 주문·취소·승인·원장 경로, `app/services/kis_futures_mock/*` 동작(상수·달력은 읽기만), 옵션·주식선물, 5750 overnight_context, retro/v1, Dagu 등록, 과거 백필은 범위 밖이다. 스케줄러 등록 없음(하드룰 6).

- **게이트**: `KIS_FUTURES_QUOTES_ENABLED` 정확히 `"true"`(기본 off, 기존 `KIS_FUTURES_READ_ENABLED` 와 별개). `open_quotes_view()` 가 KIS 클라이언트를 만들기 **전에**, KIS 계층(`futures_quotes.py`)이 토큰 조회 전과 후에 다시 검사한다(꺼지면 `FuturesQuotesGateOff`, CLI 종료 코드 2). 실전 호스트(`https://openapi.koreainvestment.com:9443`)·실전 앱키 네임스페이스만 쓰며 새 자격증명은 없다. 모의 클라이언트·다른 호스트는 요청 전에 거부된다.
- **표면**: `app/services/brokers/kis/futures_quotes.py`(`/uapi/domestic-futureoption/v1/quotations/` 두 경로뿐 — 시세 `inquire-price` TR `FHMIF10000000`, 분봉 `inquire-time-fuopchartprice` TR `FHKIF03020200`, 시장 코드 `F`; 근거: KIS 공식 샘플 `examples_llm/domestic_futureoption/{inquire_price,inquire_time_fuopchartprice}/`와 `examples_user/domestic_futureoption/domestic_futureoption_functions.py`, https://github.com/koreainvestment/open-trading-api) + `KISClient.quote_futures_{master,price,minutes}` + 읽기 view `app/services/kis_futures_quotes/view.py`(`price`·`minute_bars`·`master`·`close` — 종목마스터는 계약월 해석에 필요한 공개 다운로드). `/trading/` 경로·주문 TR·계좌번호는 도달 불가(정적 테스트 `tests/services/kis_futures_quotes/test_view_and_layer.py`가 import·문자열 상수·허용 메서드를 고정한다). `[unverified]` 미니 선물도 시장 코드 `F` 인지, `stck_cntg_hour` 가 시작 라벨인지는 운영자 첫 스모크로 확인한다.
- **🔴 야간은 `unsupported`**: KIS 공식 샘플에 야간 시세·분봉 TR 이 없다(야간 샘플은 잔고·주문·체결·증거금뿐, 야간 시세는 실시간 웹소켓). `minute_bars(session="night")` 는 `FuturesQuotesUnsupported` 이고 수집기 출력은 `night: "unsupported"` 로 이를 드러낸다(0행을 조용히 두지 않는다). 야간 TR 이 문서화되면 별도 PR.
- **닫힌 상품·계약월**(`contracts.py`): KOSPI200 선물(`F200`, 마스터 상품종류 `1`, 이름 `F YYYYMM` — `[unverified]` 레포의 기존 마스터 픽스처 기준)과 미니 KOSPI200 선물(`MF200`, kis_futures_mock 상수 재사용). 근월·차근월은 종목마스터 행(상품종류·기초자산·이름 패턴·단축코드의 연도 숫자·월 일치)에서만 정하고 코드를 추측하지 않는다. 같은 달에 단축코드가 둘이면 그 달은 버린다. `roll_state` ∈ {front, next, expiring, expired}: expired = 최종거래일 다음 날부터, expiring = 최종거래일이 든 주(월–일)의 최종거래일까지, 그 외는 만료 전 계약 중 순서(근월=front, 차근월=next, 셋째 이후는 `ValueError`). 최종거래일 = 둘째 목요일 — kis_futures_mock 달력 범위(2026-11-23~12-30) 안에서는 휴장 보정, **밖에서는 휴장 보정 없는 둘째 목요일**(docstring 에 명시).
- **세션·거래일**(`session_segment.py`, `session_segment_of("futures", …)`·`futures_session_date`, venue 무시, KR/US 출력은 바이트 동일): KST 시작 시각 기준 08:45–15:45 `FUT_DAY`(XKRX 거래일만, `session_date` = 그 날), 18:00–익일 06:00 `FUT_NIGHT`(그 야간이 **시작된 날** D 가 XKRX 거래일일 때만, `session_date` = D 다음 XKRX 거래일 — 금요일 야간 → 월요일, 휴일 전날 야간 → 휴일 다음 거래일), 그 밖·비거래일·달력이 다음 거래일을 확인하지 못하면 `UNKNOWN` + `session_date` NULL(추측 없음). `session_date` 규칙은 시세 행에만 쓴다.
- **표**: `research.kr_futures_candles_1m`(마이그레이션 `20261124_hk6148_futures_min`, 추가형) — `time_utc`(봉 시작)·`symbol`(`MF200.2612`)·`product`·`contract_month`(YYMM)·`roll_state`·`session_segment`(FUT_DAY|FUT_NIGHT|UNKNOWN)·`session_date`(nullable)·`source`(KIS|UNKNOWN)·OHLC·`volume`·`retrieved_at`(최초 수신)·`batch_id`, PK `(time_utc, symbol)`. `value` 열은 없다(`acml_tr_pbmn` 은 누적, 분 단위 값 미증명 — 6145 와 같은 이유). `open_interest` 열도 없다(분봉 TR 은 미결제약정을 봉별로 주지 않는다). timescaledb 가 있을 때만 하이퍼테이블, 보관·refresh 정책 없음, `at_app` 조건부 GRANT(SELECT·INSERT, 하드룰 18), downgrade 는 행이 있으면 거부.
- **수집기**: `app/services/research_candles/futures_minutes_collector.py` + 수동 CLI `scripts/collect_futures_minutes.py [--dry-run]`(6145 수집기의 대상 선정 코드는 건드리지 않았다). 마스터에서 근월·차근월을 해석해 당일(KST) 주간 분봉을 읽고, 닫힌 봉(`시작 + 1분 + 10초 <= 요청 직전`)만 `ON CONFLICT (time_utc, symbol) DO NOTHING` 으로 쓴다(재실행은 0행·`retrieved_at` 불변). 출력 JSON: `inserted`/`already_present`/`skipped_forming`/`skipped_invalid`/`products_resolved`/`night`/`errors`/(dry-run 만) `samples`(+`error_details`: 종목·예외 클래스만), 종료 코드 0 = 정상·1 = 오류·2 = 게이트 꺼짐. 해석되지 않은 상품은 `UnresolvedProduct` 오류다.
- **진단**: `scripts/diagnose_session_coverage.py --market futures --date D`(같은 READ ONLY·8초 규칙). 예상 분은 주간 세션만이고 야간 행은 예상 0 으로 따로 나오며 `night_collection: "unsupported"` 가 붙는다.
- **DAG 사본**: `deploy/dagu/at-futures-minutes/`(주간 월–금 08:40–15:50 KST, 야간 월–금 17:55–익일 06:10 KST, 5분 간격 — 등록되지 않는 복사본, 등록은 별도 운영자 승인). **테스트**: `tests/services/kis_futures_quotes/`, `tests/services/research_candles/test_futures_{session_segment,minutes_collector,session_coverage,minutes_db}.py`, `tests/test_hk6148_futures_minutes_migration_db.py`, `tests/scripts/test_dagu_futures_minutes_dags.py`(DB 는 CI). 런북 `docs/runbooks/futures-minutes.md`.

### 투자자 수급 history 종가의 출처 표시 (hk 6317)

`get_investor_trends`(`data[]`, `period=week|month` 버킷 포함)와 `get_intraday_investor_flow`의 `confirmed.history[]`가 내는 `close`는 네이버 모바일 trend JSON의 `closePrice`(`app/services/naver_finance/investor.py` `_parse_trend_row`, `fundamentals_sources_naver._fetch_investor_trends_naver`)다. 이 값은 KRX 정규장 확정 종가가 아니다. 2026-11-24 005930은 네이버가 `closePrice` 182,900을 주지만, 다음 세션(11-25) 네이버 비교 필드로 역산한 확정 종가는 189,400이다(000660도 1,113,000 대 1,158,000). 확정 일봉 종가는 `public.kr_candles_1d`다.

- 🔴 `close`를 가진 모든 행은 `close_source`(`naver_trend_closePrice`)와 `close_basis`(`provider_unverified`)를 같이 싣는다(`_investor_flow_common.label_provider_close`). `close`가 없으면 두 키는 null이다. 키 집합은 행마다 같다.
- `close` 값 자체와 `change`·`change_pct`는 바꾸지 않았다(하위 호환). 이 두 파생값도 같은 공급자 종가에서 나오므로 같은 basis를 따른다.
- 확정 KRX 종가로 바꾸는 선택은 하지 않았다. 이 도구는 네이버만 읽고 DB 의존을 더하지 않는다. 확정 종가가 필요하면 `kr_candles_1d`를 같은 거래일로 읽는다.
- 🔴 `investor_flow_snapshots.close`(18:10 KST job, `app/services/investor_flow_snapshots/builder.py:247-264`)는 같은 공급자 값이 레이블 없이 저장된다. 이 테이블을 읽는 리더는 같은 레이블을 붙이거나 확정 종가를 써야 하며, 현재 MCP 수급 도구는 이 열을 노출하지 않는다.
- 테스트: `tests/mcp_server/test_investor_flow_close_basis.py`(실제 네이버 trend 페이로드 4행 픽스처 `tests/fixtures/investor_flow/005930_trend_2026-11-24_provider_close.json`).

### Go↔Python 분봉 전환 비교 (hk 6345)

`scripts/compare_go_py_minutes.py` 는 Go `market-tap` JSONL 과 `research.{kr,us}_candles_1m` 을 `(time_utc, symbol, venue)` 로 맞춰 전환 판정을 낸다. 판정 함수는 `app/services/research_candles/go_py_compare.py`(순수, DB·네트워크 없음)이며 막는 조건은 **통째로 없는 종목 또는 (종목, venue, 거래일) 세션**뿐이다. 비교할 근거가 없으면(한쪽 0행, 겹치는 봉 0개, 요청 종목이 양쪽에 없음) PASS 가 아니라 `INCONCLUSIVE`(종료 코드 4)다. Python 쪽은 세션 수집기 행(`batch_id` 접두어 `session-minutes-`)만 본다. 행 수 허용오차 밖, 표본 OHLCV 불일치, value 차이, 부분 결손은 목록에만 남는다. CLI 는 `READ ONLY` 트랜잭션과 `statement_timeout 8s` 안에서 SELECT 만 실행하고 주문·원장·브로커 모듈을 import 하지 않는다(정적 테스트 고정). Go JSONL 에는 수신 시각이 없어 수신 지연은 `unavailable` 로 보고한다. 실패 코드는 hk 6299 어휘로 매핑하는 표(`FAILURE_CODE_MAP`)를 가지며 테스트가 고정한다. 런북 `docs/runbooks/session-minutes.md` §5.

### FnGuide 컨센서스·추정치·투자지표 일별 스냅샷 (hk 6508)

운영자 결정 `decision/2026-11-25/at-public-scraping-fnguide`에 따라 로그인 없이 열리는 FnGuide 공개 CompanyInfo 페이지를 읽는다. 🔴 확인 사실(2026-11-25): `comp.fnguide.com/CompanyInfo/*`는 `wcomp.fnguide.com`으로 302를 보내며, 두 호스트의 `robots.txt`는 `User-agent: *` 전면 `Disallow: /`이다. 이 수집기는 robots 준수를 구현하지 않는다. 운영자가 스크래핑을 승인했고 이 승인은 이 수집기에만 적용된다. 승인 범위를 넓히거나 robots 정책을 바꾸려면 새 결정이 필요하다.

- **소스**: `https://wcomp.fnguide.com/CompanyInfo/{Consensus|Invest|Finance}?cmp_cd=<6자리>`만 허용한다(호스트·스킴·포트는 요청 전에 검사하고 `follow_redirects=False`, 3xx는 기록만 하고 따르지 않는다). `www.fnguide.com`(SPA, 로그인)은 쓰지 않는다. 요청은 동시 2개 이하, 전체 약 1초 간격, 일반 User-Agent `at-research-collector/1.0`. 재시도는 timeout·transport·5xx에만 제한 횟수로 한다. 4xx는 즉시 기록한다.
- **파싱**: 페이지가 내장한 JSON만 읽는다 — `key: {"header":[…],"data":[…]}` 차트 블록(`header`/`data` 는 같은 객체의 인접 멤버여야 한다), 평면 `{"ACC_NM":…}` 컨센서스 행, `{"RPT_TITLE":…}` 리포트. 숫자는 페이지 원문 문자열을 유지하고(`parse_float=str`), 검증된 정확한 10진 문자열만 값으로 쓴다(쉼표 제거). 빈 값·`-`는 `missing`, 해석 불가는 `unparsable`로 NULL과 사유를 남기며 0으로 채우지 않는다. 페이지 제목의 종목 코드가 요청 코드와 다르면 `code_mismatch`, 로그인·없는 페이지는 `login_or_missing`으로 저장하지 않는다.
- **헤더 라벨**: `headers`는 각 차트의 헤더를 페이지가 준 텍스트 그대로 담고, `fnguide_values.column_label`은 그 라벨을 쓴다. 컨센서스 `VAL1`의 라벨은 페이지가 `당기순이익`이라고 준다(값과 맞지 않는 페이지 표기이며 재해석하지 않는다). `VAL2`–`VAL5`는 `1개월전`·`3개월전`·`6개월전`·`1년전`이다.
- **저장**: 단일 writer `app/services/fnguide_consensus/writer.py`만 INSERT 한다. `fnguide_snapshots`는 `(symbol, page, observed_date)` 자연키 `ON CONFLICT DO NOTHING`이고, 같은 날 재실행은 스냅샷이 이미 있으면 자식 행을 쓰지 않아 0행이다. `fnguide_values`는 `(snapshot, section, item_name, item_code, item_qualifier, column_key)`, `fnguide_reports`는 `(symbol, observed_date, report_hash)`가 자연키다. 가져오기 실패(요청 단위 오류)는 저장하지 않고 CLI 보고에만 닫힌 종류와 HTTP 상태로 남긴다. 그래야 같은 날 재실행이 채울 수 있다. 원문 본문은 저장하지 않는다. 원시 내장 JSON은 직렬화 128KiB 이하일 때만 `raw_embedded`에 저장하고, 넘으면 `raw_reason='over_bound'`다.
- **사유**: 파싱은 됐지만 값이 없으면 `status='no_values'`와 닫힌 사유(`no_embedded_blocks`·`no_consensus_rows`·`all_consensus_values_null`·`no_values_present`)를 준다. 컨센서스 값이 전부 비었지만 다른 차트 값이 있으면 `status='parsed'`이고 `reason='all_consensus_values_null'`이다. 028300이 이 경우이며 목표주가·투자의견 행을 만들지 않는다.
- **읽기 함수**: `app/services/fnguide_consensus/query.py::read_latest_consensus(session, symbol)`은 최신 컨센서스 스냅샷과 목표주가·EPS의 리비전(현재 `VAL1` 대 `VAL2`–`VAL5`, 그리고 직전 저장일 `VAL1`)을 정확한 10진 문자열로 준다. 직전 저장일은 날짜를 함께 보여 수집 공백이 드러난다. 읽기 전용이며 MCP 도구로 등록하지 않는다(후속). 거래·승인·주문 경로는 이 데이터를 읽지 않는다.
- **유니버스·실행**: 기본 대상은 `kr_symbol_universe`의 활성 KOSPI·KOSDAQ 중 `security_type='STOCK'`이다. 수동 CLI `uv run python -m scripts.collect_fnguide_consensus [--symbols …|--limit N] [--pages …] [--dry-run|--commit]`이며 기본은 dry-run이다. `--symbols`를 주면 DB를 열지 않는다. 스케줄은 `deploy/dagu/at-fnguide-consensus/`의 사본이며 저장소가 등록하지 않는다.
- **테스트**: `tests/services/fnguide_consensus/`(저장된 HTML 픽스처 005930·000660·028300·005930 invest, 네트워크 없음), DB 왕복은 CI(`test_fnguide_db.py`). 정적 테스트가 패키지의 주문·브로커·MCP·승인 모듈 import가 없음을 고정한다.
- **운영 주의**: 원시 JSON 보관량은 종목·페이지·일 단위로 늘어난다. 보존 기간 정책은 이 PR 범위 밖이며 운영자 결정 대상이다.
- **쓰기 실패 분류**: 일시 오류(OperationalError, 연결 무효화, SQLSTATE 40001/40P01)만 2회까지 재시도한다. 재시도는 writer 가 세 자연키 `ON CONFLICT DO NOTHING` 이라 안전하다. 그 밖의 오류(무결성·CHECK)는 즉시 기록한다. 연속 5회 쓰기 실패면 수집을 멈추고(대기 중인 가져오기 취소) 종료 코드 1 로 끝나며, 그때까지 쓴 행은 남는다. 유니버스는 `cmp_cd` 로 쓸 수 있는 6자리 숫자 코드만 받고, 그 밖의 행은 제외 수를 보고서에 남긴다.

### KR 관리·시장경고·정리매매·거래정지 플래그 (hk 6503)

국내 종목의 관리종목·시장경고·정리매매·거래정지 상태를 `public.kr_symbol_trading_flags`(PK `(symbol, category)`, 마이그레이션 `20261125_hk6503_kr_trading_flags`, 추가형)에 둔다. 🔴 **행이 없으면 unknown 이며 `none` 이 아니다.** `none` 은 소스가 명시한 값(KIS `N`, 네이버 `tradeStopYn=N`, 시장경고 `00`)에서만 온다.

- **소스와 오프라인 계약**: KIS 종목정보(`kospi_code.mst`·`kosdaq_code.mst`)의 tail 고정폭 필드(거래정지·정리매매·관리종목·시장경고)는 공개 스펙 `github.com/koreainvestment/open-trading-api` `stocks_info/kis_{kospi,kosdaq}_code_mst.py` 의 오프셋으로 읽는다(`app/services/kr_trading_flags/kis_master.py`). 스펙은 개행을 포함한 줄에서 `row[-228:]`/`row[-222:]` 로 자르므로, 개행을 뗀 줄(다운로더가 주는 형태)에서는 tail 이 227/221 자이고 폭 합과 같다. 228/222 로 자르면 모든 플래그가 한 칸 앞 열로 읽힌다(PR #2584 리뷰 지적, `test_kis_master.py` 가 전체 폭 표로 고정). 🔴 **[unverified]** 스펙은 필드 이름과 폭만 주고 값 샘플은 주지 않는다. 시장경고 `02`/`03` 과 `Y/N` 의 실제 분포는 라이브 스모크 전에는 확정하지 않는다. 네이버 `manageStatusGb=1` 을 관리로 보는 것도 픽스처 224060 에서만 추정한 것이다(`naver_status.py`).
- **쓰기 가드**(`store.py`, SQL 에 있다): 새 `observed_at` 만 기존 행을 덮는다. `unmapped` 은 매핑된 상태를 절대 덮지 않는다(읽지 못한 코드가 이미 제외된 종목을 다시 열지 못하게 한다). 매핑되지 않는 코드는 `raw_code` 로 보존하고 추측하지 않는다.
- **닫힌 어휘**: 관리 `designated|none|unmapped`, 시장경고 `none|caution|warning|risk|unmapped`(투자주의=caution, 투자경고=warning, 투자위험=risk), 정리매매 `in_progress|none|unmapped`, 거래정지 `halted|none|unmapped`. DB CHECK 가 카테고리별 상태를 강제한다.
- **스크리너 게이트**(`app/services/kr_trading_flags/gate.py`, `app/mcp_server/tooling/screening/trading_flag_filter.py`, `screen_stocks` 진입점의 halt 필터 뒤): 관리·투자경고·투자위험·정리매매·거래정지는 제외되고 `meta.trading_flag_excluded` 에 사유 코드(`management_designated`·`market_alert_warning`·`market_alert_risk`·`delisting_trade_in_progress`·`trading_halt`), 출처, 관측 시각, 소스 사유 코드, 지정일이 남는다. **투자주의는 제외하지 않고** 행의 `trading_flag_labels` 로만 표시한다. 저장소 읽기 실패는 fail-open(모든 행 유지, `meta.trading_flags_check_skipped`). 행 키는 `kr_row_symbol`(`symbol` → `short_code` → 6자 `code`)로 정한다. 레거시 경로의 `code` 는 ISIN 이다. 96시간(`FLAG_MAX_AGE`)보다 오래된 관측은 unknown 으로 보고 제외하지 않는다(`meta.trading_flags_stale`). 기준값과 장중 보조를 함께 돌리지 않으면 해제 상태가 남기 때문이다. 이 게이트는 `screen_stocks`(매수 후보 fanout 포함)에만 있고 DB-only `screen_stocks_snapshot` 에는 아직 없다(후속).
- **halted_suspect**: 판정 로직은 그대로다. 국내 종목에 halt suspicion 이 서고 저장된 `trading_halt=halted` 행이 있을 때만 `halt_suspect.confirmed_halt_flag` 로 근거를 붙인다(`app/services/kr_trading_flags/lookup.py`).
- **쓰기 경로(운영자 수동)**: `scripts/sync_kr_trading_flags.py --source kis-master|naver-intraday [--dry-run]`. 소스 실패·형식 오류는 쓰기 전에 종료 코드 1. 네이버 목록은 상태가 있는 종목만 준다. 목록에 없는 종목은 "해제"의 증거가 아니므로 아무것도 쓰지 않는다. 스케줄 등록은 없다. DAG 사본은 `deploy/dagu/at-kr-trading-flags/`(등록 안 됨): 장중 10분 간격 보조 + 월–금 07:00 일 기준값. 두 DAG 는 함께 등록한다. kis-master 쓰기는 한 카테고리의 대부분(20행 이상, 50% 초과)이 `unmapped` 이면 거부한다.
- **범위 밖**: 주문 경로, Upbit, US, KRX 거래정지 마스터(`halted_suspect` 의 `krx_halt_master: unavailable` 는 그대로다), 스냅샷 전용 스크리너(`screen_stocks_snapshot`), KOSDAQ 의 `투자주의환기종목여부`(별도 필드).

### Upbit 원화장 장중 그림자 신호·가상 원장 기록기 (hk 6036)

`app/services/upbit_intraday_shadow/` + 수동 CLI `scripts/upbit_intraday_shadow.py` 는 hk 6035 동결 규칙을 마감된 15분 봉에 적용해 **전향 증거만** 쌓는다. 6035 는 세 계열 모두 `NOT_SUPPORTED` 였으므로 이 행들은 매매 신호·알림·정책 입력이 아니다. 🔴 주문·미리보기·승인·원장 모듈을 import 하지 않고(정적 import 폐쇄 + 새 인터프리터 로드 검사 `tests/services/upbit_intraday_shadow/test_static_boundary.py`), 스케줄러 등록 없음(하드룰 6: `deploy/dagu/at-upbit-intraday-shadow/` 는 미등록 복사본, 등록은 마이그레이션 적용 뒤 설계 세션).

- **데이터**: Upbit 공개 GET 두 경로(`/v1/market/all`, `/v1/candles/minutes/15`)만, 6035 의 허용 목록·파싱·페이서 재사용(키 없음, 요청 간격 0.15초, 429/5xx 재시도). websocket 대신 candles GET 인 이유: 규칙이 이 거래소 집계 봉으로 시험됐고, 상주 프로세스가 필요 없고, 놓친 회차가 있어도 같은 페이지를 다시 읽는다. 봉은 마감 10초 뒤부터만 읽고 형성 중 봉은 요청하지 않는다. 1분봉 저장은 이 기록기 범위가 아니다.
- **규칙**: `research/upbit_intraday/rules.py` 를 import 해 12개 설정 전부를 평가한다(`selected_config` = `frozen_selection.json` 의 3개). 파일 sha256 이 6035 매니페스트 핀과 다르면 실행 전에 거부한다(종료 코드 2). 규칙·평가 파일은 고치지 않는다. 스테이블 제외·보유 집합 분할은 규칙의 것이고 제외 행을 만들지 않는다.
- **후보 행** `research.upbit_shadow_candidates`(키 `rules_version, market, config_id, signal_bar_open_at`): 동결 신호, 또는 가격·거래량 배수 조건은 맞지만 거래대금 20,000,000 KRW 미만인 후보(16봉 쿨다운 밖)가 한 행이다. 쿨다운이 눌러 준 원 조건과 룩백 봉이 75% 미만인 시각(unknown)은 행이 없다. `disposition` ∈ `signal`|`excluded`, `exclusion_reasons` 는 닫힌 어휘 `market_event_warning`·`market_event_caution`·`market_event_unknown`·`market_event_stale`·`liquidity_below_floor`·`cooldown_unverified`(해당하는 것 전부, 이 순서). `cooldown_unverified` = 패널 안에 16봉을 넘는 공백이 없어 동결 쿨다운 결과를 보지 못한 봉으로부터 확정할 수 없는 경우. 신호 봉 OHLC·거래대금, 직전 N 고/저(`ref_level`)·이격, 중앙값·배수, 24시간 거래대금·구간, 수수료(편도 0.05%)·동결 왕복 비용·호가 단위(`app/core/upbit_tick.py`), 경보 상태·타입·관측 시각을 함께 남긴다. `signal_time` = 봉 마감, `first_received_at` = 마감 봉을 처음 받은 시각.
- **경보 상태**: 그 회차의 `market/all` 의 `market_event` 를 `app/services/upbit_market_event.classify_market_event`(hk 6026 어휘)로 분류한다. 이 기록기에서는 `caution` 도 제외 사유다(구매측 리더 규칙은 바뀌지 않는다). 🔴 관측이 신호 시각보다 30분 넘게 늦으면(놓친 회차를 따라잡는 경우) 그 시각의 상태를 모르므로 `market_event_unknown` 이고 과거 상태를 지어내지 않는다.
- **가상 왕복** `research.upbit_shadow_virtual_trades`(키 `candidate_id, horizon_bars` = 4·16·96, `is_virtual` 항상 true, `disposition` 은 복합 FK 로 후보와 결속 — 제외 후보의 행도 있으므로 소비자는 반드시 구분한다): 🔴 진입은 **신호 다음 봉의 시가뿐**이다. 저가·고가가 가격에 닿는 것은 체결이 아니며, 다음 15분에 봉이 없으면 진입 없이 `missing_reason=next_bar_no_trades` 다. 청산은 진입 봉부터 `h` 번째 봉 종가, 그 칸에 체결이 없으면 직전 체결 종가(`exit_basis`). `gross_return`(방향 부호 적용 — C 는 매도 뒤 재매수 이득)·`mae`·`mfe` 는 동결 함수 값, `net_return = gross - 왕복 비용`. 아직 판정할 수 없으면 행이 없고(다음 회차), 영원히 판정할 수 없으면(상장 목록에서 빠지고 404 = `market_unavailable`) 사유만 있고 숫자는 NULL 이다 — 0 으로 채우지 않는다(DB CHECK).
- **쓰기**: 두 표의 유일한 writer 는 `writer.py`, `ON CONFLICT DO NOTHING` 삽입뿐이라 재실행은 첫 수신 시각과 값을 바꾸지 않는다. 어떤 live·mock 체결 표와도 분리돼 있고 그 표를 읽거나 쓰지 않는다. 마이그레이션 `20261125_hk6036_upbit_shadow`(추가형, `at_app` 조건부 GRANT SELECT·INSERT·schema USAGE, downgrade 는 행이 있으면 거부).
- **실행**: 기본은 드라이런(DB 접근 0, 후보·가상 행 출력), `--commit` 만 쓴다. `--catchup-bars`(기본 4, 최대 96), `--markets`. 시장 하나의 실패는 `errors` 에 남기고 계속하며 종료 코드 1. 실계좌·전략·게이트 변경 없음.
- **테스트**: `tests/services/upbit_intraday_shadow/`(2026-11-25 BTC·XRP·SAHARA 녹화 봉 재생, 터치=진입 아님, 동결 해시), `tests/test_hk6036_upbit_shadow_migration_db.py`(DB 부분은 CI), `tests/scripts/test_dagu_upbit_intraday_shadow_dag.py`. 이미지에는 `research/upbit_intraday` 의 규칙·fetch·선택 파일과 `research/crypto_universe/public_http.py` 만 더 싣는다.
