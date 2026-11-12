# Freeze deviation record — KIS KR live terminal-only reconcile

```yaml
freeze_epoch_id: UNKNOWN
change_id: kis-live-terminal-only-reconcile-20261112
pr_number: TBD
merge_sha: TBD_ON_MERGE
operator_decision_ref: hk:doc decision/2026-11-12/at-b02-reconcile-and-3792
market: kr
account_scope: kis_live
change_class: terminal_only_reconcile
what_is_relaxed: >-
  무인 CLI에서 KIS_LIVE_AUTO_RECONCILE_ENABLED만으로 증거가 있는
  만료·취소 상태와 제안 rung을 반영한다.
what_stays_gated: >-
  체결, 거래, 저널 및 실현손익 기록은 기존 두 플래그와
  KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED 뒤에 남는다.
rollback: >-
  PR을 되돌리고 종료 상태 전용 실행을 중지한다. 이미 반영된 원장과
  projection 상태는 되돌리지 않으며 운영자가 증거를 검토한다.
```

`_repair_terminal_kis_proposal_projections`와 `run_resting_rung_sweep` 사전
처리는 기존대로 실행되어 이미 종료된 원장의 상태를 투영한다. 이들은 저널을
기록하지 않는다. 스케줄이나 환경 값은 변경하지 않는다.
