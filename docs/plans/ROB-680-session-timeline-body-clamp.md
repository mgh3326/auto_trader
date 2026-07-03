# ROB-680 — 세션 타임라인 본문 클램프 (판단 품질 above the fold)

## 배경

`/invest/insights`(desktop 전용, `src/routes.tsx` `{ path: "/insights", element: <DesktopInsightsPage /> }`)의
**최근 핸드오프**(`SessionContextTimelinePanel`) 패널이 라이브에서 약 5370px 높이로
페이지를 지배한다. 관측:

- `fetchRecentSessionContext({ limit: 15 })` — 15행만 렌더하는데도 패널이 초대형.
  라이브 데이터는 47건 중 34건(72%)이 `entry_type="decision"`.
- 원인: `SessionRow`의 본문 div가 **본문 전체를 `white-space: pre-wrap`으로** 렌더한다
  (`SessionContextTimelinePanel.tsx:77`):
  ```tsx
  <div style={{ fontSize: 13, opacity: 0.85, whiteSpace: "pre-wrap" }}>{entry.body}</div>
  ```
  긴 결정 메모(여러 줄/장문)가 그대로 펼쳐져 각 행이 수십~수백 px로 커진다.
- 결과: 페이지 상단의 **판단 품질**(`ForecastCalibrationPanel`) 섹션이 아래로 밀려
  스캔 불가. (섹션 순서는 ROB-677에서 이미 시장 관찰 → 판단 품질 → 학습·회고 →
  세션 기록으로 정리됨. 그럼에도 세션 기록 패널 자체 높이가 페이지 전체 스크롤을
  비대하게 만들고, 세션 기록 위 카드들의 "read density"를 떨어뜨림.)

**목표**: 각 타임라인 행의 본문을 ~3줄로 시각적으로 클램프하고, 길이가 클램프
임계를 넘는 행에만 per-row **더보기/접기** 토글(`useState`)을 붙여 타임라인을
scannable하게 만든다. 이렇게 하면 세션 기록 패널이 압축되어 위 섹션들의 밀도가
회복되고 판단 품질이 above the fold로 올라온다.

**보존해야 할 기존 배선(회귀 금지)**:
- ROB-673: `SessionRow` refs 푸터(심볼 `stockDetailPath` 링크 / `주문 …` / `리포트 …`).
- ROB-676: `ENTRY_TYPE_TONE` Pill 색상 + `groupByDate` per-`kst_date` 그룹 헤더.
- ROB-679: 행에는 시간만(`hhmm(entry.created_at)`), 날짜는 그룹 헤더에만.
- ROB-677: `onEmptyChange` → 페이지 accumulating 배너.

기존 코드베이스에 **동형 패턴이 이미 존재**하므로 그대로 차용한다:
- CSS line-clamp: `src/components/signals/SignalCard.tsx:99-111`
  (`display: -webkit-box` + `WebkitBoxOrient: "vertical"` + `WebkitLineClamp: 2` + `overflow: "hidden"`).
- per-row 더보기/접기 토글 버튼: `src/components/news/NewsListItem.tsx:215-235`
  (transparent 배경, `fontSize:12`, `fontWeight:700`, `aria-expanded`/`aria-controls`/`aria-label`,
  라벨 `요약 접기`/`요약 보기`) 및 `src/components/discover/IssueCard.tsx:92-116`
  (`aria-expanded` + `aria-controls` + 더보기/접기 aria-label).

## 파일별 변경

### 기존 파일

1. **`frontend/invest/src/components/insights/SessionContextTimelinePanel.tsx`** (핵심)
   - 파일 상단에 클램프 상수 추가: `const CLAMP_LINES = 3;` 와 본문이 "길다"고
     간주할 임계 `const CLAMP_CHAR_THRESHOLD = 160;` (레이아웃 측정 없이 결정 가능한
     content-based 휴리스틱 — jsdom 테스트 결정성 확보).
   - 순수 헬퍼 `isBodyClampable(body: string): boolean` 추가:
     `body.split("\n").length > CLAMP_LINES || body.length > CLAMP_CHAR_THRESHOLD`.
   - 새 서브컴포넌트 `ExpandableBody({ body }: { body: string })`:
     - `const [expanded, setExpanded] = useState(false);` — **per-row 로컬 상태**
       (행은 `entry_uuid`로 keyed & ROB-676 그룹 내 순서 안정 → 로컬 상태 안전).
     - `const clampable = isBodyClampable(body);`
     - 본문 div: 항상 `whiteSpace: "pre-wrap"` 유지. `clampable && !expanded`일 때만
       클램프 스타일 적용:
       ```
       display: "-webkit-box", WebkitBoxOrient: "vertical",
       WebkitLineClamp: CLAMP_LINES, overflow: "hidden"
       ```
       그 외(짧은 본문 또는 expanded)에는 `display: "block"`, 클램프 없음.
     - `clampable`일 때만 토글 `<button>` 렌더:
       - `type="button"`, `onClick={() => setExpanded(v => !v)}`
       - `aria-expanded={expanded}`, `aria-controls={bodyId}`,
         `aria-label={expanded ? "본문 접기" : "본문 더보기"}`
       - `data-testid="session-row-toggle"` (테스트 앵커)
       - 라벨 텍스트 `{expanded ? "접기" : "더보기"}`, 스타일은 NewsListItem 토글과
         동형(transparent bg, `fontSize:12`, `fontWeight:700`, `color: var(--fg-3)`,
         `cursor:pointer`, `border:none`, `padding:"2px 0"`, `fontFamily:"inherit"`).
       - 본문 div에는 `id={bodyId}`(예: `` `sess-body-${entry_uuid}` `` — id는
         props로 전달하거나 `useId`로 생성) 부여해 `aria-controls`와 연결.
   - `SessionRow`의 기존 본문 라인(`:77`)을 `<ExpandableBody body={entry.body} />`
     (또는 `entry_uuid`를 함께 넘겨 id 생성)로 교체. **refs 푸터/Pill/시간/그룹 로직은
     불변** — `ExpandableBody`는 title div와 refs 푸터 사이에만 삽입.
   - import: `useState`는 이미 있음. id 생성에 `useId`를 쓰면 `react`에서 추가 import
     (또는 `entry_uuid` 기반 문자열 id로 import 없이 처리 — 선호).

2. **`frontend/invest/src/__tests__/SessionContextTimelinePanel.test.tsx`**
   - 기존 유일 테스트(그룹/톤/hhmm/refs)는 **본문이 짧아** 토글이 없어야 하므로:
     - 짧은 두 행에는 `queryByTestId("session-row-toggle")`가 없거나 개수 0임을 단언
       추가(회귀 가드).
   - **새 테스트 케이스** "clamps long bodies behind a 더보기 toggle":
     - fixture에 `entry_type="decision"`, `body`가 6줄(`"a\nb\nc\nd\ne\nf"`) 또는
       200+자 장문인 세 번째(또는 별도 fixture) 행 추가.
     - `render` 후 해당 행에 `getByTestId("session-row-toggle")` 존재,
       초기 `aria-expanded="false"`, 라벨 `더보기`.
     - **본문 텍스트 자체는 DOM에 존재**함을 단언(jsdom은 CSS 클램프를 계산하지 않아
       full text가 항상 렌더됨 — 텍스트 접근성 회귀 없음 확인).
     - `fireEvent.click(toggle)` → `aria-expanded="true"`, 라벨 `접기`로 토글됨을 단언.
   - `@testing-library/react`의 `fireEvent`(또는 이미 import된 것) 사용, 기존
     `MemoryRouter` 래핑 유지.

### 신규 파일
없음. (`ExpandableBody`는 동일 파일 내 서브컴포넌트.)

## 구현 단계 (순서/구체 위치)

1. `SessionContextTimelinePanel.tsx` 상단(현재 `ENTRY_TYPE_TONE` 근처, `:22` 위/아래)에
   `CLAMP_LINES`, `CLAMP_CHAR_THRESHOLD`, `isBodyClampable()` 순수 헬퍼 추가.
2. `SessionRow`(`:54`) 위 또는 아래에 `ExpandableBody` 서브컴포넌트 정의
   (SignalCard의 clamp 스타일 + NewsListItem의 토글 버튼 스타일 차용).
3. `SessionRow` 본문 라인(`:77`)을 `<ExpandableBody ... />` 호출로 교체. id 연결을 위해
   `entry.entry_uuid`(또는 title div의 `bodyId`)를 넘긴다. **refs 푸터(`:78-107`) 이전에
   위치**시켜 시각 순서(제목 → 본문 → refs) 유지.
4. 테스트 파일에 짧은 본문 no-toggle 회귀 단언 + 장문 toggle 케이스 추가.
5. 로컬 검증:
   `cd frontend/invest && npm run typecheck && npm test -- SessionContextTimelinePanel`.
6. 전체 스위트 스모크: `cd frontend/invest && npm test`(insights 관련 파일만이라도).

## 테스트

- **단위/컴포넌트(vitest + jsdom, `frontend/invest/src/__tests__/SessionContextTimelinePanel.test.tsx`)**:
  - 회귀: 기존 그룹 헤더(`2026-07-03`/`2026-07-02`), 톤(`decision=gain`,
    `handoff_note=paper`), hhmm(`09:00`/`22:00`), refs 링크/주문 단언 **전부 그대로 통과**.
  - 신규: 짧은 본문 → 토글 없음. 장문 본문 → 토글 존재 + 클릭 시
    `aria-expanded`/라벨(`더보기`↔`접기`) 토글 + 본문 텍스트 상시 DOM 존재.
- **typecheck**: `npm run typecheck`(신규 서브컴포넌트 props 타입 포함).
- **수동/시각(선택, 배포 후)**: `/invest/insights`에서 세션 기록 패널이 압축되고
  각 결정 행이 ~3줄로 클램프되며 더보기로 펼쳐지는지, refs/시간/그룹/톤이 유지되는지 확인.

주의: jsdom은 레이아웃(`scrollHeight`/`offsetHeight`)을 계산하지 않으므로 DOM 측정 기반
토글 결정은 테스트 불가 → **content-based 휴리스틱**(`isBodyClampable`)을 채택해
결정성을 확보한다. 이 선택의 트레이드오프는 아래 리스크 참조.

## 리스크·결정 필요

- **[결정] 토글 노출 판정 방식 — 휴리스틱 vs DOM 측정.**
  - 채택(권장): content-based 휴리스틱(`newline > 3 || length > 160`). 장점: jsdom에서
    결정적으로 테스트 가능, `useLayoutEffect`/ref 불필요. 단점: 근사치라 경계 길이
    본문에서 (a) 실제로는 3줄에 딱 맞는데 더보기가 뜨거나(펼쳐도 거의 변화 없음 —
    cosmetic), (b) 임계 아래인데 좁은 뷰포트에서 4줄로 wrap될 수 있음. (b)를 줄이려
    임계를 3줄 상당(~160자)보다 약간 낮게 잡아 **false-negative(내용이 잘렸는데
    토글 없음)를 최소화**하고 fail-open(내용 노출) 방향으로 편향. 컬랩스 시에도
    클램프는 3줄이라 짧은 본문엔 무해(잘릴 것이 없음).
  - 대안: `useRef` + `useLayoutEffect`로 `scrollHeight > clientHeight` 측정해 토글 노출.
    브라우저에서 정확하지만 jsdom에서 항상 false → 토글 케이스 테스트 불가(또는
    측정 모킹 필요). 정확도가 중요하면 이 방식 + 테스트 전략 재설계 필요 — 리뷰어 판단.
- **[결정] per-row 상태 소유 위치.** 채택: `ExpandableBody` 로컬 `useState`(캡슐화,
  lifting/Set 배선 불필요, 행이 `entry_uuid`로 안정 keyed). 대안: 패널 레벨
  `Set<entry_uuid>`(전역 펼침/접힘 컨트롤이 향후 필요하면 유리하나 현재 요구엔 과함).
- **`WebkitLineClamp` 브라우저 지원**: `-webkit-box` 클램프는 모든 타깃(Chromium/WebKit/
  최신 Firefox)에서 동작. 미지원 fallback 시 클램프가 안 될 뿐 내용/토글은 그대로 →
  degrade-safe.
- **`white-space: pre-wrap` + `-webkit-line-clamp` 상호작용**: 명시적 `\n`이 있는 본문도
  `-webkit-box`가 N 시각 줄로 클램프하므로 안전. pre-wrap은 유지(펼쳤을 때 줄바꿈 보존).
- **접근성**: 클램프는 CSS만이라 스크린리더/DOM에는 full text가 남음(내용 숨김 아님).
  토글은 `aria-expanded`/`aria-controls`/`aria-label`로 IssueCard/NewsListItem과 동형.
- **범위**: 변경은 shared `SessionContextTimelinePanel` 내부에 한정 — `/insights`는
  desktop 단일 라우트라 미러링할 mobile 뷰 없음. `RetrospectivesPanel`(공유, /my에도
  사용) 등 다른 패널은 건드리지 않음.
- **대안(비채택, 문서화만)**: (1) `limit` 15→더 작게 축소 — 정보 손실, 스크롤 축소엔
  둔함. (2) 세션 기록 섹션 전체 접기(collapse) — 패널 존재감은 줄지만 개별 행
  scannability는 그대로. 본 이슈는 per-row 클램프가 핵심 요구이므로 위 두 안은 보조.
