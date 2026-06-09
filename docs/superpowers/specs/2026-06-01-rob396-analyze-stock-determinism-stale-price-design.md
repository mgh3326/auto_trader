# ROB-396 — analyze_stock_batch 비결정성 + stale price 해소 설계

- **이슈**: ROB-396 (오케스트레이션 ROB-411 C라인, 397 다음 순서)
- **상태**: design (브레인스토밍 승인 완료)
- **날짜**: 2026-06-01
- **선행**: ROB-397 (`app/services/symbol_analysis/` 계약 — main c34c79e3). 관련 ROB-392(스코프), ROB-411(오케스트레이션).

---

## 1. 문제 (ROB-396 실증, 2026-06-01)

`analyze_stock_batch`(코어 `analyze_stock_impl`)를 같은 날 같은 종목에 두 번 호출했더니:

- **증상 1 — source·판정 flip**: `analysis_analyze.py:559-576`의 `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED` + `RESEARCH_PIPELINE_ENABLED` 게이트 아래 `run_research_session()` → `_get_pipeline_result()` 분기. 파이프라인이 성공하면 sparse한 `source:"research_pipeline"`(rsi/consensus null, sell 가능), 예외면 `except`로 legacy `source:"kis"`(rich, buy)로 폴백. 즉 파이프라인 성공 여부에 따라 **source와 매매 판정이 비결정적으로 뒤집힘**. (부수 결함: `_get_pipeline_result`가 summaries 없을 때 `{}`를 그대로 반환.)
- **증상 2 — stale current_price**: KR 경로는 `_build_kr_quote_from_ohlcv`(일봉 df의 마지막 종가) 또는 `_fetch_quote_equity_kr`(`inquire_daily_itemchartprice n=1`)로 current_price를 만든다 — 둘 다 **일봉 종가**. 정규장 중에도 전일/당일 종가를 반환해 라이브 체결가와 어긋남(012450 1,173,000 vs 실제 ~1,225,000 등).

## 2. 목표·범위

호출 시점 합성을 **결정적·정직**하게 만든다 (ROB-397 계약 원칙 재사용). 397 snapshot read-model 런타임 전환은 후속(ROB-398+collector)이고 본 건이 아니다.

**범위**: KR 경로 중심. 단 §3 source 결정성(파이프라인 분기 제거)은 분기가 market-agnostic이라 전 시장 적용. US/crypto의 라이브 price는 이미 존재(US `fetch_us_live_last_price`, crypto Upbit)하므로 §5 price 수정은 KR 한정.

**안전 경계**: broker/order/watch/order-intent mutation 없음, production DB backfill 없음, scheduler activation 없음, secret/env 변경 없음. 마이그레이션 0. 응답은 read-only.

## 3. 증상 1 수정 — source 결정성 (pipeline 분기 제거)

`analyze_stock_impl`에서 `RESEARCH_PIPELINE_*` 게이트 + `run_research_session` → `_get_pipeline_result` 분기(약 `analysis_analyze.py:551-578`)를 **통째로 제거**. 항상 legacy KIS-rich 합성 경로를 탄다.

- flip 경로 소멸 + `{}` 반환 부수 결함 소멸.
- 전 시장 적용 (clean-cut). `run_research_session`은 `research_pipeline_service.py` 등 다른 사용처가 있으므로 보존; 제거 대상은 **이 도구 내 분기**뿐.
- `_get_pipeline_result` / `_map_pipeline_to_analysis`는 본 파일에서만 사용(grep 확인) → 분기 제거 후 dead code로 함께 삭제. 관련 import(`run_research_session`, `ResearchSession`, `selectinload` 등) 미사용분 정리.

## 4. verdict fail-closed floor (397 정책 재사용)

`_apply_recommendation(analysis, market_type)` 직후 floor post-step을 추가한다. 기존 `recommendation` dict **shape는 불변**(reasoning/rsi14/buy_zones/sell_targets/stop_loss 유지) — Hermes/리포트 소비자 비파괴.

floor 규칙 (ROB-397 `derived` 정책과 동일 정신):

- `quote`의 current_price 부재 → `recommendation["action"]="unavailable"`, `confidence="low"`.
- core 입력 부족 — rsi14 null **또는** consensus 부재/null — 이면 확신적 buy/sell 금지: `action="hold"`, `confidence="low"`.
- `recommendation["insufficient_inputs"]`(list[str]) 추가로 사유 명시(예: `["consensus"]`, `["rsi14","consensus"]`, `["price"]`).

`mcp_server → services` import는 허용 방향이므로, 397 패키지에 작은 순수 헬퍼를 두고 재사용하는 것을 우선한다. 헬퍼 시그니처(안):

```python
# app/services/symbol_analysis/floor.py (신규, 작은 순수 함수)
def insufficient_inputs(*, price_present: bool, rsi_present: bool,
                        consensus_present: bool) -> list[str]: ...
def floored_action(action: str, *, price_present: bool,
                   insufficient: list[str]) -> tuple[str, str]:
    """(action, confidence). price 부재→unavailable/low;
    insufficient 있으면→hold/low; 아니면 입력 그대로."""
```

(헬퍼 추가 시 ROB-397 패키지에 단위테스트 동반. 인라인+인용으로 할지 헬퍼로 뺄지는 plan에서 확정하되, 중복 로직을 피하는 헬퍼를 기본으로 한다.)

## 5. 증상 2 수정 — 라이브 KR price + 정직 태그

KR current_price 소스를 일봉 종가 → **라이브 `inquire_price`**(`app/services/brokers/kis/domestic_market_data.py:175`, `stck_prpr`)로 전환한다. 일봉 df(`ohlcv_df`)는 지표 계산용으로 그대로 사용. US가 `fetch_us_live_last_price`로 오버레이하는 패턴(`market_data_quotes.py:875` 부근)을 미러링한다.

- KR quote 조립 시 current_price를 `inquire_price.stck_prpr`로 채운다.
- `quote["price_as_of"]`: `inquire_price`의 `stck_bsop_date` + `stck_cntg_hour`(체결 일자·시각)로 구성한 timestamp.
- `quote["is_stale_price"]`: ROB-397 `compute_is_stale("price", price_as_of, trading_date=<KST 영업일>)`. 장외엔 `inquire_price`가 마지막 종가를 주므로 as_of 날짜 기준으로 정직하게 stale=true.
- **graceful fallback**: `inquire_price` 실패/빈 응답 시 기존 일봉 종가로 폴백하되 `is_stale_price=true` + price_as_of=일봉 날짜. (숨김 금지 — 라이브 확인 불가를 표면화.)
- 배치 시 종목당 `inquire_price` +1 read 콜. (broker mutation 아님.)

## 6. 응답 shape 변경 (additive, 비파괴)

- `analysis["quote"]` += `price_as_of`(ISO8601|null), `is_stale_price`(bool).
- `analysis["recommendation"]` += `insufficient_inputs`(list[str], 기본 `[]`).
- 기존 키 제거/타입 변경 없음.

## 7. 테스트 (TDD, 두 증상 회귀)

1. **증상1 회귀**: `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED=true`로 설정해도 `analyze_stock_impl`이 결정적으로 legacy `source`(kis 계열)를 반환하고, 반복 호출에 `recommendation.action`이 뒤집히지 않음. (pipeline 분기 제거 검증.)
2. **증상2 회귀 (live)**: `inquire_price` mock이 당일 체결(stck_bsop_date=오늘) 반환 → `quote.price`=라이브가, `is_stale_price=false`. 전일 날짜 반환 → `is_stale_price=true`.
3. **증상2 fallback**: `inquire_price` 예외 → 일봉 종가 폴백 + `is_stale_price=true`.
4. **floor 회귀**: consensus 부재 + rsi만 존재 → `action="hold"`(buy/sell 아님) + `insufficient_inputs=["consensus"]`; price 부재 → `action="unavailable"`, `insufficient_inputs=["price"]`.
5. KIS 호출은 기존 테스트 패턴대로 fake/mock (실 네트워크 없음).

## 8. 비목표 (YAGNI)

- ROB-397 snapshot read-model 런타임 전환 (후속 398+collector).
- US/crypto price 경로 (이미 라이브) — §3 source 결정성만 전 시장.
- 별칭/뉴스/이벤트 캘린더 (ROB-398/408).
- analyze_stock 응답을 397 `SymbolAnalysis` 타입으로 재구성 (소비자 비파괴 위해 dict shape 유지).
