# ROB-418 — kiwoom_mock read 도구 필수 파라미터(qry_tp/stk_bond_tp) 복구

- **이슈**: ROB-418 (E라인 E3) / **dedup**: ROB-399(동일 버그)
- **유형**: Bug fix
- **작성일**: 2026-06-02
- **연관**: ROB-421(오케스트레이션), ROB-399(중복 — 본 fix로 covered), ROB-319(account-read 배선)

## 증상 / 근본 원인

Kiwoom mock 읽기 도구가 필수 broker 파라미터 누락으로 전부 실패(operator 실측, 2026-06-01, 모두 `return_code 2`):
- `kiwoom_mock_get_orderable_cash`(no-symbol→get_balance) → `입력 값 오류입니다[1511:필수입력 파라미터=qry_tp]`
- `kiwoom_mock_get_positions`(get_balance) → 동일 `필수입력 파라미터=qry_tp`
- `kiwoom_mock_get_order_history`(get_order_status) → `필수입력 파라미터=stk_bond_tp`

근본: `app/services/brokers/kiwoom/domestic_account.py`가 요청 본문에 API 필수 파라미터를 안 채움:
- `get_balance`(kt00018) → `body={}` → **qry_tp 누락**
- `get_order_status`(kt00009) → `body={}` → **stk_bond_tp 누락**

operator의 `return_code 2` + 파라미터-이름 에러가 **API가 해당 파라미터를 요구함**을 증명한다(런타임 확정). ROB-421 검증의 "코드상 없음=already-fixed" 결론은 틀렸고, 코드가 필수 파라미터를 누락하는 것이 버그.

## ROB-399 중복 관계

ROB-399("kiwoom_mock 조회 도구가 필수 파라미터 누락으로 전부 실패 qry_tp/stk_bond_tp", High, Backlog)는 **동일 버그**. ROB-421 지시("ROB-399와 중복 없이 좁게 처리")대로 **ROB-418이 read-param fix를 소유**하고 ROB-399는 covered(duplicate/related 처리).

## 설계 (브레인스토밍: 관례 기본값 상수 + operator smoke 게이트 / US 문서화만)

### 변경 표면 (read-only 조회 복구, migration 0, broker order mutation 없음, KRX-only 유지)

- `app/services/brokers/kiwoom/constants.py` — 파라미터 기본값 상수
- `app/services/brokers/kiwoom/domestic_account.py` — `get_balance`/`get_order_status` body
- `docs/runbooks/kiwoom-mock-smoke.md` — KRX-only/US 미지원 + 값 smoke-확인 명시

### Unit 1 — 증명된 누락 파라미터 추가

`constants.py`에 상수:
```python
# ROB-418 — Kiwoom REST account-read 필수 파라미터 기본값.
# Kiwoom enum 관례 기반 기본값. 정확한 값은 operator live mock smoke로 확정한다
# (이 세션 creds 없음). 전건실패(필수입력 파라미터 누락, return_code 2)를 호출
# 성립으로 회복하는 것이 1차 목표이며, 값의 scope 정확성은 smoke가 검증한다.
ACCOUNT_BALANCE_QRY_TP_DEFAULT = "1"      # kt00018 조회구분
ACCOUNT_ORDER_STK_BOND_TP_DEFAULT = "0"   # kt00009 주식채권구분(전체)
```

`domestic_account.py`:
- `get_balance`(kt00018): `body={"qry_tp": constants.ACCOUNT_BALANCE_QRY_TP_DEFAULT}`
- `get_order_status`(kt00009): `body={"stk_bond_tp": constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT}`

**범위 제한(over-reach 회피)**:
- `get_orderable_amount`(kt00010, with-symbol) → **무변경**. operator가 직접 실패를 증명하지 않음(no-symbol 경로가 get_balance로 빠져 qry_tp 에러였음). kt00010 자체 필수 파라미터는 추측 추가하지 않고 smoke-TBD로 남긴다(wrong/unexpected-param 회피).
- `dmst_stex_tp` 등 operator가 명시하지 않은 파라미터는 추가하지 않음(증명된 누락만).

### Unit 2 — US/KRX 문서화 (코드 무변경)

KRX-only fail-closed(`orders_kiwoom_variants._exchange_error`가 non-KRX 거부)는 이미 동작. 런북/도구 설명에 "kiwoom_mock=KRX 전용, US 미지원" 명시(코드 변경 없음).

## 테스트 (TDD)

`tests/test_kiwoom_domestic_account.py` (기존 body 단언 갱신 + 신규):

1. `get_balance()` body에 `qry_tp == ACCOUNT_BALANCE_QRY_TP_DEFAULT` 포함(기존 kt00018 단언 유지).
2. `get_order_status()` body에 `stk_bond_tp == ACCOUNT_ORDER_STK_BOND_TP_DEFAULT` 포함(기존 kt00009 + cont_yn/next_key 단언 유지).
3. `get_orderable_amount(symbol=...)` body는 `{"stk_cd": ...}`로 **무변경**(회귀 가드).
4. 계좌번호 마스킹 회귀(기존 `12345678-01` 미노출) 무변경.
5. US/non-KRX 거부 회귀: 기존 `test_mcp_kiwoom_order_variants.py`의 NXT/SOR/비-KR 거부 무변경.

## 안전 경계 / Non-goals

- read-only 조회 복구(broker order mutation 없음), migration 0, KRX-only 유지.
- **값 정확성 검증 게이트 = operator live mock smoke**(creds 부재로 이 세션 미수행). 전건실패→호출 성립이 1차 목표.
- `get_orderable_amount`(kt00010 with-symbol) 필수 파라미터 = smoke 확인 후 follow-up(추측 미추가).
- 주문 경로(kt10000/kt10001 등) 파라미터 점검은 본 PR 범위 밖(read 복구만; ROB-399 제안의 주문 부분은 별도).
- US 지원 확대는 별도 product decision(Non-goal); 현재는 KRX-only fail-closed + 문서화.
- ROB-399는 이 fix로 covered → duplicate/related 처리(Linear에서 정리).
