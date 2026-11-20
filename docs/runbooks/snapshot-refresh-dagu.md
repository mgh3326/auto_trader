# 스냅샷 일일 갱신 Dagu 제안 (hk 5752)

컨센서스·재무제표 스냅샷과 KR·US 스크리너 스냅샷을 매일 한 번 갱신하는 다섯 DAG의 운영 절차다.
배경: `analyst_consensus_snapshots`와 `financial_fundamentals_snapshots`는 프로덕션에서 0행이다
(한 번도 스케줄되지 않았다). `invest_screener_snapshots`는 TaskIQ 스케줄러 은퇴 뒤 KR 11-17, US 11-16에서
멈췄다. 컨센서스 이력은 나중에 복구할 수 없으므로 지금 켠다.

이 저장소는 DAG를 **등록하지 않는다**. 등록은 설계 세션이 아래 dry-run 채움률을 확인한 뒤 호스트에서 한다.

## DAG 요약

| DAG (`deploy/dagu/at-snapshots/`) | 스케줄 (UTC) | 한국 시각 | 명령 | 게이트 env (참일 때 `--commit`) |
|---|---|---|---|---|
| `at-snapshot-screener-kr` | `CRON_TZ=UTC 25 11 * * 1-5` | 월–금 20:25 | `build_invest_screener_snapshots --market kr --all --keep-trading-days 5` | `AT_SNAPSHOT_SCREENER_KR_COMMIT_ENABLED` |
| `at-snapshot-screener-us` | `CRON_TZ=UTC 30 1 * * 2-6` | 화–토 10:30 | `build_invest_screener_snapshots --market us --all --common-stocks-only --keep-trading-days 5` | `AT_SNAPSHOT_SCREENER_US_COMMIT_ENABLED` |
| `at-snapshot-consensus-kr` | `CRON_TZ=UTC 30 12 * * 1-5` | 월–금 21:30 | `build_analyst_consensus_snapshots --market kr` | `AT_SNAPSHOT_CONSENSUS_KR_COMMIT_ENABLED` |
| `at-snapshot-consensus-us` | `CRON_TZ=UTC 0 3 * * 2-6` | 화–토 12:00 | `build_analyst_consensus_snapshots --market us` | `AT_SNAPSHOT_CONSENSUS_US_COMMIT_ENABLED` |
| `at-snapshot-financial-kr` | `CRON_TZ=UTC 0 14 * * *` | 매일 23:00 | `build_financial_fundamentals_snapshots --market kr --limit 1500 --skip-existing` | `AT_SNAPSHOT_FINANCIAL_KR_COMMIT_ENABLED` (`--commit --allow-partial`) |

- 모든 DAG는 `host: pi` 워커, 배포 이미지 digest, `/root/at-secrets/.env.api`, `dotenv: /root/at-secrets/.env.dagu-at-batch`를 쓴다.
- 게이트가 **설정되지 않으면** 기본 동작은 dry-run이다. 단 `at-snapshot-financial-kr`은 예외로 `--estimate-only`다.
  이 잡의 일반 dry-run은 DART를 호출해 일일 예산(18,000)을 쓰고 아무것도 쓰지 않으므로, 설정되지 않은 게이트에서는 예산을 쓰지 않는 추정만 한다.
- 실패와 성공은 `logger -t at-dagu-batch` journal 한 줄을 먼저 남긴다. 핑 URL 환경 변수가 있을 때만 healthchecks를 호출한다.
  핑 URL 값은 레포에 없다.

### 시각을 고른 이유

- KR 스크리너(11:25 UTC)는 KR 일봉(07:30 UTC, 월–금) 뒤다. `data-plane-kr`(09:00 UTC), `at-batch1-kr-investor-flow`(09:10 UTC), `at-batch1-kr-fundamentals`(09:20 UTC)와 겹치지 않는다.
- US 스크리너(01:30 UTC, 화–토)는 US 일봉 동기화(22:00 UTC, 월–금), `at-batch1-us-valuation`(23:30 UTC), `data-plane-us`(23:00 UTC) 뒤다.
  `at-batch1-crypto-*`와 `data-plane-crypto`(00:00–00:50 UTC)와도 떨어져 있다.
- 컨센서스 KR(12:30 UTC)·US(03:00 UTC)는 각각 KR 스크리너 뒤, US 스크리너 뒤다. 보유·관심 종목만 대상이라 짧게 끝난다.
- 재무제표(14:00 UTC)는 다른 스냅샷과 DART 공시 잡에서 떨어져 있다.
- `at-batch2-execution-ledger-reconcile`이 :15/:45에 돌아 스크리너·컨센서스 잡과 짧게 겹칠 수 있다. 쓰는 테이블이 다르다.

## 1. dry-run 채움률 확인 (등록 전, 수동, 한 번)

채움률 확인은 호스트에서 한 번 실행한다. 명령은 저장소 DAG와 같은 이미지 digest를 쓰고 `--commit`을 붙이지 않는다.
각 실행은 마지막에 `FILL_RATE_JSON {...}` 한 줄을 출력한다. `job`, `rows`, 그리고 핵심 필드별 `non_null`과 `share`가 들어 있다.
행이 0이면 `share`는 `null`이다.

```bash
IMG="$(cat /root/at-run/deployed-digest)"
docker run --rm --init --network host --workdir /app --env-file /root/at-secrets/.env.api "$IMG" \
  /app/.venv/bin/python -m scripts.build_invest_screener_snapshots --market kr --limit 20 --keep-trading-days 5
docker run --rm --init --network host --workdir /app --env-file /root/at-secrets/.env.api "$IMG" \
  /app/.venv/bin/python -m scripts.build_invest_screener_snapshots --market us --limit 20 --common-stocks-only --keep-trading-days 5
docker run --rm --init --network host --workdir /app --env-file /root/at-secrets/.env.api "$IMG" \
  /app/.venv/bin/python -m scripts.build_analyst_consensus_snapshots --market kr
docker run --rm --init --network host --workdir /app --env-file /root/at-secrets/.env.api "$IMG" \
  /app/.venv/bin/python -m scripts.build_analyst_consensus_snapshots --market us
docker run --rm --init --network host --workdir /app --env-file /root/at-secrets/.env.api "$IMG" \
  /app/.venv/bin/python -m scripts.build_financial_fundamentals_snapshots --market kr --limit 20 --skip-existing
```

- 스크리너 `--limit 20`은 KR·US 각각 활성 유니버스 앞 20개만 본다. 전체 채움률이 아니라 필드 채움 방식의 확인이다.
- 재무제표 `--limit 20 --skip-existing`은 DART 약 220회를 쓴다(20 × 11). 아직 수집되지 않은 종목 20개만 고르므로 전부 채워졌으면 행 0이다.
- 스크리너 출력의 `retention:` 줄은 `--keep-trading-days 5`일 때 dry-run으로 지워질 행 수를 보여 준다. 기록하지 않는다.

### 등록 기준 (제안, 설계 세션이 실측 후 확정)

| 잡 | 필드 | 제안 기준 (share ≥) | 근거 |
|---|---|---|---|
| 스크리너 | `latest_close`, `prev_close`, `change_rate`, `week_change_rate` | 0.98 | OHLCV에서 바로 계산된다 |
| 스크리너 | `daily_volume`, `daily_turnover` | 0.95 | 일봉 거래량·거래대금 |
| 스크리너 | `market_cap` | 0.80 | 외부 시가총액 소스에 의존한다 |
| 스크리너 | 행 수 | 기준 유니버스의 0.9 이상 | 한 날짜만 채워지면 보관 정책이 이전 날을 지운다 |
| 컨센서스 | `total_count`, `target_mean`, `current_price` | 0.50 | 보유·관심 종목은 커버리지가 좁다. 채움이 낮으면 등록하지 않고 원인을 본다 |
| 재무제표 | `revenue`, `net_income`, `filing_date` | 0.90 | DART 연간 보고서 기준 |

- 위 기준 중 하나라도 밑돌면 해당 DAG를 등록하지 않는다. 원인(소스 누락, 필드 매핑)을 먼저 본다.
- `consecutive_up_days`, `payout_ratio`, `roe` 같은 희소 필드는 기준 없이 참고만 한다.

## 2. 등록 절차 (설계 세션, 운영자 승인 뒤)

1. 머지된 `deploy/dagu/at-snapshots/*.yaml` 다섯 개를 OCI Dagu 호스트에 복사하고 검증한다. 레포는 복사하지 않는다.
2. 게이트 env는 **등록 순서대로 하나씩** 켠다. 한 번에 다섯 개를 켜지 않는다.
   1) `AT_SNAPSHOT_SCREENER_KR_COMMIT_ENABLED` (KR 스크리너, 가장 중요)
   2) `AT_SNAPSHOT_SCREENER_US_COMMIT_ENABLED`
   3) `AT_SNAPSHOT_CONSENSUS_KR_COMMIT_ENABLED`, `AT_SNAPSHOT_CONSENSUS_US_COMMIT_ENABLED`
   4) `AT_SNAPSHOT_FINANCIAL_KR_COMMIT_ENABLED` (DART 예산을 확인한 뒤에만)
   게이트 값은 `/root/at-secrets/.env.dagu-at-batch`에만 둔다. 레포와 로그에는 쓰지 않는다.
3. 각 DAG는 등록 직후 Dagu UI에서 한 번 수동 실행하고 journal(`logger -t at-dagu-batch`)에서 `status=success`를 확인한다.
4. 스크리너를 켠 뒤 `invest_screener_snapshots`에서 시장별 `snapshot_date` 개수를 SELECT로 확인한다(읽기 전용). 개수는 5 이하여야 하고, 최신 날짜가 해당 시장의 최근 거래일이어야 한다.
5. 재무제표 DAG는 첫 실행에서 DART 예산이 다른 잡과 겹치지 않는지 확인한다. 예산 한도는 `OPENDART_DAILY_REQUEST_BUDGET`(기본 18,000)이다.

## 3. 중지 절차

- **가장 빠른 중지**: Dagu UI에서 해당 DAG를 일시 정지한다(pause). 다음 스케줄이 돌지 않는다.
- **쓰기만 중지**: 게이트 env를 지우거나 `false`로 바꾸면 다음 실행부터 dry-run이다. 컨센서스 dry-run도 보유·관심 종목 조회와 외부 호출을 하므로 완전 중지가 아니다. 완전 중지는 일시 정지다.
- **보관 정책 되돌리기 불가**: `--keep-trading-days 5`가 쓴 삭제는 되돌릴 수 없다. 먼저 dry-run의 `retention:` 줄로 지워질 행 수를 확인하고, 예상과 다르면 게이트를 끈다.
- 재무제표 DAG를 멈춘 뒤에도 DART 예산 사용량은 그날 안에 기록되어 있다. 다음 날 `--skip-existing`이 이어서 수집한다.

## 4. 알려진 한계

- 재무제표 `--skip-existing`은 새 종목만 채운다. 이미 수집된 종목은 갱신하지 않는다. 기존 동작이며 이 변경에서 바꾸지 않았다.
- 스크리너 보관은 날짜 기준 최신 5개를 남긴다. 어떤 날짜가 부분 빌드로 행이 적으면 그 날도 한 날짜로 센다.
- 컨센서스 범위는 보유 ∪ 활성 관심 종목이다. 전체 유니버스 옵션은 없다(빌더 설계).
