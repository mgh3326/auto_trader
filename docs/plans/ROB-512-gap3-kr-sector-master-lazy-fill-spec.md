# ROB-512 갭3 — KR 한글 카테고리: 마스터 테이블 + lazy fill (스펙)

> 상태: 디자인 확정 (2026-06-11 브레인스토밍, user 승인 결정 반영). 구현 플랜은 별도 문서.
> 선행: ROB-512 PR1(#1249, 머지됨 2e9ad1a7)과 독립 — 어느 순서로 머지돼도 충돌 없음.

## 문제

`screen_stocks_snapshot` KR 결과의 `category`가 프리셋별로 불일치(ROB-512 갭 2):
- consecutive_gainers / investor_flow_momentum / double_buy → `"-"` (소스 없음)
- fundamentals 프리셋(high_yield_value 등) → tvscreener **영문** ("Industrial Machinery")
- 토스는 전 프리셋 한글 카테고리

레포에 한글 업종 소스가 없었다: `kr_symbol_universe`(KRX MST 기반)에 업종 컬럼 없음, tvscreener는 영문, KRX 서비스는 KOSPI200 한정.

## 확정 결정 (user)

1. **세분류 수준**: Naver 한글 업종(WICS류)으로 충분. 토스 자체 세분류("반도체팹리스")는 외부 소스로 exact 재현 불가 — comparable+honest 원칙(ROB-428 동일 reframe).
   라이브 실측(2026-06-11): 삼성전자→"반도체와반도체장비", 에스엠→"방송과엔터테인먼트", 에코프로비엠→"전기제품", 현대백화점→"백화점과일반상점", HMM→"해운사".
2. **저장 위치**: `kr_symbol_universe` **마스터 테이블** (업종=종목별 마스터성 데이터).
3. **적재 방식**: **lazy fill** — 비어 있으면 그때 Naver 요청 후 저장. 일괄 백필 크롤 없음 (심볼당 평생 1회 요청, 페이지 단위라 요청량 작음).
4. **fundamentals 프리셋 통일**: 마스터 한글 sector **우선 + 기존 영문 fallback**.

## 소스 사실관계 (라이브 검증됨)

- 한글 업종은 Naver 금융 종목 메인 페이지(`finance.naver.com/item/main.naver?code=...`)의 "동종업종비교" 섹션에 존재: `<a href="...type=upjong&no=278">반도체와반도체장비</a>`.
- **레포 기존 파서 `_parse_industry_info`(`app/services/naver_finance/valuation.py:177-191`, 셀렉터 `div.tab_con1 em a`)는 현재 페이지 구조에서 죽어 있음 — 전 종목 None 반환** (2026-06-11 라이브 확인). 작동 셀렉터: `a[href*="type=upjong"]` (KOSPI/KOSDAQ 5종목 검증).
- 이 페이지는 `fetch_valuation()`이 이미 다운로드하는 페이지와 동일(향후 빌드 piggyback 여지 — 이번 스코프 아님).

## 설계

### 1. 데이터 모델 (alembic migration 1, additive)

`kr_symbol_universe`에 nullable 컬럼 2개:

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `sector` | String(100) nullable | Naver 한글 업종명 |
| `sector_updated_at` | TIMESTAMP(tz) nullable | 채운 시점 (이번엔 기록만; stale 갱신 정책은 향후) |

**sync 보존 (검증됨)**: `kr_symbol_universe_service._apply_snapshot`(:240-296)은 name/exchange/nxt_eligible/is_active만 필드 단위로 갱신 → sector는 sync에 지워지지 않음. 회귀 테스트로 고정한다.

### 2. 파서 수리

`_parse_industry_info`의 sector 추출을 `a[href*="type=upjong"]` 셀렉터로 교체(죽은 `div.tab_con1 em a` 제거 또는 fallback 체인). 단위 테스트는 실제 페이지 구조를 본뜬 고정 HTML fixture로.

### 3. 쓰기 경로 — 전용 서비스 함수

`kr_symbol_universe_service`에 신규 함수 (모든 sector 쓰기는 이 함수 경유):

```
async def update_sectors(db, sectors: dict[str, str]) -> int
# 존재하는 심볼만 field-targeted update (sector, sector_updated_at=now)
# 반환: 갱신 행 수. 미존재 심볼은 무시(INSERT 금지 — universe 소스는 sync뿐)
```

### 4. Lazy fill — MCP enrichment 단계

`enrich_snapshot_page`(`app/services/invest_view_model/screener_analysis_enrichment.py`)에 sector 단계 추가. **이미 같은 단계가 페이지 단위 per-symbol Naver 컨센 호출(Semaphore(4) + 4.5s timeout)을 수행 중**이라 아키텍처 패턴 일치.

흐름 (market == "kr"일 때만):
1. 페이지 심볼들의 `sector`를 `kr_symbol_universe`에서 일괄 조회
2. NULL인 심볼만 Naver main 페이지 fetch → 수리된 파서로 업종 추출 (Semaphore(4), per-fetch timeout, 전체 fail-open)
3. 성공분: `update_sectors`로 persist (enrichment의 기존 session_factory 세션 사용) + **이번 응답 row의 `category`가 `"-"`/빈 값이면 한글 업종으로 교체**
4. 실패분: category 유지("-"), 영구 실패 마킹 없음 — 다음 요청 때 자연 재시도

us/crypto 무변경. 요청량 상한: 페이지 limit(≤50) × 심볼당 평생 1회.

### 5. 로더 배선 — read-time은 DB-only (HTTP 없음)

- consec / investor_flow / double_buy 로더의 **기존 KRSymbolUniverse name lookup select에 `sector` 컬럼 추가** → `row["sector"] = ...` → 포맷터(`screener_service.py:2358`, `row.get("sector") or row.get("category") or "-"`)가 자동 인식.
- fundamentals 프리셋(`kr_fundamentals_tv_screener.py` row 구성): 마스터 한글 sector 조회 추가, 있으면 한글·없으면 기존 `snap.industry or snap.sector`(영문) fallback.

### 6. 안전 경계

- 브로커/주문/감시 mutation 없음. DB 쓰기는 `update_sectors` 한 함수, 대상 컬럼 2개뿐.
- 스크리너 read 경로(build_screener_results)는 HTTP/쓰기 없음 — lazy fill은 MCP enrichment 단계에만.
- Naver 차단/실패 → 전면 fail-open (category "-"; 스크리너 결과 자체는 영향 없음).
- migration은 additive nullable — 롤백 영향 없음.

### 7. 테스트

| 대상 | 방식 |
|---|---|
| 파서 | upjong 링크 포함 HTML fixture → 한글 업종 추출; 링크 없음 → None |
| update_sectors | 존재 심볼 갱신/미존재 무시/sector_updated_at 세팅 |
| sync 보존 | `_apply_snapshot` 후 기존 sector 값 불변 (회귀 가드) |
| lazy fill | fake fetch로 NULL→fetch→persist→category 교체; 실패 시 fail-open; 이미 채워진 심볼은 fetch 안 함(요청 0) |
| 로더 | 3개 로더 row에 sector 노출 → category 한글 렌더; fundamentals 한글 우선·영문 fallback |

### 8. 운영 / 한계 (정직)

- operator 절차: `alembic upgrade head`뿐. 백필 크롤 없음 — MCP 사용으로 자연 워밍업.
- 웹 프론트(/invest/screener 라우터)는 enrichment를 타지 않으므로 **워밍된 심볼만** 카테고리 표시. MCP가 주 소비자라 수용; 필요 시 일괄 워밍 스크립트는 follow-up.
- 콜드 페이지 첫 조회는 enrichment 레이턴시 증가(최대 ~수 초, 이후 해당 심볼 0).
- Naver 업종 ≠ 토스 세분류 — comparable, exact 아님 (확정 결정 1).

## 스코프 제외

- 갭4(flow 신선도 lag): operator 활성화 트랙 별도.
- valuation 빌드 piggyback(빌드 시 sector 일괄 갱신): follow-up 후보.
- `sector_updated_at` 기반 stale 자동 갱신 정책: follow-up.
- US/crypto 카테고리.
