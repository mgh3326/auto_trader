# Dagu batch2 제안 복사본

아래 네 DAG 파일은 **저장소에서 등록되지 않는 복사본**이다. Dagu 호스트에 설치하는 일은 머지 후 별도 운영 절차에서 한다. 모든 DAG는 `host: pi` 워커, 배포 이미지 digest, `/root/at-secrets/.env.api`를 사용한다.

## 호스트 복사 순서

1. 기존 TaskIQ `at-scheduler`가 중지되어 있고 일봉 cron이 실행되지 않는지 확인한다. 아직 실행 중이면 먼저 중지해 TaskIQ와 Dagu가 같은 시장을 동시에 동기화하지 않게 한다.
2. 확인 후에만 해당 일봉 DAG를 Dagu 호스트에 복사하고 검증한다. 이 저장소/PR은 복사나 등록을 수행하지 않는다.

| DAG | 스케줄 (KST) | 스케줄 (UTC) | 진입점 |
|---|---|---|---|
| `at-batch2-daily-candles-kr` | 월–금 16:30 | 월–금 07:30 | `scripts.sync_daily_candles --market kr` |
| `at-batch2-daily-candles-us` | 화–토 07:00 | 월–금 22:00 | `scripts.sync_daily_candles --market us` |
| `at-batch2-daily-candles-crypto` | 매일 09:00 | 매일 00:00 | `scripts.sync_daily_candles --market crypto` |
| `at-batch2-order-proposal-night-sweep` | 월–금 16:30 및 07:00 | 월–금 07:30 및 일–목 22:00 | `scripts.order_proposal_night_sweep` |

일봉 스크립트는 `--market kr|us|crypto`를 받고 job 결과를 JSON으로 출력한다. `run_daily_candles_sync`에는 날짜나 lookback 인자가 없어 `--date`/별도 backfill 옵션은 제공하지 않는다. 2026-11-18 보충 여부는 job 자체 lookback 범위에 따른다.

야간 스윕은 기존 `ORDER_PROPOSAL_NIGHT_SWEEP_SCHEDULE_ENABLED`와 `ORDER_PROPOSAL_NIGHT_SWEEP_ENABLED`가 모두 켜져야 실행된다. 어느 하나라도 꺼져 있으면 `{"status": "skipped"}`를 출력하고 성공 코드로 끝난다. 기존 body gate는 그대로이며, 새 flag는 추가하지 않는다. Per-rung inference 실패는 JSON 결과에 포함되고 non-zero 종료 코드가 된다.
