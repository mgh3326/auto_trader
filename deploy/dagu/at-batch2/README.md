# Dagu batch2 제안 복사본

아래 네 DAG 파일은 **저장소에서 등록되지 않는 복사본**이다. Dagu 호스트에 설치하는 일은 머지 후 별도 운영 절차에서 한다. 모든 DAG는 `host: pi` 워커, 배포 이미지 digest, `/root/at-secrets/.env.api`를 사용한다.

| DAG | 스케줄 (KST) | 스케줄 (UTC) | 진입점 |
|---|---|---|---|
| `at-batch2-daily-candles-kr` | 월–금 16:30 | 월–금 07:30 | `scripts.sync_daily_candles --market kr` |
| `at-batch2-daily-candles-us` | 화–토 07:00 | 월–금 22:00 | `scripts.sync_daily_candles --market us` |
| `at-batch2-daily-candles-crypto` | 매일 09:00 | 매일 00:00 | `scripts.sync_daily_candles --market crypto` |
| `at-batch2-order-proposal-night-sweep` | 월–금 16:30 및 07:00 | 월–금 07:30 및 일–목 22:00 | `scripts.order_proposal_night_sweep` |

일봉 스크립트는 `--market kr|us|crypto`를 받고 job 결과를 JSON으로 출력한다. `run_daily_candles_sync`에는 날짜나 lookback 인자가 없어 `--date`/별도 backfill 옵션은 제공하지 않는다. 2026-11-18 보충 여부는 job 자체 lookback 범위에 따른다.

야간 스윕은 기존 `ORDER_PROPOSAL_NIGHT_SWEEP_ENABLED` 게이트를 그대로 따른다. 값이 꺼져 있으면 `{"status": "skipped"}`를 출력하고 성공 코드로 끝난다. 이 복사본은 새 게이트를 만들거나 TaskIQ 스케줄을 등록하지 않는다.
