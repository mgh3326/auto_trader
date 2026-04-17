# Scout 분석 depth 편차 post-mortem

- 발생 체인: [ROB-135](/ROB/issues/ROB-135) → [ROB-158](/ROB/issues/ROB-158) → [ROB-159](/ROB/issues/ROB-159)
- 발견일: 2026-04-17
- Umbrella 설계: [ROB-170](/ROB/issues/ROB-170) / [ROB-172](/ROB/issues/ROB-172) (plan v2, §1)
- 작성자: CIO
- 상태: 설계 확정 — 개선 조치는 [ROB-192](/ROB/issues/ROB-192) / [ROB-194](/ROB/issues/ROB-194) / [ROB-197](/ROB/issues/ROB-197)로 연동

## 1. Context

2026-04-17 KR 포트폴리오 재검토 과정에서 Scout가 제출한 Report([ROB-158](/ROB/issues/ROB-158))를 CIO가 보드-facing 최종안([ROB-159](/ROB/issues/ROB-159))으로 흡수할 때, **같은 `신규 후보` 라벨 안에서 분석 depth가 불균일**한 것이 확인됐다. 본 문서는 해당 사례를 root cause · prevention gate 관점에서 공식화하여 재발 방지 기준선을 남긴다.

개별 종목 재평가가 아니라, **Scout 지침 · Report 템플릿 · CIO 수용 규칙**을 바꾸기 위한 structural post-mortem이다.

## 2. Observations — ROB-158에서 shallow였던 지점

### 2.1 신규 후보 depth 편차

| 신규 후보 | Quote | 지표(RSI/MACD/BB/ADX/EMA) | S/R + volume profile | 뉴스 | 컨센서스·밸류 | execution path | direct DCA 비교 |
|---|---|---|---|---|---|---|---|
| Krafton 259960 | ✅ | ✅ | ✅ | ❌ | 목표가 1줄만 | ❌ | ✅ |
| LG이노텍 011070 | ✅ | ✅ | ✅ | ❌ | 목표가 1줄만 | ❌ | ✅ |
| 삼성전기 009150 | ❌ | RSI만 | ❌ | ❌ | 목표가 1줄 | ❌ | 한 줄(avoid) |
| 삼성SDI 006400 | ❌ | RSI만 | ❌ | ❌ | 목표가 1줄 | ❌ | 한 줄(avoid) |

상단 2종은 어느 정도 깊이 있는 분석이고, 하단 2종은 "RSI 과열 → avoid" 수준의 문장이다. 같은 `신규 후보` 라벨로 병렬 배치됐다.

### 2.2 Grouped rejection에 exception 검증 부재

> "oversold 상위 11개: 전원 KOSDAQ microcap / SPAC / REIT → 메인보드 DCA 후보 0건"

개별 종목 예시는 3개만(SKonec · Alpha AI · JR GLOBAL REIT), 나머지 8개 미분해. 메인보드 여부 / 유동성 / 섹터 관심 exception 검증이 없었다. "전원"이라는 단정이 근거 없이 채택될 수 있는 구조였다.

### 2.3 Fundamental / news / consensus 거의 부재

`get_news`, `get_financials`, `get_sector_peers` 호출 흔적이 신규 후보 섹션에 나타나지 않는다. catalyst · 컨센서스 · fundamental 없이 **기술적 지표 + 애널 목표가**만으로 비교된 형태였다.

### 2.4 Execution path 편차

- 파마리서치 "Toss only — 수동", 알테오젠 "KIS 4주 + Toss 3주 — mixed" — OK
- 신규 후보 Krafton / LG이노텍 — execution path 미기재 (implicit KIS 가정)
- CIO가 별도로 확인해야 하는 상황. 보드-facing action에서는 이 필드가 누락되면 안 된다.

### 2.5 예수금 / 실행 제약 결측

> "캐비앳: get_holdings 응답에 KIS 주문가능 KRW 잔고 필드가 없음 — CIO가 실제 KIS 예수금으로 사이즈 재조정 필요."

Scout는 `get_cash_balance` 호출 없이 종료 → CIO가 실측해 **예수금 ₩1.67M** 확인 → Scout 제안 ₩7.6M Tier 1 합계는 4.5x 초과. Tool 호출 누락이 disclosure 없이 넘어갔고, CIO가 전량 재산정했다.

### 2.6 비교 프레임 비대칭

기존 보유 11종 + 신규 2종이 같은 표에 들어가 있지만, 신규 후보에는 **진입 가능한 price scenario**(DCA limit / 조건부 매수)가 비어 있고 "watch only"로만 종결. 기존 DCA vs 신규 후보 direct comparison 문장은 "왜 신규가 아닌가(한 줄 요약)" 3줄이 전부였다.

### 2.7 결과적으로 CIO가 흡수한 구조

CIO([ROB-159](/ROB/issues/ROB-159))는 Scout 구조(BB mid/lower · ADX · 모멘텀 비교)를 **그대로 채택**하고 사이즈만 예수금 기준으로 축소했다. 즉 **depth 편차가 보드-facing 최종안까지 투과됐다**. 본 post-mortem 및 후속 설계의 목표는 이 투과를 막는 것.

## 3. Root cause

세 축이 맞물려서 발생했다.

1. **Scout 최소 depth 규정 부재** — 같은 `신규 후보` 라벨 안에서 4종의 분석 depth가 달라도 reject되지 않는 구조였다. Report 템플릿과 heartbeat workflow에 per-candidate checklist 기준이 명시되어 있지 않았다.
2. **MCP permission 갭** — `get_cash_balance` 등 예수금 실측 도구가 Scout allow-list에 누락되어 있었고, Scout가 주문안 총액 vs 예수금 reality check를 하지 않은 채로 Report를 냈다. CIO가 실측하기 전까지 disclosure 없이 통과했다.
3. **Execution path 필드 누락** — 보드-facing 액션에 올라갈 종목의 `KIS 즉시 / Toss manual / KIS+Toss mixed / 해외·미지원` 분류가 강제되지 않아 신규 후보 row에서 비어 있었다. CIO가 별도 확인해야 하는 work가 상시화됐다.

공통 구조는 "Scout 산출물의 shallow/누락이 CIO 최종안 직전까지 드러나지 않는다"는 것이다. CIO adoption rule 측면에서는 reopen 기준이 명시되지 않아 사후 흡수가 기본 경로가 됐다.

## 4. Remediation actions

설계 단계에서 확정된 개선 조치. 실제 발효는 각 이슈 완료 시점에 맞춘다.

| 영역 | 조치 | 트래킹 |
|---|---|---|
| Scout 지침 | AGENTS.md patch — minimum-depth workflow(Stage 1/2/3) + Tier A/B/C fast-exit + MCP Access allow-list audit(`get_cash_balance` / `get_available_capital` 추가) | [ROB-192](/ROB/issues/ROB-192) |
| Scout 산출물 | Report template v2 마이그레이션 — 8컬럼 core + 서브라인 bullets · `same-depth status` 필드 · `### 제한사항` 섹션 | [ROB-194](/ROB/issues/ROB-194) |
| CIO 수용 규칙 | Quality gate runbook + auto checklist 스크립트 — G1~G6(§5) 자동 sweep | [ROB-197](/ROB/issues/ROB-197) |
| Screener | `screen_stocks` 필터 확장(market / ADV / instrument_types) — grouped rejection exception sweep 1-call화 | [ROB-193](/ROB/issues/ROB-193) |
| Tuning | Tier A/B/C 임계값(RSI 75 · BB×1.02 · EMA20×1.08) dry-run 검증 | [ROB-195](/ROB/issues/ROB-195) |
| QA | Shallow Scout Report e2e — CIO reopen flow 검증 | [ROB-196](/ROB/issues/ROB-196) |
| Routine | Scout heartbeat cadence 점검 — Stage 2 cap=10 실측 | [ROB-198](/ROB/issues/ROB-198) |

## 5. Prevention gates (CIO adoption rules)

다음 중 하나라도 참이면 CIO는 Scout Report를 보드-facing 최종안에 흡수하지 않고 `reopen`을 요청한다. 상세 Hard/Soft 분류는 [ROB-170 plan §7](/ROB/issues/ROB-170#document-plan).

| gate | 실패 조건 | 분류 | 조치 |
|---|---|---|---|
| G1 Depth | top 신규 후보 중 `same-depth-check = fail` 1건 이상 | **Hard** | 해당 종목 deep-dive 재요청 |
| G2 Grouped rejection | 집단 기각에서 메인보드+유동성 exception 후보 분리 없음 | Soft | 후보군 개별 분해 재요청 or CIO 본문 보강 |
| G3 Tool failure | tool failure/schema mismatch 흔적 있는데 `### 제한사항` 섹션 없음 | **Hard** | 제한사항 disclosure 요청 |
| G4 Execution path | 보드 액션에 올라갈 종목에 execution path 미기재 | **Hard** | 경로 표기 요청 |
| G5 Comparison | 기존 DCA vs 신규 후보 direct comparison 문장 없음 | Soft | 비교 문장 요구 or CIO 본문 보강 |
| G6 Budget reality | 주문안 총액 > 예수금 1.5x + disclosure 없음, 또는 `get_cash_balance` 호출 흔적 없음 | **Hard** ([ROB-192](/ROB/issues/ROB-192) 이후) | 예수금 실측 + disclosure 요청 |

Hard-gate 위반 시 보드-facing 진행은 즉시 중단되고 Scout reopen으로 돌려보낸다. Soft-gate는 CIO가 판단 근거와 함께 채택할 수 있으나, 최종안 본문에 한계 명시가 필수다.

## 6. Back-test

ROB-158 사례를 본 gate 세트로 돌리면:

- **G1 Depth hit** — 삼성전기 009150 / 삼성SDI 006400이 checklist 7개 중 1개만 기록(RSI)되어 fail.
- **G4 Execution path hit** — 신규 후보 Krafton / LG이노텍 execution path 누락.
- **G6 Budget reality hit** — 제안 총액 ₩7.6M · 예수금 ₩1.67M(4.5x 초과) · disclosure 없음 · `get_cash_balance` 호출 흔적 없음.

즉 v1 gate만으로도 보드-facing 진행은 실제로 reopen됐어야 했다. 이 back-test가 G6 hard-gate 승격의 근거로 [ROB-170 plan §7.1](/ROB/issues/ROB-170#document-plan)에 기록되어 있다.

## 7. Open items

- Investment Reviewer 응답이 추후 도착하면 v2.1 merge 여지 존재(§7 Hard/Soft 재분류 · G7 신설 등). 범위는 [ROB-170 plan §10](/ROB/issues/ROB-170#document-plan).
- `screen_stocks` 필터 확장([ROB-193](/ROB/issues/ROB-193)) 완료 전까지 §4 grouped rejection은 per-name lookup fallback을 허용한다.
- Scout heartbeat cadence 실측([ROB-198](/ROB/issues/ROB-198))에서 cap=10 초과 신호가 나오면 routine split을 CIO 판단으로 진행 후 CEO 사후 보고.
