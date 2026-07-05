# ROB-722 — analyze_stock_batch 심볼별 다가오는 실적 auto-inject

**날짜:** 2026-07-06
**이슈:** ROB-722 (related: ROB-711 레일, ROB-721 스코핑 GO #2)
**상태:** 설계 승인 대기

## 배경

`get_earnings_calendar` MCP 도구는 이미 배포되어 있다(US=live Finnhub, KR=market_events DB).
그러나 분석 체인은 이를 자동 주입하지 않아, 오퍼레이터가 심볼 분석 때마다 실적 일정을 수기로
조회한다. 검증된 코퍼스 사례(`review.investment_report_items` 330행):

- **NVDA**: "Finnhub calendar shows earnings on 2026-05-20 AMC; avoid…" — 실적 임박이 진입 판단을 뒤집음.
- **HCA**: "30일 내 실적 없음" — **무실적을 진입 정당화 근거로 인용**.
- **JPM/BAC**: 실적 인접 trim watch.

오퍼레이터가 이미 수기로 하는 검증된 행동을 auto-inject가 결정론적으로 체계화한다.
신규 벤더/auth/스키마 0, migration 0.

## 목표 / 비목표

**목표**
- `analyze_stock_batch`(compact 계약, `quick=True`)의 각 US/KR equity 심볼 결과에 "다가오는 실적
  컨텍스트"를 결정론적으로 자동 첨부.
- 무실적("향후 30일 무실적")도 명시적 신호로 첨부(HCA 사례).
- US=live Finnhub, KR=market_events DB(+freshness 표기).
- 완전 fail-open — 실적 조회 실패가 분석 결과를 절대 훼손하지 않음.

**비목표**
- KR live 소싱(DART/WiseFn 실시간) — 별도 이슈. 이번엔 DB + stale 표기.
- crypto 실적(개념 없음) — skip.
- `analyze_portfolio`(full 계약, quick=False) — 미대상(compact 계약만; decision_history와 동일 범위).
- 스키마/마이그레이션 변경 — 없음.
- LLM 판단 — 없음(ROB-501 가드). 순수 결정론 shaping.

## 아키텍처

ROB-711의 `_attach_decision_history` 레일을 1:1 미러하되, US 경로가 Finnhub fetch 디스패치를
필요로 하므로 **서비스 계층에 병렬 구현을 새로 짓지 않고** 이미 검증된 MCP 핸들러
`handle_get_earnings_calendar`를 재사용한다(둘 다 tooling 계층 → 레이어 역전 없음).
그 핸들러가 이미 처리하는 것: US/KR 시장 디스패치, Finnhub fetch, KR DB shaping, crypto 거부,
날짜 윈도우 정규화. **신규 코드 = compact shaper + `_attach_earnings` 배치 루프뿐.**

### 컴포넌트

**신규 `app/mcp_server/tooling/earnings_context.py`** (tooling 계층)

```
_TIMING_MAP = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}
_WINDOW_DAYS = 30
_KR_STALE_THRESHOLD_DAYS = 2

def _map_timing(hour: str | None) -> str          # bmo/amc/dmh → BMO/AMC/DMH, else "unknown"

def _compact_earnings(
    tool_result: dict, *, today: date, freshness: str, data_as_of: str | None,
) -> dict:
    """순수 shaper. tool_result(US finnhub / KR market_events)의 earnings[]에서
       today 이후 최근접 upcoming 1건을 선택해 compact 컨텍스트 생성.
       upcoming 없으면 has_upcoming=False + note."""

async def _kr_ingestion_freshness(db) -> tuple[str, str | None]:
    """MarketEventIngestionPartition 최신 finished_at(market=kr, category=earnings)로
       (freshness ∈ {fresh, stale, unknown}, data_as_of ISO|None) 도출.
       배치당 1회만 계산(심볼 무관 전역)."""

async def build_earnings_context(
    symbol: str, market: str, *, today: date | None = None,
    kr_freshness: tuple[str, str | None] | None = None,
) -> dict | None:
    """crypto/비-equity → None(필드 생략). 그 외 handle_get_earnings_calendar(
       symbol, from=today, to=today+30d, market) 호출 → _compact_earnings → 반환.
       US freshness="live". KR freshness는 kr_freshness 인자(배치서 1회 계산) 사용."""
```

**`app/mcp_server/tooling/analysis_tool_handlers.py`** — `_attach_decision_history`(:850) 옆 sibling:

```
async def _attach_earnings(results, *, market) -> None:
    """ROB-722: 심볼별 다가오는 실적 컨텍스트 auto-inject.
       배치(단일 세션), 심볼별 fail-open. crypto/error 행 skip.
       KR freshness는 세션당 1회 계산해 각 build 호출에 전달."""
```

호출 지점: `analyze_stock_batch_impl` `:942` `_attach_decision_history` **직후**
(quick 분기 안, `_attach_fresh_artifact_hints` → `_attach_decision_history` → `_attach_earnings`).

### 데이터 흐름

1. `analyze_stock_batch_impl(quick=True)` → `_run_batch_analysis` → `_attach_fresh_artifact_hints`
   → `_attach_decision_history` → **`_attach_earnings`**.
2. `_attach_earnings`: 단일 `AsyncSessionLocal`. KR 심볼이 하나라도 있으면 `_kr_ingestion_freshness`
   1회 계산. 각 non-error·non-crypto 심볼에 `build_earnings_context(sym, mkt, kr_freshness=...)`.
3. `build_earnings_context`: crypto면 None. 아니면 `handle_get_earnings_calendar` 호출 →
   `_compact_earnings` shaping → `result["earnings"]`에 첨부.
4. 심볼별 try/except: 한 심볼의 Finnhub 429/에러가 배치 전체를 죽이지 않음(그 심볼만 필드 생략).

## 첨부 페이로드 형태

결과 dict의 `earnings` 키로 첨부(decision_history와 동일한 sibling 위치):

```jsonc
// upcoming 실적 있음 (NVDA류)
"earnings": {
  "symbol": "NVDA",
  "market": "us",
  "as_of": "2026-07-06",
  "window_days": 30,
  "has_upcoming": true,
  "next_earnings": {
    "date": "2026-05-20",
    "d_minus": 12,               // (date - today).days
    "timing": "AMC",             // BMO | AMC | DMH | unknown
    "eps_estimate": 0.84,
    "revenue_estimate": 26500000000,
    "quarter": 1,
    "year": 2026,
    "status": "scheduled"        // KR만; US는 null
  },
  "freshness": "live",           // US=live
  "source": "finnhub"
}

// 무실적 (HCA류) — 명시적 신호
"earnings": {
  "symbol": "HCA",
  "market": "us",
  "as_of": "2026-07-06",
  "window_days": 30,
  "has_upcoming": false,
  "next_earnings": null,
  "note": "no scheduled earnings within 30 days",
  "freshness": "live",
  "source": "finnhub"
}

// KR (DB + stale 표기)
"earnings": {
  "symbol": "005930",
  "market": "kr",
  "as_of": "2026-07-06",
  "window_days": 30,
  "has_upcoming": true,
  "next_earnings": { "date": "2026-07-25", "d_minus": 19, "timing": "unknown", ... },
  "freshness": "stale",          // fresh | stale | unknown (finished_at 기준 >2일=stale)
  "data_as_of": "2026-07-01",    // KR 최신 인제스트 finished_at
  "source": "market_events"
}
```

- crypto/비-equity: `build`가 None → 필드 자체 생략(첨부 안 함).
- `d_minus`는 `date.today()` 기준 단순 일수 차. (ET/KST 분리 미도입 — YAGNI. 30일 윈도우/일 단위라
  경계 오차 무의미. 필요 시 후속.)

## 결정 사항 (사용자 승인 완료)

1. **KR 경로**: DB + stale 표기. `MarketEventIngestionPartition.finished_at`으로 freshness 도출.
2. **US rate-limit**: 심볼별 fail-open, 캐시 없음(YAGNI). 배치 최대 10심볼 → 최대 10 Finnhub 콜,
   무료 60/min 여유. 심볼별 try/except로 429 격리.
3. **무실적 신호화**: `has_upcoming=false` + `note`로 명시 첨부(None 반환 아님). crypto만 생략.

## 에러 처리 / fail-open 계층

- `_attach_earnings` 전체를 try/except로 감싸 advisory-only(decision_history와 동일 패턴).
- 심볼별로도 try/except — 한 심볼 실패가 나머지 심볼 첨부를 막지 않음.
- `handle_get_earnings_calendar`가 `_error_payload`(dict with error/source)를 반환하면 shaper는
  `has_upcoming=false` + 에러 note로 degrade(예외 아님). Finnhub 429는 `FinnhubQuotaExceededError`가
  핸들러 내부에서 `_error_payload`로 잡힘 → 그 심볼만 degrade.
- KR freshness 조회 실패 → `("unknown", None)`으로 fail-open.

## 테스트

**`tests/mcp_server/test_analyze_stock_batch_earnings.py`** (신규, decision_history 테스트 미러):
- `_attach_earnings`가 build 컨텍스트 존재 시 `earnings` 첨부(build_earnings_context monkeypatch).
- build 예외 시 fail-open(결과 untouched).
- error 행 skip, crypto 행 skip.

**`tests/mcp_server/test_earnings_context.py`** (신규):
- US upcoming: `handle_get_earnings_calendar` monkeypatch → d_minus·timing(BMO/AMC/DMH) 매핑 검증.
- US 무실적: earnings=[] → `has_upcoming=false` + note.
- KR DB 경로: freshness=stale/fresh + data_as_of 표기(partition monkeypatch 또는 seed).
- crypto/비-equity: `build_earnings_context` → None.
- `_compact_earnings` 순수 함수: today 이후 최근접 선택, 과거 실적 제외, timing 매핑, no-upcoming.

**정적 가드**: `test_no_internal_llm_imports.py` 영향 없음(LLM import 없음). migration 0.

## 완료 기준

- `analyze_stock_batch` compact 결과에 심볼별 실적 컨텍스트 결정론 포함(US live / KR DB).
- 무실적 케이스도 명시 신호로 첨부.
- 테스트: US live-Finnhub 경로 + KR DB 경로 + 무실적 케이스 + fail-open + crypto skip.
- migration 0. 기존 decision_history/fresh_artifact 첨부 회귀 없음.
