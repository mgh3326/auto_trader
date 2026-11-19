# 동결 예외 기록 — hk 3795 계획 결속 제안

이 기록은 D4만 다룬다. 머지만으로 설정이나 주문이 활성화되지 않는다.
D1~D3의 자동승인 완화는 T5에서 별도로 기록한다.

```yaml
freeze_epoch_id: UNKNOWN
change_id: hk3795-mandate-plan-bound-auto-approve
pr_number: TBD
merge_sha: TBD_ON_MERGE
operator_decision_ref:
  - decision/2026-11-10/at-new-strategy-go
  - decision/2026-11-16/at-strategy-direction
  - decision/2026-11-12/at-b02-reconcile-and-3792
  - decision/2026-11-18/at-3794-3795-recommended
relaxations:
  D4: 신규 매수 중단 결정에 대한 승인된 계획 결속 매수 제안 예외
change_class:
  - plan_bound_buy_proposal_exception
market: [kr, us]
account_scope: 승인된 위임장과 계획 및 명시된 제안 생성 계좌에 한정
  # MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES 기본값은 빈 값이다.
affected_policy_keys: []
consumer_paths:
  - app/services/mandate/
  - app/services/order_proposals/service.py
  - app/services/order_proposals/telegram_callback.py
  - app/services/order_proposals/revalidation.py
effective_at: >-
  실전 사람 카드 체크리스트 충족 후 실전 위임장 버전 승인과
  MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES 설정에 대한 별도 운영자 결정 이후.
  이 기록과 코드 머지는 활성화를 승인하지 않는다.
live_only_claim: false
mock_projection_hash_before: NOT_APPLICABLE
mock_projection_hash_after: NOT_APPLICABLE
mock_experiment_effect: NONE
rollback: >-
  MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES를 비워 새 계획 결속 제안을 중지한다.
  이미 브로커가 받은 주문은 되돌리지 않으며 증거를 바탕으로 대사한다.
  닫히지 않은 fence와 예약은 대사 전까지 유지한다.
evidence: docs/design/3795-mandate.md §4.4–§4.8 및 T4c 테스트
```

D4의 근거는 `decision/2026-11-12/at-b02-reconcile-and-3792`의 hk 3792
항목 1·4다. 기존 신규 매수·물타기 중단을 정책에 반영하는 작업의 완료 또는
운영자 예외 기록은 실전 활성화 선행 조건이다. 가격·수량 검사, 주문 게이트,
손실 매도 가드와 사람 승인 절차는 유지한다.
