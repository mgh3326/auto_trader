# ROB-681 — /insights viewport 분기 (mobile dispatch)

## 배경

`/invest/insights` 는 현재 `routes.tsx` 에서 **뷰포트 분기 없이** `DesktopInsightsPage` 를
그대로 렌더한다 (`routes.tsx:86` — `{ path: "/insights", element: <DesktopInsightsPage /> }`).
반면 다른 canonical 서페이스(`/`, `/my`, `/feed/news`, `/discover`, `/calendar`, `/reports`)는
모두 `useViewport() === "mobile"` 로 모바일 셸을 고르는 Route 래퍼를 통해 렌더된다
(`InvestHomeRoute`, `InvestPortfolioRoute`, `FeedNewsRoute`, `InvestDiscoverRoute`,
`CalendarRoute`, `InvestmentReportsRoute`).

결과적으로 모바일에서 `/invest/insights` 로 딥링크하면(ROB-672 가 추가한 모바일 홈의
"인사이트" 카드 `MobileHomePage.tsx:46-51` 포함) `DesktopShell`(3-컬럼 데스크톱 레이아웃)이
그대로 뜬다 — 하단 탭바/모바일 상단바 없이, 900px 미만 화면에 데스크톱 셸이 노출된다.

**목표**: `/insights` 를 `InvestHomeRoute` 패턴대로 뷰포트 분기시킨다. 최소 변경 원칙:
`InvestInsightsRoute` 래퍼 + `MobileInsightsPage`(동일 인사이트 패널을 `MobileShell` 로 감싸
재사용). **`DesktopInsightsPage.tsx` 는 편집하지 않는다** — 형제 이슈 ROB-682 가 그 파일을
편집하므로 병합 충돌을 피한다.

### 재사용 가능성 조사 결과 (grounding)

`DesktopInsightsPage.tsx` 가 렌더하는 무거운 패널들은 **전부 자체 파일에서 export** 되고,
셸에 독립적이며(Card 기반, 자체 fetch), **props 는 전부 optional** 이다:

| 패널/컴포넌트 | export 위치 | 관련 props (전부 optional) |
|---|---|---|
| `MarketParityStrip` | `components/home/MarketParityStrip` | `state`, `reload` (hook 주입) |
| `CommonPreferredDisparityCardView` | `components/CommonPreferredDisparityCard` | `data` (hook 주입) |
| `ForecastCalibrationPanel` | `components/insights/ForecastCalibrationPanel` | `onEmptyChange?`, `onClosedCorrelationIds?`, `linkedCorrelationIds?` |
| `RetrospectivesPanel` | `components/my/RetrospectivesPanel` | `compact?`, `onCorrelationIds?`, `linkedCorrelationIds?` |
| `AnalysisArtifactPanel` | `components/insights/AnalysisArtifactPanel` | `onEmptyChange?` |
| `SessionContextTimelinePanel` | `components/insights/SessionContextTimelinePanel` | `onEmptyChange?` |
| `PageSafetyNote` | `components/PageSafetyNote` | `routeId`, `heading`, `tag`, `items` |

`DesktopInsightsPage.tsx` 내부에만 존재하고 **export 되지 않는** 것은 다음 소형 헬퍼와
페이지 조정(coordination) 상태뿐이다:
- 헬퍼: `SectionStatus`, `PageHeader`, `Section`, `AccumulatingBanner`,
  `ReadOnlyGuardrailNote`, `RelatedScreensCard` (각 ~10–40줄의 정적 레이아웃)
- 조정 상태: `forecastEmpty/artifactEmpty/sessionEmpty` → `allDataEmpty` 배너(ROB-677),
  `closedForecastIds/retroIds` → `linkedCorrelationIds` 교차링크 memo(ROB-678)

**결론**: `DesktopInsightsPage.tsx` 를 **편집할 필요 없음**. 무거운 패널은 그대로 import 재사용하고,
export 되지 않은 소형 헬퍼·조정 상태만 `MobileInsightsPage` 에 소량 복제(duplicate)한다.
공유 모듈로 추출(=`DesktopInsightsPage.tsx` 편집)하는 대안은 ROB-682 와 충돌하므로 채택하지 않는다.

## 파일별 변경

### 신규 `frontend/invest/src/pages/mobile/MobileInsightsPage.tsx`
모바일 인사이트 페이지. `MobileShell title="인사이트"` 로 감싸고, 데스크톱과 **동일한 export 패널**을
재사용한다. `DesktopInsightsPage.tsx` 의 조정 상태/소형 헬퍼를 로컬로 복제하여 파리티 유지:
- hook: `useMarketParity()`, `useCommonPreferredDisparity()` (데스크톱과 동일)
- 상태: `forecastEmpty/artifactEmpty/sessionEmpty` + `allDataEmpty`,
  `closedForecastIds/retroIds` + `linkedCorrelationIds` memo (DesktopInsightsPage.tsx:121-134 그대로)
- 로컬 헬퍼: `SectionStatus`/`PageHeader`/`Section`/`AccumulatingBanner`/`ReadOnlyGuardrailNote`/
  `RelatedScreensCard` 를 모바일 여백(가로 16px, 다른 모바일 페이지와 동일)에 맞춰 복제.
  헤더 `h1` 폰트는 모바일 톤(예: 22px)으로 축소.
- 패널 렌더 순서/props 는 데스크톱과 동일. 단 `RetrospectivesPanel` 에 `compact` 를 전달하여
  모바일에서 8행으로 제한(패널이 이미 지원: `RetrospectivesPanel.tsx:74,94`).
- 컨테이너 padding 은 데스크톱의 `padding:24` → 모바일은 세로 gap 위주(`14px 0 16px` 등,
  `MobileHomePage`/`MobileDiscoverPage` 관례) + 섹션별 `padding:"0 16px"`.

### 신규 `frontend/invest/src/pages/InsightsRoute.tsx`
`InvestInsightsRoute` 래퍼(export). `InvestHomeRoute`(`DesktopHomePage.tsx:33-36`) 와 동형:
```tsx
export function InvestInsightsRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileInsightsPage /> : <DesktopInsightsPage />;
}
```
`DesktopInsightsPage` 와 `MobileInsightsPage` 를 둘 다 import 한다. 다른 서페이스는 래퍼를
데스크톱 페이지 파일에 두지만(관례), ROB-682 가 `DesktopInsightsPage.tsx` 를 편집하므로
**충돌 회피를 위해 별도 파일**에 둔다. 테스트가 래퍼를 직접 import 하기에 named export 필요.

### 신규 `frontend/invest/src/__tests__/InvestInsightsRoute.test.tsx`
`InvestHomeRoute.test.tsx` 를 미러한 뷰포트 분기 테스트(아래 테스트 섹션 참고).

### 편집 `frontend/invest/src/routes.tsx` (유일하게 편집하는 기존 product 파일)
- import 교체: `import { DesktopInsightsPage } from "./pages/desktop/DesktopInsightsPage";`
  → `import { InvestInsightsRoute } from "./pages/InsightsRoute";`
- element 교체: `{ path: "/insights", element: <DesktopInsightsPage /> }`
  → `{ path: "/insights", element: <InvestInsightsRoute /> }` (`routes.tsx:86`)
- (선택) 상단 라우트 계약 주석 `routes.tsx:20` 에 "responsive" 표기 보강.

## 구현 단계

1. **`MobileInsightsPage.tsx` 작성**
   - `DesktopInsightsPage.tsx` 를 참조로, 소형 헬퍼(`SectionStatus`/`PageHeader`/`Section`/
     `AccumulatingBanner`/`ReadOnlyGuardrailNote`/`RelatedScreensCard`)를 모바일 여백으로 복제.
   - `useMarketParity`/`useCommonPreferredDisparity` + 3개 empty flag + crosslink memo 복제
     (`DesktopInsightsPage.tsx:115-134`).
   - `MobileShell title="인사이트"` 안에 섹션 순서(시장 관찰 → 판단 품질 → 학습·회고 → 세션 기록
     → 관련 화면 → 가드레일)로 동일 패널 배치. `RetrospectivesPanel` 에 `compact` 추가.
   - `ReadOnlyGuardrailNote` 는 `PageSafetyNote routeId="insights"` 를 그대로 사용(데스크톱과
     동일 dismiss 키 공유 — 의도된 동작).
2. **`InsightsRoute.tsx` 작성** — `useViewport` 분기 래퍼(위 스니펫).
3. **`routes.tsx` 배선** — import + element 교체(`/insights`).
4. **테스트 추가** — `InvestInsightsRoute.test.tsx`.
5. **검증** — `cd frontend/invest && npm run typecheck && npm test`.

## 테스트

- **뷰포트 분기 (핵심, `InvestInsightsRoute.test.tsx`)**: `InvestHomeRoute.test.tsx` 미러.
  - `useMarketParity`/`useCommonPreferredDisparity` 를 loading 스텁으로 `vi.mock` 하고,
    자체 fetch 하는 4개 패널의 api 모듈(`api/forecasts`, `api/analysisArtifacts`,
    `api/sessionContext`, `api/retrospectives`)은 빈 배열/no-op 로 `vi.mock`(또는 `mockRightRail`
    관례 재사용)하여 동기 렌더.
  - `setWidth(1280)` → `getByTestId("desktop-shell")` 존재 & `queryByTestId("mobile-shell")` null.
  - `setWidth(600)` → `getByTestId("mobile-shell")` 존재 & `queryByTestId("desktop-shell")` null.
  - `MemoryRouter basename="/invest" initialEntries={["/invest/insights"]}` 사용.
- **모바일 스모크(선택)**: `setWidth(600)` 로 `MobileInsightsPage` 렌더 → `mobile-shell`,
  `mobile-top-bar`(제목 "인사이트"), heading "인사이트" 존재.
- **라우터 경로(선택)**: `routes.test.tsx` 스타일로 `/insights` 경로가 여전히 노출되는지 확인.
- 기존 `DesktopInsightsPage.test.tsx` 는 파일 미편집이므로 그대로 통과해야 함(회귀 없음).
- 전체: `cd frontend/invest && npm run typecheck && npm test`.

## 리스크·결정 필요

- **결정 1 — 래퍼 파일 위치**: (권장) 신규 `pages/InsightsRoute.tsx` 에 두어
  `DesktopInsightsPage.tsx` 무편집 → ROB-682 와 파일 충돌 0. 대안은 관례대로
  `DesktopInsightsPage.tsx` 에 `InvestInsightsRoute` 추가지만 ROB-682 편집과 겹쳐 비권장.
  (두 경우 모두 `routes.tsx` 는 편집 필요.)
- **결정 2 — 조정 상태 복제 vs 공유 추출**: (권장) 소형 헬퍼·조정 상태를 `MobileInsightsPage`
  에 복제(중복 ~40–60줄, 순수 레이아웃). 공유 모듈 추출은 `DesktopInsightsPage.tsx` 편집이
  필요해 ROB-682 와 충돌하므로 이번엔 배제. **드리프트 리스크**: ROB-682 가 데스크톱 IA/섹션을
  바꾸면 모바일이 뒤처질 수 있음 → ROB-682 머지 후 공유 추출 후속(follow-up)으로 정리 가능.
- **리스크 — 넓은 표의 가로 오버플로우**: `ForecastCalibrationPanel`/`AnalysisArtifactPanel` 등은
  내부 표가 넓어 좁은 화면에서 가로 스크롤/오버플로우가 생길 수 있음. `MobileShell__scroll` 은
  세로만 관리하므로 패널 내부 가로 처리는 패널 책임. 최소 버전에선 "패널 그대로 재사용" 수용,
  가로 스크롤 컨테이너 폴리시는 후속 폴리시로 명시.
- **리스크 — 하단 탭 비활성**: `MobileBottomNav` 에 "인사이트" 탭이 없어(홈/MY/뉴스/발견/캘린더)
  인사이트에선 어떤 탭도 활성화되지 않음. 이는 `/reports` 모바일과 동일한 기존 동작으로 수용.
- **`routes.tsx` 공유면**: 이번 이슈가 편집하는 유일한 기존 product 파일. 다른 인사이트 형제
  이슈(680/683 등)가 `routes.tsx` 에 라우트를 추가/변경하면 병합 충돌 가능 — import 블록과
  `/insights` 라인만 건드리므로 충돌면은 최소.
