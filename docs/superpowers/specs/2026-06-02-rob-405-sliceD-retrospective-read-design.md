# ROB-405 Slice D — 사이클 회고 read API (MCP)

- **이슈**: ROB-405 (G4) 회고 배선 — **Slice D (read API)**
- **부모 에픽**: ROB-401, 트래커 ROB-410 Wave 3
- **선행**: A(#1086 journal)·B(#1089 verdict)·C(#1091 counterfactual) — merged
- **작성일**: 2026-06-02
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 1. 배경 / 현 상태

LLM/Hermes가 자율 루프 결과를 정량 회고하려면 사이클별 {armed, triggered, filled, PnL, hit/miss} + verdict + counterfactual 요약을 **읽을** 표면이 필요하다. 데이터는 A/B/C로 완비됐고 집계만 남았다 — **read-only, 마이그레이션 없음**.

### 코드 매핑 (집계 소스)
- `InvestmentWatchEvent`(app/models/investment_reports.py): `kst_date`(Text NOT NULL, 인덱스 `ix_investment_watch_events_kst_date`), `outcome`, `correlation_id`(NOT NULL), `market`, `created_at`. **사이클 앵커=kst_date**.
- `InvestmentWatchAlert`: `status`(active|triggered|expired|canceled), `created_at`, `market`. **correlation_id·kst_date 없음** → armed는 created_at(KST) 버킷.
- `trade_journals`(A): account_type='mock', `correlation_id`(nullable, 인덱스), `pnl_pct`, `status`(closed/active/...).
- `trade_journal_reviews`(B): `journal_id` FK(**correlation_id 아님**), `verdict`(good|neutral|bad), `verdict_source`.
- `trade_journal_counterfactuals`(C): `correlation_id`(unique), `fill_vs_trigger_pct`, `no_action_vs_fill_pct`.
- MCP read 패턴: `app/mcp_server/tooling/paper_analytics_registration.py`(`*_TOOL_NAMES` set, `_session_factory`, `@mcp.tool`, `register_*` + registry.py:123 호출). 등록 2곳: `registry.py` + `app/mcp_server/__init__.py::AVAILABLE_TOOL_NAMES`(15).
- KST 변환: `app/core/timezone.py::to_kst_naive(dt)`.

## 2. 목표 / 비목표
**목표**: read-only MCP 도구 `get_mock_loop_retrospective`가 kst_date 범위를 일별 cycle로 쪼개 per-day 집계 리스트 반환. mock 한정. 마이그레이션·flag·task 없음.
**비목표**: E follow_up_report_item_id. HTTP 라우터(대시보드). armed의 point-in-time 정확도(created_at 프록시). 쓰기/스케줄.

## 3. 설계

### 3.1 집계 서비스 (`app/services/trade_journal/mock_loop_retrospective_service.py`)
`async def build_mock_loop_retrospective(db, *, kst_date_from: str, kst_date_to: str, market: str | None = None) -> list[dict]`:
- kst_date 범위(YYYY-MM-DD, inclusive)를 일별 순회.
- 각 `day`:
  1. **triggered events**: `InvestmentWatchEvent` where `kst_date == day` (+market) → `triggered=len`, `by_outcome={outcome:count}`. 그 날 `correlation_ids` 집합 = 앵커.
  2. **filled**: `trade_journals` where `account_type='mock'` AND `correlation_id IN correlation_ids` AND `status IN ('active','closed')` → `filled=count`. closed journal들 보관.
  3. **PnL/hit-miss**: closed journal pnl_pct → `avg_pnl_pct`, `hits=count(pnl_pct>0)`, `misses=count(pnl_pct<=0)`, `hit_ratio=hits/(hits+misses) if 합>0 else None`.
  4. **verdict**: 위 journal id들의 `trade_journal_reviews` → `{good,neutral,bad}` count.
  5. **counterfactual**: `trade_journal_counterfactuals` where `correlation_id IN correlation_ids` → `avg_fill_vs_trigger_pct`, `avg_no_action_vs_fill_pct`, `count`.
  6. **armed**: `InvestmentWatchAlert` where `to_kst_naive(created_at).date() == day` (+market) → `armed=count` (그 날 신규 arming 프록시; 의미 명시).
- 반환: per-day dict 리스트. **triggered→filled→PnL→verdict→CF는 그 날 correlation_id로 일관 앵커**(journal엔 kst_date 없음 → 트리거 event의 kst_date로 사이클 귀속). armed만 created_at 기반(별도 count).
- 빈 날도 cycle dict(0) 포함 or 생략? → **이벤트 있는 날만 cycle 생성**(빈 날 생략; 단 armed는 별도라 armed-only 날은? armed도 포함하려면 day 합집합. 단순화: kst_date 범위 전체를 순회하되 triggered=0이어도 armed 집계해 cycle 포함 — 모든 날 포함). 범위가 작으니 전체 날 포함.

### 3.2 MCP 도구 (`app/mcp_server/tooling/mock_loop_retro_registration.py`)
- `MOCK_LOOP_RETRO_TOOL_NAMES: set[str] = {"get_mock_loop_retrospective"}`.
- `register_mock_loop_retro_tools(mcp)`: `@mcp.tool(name="get_mock_loop_retrospective", description=...)` async(`kst_date_from`, `kst_date_to`, `market=None`; 기본 from/to=today KST) → `_session_factory` 세션 → 서비스 호출 → `{"success": True, "cycles": [...], "kst_date_from":..., "kst_date_to":...}`. paper_analytics 미러.
- **등록**: `registry.py`의 always-read-only 블록(123행 근처)에 `register_mock_loop_retro_tools(mcp)` + `app/mcp_server/__init__.py::AVAILABLE_TOOL_NAMES`에 `"get_mock_loop_retrospective"` 추가.

## 4. 안전 경계
순수 read-only(broker/order/DB write 없음). mock 한정 집계. 마이그레이션·config flag·task 없음. A/B/C 무변경.

## 5. 테스트
1. 서비스: 같은 correlation_id로 event(kst_date=D, outcome=executed) + closed mock journal(pnl_pct=+5) + review(good) + counterfactual seed → day=D cycle dict: triggered=1/by_outcome/filled=1/avg_pnl_pct=5/hits=1/hit_ratio=1.0/verdict good=1/CF count=1.
2. miss: pnl_pct<0 journal → hits=0/misses=1/hit_ratio=0.
3. market 필터: 다른 market event 제외.
4. 다중 날 범위 → 일별 분리 cycle.
5. correlation_id 없는/live journal 무시.
6. armed: alert created_at(KST)==day → armed count.
7. MCP 도구: `get_mock_loop_retrospective` in tools(build_tools), 호출→success+cycles. 등록 set(`registry`/`AVAILABLE_TOOL_NAMES`) 포함.

## 6. 미해결 / 후속
- E follow_up_report_item_id(investment_watch_events 자동 채움) → ROB-405 마지막 슬라이스.
- HTTP 라우터(대시보드), armed point-in-time 정확도.
- 구현 시 origin/main(A/B/C merged) 기준.
