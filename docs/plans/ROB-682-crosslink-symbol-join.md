# ROB-682 — insights forecast↔회고 crosslink을 SYMBOL 키로 재배선

/ insights (판단 품질 ↔ 학습·회고) 크로스링크는 ROB-678(G)에서 `correlation_id`
정확 일치로 배선되었으나 **LIVE 데이터에서 구조적으로 죽어 있다**. 이 플랜은
USER-PREFERRED 옵션 A(심볼 재키잉)로 교체한다. 코드 변경 없음 — 플랜 문서만.

## 배경

ROB-678의 페이지-조정 교집합(page-coordinated intersection)은 다음처럼 동작한다:

- `ForecastCalibrationPanel` — closed 예측 로드 시
  `onClosedCorrelationIds(closed.data.map(r => r.correlation_id).filter(non-null))`
  로 correlation_id 배열을 페이지에 보고
  (`ForecastCalibrationPanel.tsx:402-408`).
- `RetrospectivesPanel` — 회고 로드 시
  `onCorrelationIds(state.items.map(r => r.correlation_id).filter(non-null))`
  로 보고 (`RetrospectivesPanel.tsx:114-120`).
- `DesktopInsightsPage` — 두 배열을 받아 `linkedCorrelationIds` = **교집합**(Set)
  을 계산해 다시 두 패널로 내려보냄 (`DesktopInsightsPage.tsx:127-134`,
  `152-165`).
- 렌더 시 교집합에 속한 행만 앵커 + 링크를 얻는다:
  - closed 예측 행: `id="forecast-<corr>"` + `<a href="#retro-<corr>">회고↓</a>`
    (`ForecastCalibrationPanel.tsx:298-327`, `ClosedList`).
  - 회고 행: `id="retro-<corr>"` + `<a href="#forecast-<corr>">예측↑</a>`
    (`RetrospectivesPanel.tsx:183-211`).

### 왜 죽었는가 (근거)

두 축의 `correlation_id`가 **분리된 네임스페이스**다:

- 예측(forecast) 측: thesis-style — 예 `hynix-reentry-thesis-0703`.
  `forecast_tools.py:53`은 심볼만 `.strip()`, correlation_id는 caller가 자유
  형식으로 넣음.
- 회고(retro) 측: exec-style — 예 `toss_live:...` / `live:<uuid>`.

동일 종목 `000660`이 양쪽에 존재해도 corr_id가 달라 `linkedCorrelationIds`
교집합이 **항상 공집합** → 앵커/링크가 절대 렌더되지 않음(구조적 dead code).

### 옵션 A: SYMBOL 키로 재키잉

교집합의 키를 correlation_id → **정규화된 심볼 키**로 바꾼다. 동일 종목이 예측·
회고 양쪽에 있으면 그 종목의 예측 행 ↔ 회고 행을 서로 점프하는 링크를 렌더한다.

**서버 측 심볼 정규화(근거 — 프런트 키가 이를 맞춰야 함):**

- 회고: `trade_retrospective_service.py:96-105 _normalize_symbol`
  - crypto → `KRW-<COIN>` (dash, upper)
  - equity_us → `to_db_symbol(x).upper()` = `-`/`/`→`.`, upper → 예 `BRK.B`
  - equity_kr/기타 → upper only (숫자코드 무변)
- 예측: `forecast_tools.py:53` = `.strip()`만 (upper/구분자 정규화 **없음**).
  → 같은 종목이라도 대소문자/구분자가 다를 수 있음(`BRK-B` vs `BRK.B`,
  `btc` vs `KRW-BTC`).

따라서 프런트 canonical 키는 **양쪽을 같은 형태로 접는다**: equity는
`upper + (-|/)→.`(app DB 관례 `to_db_symbol`와 동형), crypto는 기존
`stockDetailRouteSymbol("crypto", …)`(→ `KRW-BTC`) 재사용. market으로 접두(kr/us/
crypto) 하여 크로스마켓 충돌을 배제한다.

## 파일별 변경

### 신규: `frontend/invest/src/insightsCrosslink.ts`

크로스링크 심볼 키 헬퍼(순수 함수, 두 패널 공용 — 키 산출이 갈라지면 링크가 다시
죽으므로 단일 소스로 강제).

```ts
import { stockDetailRouteSymbol } from "./stockDetailPath";

export type CrosslinkMarket = "kr" | "us" | "crypto";

// forecast instrument_type("equity_kr"|"equity_us"|"crypto") → market
export function forecastMarket(instrumentType: string | null): CrosslinkMarket | null;

// retro는 market(str) 우선, 없으면 instrument_type로 폴백
export function retroMarket(
  market: string | null,
  instrumentType: string | null,
): CrosslinkMarket | null;

// 교집합 canonical 키. market·symbol 중 하나라도 없으면 null(링크 제외).
//   crypto → `crypto:${stockDetailRouteSymbol("crypto", s)}`  (예 crypto:KRW-BTC)
//   kr/us  → `${market}:${s.toUpperCase().replace(/[-/]/g, ".")}` (예 us:BRK.B, kr:000660)
export function crosslinkKey(market: CrosslinkMarket | null, symbol: string): string | null;

// 앵커/URL-fragment 안전 slug: 영숫자 외 → "-"
//   us:BRK.B → us-BRK-B, crypto:KRW-BTC → crypto-KRW-BTC, kr:000660 → kr-000660
export function crosslinkAnchorSlug(key: string): string;
```

- `INSTRUMENT_MARKET` 맵(`equity_kr→kr` 등)은 이 모듈에 단일 정의.
  `ForecastCalibrationPanel.tsx:33-37`의 로컬 복제는 유지(돈/href 표시용)하되,
  **크로스링크 키만** 이 모듈을 경유(블라스트 반경 최소화). 선택적으로 로컬
  맵도 `forecastMarket`로 대체 가능(리스크·결정 필요 참조).

### 수정: `frontend/invest/src/components/insights/ForecastCalibrationPanel.tsx`

- import 추가: `forecastMarket, crosslinkKey, crosslinkAnchorSlug` from
  `"../../insightsCrosslink"`.
- Props(`351-359`) 이름 변경(내부 API, additive-safe):
  `onClosedCorrelationIds` → `onClosedSymbolKeys`,
  `linkedCorrelationIds` → `linkedSymbolKeys`.
- 보고 effect(`402-408`): closed 행 → `crosslinkKey(forecastMarket(r.instrument_type), r.symbol)`
  로 매핑, non-null 필터 + **중복 제거**(Set) 후 `onClosedSymbolKeys(keys)`.
- `ClosedList`(`282-332`):
  - prop `linkedCorrelationIds` → `linkedSymbolKeys`.
  - map 밖에 `const anchored = new Set<string>()` 선언(앵커 id 중복 제거용).
  - 행마다 `const key = crosslinkKey(forecastMarket(r.instrument_type), r.symbol)`,
    `const slug = key ? crosslinkAnchorSlug(key) : null`,
    `const linked = key != null && (linkedSymbolKeys?.has(key) ?? false)`.
  - 앵커 id: `id={linked && slug && !anchored.has(key) ? \`forecast-${slug}\` : undefined}`
    (첫 행에만 부여; 이후 `anchored.add(key)`). — 한 종목이 여러 closed 예측을
    가질 때 **id 중복(invalid HTML)** 방지.
  - 링크: `<a href={\`#retro-${slug}\`}>회고↓</a>`.

### 수정: `frontend/invest/src/components/my/RetrospectivesPanel.tsx`

(공유 패널 — /my desktop·mobile에서도 사용. 크로스링크 props는 optional이라
그쪽은 no-op 유지 = additive-only.)

- import 추가: `retroMarket, crosslinkKey, crosslinkAnchorSlug`.
- Props(`73-81`) 이름 변경: `onCorrelationIds` → `onSymbolKeys`,
  `linkedCorrelationIds` → `linkedSymbolKeys`.
- 보고 effect(`114-120`): 행 → `crosslinkKey(retroMarket(r.market, r.instrument_type), r.symbol)`
  매핑, non-null + 중복 제거 후 `onSymbolKeys(keys)`.
- 행 렌더(`183-211`):
  - `rows.map` 밖에 `const anchored = new Set<string>()`.
  - `const key = crosslinkKey(retroMarket(row.market, row.instrument_type), row.symbol)`,
    `slug`, `linked = key != null && (linkedSymbolKeys?.has(key) ?? false)`.
  - 앵커 id: 첫 행에만 `retro-${slug}` (dedupe), 링크
    `<a href={\`#forecast-${slug}\`}>예측↑</a>`.

### 수정: `frontend/invest/src/pages/desktop/DesktopInsightsPage.tsx`

- 상태 이름 변경(`127-134`):
  `closedForecastIds` → `closedForecastKeys`, `retroIds` → `retroKeys`,
  memo `linkedCorrelationIds` → `linkedSymbolKeys` (교집합 로직 동일).
- 주석(`127-128`) 갱신: "by correlation_id" → "by normalized symbol key (ROB-682)".
- 패널 배선(`152-165`):
  `<ForecastCalibrationPanel onClosedSymbolKeys={setClosedForecastKeys} linkedSymbolKeys={linkedSymbolKeys} … />`,
  `<RetrospectivesPanel onSymbolKeys={setRetroKeys} linkedSymbolKeys={linkedSymbolKeys} />`.

### 테스트 파일(수정/신규)

- 신규 `frontend/invest/src/__tests__/insightsCrosslink.test.ts` — 헬퍼 단위.
- 수정 `frontend/invest/src/__tests__/ForecastCalibrationPanel.test.tsx`
  (ROB-678 케이스 `157-178` 재작성).
- 수정 `frontend/invest/src/__tests__/RetrospectivesPanel.test.tsx`
  (ROB-678 케이스 `51-69` 재작성).
- 수정 `frontend/invest/src/__tests__/DesktopInsightsPage.test.tsx`
  (disjoint corr_id + same symbol 회귀 테스트 추가).

## 구현 단계

1. **헬퍼 모듈 신설** — `insightsCrosslink.ts`에 `forecastMarket`, `retroMarket`,
   `crosslinkKey`, `crosslinkAnchorSlug` 작성. crypto는 `stockDetailRouteSymbol`
   재사용, equity는 `upper + (-|/)→.`.
2. **헬퍼 단위 테스트** — `insightsCrosslink.test.ts`:
   - `crosslinkKey(forecastMarket("equity_us"), "BRK-B") === "us:BRK.B"` 이고
     `crosslinkKey(forecastMarket("equity_us"), "brk.b") === "us:BRK.B"` (양쪽 접힘).
   - `crosslinkKey("crypto", "btc") === "crypto:KRW-BTC"`,
     `crosslinkKey("crypto", "KRW-BTC") === "crypto:KRW-BTC"`.
   - `crosslinkKey("kr", "000660") === "kr:000660"`.
   - `retroMarket("kr", null) === "kr"`, `retroMarket(null, "equity_us") === "us"`,
     `retroMarket(null, null) === null`, `crosslinkKey(null, "X") === null`.
   - `crosslinkAnchorSlug("us:BRK.B") === "us-BRK-B"`.
3. **ForecastCalibrationPanel 재배선** — props rename, 보고 effect를 심볼 키로,
   `ClosedList` 앵커/링크를 slug + dedupe로 교체.
4. **RetrospectivesPanel 재배선** — 동일 패턴(공유 패널이므로 optional props
   유지 확인).
5. **DesktopInsightsPage 재배선** — 상태/ memo/ prop 이름 및 교집합.
6. **패널 테스트 갱신** — 아래 "테스트" 참조.
7. **DesktopInsightsPage 통합 회귀 테스트** 추가.
8. `cd frontend/invest && npm run typecheck && npm test` 그린 확인.

## 테스트

- `insightsCrosslink.test.ts` (신규, 단위): 위 2단계 케이스.
- `ForecastCalibrationPanel.test.tsx` — ROB-678 케이스 재작성:
  closed 예측 심볼 `AAPL`/`equity_us`, correlation_id는 **무관한 thesis-style**
  (예 `"aapl-thesis-0704"`). `<ForecastCalibrationPanel linkedSymbolKeys={new Set(["us:AAPL"])} />`.
  단언: `link href === "#retro-us-AAPL"`, `getElementById("forecast-us-AAPL")` 존재.
- `RetrospectivesPanel.test.tsx` — ROB-678 케이스 재작성:
  회고 심볼 `005930`/`market:"kr"`, corr_id는 exec-style(`"toss_live:x"`).
  `linkedSymbolKeys={new Set(["kr:005930"])}`. 단언: `href === "#forecast-kr-005930"`,
  `getElementById("retro-kr-005930")` 존재.
- `DesktopInsightsPage.test.tsx` — **핵심 회귀**: fetch mock으로 calibration/open/
  closed(심볼 `AAPL`, corr `"aapl-thesis"`) + retrospectives(심볼 `AAPL`, corr
  `"toss_live:uuid"`) + next-actions 반환. corr_id는 서로 disjoint지만 심볼 동일 →
  크로스링크 `회고↓`/`예측↑` 링크가 렌더됨을 단언(ROB-678 스킴이었다면 dead).
  추가로 두 축 심볼이 다른 경우(교집합 없음)엔 링크 부재도 단언.
- 전체: `npm run typecheck`, `npm test` 그린.

## 리스크·결정 필요

**결정 필요(사용자 사인오프 후 구현):**

1. **앵커 id 충돌 처리** — 한 종목이 closed 예측 N개 + 회고 M개를 가질 수 있어
   `#forecast-<symbol>`/`#retro-<symbol>`가 모호. **권장(v1)**: 종목당 **첫 행에만**
   앵커 id 부여(dedupe), 링크는 그 첫 행으로 점프(순수 앵커, JS 무). 링크는 모든
   매칭 행에 표시 가능. **선택 확장(defer)**: "회고 N건↓" 카운트 배지 — 각 패널이
   상대 축의 건수를 모르므로 페이지가 `Map<key,{forecastN,retroN}>`를 추가로 내려야
   함(플럼빙 증가). scroll+highlight(JS)도 옵션. → 승인 요청: v1=첫 행 앵커로 확정?
2. **키 입도(granularity)** — `market:symbol`(예 `kr:000660`) vs bare `symbol`.
   **권장**: market 접두(크로스마켓 동일 티커 오매칭 방지). market이 한쪽이라도
   null이면 링크 제외. → 승인: market-qualified 확정?
3. **날짜 근접 필터(optional)** — 심볼-only는 같은 종목의 **오래된 무관한 회고**를
   최신 예측에 링크할 수 있음. **권장(v1)**: 필터 OFF(심볼-only 단순). 필요 시
   forecast `resolved_at` ↔ retro `created_at` ±N일 창 가드 추가 가능(양측 날짜
   존재 시에만). → 승인: v1에서 근접 필터 생략 OK? (원하면 N일 값 지정)
4. **구분자 정규화** — equity는 `upper + (-|/)→.`(app `to_db_symbol` 동형, 회고
   서버 저장형 `BRK.B`와 일치). crypto는 `KRW-<COIN>`. → 승인: 이 정규화 규칙 OK?
   (엣지: crypto 심볼이 dot형 `BTC.KRW`로 오면 `normalizeCryptoRouteSymbol`이
   오정규화 가능 — 실데이터는 Upbit `KRW-BTC` 형이라 발생 가능성 낮음. 리스크로
   기록.)
5. **prop 이름 변경** — 공유 `RetrospectivesPanel`의 크로스링크 props를
   correlation→symbolKey 의미로 rename. /my(desktop·mobile)는 미전달이라 무영향
   (additive-only). → 관례상 진행하되 확인.

**리스크:**

- **ROB-681과 파일 충돌**: `DesktopInsightsPage.tsx`를 양쪽이 편집 → 병합 시
  섹션 배선/ import 충돌 가능. 상태·memo·패널 prop 라인이 근접하므로 rebase 주의.
- **키 산출 드리프트**: 두 패널이 다른 방식으로 키를 만들면 링크가 재-사망.
  단일 `insightsCrosslink.ts`로 강제하여 완화.
- crypto dot형 오정규화(위 4의 엣지) — 실데이터에선 저확률, 헬퍼 테스트로
  KRW- 형 커버.
- 서버 정규화 변경이 미래에 갈라지면 매칭 실패 — 프런트 정규화가 서버(`to_db_symbol`,
  `_normalize_symbol`)와 동형임을 주석으로 명시해 회귀 방지.

**스코프 밖 확인:** `/insights`는 `routes.tsx:86`에서 `DesktopInsightsPage`로
직결(모바일 뷰포트 dispatch 없음) — 크로스링크는 데스크톱 인사이트에만 존재.
백엔드/API/스키마 변경 없음(순수 프런트 read-only). DB·migration 0.
