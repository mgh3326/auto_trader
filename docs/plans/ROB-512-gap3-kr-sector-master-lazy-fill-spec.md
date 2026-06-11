# ROB-512 갭3 — KR/US 한글 카테고리: 정규화 섹터 테이블 + lazy fill (스펙)

> 상태: 디자인 확정 (2026-06-11 브레인스토밍, user 승인 결정 5건 반영). 구현 플랜은 별도 문서.
> 선행: ROB-512 PR1(#1249, 머지됨 2e9ad1a7)과 독립 — 어느 순서로 머지돼도 충돌 없음.

## 문제

`screen_stocks_snapshot` KR 결과의 `category`가 프리셋별로 불일치(ROB-512 갭 2):
- consecutive_gainers / investor_flow_momentum / double_buy → `"-"` (소스 없음)
- fundamentals 프리셋(high_yield_value 등) → tvscreener **영문** ("Industrial Machinery")
- 토스는 전 프리셋 한글 카테고리 (US 종목 포함)

레포에 한글 업종 소스가 없었다: `kr_symbol_universe`(KRX MST 기반)·`us_symbol_universe` 모두 업종 컬럼 없음(실 DB 확인 2026-06-11, KR active 3,914행), tvscreener는 영문, KRX 서비스는 KOSPI200 한정.

## 확정 결정 (user, 2026-06-11)

1. **세분류 수준**: Naver 한글 업종(WICS류)으로 충분. 토스 자체 세분류("반도체팹리스")는 외부 소스로 exact 재현 불가 — comparable+honest 원칙(ROB-428 동일 reframe).
   라이브 실측: 삼성전자→"반도체와반도체장비", 에스엠→"방송과엔터테인먼트", 에코프로비엠→"전기제품", 현대백화점→"백화점과일반상점", HMM→"해운사".
2. **저장 위치**: 심볼 universe **마스터** 기반 (일별 스냅샷 아님).
3. **적재 방식**: **lazy fill** — 비어 있으면 그때 요청 후 저장. 일괄 백필 크롤 없음 (심볼당 평생 1회, 페이지 단위라 요청량 작음).
4. **fundamentals 프리셋 통일**: 마스터 한글 **우선 + 기존 영문 fallback**.
5. **US 동일 구조**: yfinance `info`(영문 industry/sector) + 정적 한글 매핑.
6. **정규화**: free-text 컬럼 대신 **단일 `symbol_sectors` 테이블 + universe FK** — "같은 섹터 종목"/"섹터 종류 목록" 쿼리 가능, US 영문 원본·한글 매핑 동시 보관, Naver 업종번호를 안정 식별자로 사용.

## 소스 사실관계 (라이브 검증됨)

- KR: Naver 금융 종목 메인 페이지(`finance.naver.com/item/main.naver?code=...`) "동종업종비교" 섹션의 upjong 링크: `<a href="...type=upjong&no=278">반도체와반도체장비</a>` — **업종번호(no)와 한글명을 동시 제공**.
- **레포 기존 파서 `_parse_industry_info`(`app/services/naver_finance/valuation.py:177-191`, 셀렉터 `div.tab_con1 em a`)는 현재 페이지 구조에서 죽어 있음 — 전 종목 None 반환** (라이브 확인). 작동 셀렉터: `a[href*="type=upjong"]` (KOSPI/KOSDAQ 5종목 검증). href에서 `no=` 추출.
- US: yfinance `Ticker.info`의 `industry`("Semiconductors", ~145종)/`sector`("Technology", 11종) — 영문. DB 심볼→yahoo 심볼은 기존 `to_yahoo_symbol`.
- 이 Naver 페이지는 `fetch_valuation()`이 이미 다운로드하는 페이지와 동일(향후 빌드 piggyback 여지 — 이번 스코프 아님).

## 설계

### 1. 데이터 모델 (alembic migration 1, additive)

**신규 `symbol_sectors`** (단일 테이블, market 구분):

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | int PK autoincrement | |
| `market` | String(10) NOT NULL | `kr` / `us` |
| `source` | String(30) NOT NULL | `naver_upjong` / `yfinance_industry` (provenance) |
| `source_key` | String(100) NOT NULL | KR: Naver 업종번호 "278" / US: industry 영문 원문 "Semiconductors" |
| `name_kr` | String(100) nullable | KR: Naver 한글명 / US: 정적 매핑 한글 (미스면 NULL — fake 금지) |
| `name_en` | String(100) nullable | US: 영문 원문 / KR: NULL |
| `created_at` / `updated_at` | TIMESTAMP(tz) | |

UNIQUE(`market`, `source`, `source_key`). KR은 업종번호가 키이므로 Naver가 업종명을 개명해도 identity 유지(name_kr만 갱신).

**universe 테이블 2곳 공통 추가** (`kr_symbol_universe`, `us_symbol_universe`):
- `sector_id` int nullable FK → `symbol_sectors.id`
- `sector_updated_at` TIMESTAMP(tz) nullable (채운 시점; stale 갱신 정책은 향후)

migration 직후 FK 전부 NULL — lazy 워밍업 시작점.

**표시 규칙**: `category = name_kr ?? name_en ?? "-"`.

**sync 보존**: KR `_apply_snapshot`(`kr_symbol_universe_service.py:240-296`)은 name/exchange/nxt_eligible/is_active만 필드 단위 갱신 → sector_id 보존 (검증됨, 회귀 테스트로 고정). **US sync도 동일 방식인지 plan 단계에서 확인** 후 같은 회귀 테스트 추가(아니면 보존 로직 보강).

### 2. 파서 수리 (KR)

`_parse_industry_info`의 sector 추출을 `a[href*="type=upjong"]` 셀렉터로 교체하고 **업종번호(`no=` 쿼리 파라미터)와 한글명을 함께 반환**하도록 확장. 단위 테스트는 실제 페이지 구조를 본뜬 고정 HTML fixture로.

### 3. 쓰기 경로 — 전용 서비스 모듈

신규 `app/services/symbol_sectors_service.py` (모든 쓰기는 이 모듈 경유):

```
async def get_or_create_sector(db, *, market, source, source_key, name_kr=None, name_en=None) -> int
# UNIQUE 키로 upsert(get-or-create). 기존 행의 name이 새 값과 다르면 갱신(개명 추적).
# 반환: sector id

async def assign_symbol_sector(db, *, market, symbol, sector_id) -> bool
# universe 행의 sector_id + sector_updated_at=now 를 field-targeted update.
# 미존재 심볼은 무시(False) — universe 행 생성은 sync의 책임, INSERT 금지
```

### 4. Lazy fill — MCP enrichment 단계

`enrich_snapshot_page`(`app/services/invest_view_model/screener_analysis_enrichment.py`)에 sector 단계 추가. **이미 같은 단계가 페이지 단위 per-symbol 외부 호출(컨센, Semaphore(4) + 4.5s timeout)을 수행 중**이라 아키텍처 패턴 일치.

**KR (market == "kr")**:
1. 페이지 심볼들의 sector(FK join, name_kr/name_en)를 일괄 조회
2. NULL인 심볼만 Naver main 페이지 fetch → 수리된 파서로 (업종번호, 한글명) 추출 (sem 4, timeout, 전체 fail-open)
3. 성공분: `get_or_create_sector`(naver_upjong, no, name_kr) → `assign_symbol_sector` persist + **이번 응답 row의 `category`가 `"-"`/빈 값이면 표시 규칙대로 교체**
4. 실패분: category 유지("-"), 영구 실패 마킹 없음 — 다음 요청 때 자연 재시도

**US (market == "us")**: 미러.
1. `us_symbol_universe` FK join 일괄 조회 → NULL 심볼만 yfinance `Ticker.info`에서 `industry`(우선)/`sector`(fallback) 추출 (`to_yahoo_symbol` 변환, sem 4, timeout, fail-open)
2. `get_or_create_sector`(yfinance_industry, 영문 원문, name_kr=정적 매핑값 or NULL, name_en=원문) → assign
3. 정적 한글 매핑 모듈: industry ~145종 + sector 11종 ("Semiconductors"→"반도체"). **매핑 미스는 name_kr NULL → 영문 표시** (fake 금지)

crypto 무변경. 요청량 상한: 페이지 limit(≤50) × 심볼당 평생 1회 (시장별).

### 5. 로더 배선 — read-time은 DB-only (HTTP 없음)

- consec / investor_flow / double_buy 로더의 **기존 KRSymbolUniverse name lookup을 symbol_sectors LEFT JOIN으로 확장** → `row["sector"] = name_kr ?? name_en` → 포맷터(`screener_service.py:2358`, `row.get("sector") or row.get("category") or "-"`)가 자동 인식.
- fundamentals 프리셋(`kr_fundamentals_tv_screener.py` row 구성): 마스터 sector 조회 추가, 있으면 그 값·없으면 기존 `snap.industry or snap.sector`(영문) fallback.
- **US 로더**: market=="us" 경로의 결과 row에 `us_symbol_universe` FK join lookup 추가 (구체 배선 지점은 plan 단계에서 US 로더별 확인).

### 6. 안전 경계

- 브로커/주문/감시 mutation 없음. DB 쓰기는 `symbol_sectors_service` 두 함수뿐 (sectors upsert + universe FK 2컬럼).
- 스크리너 read 경로(build_screener_results)는 HTTP/쓰기 없음 — lazy fill은 MCP enrichment 단계에만.
- 외부 소스 실패 → 전면 fail-open (category "-"; 스크리너 결과 자체는 영향 없음).
- migration은 additive(신규 테이블 + nullable FK) — 롤백 영향 없음.

### 7. 테스트

| 대상 | 방식 |
|---|---|
| 파서 | upjong 링크 포함 HTML fixture → (번호, 한글명) 추출; 링크 없음 → None |
| get_or_create_sector | 신규 생성 / 동일 키 재호출 시 같은 id / 개명 시 name 갱신 |
| assign_symbol_sector | 존재 심볼 갱신 + sector_updated_at / 미존재 무시 |
| sync 보존 | `_apply_snapshot` 후 기존 sector_id 불변 (KR, US 각각 회귀 가드) |
| lazy fill | fake fetch로 NULL→fetch→persist→category 교체; 실패 fail-open; 이미 채워진 심볼 fetch 0; KR/US 분기 |
| US 매핑 | 매핑 존재 industry→name_kr / 미스→name_kr NULL·name_en 원문 |
| 로더 | 3개 로더 row에 sector 노출 → category 한글 렌더; fundamentals 한글 우선·영문 fallback |
| 표시 규칙 | name_kr ?? name_en ?? "-" 우선순위 |

### 8. 운영 / 한계 (정직)

- operator 절차: `alembic upgrade head`뿐. 백필 크롤 없음 — MCP 사용으로 자연 워밍업.
- 웹 프론트(/invest/screener 라우터)는 enrichment를 타지 않으므로 **워밍된 심볼만** 카테고리 표시. MCP가 주 소비자라 수용; 필요 시 일괄 워밍 스크립트는 follow-up.
- 콜드 페이지 첫 조회는 enrichment 레이턴시 증가(최대 ~수 초, 이후 해당 심볼 0).
- Naver 업종/yfinance industry ≠ 토스 세분류 — comparable, exact 아님 (확정 결정 1).
- FK는 market 불일치(kr universe가 us 섹터를 가리키는)를 구조적으로 막지 못함 — 서비스 함수에서 market 일치 강제 + 테스트.

## 가능해지는 쿼리 (이번 PR에서 도구화하지는 않음)

- 같은 섹터 종목: universe self-join via `sector_id`
- 섹터 종류 목록/분포: `symbol_sectors` select + universe group by
- 향후 `get_sector_peers`류 MCP 도구의 DB 기반 (Naver 라이브 `fetch_sector_peers`의 오프라인 대체) — follow-up

## 스코프 제외

- 갭4(flow 신선도 lag): operator 활성화 트랙 별도.
- valuation 빌드 piggyback(빌드 시 sector 일괄 갱신): follow-up 후보.
- `sector_updated_at` 기반 stale 자동 갱신 정책: follow-up.
- crypto 카테고리.
- 일괄 워밍 스크립트(KR ~3.9k / US 수천 요청 1회성): follow-up 후보 — lazy만으로 동작.
- 섹터 기반 신규 MCP 도구(피어 조회 등): follow-up.
