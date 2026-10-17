# funnel_{kr,us,crypto}.csv — 방법

입력: `proposals_buy.csv`(desk 읽기 전용 export, BUY 제안 231건, 제안당 rung 1개; 파일 물리 행 243은 `strategy` 셀 안 줄바꿈 때문) + `forecasts_all.json`.

`approval_events` 가 이 기간 0행이라 단계 전이 이력이 없다. 단계는 제안/rung 의 상태·시각 열로 재구성한다:

| 단계 | 규칙 |
|---|---|
| candidates_forecasts | 같은 기간(제안 최초~최종 생성 시각)의 forecast 중 `deferred_no_action` + `new_buy_candidate` |
| proposed | `action == place` (cancel 제안 3건 제외) |
| approved | `auto_approved` 가 `yes` 로 시작 **또는** `approved_at` 있음 |
| gate_pass | approved **AND** `broker_order_id` 있음(전송 시점 재검증 게이트 통과 후 브로커 전송) |
| accepted | gate_pass **AND** 원장 매칭(`ledger_status` 또는 `toss_broker_status` 있음). 전송됐는데 둘 다 없으면 `n_unmatched_ledger` 로 따로 센다(US 5건) — accepted 로 세지 않는다 |
| filled | accepted **AND** (`rung_state == filled` 또는 `ledger_filled_qty > 0` 또는 `toss_broker_status == FILLED`) |

- 전환율 = 직전 단계 대비, Wilson 95% CI + 종목 군집 bootstrap CI.
- candidates → proposed 는 **부분집합 전환이 아니다**(제안에는 add·reserve 티어 등 forecast 없는 경로도 있다) — `ratio_not_subset` 로만 표기.
- 금액(통화 원본: KR·crypto=KRW, US=USD): 승인 예산 = rung `notional`, 없으면 `quantity × limit_price`(시장가처럼 둘 다 없으면 0, `n_approved_budget_missing` 로 표기). 체결 금액 = 체결 수량 × 평균 체결가(원장에 평균가가 없으면 지정가). 비율 CI = 승인 rung 단위 percentile bootstrap.
- `detail` 열: 승인 후 미전송 rung 상태, 접수 후 미체결 rung 상태 분포.
