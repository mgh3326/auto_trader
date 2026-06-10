# ROB-347 — US 리포트 신규매수 예산·환전 전제 정책화 (Design)

- **Issue**: ROB-347 (Improvement / Medium) — parent ROB-336
- **Date**: 2026-06-09
- **Scope**: PR-C (A→B→C 배치). Migration-0. **US kis_live**.
- **Status**: design + adversarial spec-review 반영. pending user review → plan
- **Depends**: PR-B(346) 머지 후. 같은 candidate 루프를 건드리지만, B와 동일하게
  **classify_candidate_symbol signature는 미변경**하고 후처리 demotion 헬퍼만 추가 →
  B의 `demote_for_quality` 다음에 `demote_for_budget` 를 체인(서명 충돌 없음).

## 1. Context / 현황 (코드 검증) — dead-code 정정

US KIS live readiness 점검: USD 주문가능금액 `$0`, KRW 주문가능금액 별도 존재. 후보가
어떤 예산을 전제하는지 불명확.

**정정(검증):**
- `app/services/action_report/us/new_buy_candidates.py:72`, `us/account_snapshot.py:403`
  (silent `quantity=0`, `_capital_basis`, `sizing_basis`)는 **dead code**(app/ 내 non-test
  caller 0). **수정 위치 아님.**
- 실제 US 후보 경로(`auto_emit.py:464-518` + `action_verdict.py:74-98`)는 **금액/예산을
  전혀 읽지 않음**. `beyond_candidate_budget` 은 후보 *개수* cap. → "USD=0이 buy처럼
  보임"은 verdict가 **구조적으로 budget-blind** 이기 때문.
- USD buying power는 `collectors/portfolio.py:407-410` 에서 `buying_power={"krw":…,"usd":…}`
  로 portfolio snapshot payload에 적재되나, `portfolio_journal.py:_usd_totals(:74-90)` 의
  **display 전용**. USD/KRW 혼동 없음(`:90` USD 부재 시 None, KRW fallback 안 함 — 보존).
- `request.py:14-149` ReportGenerationRequest 에 budget 필드 없음.

### 1.1 feasibility 검증 (블로커 해소)
- `auto_emit.propose(snapshots, request_market, account_scope, now)` 는 portfolio
  snapshot을 순회해 `portfolio_payload`(`:361-363`)를 이미 보유. → `buying_power` 는
  `portfolio_payload.get("buying_power")` 로 **즉시 접근 가능**(신규 collector 쓰기 불요).
- request의 budget 파라미터는 `propose()` 인자로 전달(현재 `request_market`/`account_scope`
  를 개별 인자로 받는 패턴 동일). generator는 `_auto_emit_items_from_bundle`
  (`generator.py:560-600`)에서 propose 호출 시점에 `request` 보유 → budget 필드 전달 가능.
- → 예산 신호 = **portfolio snapshot(자동 계산) + request override(사용자)**. 둘 다
  propose 진입 시점에 가용함을 확인.

## 2. Goal

US 후보/ActionPacket이 예산 전제를 명시. USD buying power=0이어도 후보 발굴은 가능하되,
`buy_review`/`watch_only`/`data_gap` 의미 혼동 없이 `budget_gap`/`fx_required`/
`operator_budget_required` 를 남긴다.

## 3. Design (migration-0)

### 3.1 budget basis (request 필드)
- `ReportGenerationRequest` 추가:
  - `budget_basis: Literal["available_usd","krw_orderable_reference","operator_budget_override"]`
    — **기본 `available_usd`**.
  - `operator_budget_override_usd: Decimal | None`.
- MCP `investment_report_generate_from_bundle`(`investment_reports_handlers.py:854-880`)에
  두 파라미터 노출(선택, 기본 available_usd).
- **override precedence(명확화)**: `operator_budget_override_usd` 가 non-null이면 basis와
  무관하게 **effective_usd = override**(이슈의 "request budget이 있으면 그 값" 반영).
  null이면 basis 선택을 따른다.

### 3.2 budget 신호 소스 (propose 내부)
propose가 portfolio snapshot payload에서 추출:
- `usd_buying_power = (buying_power or {}).get("usd")` (0/None 가능)
- `krw_orderable_reference = (buying_power or {}).get("krw")` (**reference only**)
- `operator_override = operator_budget_override_usd` (request 인자)
이를 `budget_state` dict로 정규화(아래 헬퍼 입력).

### 3.3 budget-aware demotion (verdict, B의 다음 단계)
`action_verdict.py` 에 **순수 헬퍼** 추가(서명 미변경 원칙 동일):
```
def demote_for_budget(verdict: str, budget_state: dict) -> tuple[str, list[str]]:
    # buy_review 에만 적용. 절대 상향 없음. 사유는 리스트(복수 가능).
    if verdict != "buy_review":
        return verdict, []
    basis = budget_state["basis"]; override = budget_state.get("override_usd")
    usd = override if override is not None else budget_state.get("usd")
    krw = budget_state.get("krw") or 0
    if basis == "krw_orderable_reference" and override is None:
        return "watch_only", ["fx_required"]          # USD 날조 금지; 환전 전제 표시
    if usd is not None and usd > 0:
        return "buy_review", []                        # 예산 충분 — 품질 verdict 유지
    # usd <= 0 (0/None) and not krw_reference-permissive:
    reasons = ["budget_gap"]
    if krw > 0:        reasons.append("fx_required")
    if override is None: reasons.append("operator_budget_required")
    return "watch_only", reasons
```
- 적용 순서(candidate 루프): `base = classify_candidate_symbol(...)` →
  `v, q = demote_for_quality(base, quality_flags)`(PR-B) →
  `v, budget_reasons = demote_for_budget(v, budget_state)`(본 PR) → count-cap 마지막.
- **품질 verdict 우선, budget은 buy_review만 하향**(rejected/data_gap/품질 watch는 불변).
- **never fabricate USD from KRW.**

### 3.4 reason / evidence 기록
- primary `reject_or_wait_reason`: budget 하향 시 첫 budget reason(`budget_gap` 등)으로 세팅.
- evidence_snapshot에 **enumerated additive 필드**(JSONB, 모델/migration 불요):
  - `budget_basis: str`
  - `budget_fit: bool` (effective_usd > 0 또는 충분)
  - `available_usd: float | None`
  - `krw_orderable_reference: float | None`
  - `operator_budget_override_usd: float | None`
  - `budget_reasons: list[str]` (∈ `budget_gap`/`fx_required`/`operator_budget_required`)
- `auto_emit.py:476-490` verdict-assignment if/elif에 budget 분기/헬퍼 결과 병합.

### 3.5 surfacing
- ActionPacket/Hermes/UI가 evidence 필드 소비. UI에서 "현금 부족(budget_gap)"/"환전
  필요(fx_required)"/"운영자 예산 입력 필요(operator_budget_required)" 구분. KRW는 USD와
  섞지 않고 reference로만. (프론트 카드 컴포넌트 상세는 evidence 계약 확정 후 후속 가능.)

## 4. Acceptance criteria (이슈 매핑)
- USD buying power=0이면 후보를 숨기지 않고 `budget_gap`/`fx_required` 등 사유를 남김. → 3.3
- KRW reference가 있어도 USD 주문가능금액과 혼동 안 함(합산/날조 금지). → 3.2/3.3
- 카드에 budget basis + budget fit 표시. → 3.4
- request에 operator budget/candidate cap이 오면 우선 적용. → 3.1(override precedence)
- focused tests가 USD=0, KRW reference present, operator budget override 케이스 커버. → §5

## 5. Test plan
1. **demote_for_budget 단위(순수)**: basis×(usd 0/>0)×(krw 0/>0)×(override 유무) 매트릭스 →
   기대 verdict/reasons. buy_review만 하향, 비-buy 불변.
2. **USD=0 (available_usd 기본)**: 후보 present, watch_only, evidence `budget_reasons`에
   `budget_gap`(+ KRW>0 시 `fx_required`, override 없으면 `operator_budget_required`).
   buy_review 아님.
3. **KRW reference present**: evidence `krw_orderable_reference` 세팅 + `available_usd` 에
   합산 안 됨(별도 필드). basis별 표기 검증.
4. **operator_budget_override**: override>0 제공 → basis 무관 effective_usd=override,
   충분 시 buy_review 유지; override 없음 + USD=0 → `operator_budget_required`.
5. **USD>0**: budget gate 미하향(buy_review 유지; budget은 절대 상향 안 함).
6. **default basis**: budget_basis 미지정 → available_usd 적용.

## 6. Safety boundaries / Non-goals
- 환전 실행·주문 preview/submit/cancel/modify·broker/order/watch/order-intent mutation·
  live trading automation 금지.
- 자동 예산 추정으로 매수 권고 강화 금지(USD=0은 절대 silent buy 아님; budget은 하향 전용).
- KRW→USD 숫자 fabrication 금지(`portfolio_journal.py:90` 정직 동작 보존).
- **classify_candidate_symbol signature 미변경**(후처리 헬퍼만; PR-B와 무충돌).
- migration 없음(request 필드 in-memory; evidence 필드 JSONB additive).

## 7. Out of scope / follow-up
- 실 FX rate 적용 후보 sizing(자동화 금지 경계); 프론트 카드 상세 디자인; dead `us/` 제거 — 별도.
