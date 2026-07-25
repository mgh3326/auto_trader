# ROB-691 — 판단 성적표(승률·실현손익·승패) 웹 노출 + 거래이력 필터

판단 성적표(승률·실현손익·승패)를 웹에 표면화하고, 거래(회고) 이력에
**승패 / 심볼검색 / 날짜범위** 필터를 추가한다. 결정론 집계·거래이력 로직은
이미 존재하므로 새 비즈 로직은 최소화하고, 대부분 **read 라우터 확장 + 프런트**
작업이다. Migration 0 (read-only).

---

## 1) 목표

- 이미 존재하는 결정론 집계 `build_retrospective_aggregate`(승률·승/패·통화별 실현손익
  ·by_outcome/by_trigger_type/by_root_cause)를 MCP 전용에서 **FastAPI read 엔드포인트**로
  미러링하여 `/invest/insights` 웹에 "판단 성적표" 타일로 노출.
- 회고 이력 리스트(`GET /trading/api/invest/retrospectives`)에 **승패 필터·심볼 prefix
  검색·KST 날짜범위(from/to)** 필터를 추가 (기존엔 부재).
- 의미 라벨을 명확히: auto_trader 승패는 **실 체결 + PnL 증거 기반**(무증거·rejected·
  cancelled 제외)이라 self-declared보다 강하나 표본이 적다 → UI에 "체결·증거 기반" 명시.
- 브로커/주문/워치 mutation 도달 금지. 기존 invest read 라우터 auth 패턴 준수.

---

## 2) 검증된 현재 상태 (file:line, 교정 포함)

### 결정론 집계 서비스 — `app/services/trade_journal/trade_retrospective_service.py`
- **교정**: 이슈는 `~604-711`로 표기했으나 실제:
  - `_is_win(r)` = **604-607** — `realized_pnl is not None → realized_pnl > 0`, 아니면
    `pnl_pct is not None and pnl_pct > 0`.
  - `_is_decided(r)` = **610-611** — `realized_pnl is not None or pnl_pct is not None`.
  - `build_retrospective_aggregate(...)` def = **614-713** (본문 끝 713).
- `kst_date_from/to` 파라미터 지원 확인: 시그니처 **617-618**, 적용 **637-640**
  (`_kst_day_start`/`_kst_day_end` = **447-454**, `_KST = ZoneInfo("Asia/Seoul")` line 54).
- 그룹 dict 반환 필드(692-706): `group, sample_size, wins, misses,
  win_rate_pct(=wins/decided*100 | None), avg_pnl_pct, realized_pnl_sum(통화별 dict),
  fx_pnl_krw_sum, total_pnl_krw_sum, by_outcome, by_trigger_type, by_root_cause_class`.
- 최상위 반환(709-713): `{group_by, groups(sample_size desc 정렬), excluded_no_fill_evidence}`.
- **중요한 설계 사실**: 반환은 **그룹별**이고 **전체 롤업(totals)은 없음**. 헤드라인 타일
  1개를 그리려면 그룹 합산이 필요 (§3, §4에서 라우터 롤업으로 해결 — 서비스 무수정).
- `include_no_evidence`(629): `group_by ∈ {trigger_type, root_cause}` 일 때만 무증거 행 포함
  → 이 두 그룹핑의 win_rate/decided 의미는 희석됨. 헤드라인은 strategy/day 그룹핑 고정.
- `_is_win`은 `>0`만 승 → **동점(0)은 패로 분류** (decided이지만 win 아님). UI/필터 문구 반영.

### 리스트 서비스 — `get_retrospectives(...)` = **461-513**
- 현재 필터: `symbol`(strip().upper() **정확일치**, 476-477), `account_mode`, `strategy_key`,
  `market`, `correlation_id`, `days`(상대일수), `trigger_type`, `root_cause_class`,
  `limit`, `offset`. **부재 확인**: `kst_date_from/to`(절대 날짜범위) 없음, **승패 필터** 없음,
  **심볼 prefix/LIKE** 없음(정확일치만). → §4에서 확장.

### MCP 전용 노출 (FastAPI 라우터 부재 재확인)
- `get_retrospective_aggregate(...)` = `app/mcp_server/tooling/trade_retrospective_tools.py`
  **164-194** (오늘 default로 from/to 채움). `app/routers/` 전체 grep 결과 aggregate/win_rate
  라우터 **없음** → 신규 노출 필요. ✔

### 미러 대상 read 라우터 패턴 — `app/routers/invest_forecasts.py` (전체 1-131)
- prefix `/trading/api/invest/forecasts`, `/calibration`(집계) + `/open` + `/closed`(리스트)가
  **한 파일에** 공존. 각 핸들러: `Depends(get_authenticated_user)` + `Depends(get_db)`,
  잘못된 enum → `HTTPException(422)`, 응답 스키마 `model_config=extra="forbid"`,
  `as_of=datetime.now(UTC)`. **→ 스코어보드도 별도 파일이 아니라 기존 retrospectives 라우터에
  `/scoreboard`로 붙이는 것이 동형(교정: 이슈의 "새 라우터 파일"보다 기존 확장이 정합).**

### 거래이력 라우터
- `app/routers/invest_fills.py` **24-59**: `recent`/`by-symbol`/`sell-history`/`freshness`;
  필터 `market/side/days/limit`뿐. 승패·심볼검색·from/to **부재**. (이번 스코프 밖 — 회고 이력에 집중.)
- `app/routers/invest_retrospectives.py` **42-114**: `GET ""`(list) 필터
  `market/trigger_type/root_cause_class/symbol(정확)/days/limit/offset`; `GET /next-actions`.
  `_normalize_symbol`(35-39) US는 `to_db_symbol`, 그 외 upper. 승패·심볼검색·from/to **부재** 확인.

### 프런트
- `frontend/invest/src/pages/desktop/DesktopInsightsPage.tsx` — Section "판단 품질"(154-160)에
  `<ForecastCalibrationPanel/>`, "학습·회고"(162-167)에 `<RetrospectivesPanel/>`.
- `frontend/invest/src/pages/mobile/MobileInsightsPage.tsx` — 데스크톱과 **1:1 미러**(파일 헤더
  주석대로 의도적 중복). 172-190에 동일 두 패널. **양쪽 lockstep 수정 필요.**
- `frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx` — 탭 UI, `retrospectives` 탭
  (163-164)에서 `<RetrospectivesPanel/>` 재사용. `portfolioTabs.ts`에 탭 목록.
- `components/insights/ForecastCalibrationPanel.tsx` — **타일/표 스타일 참조원**: 칩 토글
  (그룹/일수), 표 정렬, `Pill`(gain/loss/paper/warn), 소표본 가드(`SMALL_SAMPLE=5`), `CalibrationBar`,
  `LoadState<T>` 패턴, `Section`/`Card`.
- `components/my/RetrospectivesPanel.tsx` — 회고 리스트; 시장 칩(14-19) + 트리거 칩(21-32)
  이미 존재. **여기에 승패/심볼검색/날짜범위 필터 추가** → /insights·/my 양쪽에 이득.
- `components/my/SellHistoryPanel.tsx` — **교정**: 통화별 합은 `profitByCurrency` **107-124**,
  `totalByCurrency` **97-105**. 통화별 dict→행 렌더 패턴 참조 (실현손익 통화별 타일에 재사용).
- API/타입: `api/retrospectives.ts`(+`RetrospectivesQuery`), `types/retrospectives.ts`,
  `api/forecasts.ts`(집계 fetch 참조), `types/forecasts.ts`.

### auth / 테스트 관례
- `app/routers/dependencies.py` `get_authenticated_user`(35-39) → 미인증 시 401 `"로그인이 필요합니다."`.
- 라우터 등록: `app/main.py` 37-40 import, 197-201 `include_router`. 신규 없이 기존 확장이면 등록 무변경.
- 라우터 테스트 관례: `tests/routers/test_invest_retrospectives_router.py` — `monkeypatch`로 서비스
  함수 대체 + `app.dependency_overrides`로 auth/db 스텁, 파라미터 forwarding·422·401 검증.
- 서비스 통합 테스트 관례: `tests/test_trade_retrospective_aggregate.py` — `svc.save_retrospective`로
  시드 후 집계 검증(`@pytest.mark.integration`, cleanup fixture).
- 모델: `app/models/review.py` `TradeRetrospective` — `symbol`(Text,1024), `market`(Text|None,1031),
  `account_mode`(Text,1030), `outcome`(Text,1033), `realized_pnl`(Numeric,1036),
  `realized_pnl_currency`(Text,1037), `pnl_pct`(Numeric,1039), `fill_evidence_available`(Bool,1050),
  `created_at`(1069). 승패 SQL predicate를 이 컬럼들로 표현 가능.

---

## 3) 설계 결정

### 3.1 타일 위치 — **/insights "판단 품질" 섹션** (ForecastCalibrationPanel 위)
근거:
- 지표의 본질이 **결정된(decided) 회고의 승률**로, 판단 품질(judgment quality)이다. 이미
  /insights "판단 품질"에 있는 `ForecastCalibrationPanel`(예측 신뢰도)과 형제 지표.
- /insights는 이미 "예측 판단 품질·회고"를 프레이밍하고 `RetrospectivesPanel`을 "학습·회고"로
  호스팅 → 성적표는 그 위 헤드라인으로 자연스럽게 얹힘.
- **실현손익의 포트폴리오 성격 반론 처리**: 여기서의 실현손익은 개별 보유 평가액이 아니라 "결정된
  회고들의 통화별 실현손익 합계" = *내 판단이 얼마나 벌었나*의 스코어카드 스탯이다. 라이브 포트폴리오
  상태(/my 보유 현황)와 의미가 다르므로 /insights가 정합. `total_pnl_krw_sum`도 있으면 보조 표기.
- 결정: **1차 홈 = /insights** (신규 `JudgmentScoreboardPanel`). /my 회고 탭에는 성적표 타일을
  중복 배치하지 않는다(스코프 절제). 단, **회고 리스트 필터 개선은 `RetrospectivesPanel`에 넣어**
  /insights·/my 양쪽이 자동으로 이득을 본다.
- 데스크톱·모바일 InsightsPage는 1:1 미러이므로 **양쪽 lockstep**으로 패널 삽입.

### 3.2 의미 라벨 — "체결·증거 기반"
- 패널 타이틀 "판단 성적표", 서브텍스트:
  "체결·증거 기반 — 실제 체결과 PnL 증거가 있는 회고만 집계합니다(무증거·거부·취소 제외).
  자기신고보다 강하지만 표본이 적을 수 있습니다."
- `excluded_no_fill_evidence` 카운트를 muted 각주로 표기("증거 부족 N건 제외").
- 소표본(decided < 5) 시 `ForecastCalibrationPanel`과 동일하게 `Pill tone="warn"` "소표본" 경고.
- 동점(0) = 패 규칙(`_is_win`은 `>0`) 문구화: 승률 정의를 "실현손익 > 0 기준"으로 표기.

### 3.3 필터 구현
- **승패 필터** = `_is_win`/`_is_decided` 재사용을 **SQL WHERE**로 번역(서비스 `get_retrospectives`에
  `outcome_filter: "win"|"loss"|"decided"` 추가). Python predicate와 병렬 유지(주석 cross-ref + 병렬
  테스트로 drift 방지):
  - `decided`: `realized_pnl IS NOT NULL OR pnl_pct IS NOT NULL`
  - `win`: `realized_pnl > 0 OR (realized_pnl IS NULL AND pnl_pct > 0)`
  - `loss`: decided AND NOT win
    `= (realized_pnl IS NOT NULL AND realized_pnl <= 0) OR (realized_pnl IS NULL AND pnl_pct IS NOT NULL AND pnl_pct <= 0)`
  - SQLAlchemy `and_/or_`로 구성. **정합성 함정**: 정확히 `_is_win`/`_is_decided`와 동일 경계여야 함.
- **심볼검색** = prefix(ILIKE) — 기존 `symbol`(정확일치)은 보존하고 신규 `q` 파라미터로 분리:
  서비스 `symbol_search: str | None` → `TradeRetrospective.symbol.ilike(f"{q.strip().upper()}%")`
  (심볼은 upper 저장). LIKE 와일드카드는 escape. US 대시/슬래시 정규화는 prefix엔 미적용(사용자가 접두만 입력).
- **날짜범위(from/to)** = 서비스가 이미 가진 `_kst_day_start/_kst_day_end`를 `get_retrospectives`에
  `kst_date_from/kst_date_to`로 노출(집계 서비스와 동일 시맨틱). 라우터에서 `YYYY-MM-DD` 형식 검증(422).
  기존 `days`와 공존 시 둘 다 AND 적용(문서화).

### 3.4 스코어보드 엔드포인트 배치 — **기존 라우터 확장** (신규 파일 X)
- `invest_forecasts.py`가 `/calibration`+`/open`+`/closed`를 한 파일에 두듯, 회고 집계는 회고 관심사이므로
  `app/routers/invest_retrospectives.py`에 `GET /scoreboard` 추가. `main.py` 등록 무변경.
- (대안: 신규 `invest_scoreboard.py` — 채택 안 함, 응집도·등록비용 이유. 필요 시 손쉽게 분리 가능.)

---

## 4) 단계별 구현 (API 스키마 포함)

### Step 1 — 서비스 `get_retrospectives` 필터 확장 (`trade_retrospective_service.py`)
시그니처에 추가: `outcome_filter: str | None = None`, `symbol_search: str | None = None`,
`kst_date_from: str | None = None`, `kst_date_to: str | None = None`.
- 필터 절 추가(기존 `filters` 리스트에):
  - `symbol_search` → `filters.append(TradeRetrospective.symbol.ilike(_prefix_like(symbol_search)))`
    (`_prefix_like`는 `%_` escape 후 `f"{s.strip().upper()}%"`).
  - `kst_date_from/to` → `_kst_day_start/_kst_day_end` 재사용(집계와 동일).
  - `outcome_filter` → §3.3 SQL predicate. `_is_win`/`_is_decided` 바로 위/옆에 SQL 헬퍼
    `_sql_is_win()`/`_sql_is_decided()`를 정의하고 주석으로 "keep in lock-step with `_is_win`/`_is_decided`".
- 반환 dict·정렬 무변경. `total`/`count`는 필터 반영된 값으로 자연 계산됨(기존 로직 재사용).

### Step 2 — 라우터 확장 (`app/routers/invest_retrospectives.py`)

**2a. 기존 `GET ""` 리스트에 필터 추가**
```python
outcome_filter: Annotated[str | None, Query()] = None,   # "win" | "loss" | "decided"
q: Annotated[str | None, Query(max_length=32)] = None,    # 심볼 prefix 검색
kst_date_from: Annotated[str | None, Query()] = None,     # YYYY-MM-DD (KST)
kst_date_to: Annotated[str | None, Query()] = None,
```
- 검증: `outcome_filter not in {None,"win","loss","decided"}` → 422;
  날짜는 `_parse_kst_date`(라우터 로컬, `datetime.strptime(..,"%Y-%m-%d")` 실패 시 422).
- 서비스로 forward. `symbol`(정확)과 `q`(prefix)는 공존 가능(둘 다 AND).
- `RetrospectivesResponse`(`extra="forbid"`)에 echo 필드 추가: `outcome_filter/q/kst_date_from/kst_date_to`
  (schema에 optional 필드 추가).

**2b. 신규 `GET /scoreboard`**
```python
@router.get("/scoreboard")
async def get_retrospective_scoreboard(
    _user, db,
    group_by: Annotated[str, Query()] = "strategy",   # strategy|day|trigger_type|root_cause
    market: Annotated[Market, Query()] = "all",
    account_mode: Annotated[str | None, Query()] = None,
    strategy_key: Annotated[str | None, Query()] = None,
    kst_date_from: Annotated[str | None, Query()] = None,
    kst_date_to: Annotated[str | None, Query()] = None,
) -> ScoreboardResponse:
```
- `group_by not in {"strategy","day","trigger_type","root_cause"}` → 422. 날짜 형식 검증(422).
- `retro_svc.build_retrospective_aggregate(db, group_by=..., market=None if all, ...)` 호출.
- **totals 롤업**을 라우터에서 순수 계산**(서비스 무수정, 결정론)**:
  `total_wins=Σg.wins`, `total_misses=Σg.misses`, `decided=wins+misses`,
  `win_rate_pct=wins/decided*100 | None`, `realized_pnl_sum`=통화별 merge,
  `fx_pnl_krw_sum/total_pnl_krw_sum/sample_size` 합, `excluded_no_fill_evidence` 그대로.
- **정합성 주의**: totals의 승률 의미가 명확하려면 group_by는 strategy/day(PnL 지향)여야 함.
  헤드라인 타일용 요청은 프런트가 항상 `group_by=strategy`로 보내고, 브레이크다운 토글은 별도 요청.
  응답에 `group_by`를 echo해 프런트가 인지.

**2c. 스키마** (`app/schemas/invest_retrospectives.py`에 추가, `extra` 규칙 기존 관례 준수)
```python
class ScoreboardGroupRow(BaseModel):
    model_config = ConfigDict(extra="ignore")   # 집계 group dict 전체 주입, 부분 유지
    group: str
    sample_size: int = Field(ge=0)
    wins: int = Field(ge=0)
    misses: int = Field(ge=0)
    win_rate_pct: float | None = None
    avg_pnl_pct: float | None = None
    realized_pnl_sum: dict[str, float] = Field(default_factory=dict)
    fx_pnl_krw_sum: float = 0.0
    total_pnl_krw_sum: float = 0.0
    by_outcome: dict[str, int] = Field(default_factory=dict)
    by_trigger_type: dict[str, int] = Field(default_factory=dict)
    by_root_cause_class: dict[str, int] = Field(default_factory=dict)

class ScoreboardTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_size: int = Field(ge=0)
    wins: int = Field(ge=0)
    misses: int = Field(ge=0)
    decided: int = Field(ge=0)
    win_rate_pct: float | None = None
    realized_pnl_sum: dict[str, float] = Field(default_factory=dict)
    fx_pnl_krw_sum: float = 0.0
    total_pnl_krw_sum: float = 0.0
    excluded_no_fill_evidence: int = Field(ge=0)

class ScoreboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_by: str
    market: Literal["all","kr","us","crypto"]
    kst_date_from: str | None = None
    kst_date_to: str | None = None
    count: int = Field(ge=0)
    groups: list[ScoreboardGroupRow]
    totals: ScoreboardTotals
    as_of: datetime
```
- `RetrospectivesResponse`에 `outcome_filter/q/kst_date_from/kst_date_to` optional 추가.

### Step 3 — 프런트 API/타입
- `frontend/invest/src/types/scoreboard.ts` (신규): `ScoreboardGroupRow`, `ScoreboardTotals`,
  `ScoreboardResponse`, `ScoreboardGroupBy = "strategy"|"day"|"trigger_type"|"root_cause"`.
- `frontend/invest/src/api/scoreboard.ts` (신규): `fetchScoreboard({groupBy,market,accountMode,
  strategyKey,dateFrom,dateTo})` → `/trading/api/invest/retrospectives/scoreboard`,
  `credentials:"include"`, non-ok throw (기존 api 관례).
- `api/retrospectives.ts` `RetrospectivesQuery`/`fetchRetrospectives` 확장:
  `outcomeFilter?, q?, dateFrom?, dateTo?` → 쿼리스트링 `outcome_filter/q/kst_date_from/kst_date_to`.
  `types/retrospectives.ts` `RetrospectivesResponse`에 echo 필드 추가.

### Step 4 — `JudgmentScoreboardPanel.tsx` (신규, `components/insights/`)
- `ForecastCalibrationPanel` 스타일 차용: `Card` + `Section` + `Pill` + `LoadState<T>` + 칩 토글.
- 상단 **헤드라인 타일 4개**(항상 `group_by=strategy` 요청의 `totals`):
  ① 승률(`win_rate_pct`, "결정 N건 중") ② 승/패(`wins`/`misses`, gain/loss Pill)
  ③ 결정 표본(`decided`) + 증거부족 제외 각주 ④ 실현손익 통화별(`realized_pnl_sum` dict→행,
  `SellHistoryPanel.profitByCurrency` 렌더 패턴 참조, `+`/부호·`toLocaleString`).
- 컨트롤: 날짜범위(from/to `<input type="date">` 또는 30/90/전체 프리셋), 시장 칩(all/kr/us/crypto).
- 하단 **브레이크다운 표**: group_by 토글(전략/일자/트리거/원인) → `fetchScoreboard(groupBy)` 그룹 행
  (그룹·표본·승/패·승률·통화별 실현손익). trigger_type/root_cause 선택 시 "무증거 포함, 승률 참고용"
  안내 배지.
- 소표본·빈 상태·에러: 캘리브레이션 패널과 동일 UX. `onEmptyChange?` 콜백으로 페이지 누적 배너 연동.

### Step 5 — 페이지 배선 (데스크톱 + 모바일 lockstep)
- `DesktopInsightsPage.tsx` "판단 품질" Section(154-160): `<ForecastCalibrationPanel/>` **위**에
  `<JudgmentScoreboardPanel onEmptyChange={setScoreboardEmpty}/>` 삽입. `allDataEmpty` 계산에 포함.
- `MobileInsightsPage.tsx` 동일 위치(172-180)에 `compact`로 삽입(1:1 미러 유지; 헤더 주석 규칙 준수).

### Step 6 — `RetrospectivesPanel.tsx` 필터 확장 (/insights·/my 공용)
- 신규 컨트롤: **승패 칩**(전체/승/패/결정), **심볼검색 입력**(`q`, debounce), **날짜범위**(from/to).
- `fetchRetrospectives`에 `outcomeFilter/q/dateFrom/dateTo` 전달. 기존 시장·트리거 칩과 공존.
- compact 모드(모바일/홈)에서는 심볼검색·날짜범위 숨김(트리거 칩처럼 `!compact` 게이트) — 밀도 유지.

---

## 5) 테스트 계획

### 백엔드 — 라우터 단위 (`tests/routers/test_invest_retrospectives_router.py` 확장)
- `/scoreboard`: 파라미터 forwarding(group_by/market→None if all/date), `build_retrospective_aggregate`
  monkeypatch로 그룹 fixture 주입 → **totals 롤업 정확성**(통화별 merge·승률) 검증.
- `/scoreboard` 422: invalid group_by, invalid 날짜 형식.
- `/scoreboard` 401: unauth 클라이언트.
- 리스트 필터: `outcome_filter/q/kst_date_from/kst_date_to`가 `get_retrospectives`로 forward됨,
  invalid `outcome_filter`·날짜 → 422, echo 필드 응답 확인.

### 백엔드 — 서비스 통합 (`tests/test_trade_retrospective_aggregate.py` 또는 신규 list 테스트)
- `save_retrospective`로 win(pnl>0)/loss(pnl<=0, 0 포함)/무증거/pnl_pct-only 시드.
- `outcome_filter="win"|"loss"|"decided"` 각각 정확한 행 반환.
- **병렬성 테스트**: 시드셋에서 `outcome_filter` SQL 결과 == Python `[r for r in rows if _is_win/_is_decided]`
  (drift 가드, 0-동점 경계 포함).
- `symbol_search` prefix ILIKE 매칭, `kst_date_from/to` 경계(일 시작/끝 포함) 검증.

### 프런트 — vitest (`__tests__/`)
- `scoreboard.api.test.ts`: `fetchScoreboard`가 쿼리스트링 정확 구성.
- `retrospectives.api` 확장: 신규 필터 파라미터 매핑.
- `JudgmentScoreboardPanel.test.tsx`: mock totals→타일 렌더(승률·승/패·통화별 실현손익), 소표본 경고,
  빈/에러 상태.
- `RetrospectivesPanel` 필터: 승패/심볼/날짜 입력 시 fetch 쿼리 반영.

### 정적 가드
- `make lint` / `make typecheck` / `uv run pytest tests/routers/test_invest_retrospectives_router.py`
- `no_internal_llm_imports` 등 기존 가드 무영향(신규 provider import 없음).

---

## 6) 마이그레이션 노트 (0)

- **Migration 0**. 스키마 변경 없음 — 신규 쿼리 파라미터 + 라우터 롤업 + 프런트뿐.
- DB 접근은 전부 `SELECT`(기존 `get_retrospectives`/`build_retrospective_aggregate` 재사용, 신규 WHERE 절만).
- **Mutation 도달 없음 확인**: 라우터는 read 서비스 함수만 호출, 서비스는 broker/order/watch/order-intent
  코드 경로에 도달하지 않음(순수 `TradeRetrospective` SELECT). /insights 페이지의 `ReadOnlyGuardrailNote`
  가드레일과 정합.
- auth: 신규/확장 엔드포인트 모두 `Depends(get_authenticated_user)` → 미인증 401.

---

## 7) 리스크 · 스코프 밖

**리스크**
- **승패 SQL vs Python `_is_win` drift** (가장 큰 정합성 함정): 동점(0)=패, `pnl_pct` fallback,
  NULL 처리가 정확히 일치해야 함 → §5 병렬 테스트 + 코드 cross-ref 주석으로 방지.
- **totals 의미 희석**: `group_by ∈ {trigger_type,root_cause}`는 무증거 행 포함(`include_no_evidence`)
  → 헤드라인 totals는 항상 strategy/day 그룹핑으로 요청(프런트 고정), 브레이크다운은 참고용 배지.
- **표본 희소**: decided 회고가 적으면 승률 변동 큼 → 소표본 경고 UX로 완화.
- **다통화 실현손익**: 단일 숫자로 환산하지 않고 통화별 dict 유지(FX 환산은 별도 `total_pnl_krw_sum`
  존재 시만). 프런트가 통화별 행으로 표시.
- **데스크톱/모바일 InsightsPage 중복**: 패널 삽입을 lockstep으로 하지 않으면 드리프트 → 두 파일 동시 수정.

**스코프 밖**
- 회고 작성/채점(save_retrospective, forecast_resolve) 등 write 경로.
- fills(`invest_fills.py`) 이력의 승패/검색/날짜 필터(별도 이슈로 분리; 이번은 회고 이력에 집중).
- 종목별 스코어보드 드릴다운, 신규 집계 차원(side별 등), CSV/export.
- /my 회고 탭에 성적표 타일 중복 배치(1차 홈은 /insights).
- 모바일 UX 재설계(데스크톱 미러 파리티까지만).
