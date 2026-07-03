# ROB-683 — Crypto artifact 종목 링크 malformed 수정

## 배경

`/invest/insights` 의 분석 아티팩트 패널(`AnalysisArtifactPanel`)에서 코인 종목 링크가
깨진 URL 로 렌더된다.

LIVE evidence:
- 아티팩트의 crypto `symbols[]` 는 **DB dot-format** 로 저장/서빙된다 (`KRW.XRP`, `KRW.ETH`, `KRW.SOL`).
- 근거(백엔드): `app/services/analysis_artifact.py` 는 심볼 필터/조회 시
  `app/core/symbol.py::to_db_symbol` 를 사용한다 (`to_db_symbol` = `-`/`/` → `.`).
  즉 `analysis_artifacts.symbols` 컬럼의 crypto 값은 quote·base 를 점으로 구분한
  `KRW.XRP` 형태이며, GIN 필터(`.op("@>")`, `.op("&&")`)도 dot-format 을 전제한다.
- 프론트 `stockDetailPath('crypto', 'KRW.XRP')` →
  `normalizeCryptoRouteSymbol('KRW.XRP')` 로 들어가면:
  - `startsWith("KRW-")` false, `endsWith("-KRW")` false, `includes("-")` false
  - → 마지막 분기에서 `KRW-${clean}` = **`KRW-KRW.XRP`** 를 반환 (오작동)
  - → href `/invest/stocks/crypto/KRW-KRW.XRP` (broken; 상세 페이지 404/미스매치)
- KR(`000660`)·US(`BRK-B`)·이미 잘 되는 crypto dash 형(`KRW-JUP`, `BTC` bare, `BTC-KRW`)은 정상.

수정 방향: `normalizeCryptoRouteSymbol` 이 **dot-format crypto 심볼을 dash 로 정규화**하도록
한다 (`KRW.XRP` → `KRW-XRP`). 이미 정상인 dash 형(`KRW-JUP`)·bare 형(`XRP`)은 불변이어야 한다.

### 수정 위치 결정: 공유 헬퍼 (`stockDetailPath.normalizeCryptoRouteSymbol`)

- **채택: 공유 `normalizeCryptoRouteSymbol` 수정** — 가장 안전하고 범위가 넓다.
  `normalizeCryptoRouteSymbol` 은 crypto 라우트에서만 호출된다
  (`stockDetailRouteSymbol` line 34 가 non-crypto 는 `cleanSymbol` 그대로 반환하며 가드).
  따라서 dot→dash 정규화는 KR/US 심볼에 절대 닿지 않는다.
- 이 한 곳을 고치면 crypto dot-format 을 소비할 수 있는 **모든** 호출부가 함께 고쳐진다:
  `AnalysisArtifactPanel`(dot-format), `SessionContextTimelinePanel`(`refs.symbols`),
  그리고 `stockDetailRouteSymbol` 를 쓰는 `RightRemotePanel` 등.
- **기각: AnalysisArtifactPanel-only 수정** — 같은 dot-format 버그가 세션 타임라인 등
  다른 crypto 소비자에서 재발할 수 있어 근본 해결이 아니다.
- **기각: 백엔드에서 dash 형으로 emit** — 백엔드 canonical 은 dot 이고,
  `analysis_artifacts.symbols` GIN 필터가 `to_db_symbol`(dot) 을 전제하므로 저장형을
  바꾸면 필터·조회가 깨진다. 프론트 라우트 정규화가 올바른 계층.

호출부 전수 확인(grep `stockDetailPath`/`stockDetailRouteSymbol`):
crypto 심볼을 넘길 수 있는 곳 — insights: `AnalysisArtifactPanel`, `SessionContextTimelinePanel`,
`ForecastCalibrationPanel`(instrument_type→market 매핑); my: `RetrospectivesPanel`(retro·next-action,
**dash 형 `KRW-JUP`**), `UnifiedHoldingsTable`, `WatchAlertsPanel`, `SellHistoryPanel`,
`BuyHistoryPanel`, `CurrentOrdersPanel`; mobile: `MobilePortfolioPage`; desktop: `RightRemotePanel`.
retro/next-action 은 dash 형을 넘기므로 이번 수정 후에도 **불변**이어야 한다(회귀 테스트로 고정).

## 파일별 변경

### 기존 파일 (수정)

- **`frontend/invest/src/stockDetailPath.ts`** — `normalizeCryptoRouteSymbol` 의 `clean`
  정규화 단계에 dot→dash 변환을 추가한다. 나머지 분기 로직(`KRW-` prefix / `-KRW` suffix /
  bare → `KRW-`)은 그대로 두면 dot 형이 dash 형과 동일 경로로 흡수된다.

  현재:
  ```ts
  const clean = symbol.trim().toUpperCase();
  ```
  변경:
  ```ts
  // Crypto DB symbols arrive dot-format (KRW.XRP; app/core/symbol.to_db_symbol).
  // Fold "." → "-" so KRW.XRP joins the KRW- dash path instead of falling through
  // to the bare-symbol branch (which would emit KRW-KRW.XRP). Dash/bare forms
  // already normalized here are unaffected since they contain no ".".
  const clean = symbol.trim().toUpperCase().replace(/\./g, "-");
  ```

  변환 후 분기 추적:
  - `KRW.XRP` → `KRW-XRP` → `startsWith("KRW-")` → `KRW-XRP` ✓
  - `KRW-JUP` → `KRW-JUP`(dot 없음) → `startsWith("KRW-")` → `KRW-JUP` (불변) ✓
  - `XRP` → `XRP`(dot 없음) → no `-` → `KRW-XRP` (불변) ✓
  - `BTC-KRW` → `BTC-KRW` → `endsWith("-KRW")` → `KRW-BTC` (불변) ✓
  - `BTC.ETH`(BTC 마켓 dot 형, 방어적) → `BTC-ETH` → `includes("-")` → `BTC-ETH` (마켓 접두 보존) ✓

### 기존 파일 (테스트 추가)

- **`frontend/invest/src/__tests__/stockDetailPath.test.ts`** — dot-format 회귀 케이스와
  dash/bare 불변 케이스를 추가.

### 신규 파일

없음. (마이그레이션 0, 백엔드 변경 없음, 스키마 변경 없음)

## 구현 단계

1. `frontend/invest/src/stockDetailPath.ts` line 20 `const clean = symbol.trim().toUpperCase();`
   를 `.replace(/\./g, "-")` 를 붙인 형태로 교체하고, 위 주석을 추가한다.
   함수의 나머지 분기(21–25행)는 수정하지 않는다.
2. `frontend/invest/src/__tests__/stockDetailPath.test.ts` 에 회귀 테스트 블록을 추가한다
   (아래 "테스트" 참조). 기존 테스트는 그대로 통과해야 한다.
3. `cd frontend/invest && npm run typecheck` (tsc) 로 타입 회귀 없음 확인.
4. `cd frontend/invest && npm test -- stockDetailPath` 로 신규·기존 케이스 green 확인.
5. (선택, 시각 확인) `AnalysisArtifactPanel` 렌더 시 crypto 링크가
   `/invest/stocks/crypto/KRW-XRP` 로 생성되는지 dogfooding — 코드 변경은 없음.

## 테스트

`frontend/invest/src/__tests__/stockDetailPath.test.ts` 에 추가:

```ts
test("stock detail path normalizes dot-format crypto symbols (KRW.XRP → KRW-XRP)", () => {
  expect(stockDetailPath("crypto", "KRW.XRP")).toBe("/stocks/crypto/KRW-XRP");
  expect(stockDetailPath("CRYPTO", "KRW.ETH")).toBe("/stocks/crypto/KRW-ETH");
  expect(stockDetailPath("crypto", "krw.sol")).toBe("/stocks/crypto/KRW-SOL");
  expect(stockDetailRouteSymbol("crypto", "KRW.XRP")).toBe("KRW-XRP");
});

test("stock detail path leaves already-valid crypto dash/bare forms unchanged", () => {
  // retro / next-action symbols arrive dash-form; must not regress.
  expect(stockDetailPath("crypto", "KRW-JUP")).toBe("/stocks/crypto/KRW-JUP");
  expect(stockDetailPath("crypto", "XRP")).toBe("/stocks/crypto/KRW-XRP");
});
```

기존 테스트(`BTC`/`btc`/`BTC-KRW`/`KRW-BTC` → `KRW-BTC`, KR `005930`, US `BRK-B`)는
변경 없이 통과해야 한다 — dot 미포함 입력은 `replace` 가 no-op 이기 때문.

실행:
- `cd frontend/invest && npm test -- stockDetailPath`
- `cd frontend/invest && npm run typecheck`
- (전체) `cd frontend/invest && npm test`

## 리스크·결정 필요

- **리스크: 낮음.** 순수 문자열 정규화 한 줄, crypto 라우트 전용 경로(비-crypto 는 line 34
  가드로 미도달), 마이그레이션·백엔드·스키마 변경 없음.
- **dash 형 회귀 없음 보장:** dot 미포함 입력에는 `replace(/\./g, "-")` 가 no-op 이므로
  `KRW-JUP`/`XRP`/`BTC-KRW`/`005930`/`BRK-B` 등 기존 형태의 출력은 바이트 동일. 회귀 테스트로 고정.
- **BTC/USDT 마켓 dot 형:** Upbit 은 KRW 외 BTC/USDT 마켓도 존재(`BTC.ETH` 등). dot→dash
  변환은 마켓 접두를 보존(`BTC-ETH`)하므로 KRW 하드코딩보다 견고 — 별도 KRW-only 분기보다
  `replace` 전역 치환을 선택한 이유.
- **범위 밖(결정 불요, 명시만):**
  - 백엔드 저장형(dot)·GIN 필터는 그대로 둔다 — canonical 유지.
  - 모바일 뷰포트 dispatch(insights 모바일 셸)는 이 이슈 범위 아님. 링크 정규화는 공유
    헬퍼라 데스크톱/모바일 양쪽에 자동 적용됨(추가 작업 없음).
  - crypto 심볼의 bare→KRW 가정(`XRP` → `KRW-XRP`)은 기존 동작으로 유지(BTC/USDT 마켓
    bare 심볼 구분은 pre-existing 한계, 이번 스코프 아님).
- **결정 필요: 없음.** 수정 위치(공유 헬퍼)·정규화 방식(dot→dash 전역 치환) 모두 코드 근거로 확정.
