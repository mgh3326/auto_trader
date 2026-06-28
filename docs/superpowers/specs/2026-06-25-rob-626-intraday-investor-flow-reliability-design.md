# ROB-626 — `get_intraday_investor_flow` 신뢰성 + 외인수급 자급성 설계

- **Linear**: ROB-626 (High) — *get_intraday_investor_flow: freshness 라벨 호출간 불일치 + 멀티데이 순매수 이력·외인소진율 부재 → 외인 수급 판정 불가(CDP 네이버 우회 의존)*
- **Date**: 2026-06-25
- **Branch base**: `rob-626` @ `origin/main` (d602d847)
- **Migration**: **0** (없음 — 기존 페처/리포 재사용)
- **Scope**: KR 전용 (US/crypto 무변경)

---

## 1. 배경 / 문제

2026-06-25 마이크론 호실적發 KR 회복장 분석에서 "진성 V-회복 vs dead-cat" 판정의 핵심 게이트가 **외인 순매수 전환 여부**였다. `get_intraday_investor_flow`로는 이를 신뢰성 있게 확인할 수 없어, 결국 CDP 네이버 투자자 매매동향을 커스텀 JS로 우회 스크래핑해야 결론을 낼 수 있었다.

세 가지 결함:

1. **freshness 라벨이 호출 간 불일치** — 동일 종목·동일 데이터(`foreign_net_qty: -1,931,000`)인데 호출 시각에 따라 `confidence`가 `carry_over`(전일)↔`observed`(당일 라이브)로 흔들림.
2. **당일 장중 외인 순매수를 신뢰성 있게 못 줌** — carry_over로 전일치가 자주 반환됨.
3. **멀티데이 이력 + 외인소진율 부재** — MCP는 단일 누적 수치(`foreign_net_qty`) 1개만. 네이버는 N일치 외/기/개 순매수 + 외인소진율%로 추세 판단 가능.

### 1.1 근본 원인 (코드 그라운딩)

`app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py::_classify_session` (현 :87–134):

- KIS `investor-trend-estimate`(TR `HHPTJ04160200`) 페이로드는 **날짜 필드가 없다**. 슬롯(`bsop_hour_gb` 1~5 = 09:30/10:00/11:20/13:20/14:30)별 외인/기관/합계 **추정 수량**(`*_fake_*`)만 준다. KIS는 장외/장전에 **직전 세션 행을 계속 서빙**한다.
- 라벨은 데이터가 아니라 **시계 위치**로 추론된다: `slot_in_future = (오늘날짜@슬롯시각) > now` (:122). 동일한 stale "14:30" 슬롯이 **오전엔 미래**(→ `carry_over`, 정확)지만 **오후엔 과거**(→ `observed`, **오판: 전일 데이터를 당일 라이브로 단정**)로 분류된다.
- 게다가 `now`가 한 호출 안에서 **2~3회 독립적으로** 읽힌다: `_classify_session` 내부 `now = now_kst()` (:114) vs `kr_market_data_state()`(인자 없음 → 내부에서 별도 `pd.Timestamp.now` :132) vs top-level `market_session_state`(또 다른 `kr_market_data_state()` :185). 두 시계 읽기가 장 경계를 가로지르면 `confidence`와 `market_session_state`가 한 호출 안에서도 모순될 수 있다.

> **핵심 인사이트**: "fake observed"(전일 stale 데이터를 당일 라이브로 라벨)가 가장 위험하다. 단순히 `now`를 1회로 통일하는 것만으로는 **호출 시각이 다른 두 호출**이 같은 stale 데이터에 다른 라벨(특히 14:30–15:30 fake observed)을 주는 문제를 제거하지 못한다. 데이터-앵커 기반의 보수적 라벨링이 필요하다.

### 1.2 이미 존재하는 데이터 (재사용 가능)

- `app/services/naver_finance/investor.py::fetch_investor_trends(code, days)` — `finance.naver.com/item/frgn.naver` 9-cell 테이블을 평문 HTTP로 파싱. 행당 키: `date`, `close`, `change`, `change_pct`, `volume`, `institutional_net`, `foreign_net`, `foreign_holding_shares`, `foreign_holding_rate`(0~100). **CDP 불필요.** 이것이 사용자가 우회 스크래핑한 바로 그 소스다.
- `app/mcp_server/tooling/fundamentals/_valuation.py::handle_get_investor_trends` — 위 페처를 호출하고 `individual_net = -(institutional_net + foreign_net)`을 파생(:154–158). 주/월 집계 경로엔 `foreign_holding_rate_change`(외인소진율 추세, ROB-448 `_holding_rate_change` :170–180)가 이미 있으나 **일별(`period="day"`) 경로엔 추세 플래그가 없다**.

→ 제안 #3(멀티데이 + 외인소진율)의 데이터는 **사실상 이미 `get_investor_trends`에 있다**. 본 작업의 본질은 (a) 진짜 결함인 intraday 라벨 결정성 수정, (b) 확정 이력을 **판정 워크플로가 실제로 호출하는 도구**에 통합("워크어라운드보다 소스 수정"), (c) 일별 추세 플래그 보강이다.

---

## 2. 목표 / 비목표

### 목표
1. **거짓 freshness flip 제거.** 동일 `(now, rows, confirmed)` 입력 → 항상 동일 출력. **stale 직전세션 데이터는 절대 `observed`로 라벨되지 않는다.**
2. **한 번의 호출로 외인수급 판정.** intraday 도구에 확정 멀티데이 요약(네이버) 임베드.
3. **`get_investor_trends` 일별 외인소진율 추세 플래그** 추가(주/월 집계와 패리티).

### 비목표 (YAGNI)
- 신규 DB 테이블/마이그레이션 없음 (**migration-0**).
- 당일 intraday 수치를 KIS가 주는 것보다 더 신뢰성 있게 만들 수 없다(intraday 유일 소스가 KIS 추정). 정직하게 라벨 + 확정 이력으로 보완.
- 목표가 recency(이슈 '참고') → **별도 Linear 이슈**로 분리.
- US/crypto 무변경.

---

## 3. 결정적(deterministic) 분류기 — 핵심 수정

### 3.1 단일 `now` 스레딩

`handle_get_intraday_investor_flow` 진입부에서 `now = now_kst()`를 **1회** 캡처. `kr_market_data_state(now)`(이미 `now` 인자 지원 — `market_session.py:40`), `_classify_session(..., now)`, top-level `market_session_state` 모두 동일 `now`/동일 state 값을 공유. 3회 시계 읽기 → 1회. `confidence`와 `market_session_state` 모순 불가.

### 3.2 보수적·데이터-앵커 라벨링

입력: 1회 캡처한 `now`, KIS 슬롯 행, 확정 네이버 시리즈(앵커).

- `latest_slot` = 존재하는 최대 슬롯, `latest_slot_time` = 그 슬롯의 벽시계 시각.
- `expected` = 슬롯 시각이 `now` 이하인 최대 슬롯(오늘 세션이 `now`까지 생성했어야 할 최신 슬롯).
- `last_confirmed_date` = 네이버 확정 시리즈의 최신 행 날짜(없으면 `previous_kr_session(today)`).
- `today_confirmed` = 네이버 확정 시리즈가 **오늘 날짜 행**을 이미 보유.

규칙 (위에서부터 순서대로 평가):

`now`로부터 모두 파생. `today = now.date()`, `max_slot_dt = combine(today, max(_SLOT_TIMES)=14:30, KST)`. 규칙은 위에서부터 순서대로 평가하고 첫 매치 반환:

| # | 조건 | `confidence` | `today_available` | `as_of` | `as_of_date` | `is_prior_session` |
|---|---|---|---|---|---|---|
| 1 | 행 없음(`latest_slot_time is None`) | `null` | false | null | null | false |
| 2 | 오늘이 세션일 아님(`not is_kr_session_day(today)`) | `carry_over` | false | null | `previous_kr_session(today)` | true |
| 3 | `latest_slot_time`이 now보다 **미래**(`slot_dt > now`) | `carry_over` | false | null | `previous_kr_session(today)` | true |
| 4 | 미래 아님 **AND** `today_confirmed` | `inferred` | true | today-stamp | today | false |
| 5 | 미래 아님, today 미확정, **`market_state == fresh` AND `now < max_slot_dt`(즉 now < 14:30)** | `observed` | true | today-stamp | today | false |
| 6 | 미래 아님, today 미확정, 그 외(14:30~마감 full-set, 마감후-미게시 포함) | **`provisional_unconfirmed`** (신규) | false | null | **null** | false |

> **규칙 3이 장전(now<09:00)을 흡수한다**: 비어있지 않은 행은 `latest_slot_time ≥ 09:30`이므로 now<09:00이면 항상 미래 → 규칙 3. 별도 "장전" 규칙은 dead라 두지 않는다.

**판별 근거**: 슬롯 위치는 14:30 전까지 진성-today와 stale을 명확히 구분한다 — stale은 항상 직전세션 **full-set(`latest_slot=5`, 14:30)**인데, `now < 14:30`이면 14:30 > now이므로 규칙 3이 "미래"로 먼저 잡는다. 따라서 `now < 14:30`인 라이브 구간(규칙 5)에 도달한 데이터는 stale이 이미 규칙 3에서 걸렀으므로 **진성-today로 단정 가능**하다. KIS 게시 지연으로 `latest_slot`이 현재 경과 슬롯보다 뒤처져도(예: now 14:00인데 slot 4 미게시로 latest=3) 여전히 진성-today이므로 `latest_slot == 경과슬롯` 같은 엄격 조건은 두지 않는다(지연 케이스 오분류 방지). 모든 슬롯이 경과 가능한 14:30 이후엔 진성/​stale full-set 구분 불가 → **`observed` 주장을 거부**(규칙 6, 신규 `provisional_unconfirmed`)하여 fake observed 위험을 제거한다.

**`as_of` today-stamp 정의**: `as_of = datetime.combine(today, latest_slot_time, tzinfo=KST).isoformat()` (기존 `_classify_session` :116–120 동작과 동일). today-stamp인 규칙(4/5)만 비-null `as_of`를 가지며, carry_over/provisional_unconfirmed는 `as_of=null`(직전세션 날짜를 시각으로 위조하지 않음 — ROB-492 불변식 유지).

**`as_of_date` semantics**: carry_over는 직전세션 DATE(`previous_kr_session(today)`)를 유지(ROB-492/542 계약·기존 테스트). provisional_unconfirmed는 날짜를 모르므로 `as_of_date=null`이고, 대신 항상-populated **`last_confirmed_session_date`**(top-level)가 floor를 제공: `today_confirmed`면 today, 아니면 네이버 최신 확정행 날짜, 네이버 미가용이면 `previous_kr_session(today)`.

**결정성 보장**: 동일 `(now, rows, confirmed)` → 동일 출력. 라벨이 하루 동안 `carry_over → observed → provisional_unconfirmed`로 진행될 수는 있으나(실제 확증가능성 변화 반영), **거짓으로 today를 단정하지 않는다.** 안정적 판정은 `today_available` + 임베드된 확정 이력(둘 다 날짜/시간-안정)으로 한다.

> **선택적 강한 앵커 (v1 보류, 검증 게이트)**: KIS `inquire_investor`(FHKST01010900)는 today-scoped라 14:30–15:30 구간에서 "today 흐름"을 양성 확증해 `observed` 회복에 쓸 수 있다. 추가 호출 + 라이브 시맨틱 미검증이라 v1 제외. 보수 구간이 실전에서 과하면 후속.

`is_kr_session_day`(`market_session.py`), `previous_kr_session`은 기존 헬퍼 재사용. `expected`/슬롯-시각 매핑은 기존 `_SLOT_TIMES`(:25–31) 재사용.

---

## 4. 출력 계약 (intraday 도구) — additive, 하위호환

기존 top-level 키는 **불변**(ROB-492/542 계약 + 테스트 + README 의존: `foreign_net_qty`, `institution_net_qty`, `combined_net_qty`, `as_of`(carry_over면 null 유지), `confidence`, `is_prior_session`, `warning`, `note`, `rows`). 추가만 한다:

```jsonc
{
  // ── 기존 top-level KIS 잠정 필드 (불변) ──
  "symbol": "005930", "instrument_type": "equity_kr", "source": "kis",
  "data_state": "intraday_provisional", "market_session_state": "...", "provisional": true,
  "as_of": null, "as_of_date": "2026-06-24",
  "confidence": "observed|inferred|carry_over|provisional_unconfirmed",   // 신규 값 1개 추가
  "is_prior_session": true, "warning": {...}, "as_of_time_kst": "14:30",
  "foreign_net_qty": -1931000, "institution_net_qty": ..., "combined_net_qty": ...,
  "rows": [...], "note": "...",

  // ── 신규 top-level ──
  "today_available": false,
  "last_confirmed_session_date": "2026-06-24",

  // ── 신규 confirmed 블록 (네이버, 정직 provenance) ──
  "confirmed": {
    "source": "naver",
    "foreign_ownership_pct": 47.41,            // 최신 확정행 foreign_holding_rate
    "foreign_ownership_trend": "down",         // up|down|flat (정의: §4.1)
    "foreign_ownership_rate_change": -0.42,    // pp, newest − oldest (윈도우)
    "history": [                               // 고정 N=5, newest-first (intraday는 신규 인자 없음)
      {"date": "2026-06-24", "foreign_net": -596340, "institution_net": 2969153,
       "individual_net": -2337684, "close": 340500},
      ...
    ],
    "days": 5
  }
}
```

- `confirmed.history` 키는 레포 기존 명명을 따른다: `foreign_net`, `institutional_net`, `individual_net`(파생), `close`. (이슈 본문의 `institution_net`이 아니라 `institutional_net`.)
- `provisional_unconfirmed`만이 유일한 동작 변경 값(과거 `observed`였던 모호 구간).
- **하위호환 우선**: 승인된 미리보기는 `provisional:{}/confirmed:{}` 중첩을 보였으나, LLM이 읽는 도구에서 전면 중첩 리스트럭처는 모든 기존 소비자/테스트를 깨므로 **additive**로 간다. (전면 중첩 break를 원하면 전환 가능 — spec 리뷰에서 결정.)
- `today_available=false` 의미: "당일 데이터를 양성 확증하지 못함" (이슈 제안 #2의 명시 신호). `last_confirmed_session_date`로 가장 최근 확정 세션 분리 제공.
- intraday `confirmed.history`는 **고정 N=5**(도구 시그니처에 신규 인자 추가 없음 — 단순 유지). 더 긴 윈도우는 기존 `get_investor_trends(symbol, days=...)`로.

### 4.1 `foreign_ownership_trend` 정의

`foreign_ownership_rate_change`(pp = 최신행 rate − 가장 오래된행 rate, 윈도우 내)를 기준으로:
- 두 끝점 중 하나라도 `foreign_holding_rate`가 None(레거시 7-cell 행) → `trend = null`, `rate_change = null`.
- `|rate_change| < 0.01`pp → `"flat"`.
- `rate_change > 0` → `"up"`(외인 누적), `< 0` → `"down"`(외인 이탈).

동일 정의를 `get_investor_trends` 일별 보강(§5)에서도 공유(§6 헬퍼).

---

## 5. `get_investor_trends` 일별 보강

`handle_get_investor_trends`의 `period="day"` 경로에 top-level 추가(주/월 집계와 패리티):

- `foreign_ownership_pct` — 최신 일별행 `foreign_holding_rate`.
- `foreign_ownership_trend` — `up|down|flat`.
- `foreign_holding_rate_change` — 윈도우 newest − oldest (pp), 기존 `_holding_rate_change` 헬퍼 재사용.

순수 additive(기존 `data[]` 무변경).

---

## 6. 공유 enrichment 헬퍼 (DRY)

두 도구가 동일 로직(네이버 페치 + `individual_net` 파생 + 외인소진율/추세 산출)을 공유하도록 작은 헬퍼를 도입한다. 후보 위치: `app/mcp_server/tooling/fundamentals/_valuation.py`(또는 신규 소형 모듈 `_investor_flow_common.py`).

- `enrich_confirmed_daily(symbol, days) -> dict` — 네이버 페치, 행마다 `individual_net` 파생, `foreign_ownership_pct`/`trend`/`rate_change` 계산, newest-first N행 반환.
- `handle_get_investor_trends`(일별)와 intraday `confirmed` 블록 빌더가 이 헬퍼를 공유 → 명명/계산 분기 방지.

`individual_net` 파생식은 기존과 동일: `-(institutional_net + foreign_net)`.

---

## 7. 열화(degradation) & 에러

- intraday의 네이버 확정 페치는 **best-effort**: 실패 시 KIS 잠정 블록은 그대로 반환, `confirmed`는 `{"source":"naver","error":"...","history":[],"days":0}`로 열화, 앵커는 시계-only 휴리스틱으로 폴백(규칙 5a/`today_confirmed` 판정 불가 → 5b/5c로). note에 앵커 미가용 명시. **네이버 다운이 도구 전체 실패를 유발하지 않는다.**
- KIS 에러 경로(`_error_payload`) 불변.
- `today_confirmed` 판정은 네이버 시리즈 가용시에만 신뢰. 미가용시 5a로 가지 않음(보수).

---

## 8. 테스트 계획

`tests/test_mcp_fundamentals_tools.py::TestGetIntradayInvestorFlow`(현 :5216–5473) 갱신 + 신규.

- **분류기 진리표**: 전 시간 그리드 — 장전, 각 슬롯 윈도우(09:30–14:30), 14:30–마감 라이브 full-set, 마감후-미게시, 마감후-게시(today_confirmed), 주말/비세션일. **명시 단언: stale full-set은 어떤 시각에도 `observed` 아님.**
- **결정성 테스트**: frozen `(now, rows, confirmed)` → 반복 호출 동일 출력.
- **confirmed 임베드 테스트**(네이버 모킹) + **열화 테스트**(네이버 raise → KIS 블록 무손상).
- 기존 7개 테스트 갱신:
  - `kr_market_data_state` 모킹이 `now` 인자 수용(현재 no-arg 람다 :5439–5440,:5474).
  - 네이버 확정 페치 모킹 추가(모든 케이스 — 미모킹 시 실 HTTP 위험).
  - **"observed @14:30" 케이스(:5217)** → 14:30은 5b 경계 밖(`expected=5`)이므로 today 미확정이면 `provisional_unconfirmed`. observed를 유지하려면 슬롯을 14:30 이전(예: slot 3 @11:20, now 12:00)으로 바꾼 신규/수정 케이스로 표현.
  - **"after-close inferred" 케이스(:5410)** → 새 규칙상 today 미확정이면 `provisional_unconfirmed`(5c); 네이버 모킹이 today 확정행을 포함하면 `inferred`(5a). 의도한 동작에 맞춰 모킹/기대 갱신.
  - carry_over(future slot :5308 / 비세션일 :5359), empty(:5272), non-KR(:5453), upstream-error(:5459)는 계약 유지(네이버 모킹만 추가).
- **`get_investor_trends` 일별 추세 플래그 테스트**.
- shared-DB flake 회피: 외부 페치는 모킹(메모리 교훈 — DB-resolved 값 단언 대신 모킹).

게이트: `make lint` (ruff + ty, app/ + tests/ 둘 다), 전체 스위트. migration-0이므로 alembic head 무변경.

---

## 9. 영향 받는 파일 (구현 가이드)

- `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py` — 단일 `now` 스레딩, `_classify_session` 재작성(앵커 인자 추가), `confirmed` 블록 + `today_available`/`last_confirmed_session_date` 빌드, best-effort 네이버 페치.
- `app/mcp_server/tooling/fundamentals/_valuation.py` — 일별 추세 플래그, 공유 `enrich_confirmed_daily` 헬퍼.
- `app/mcp_server/tooling/market_session.py` — (필요시) `kr_market_data_state(now)` 호출 경로 확인(이미 `now` 지원).
- `app/mcp_server/tooling/fundamentals_handlers.py:230–253` — 도구 description 갱신(신규 필드 계약 문서화).
- `app/mcp_server/README.md` — intraday 도구 계약 갱신.
- `tests/test_mcp_fundamentals_tools.py` — §8.

---

## 10. 비-PR 액션

- 별도 Linear 이슈 신설: *"analyze_stock_batch 컨센서스 목표가 recency — 폭락/회복 국면에서 upside_pct 과대표시(newest_opinion 날짜 가중/경고 강화)."* (이슈 ROB-626 '참고' 섹션 발).
- 운영(operator): MCP 재시작 + 장중 라이브 스모크 `get_intraday_investor_flow("005930")` / `get_intraday_investor_flow("000660")` — `confidence` 결정성 + `confirmed.history`/`foreign_ownership_pct` 확인.

---

## 11. 미해결/검증 항목

- KIS `investor-trend-estimate`가 **라이브 세션 중 today로 리셋**하는지 vs 장중에도 직전세션 행을 서빙하는지 — 라이브 스모크로 확인. (보수 규칙은 두 동작 모두에 안전하도록 설계됨.)
- 14:30–15:30 `provisional_unconfirmed` 보수성이 실전에서 과하면 §3.2의 `inquire_investor` 강앵커를 후속 도입.
