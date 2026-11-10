# Dagu 전환 런북 — 배치 1 (hk 3848)

Prefect 배치 11개를 Dagu 로 옮기는 절차다. **이 PR 은 아무것도 등록하지 않는다.** DAG 파일은
`deploy/dagu/at-batch/` 에 제안으로만 있고, 등록(복사)·Prefect 스케줄 pause 는 이 런북을
운영자가 한 잡씩 따라 할 때만 일어난다. 승인 근거: 운영자 결정
`hk:doc decision/2026-11-10/ncp-schedules-dagu-fillwire` §3 (이 PR 머지 *후*, 이 런북대로).
인벤토리: `hk:doc report/2026-11-10/ncp-schedule-inventory` §1·§4. 제외: 라이브 세션 kickoff 11개,
US 스크리너(`US Invest Screener Snapshots`, TaskIQ `us_post_close` 와 중복 의심), TaskIQ, env 변경.

🔴 `deploy/dagu/**` 는 mergewake `deploy_approval` 대상이라 머지 후 롤아웃은 사람 승인 건으로 남는다.
이 런북의 어떤 단계도 그 승인을 대신하지 않는다.

## 대응표

시각은 KST ↔ UTC. OCI Dagu 스케줄러가 UTC 로 돌기 때문에 DAG 의 `schedule` 은 UTC 다.
Prefect 스케줄은 `flows/auto_trader/*.py` 의 `CronSchedule`(robin-prefect-automations@d8f0054).

| # | DAG 파일 (`deploy/dagu/at-batch/`) | Prefect deployment | Prefect (KST) | Dagu (UTC) | Dagu (KST) | 핑 env 이름 | 게이트 |
|---|---|---|---|---|---|---|---|
| 1 | `at-batch1-crypto-screener` | `Invest Crypto Screener Snapshots/daily-kst` | 09:20 매일 | `35 0 * * *` | 09:35 (+15분) | `AT_BATCH_PING_URL_CRYPTO_SCREENER` | — |
| 2 | `at-batch1-crypto-insight` | `Invest Crypto Insight Snapshots/daily-kst` | 09:20 매일 | `50 0 * * *` | 09:50 (+30분) | `AT_BATCH_PING_URL_CRYPTO_INSIGHT` | — |
| 3 | `at-batch1-kr-investor-flow` | `KR Investor Flow Snapshots/daily-freshness` | 18:10 평일 | `10 9 * * 1-5` | 18:10 | `AT_BATCH_PING_URL_KR_INVESTOR_FLOW` | — |
| 4 | `at-batch1-kr-fundamentals` | `invest_kr_fundamentals_snapshots/daily-kst` | 18:00 매일 | `20 9 * * *` | 18:20 (+20분) | `AT_BATCH_PING_URL_KR_FUNDAMENTALS` | — |
| 5 | `at-batch1-us-valuation` | `US Market Valuation Snapshots/daily-post-us-close` | 08:30 화–토 | `30 23 * * 1-5` | 08:30 화–토 | `AT_BATCH_PING_URL_US_VALUATION` | — |
| 6 | `at-batch1-toss-warnings` | `Toss Warnings Sync/daily-preopen` | 07:30 매일 | `30 22 * * *` | 07:30 | `AT_BATCH_PING_URL_TOSS_WARNINGS` | — |
| 7 | `at-batch1-toss-symbol-master` | `Toss Symbol Master Sync/weekday-preopen` | 07:20 평일 | `20 22 * * 0-4` | 07:20 월–금 | `AT_BATCH_PING_URL_TOSS_SYMBOL_MASTER` | — |
| 8 | `at-batch1-us-fundamentals` | `US Financial Fundamentals Snapshots/weekly-sunday` | 일 09:00 | `0 0 * * 0` | 일 09:00 | `AT_BATCH_PING_URL_US_FUNDAMENTALS` | **hk 3846** |
| 9 | `at-batch1-market-events-daily` | `Market Events Daily/daily` | 24시간 interval (고정 시각 없음) | `30 21 * * *` (임시) | 06:30 | `AT_BATCH_PING_URL_MARKET_EVENTS_DAILY` | — (아래 주의) |
| 10 | `at-batch1-dart-0700` | `Market Events KR Disclosures/dart-0700-kst` | 07:00 매일 | `0 22 * * *` | 07:00 | `AT_BATCH_PING_URL_DART_0700` | **hk 3845** |
| 11 | `at-batch1-dart-1830` | `Market Events KR Disclosures/dart-1830-kst` | 18:30 매일 | `30 9 * * *` | 18:30 | `AT_BATCH_PING_URL_DART_1830` | **hk 3845** |

**겹침 이동(두 쌍)** — 둘 다 KIS/Toss/Upbit REST 를 같은 시각에 때리던 쌍이다. `data-plane-crypto`(00:20 UTC =
09:20 KST)와 `data-plane-kr`(09:00 UTC 평일 = 18:00 KST)는 그대로 두고 배치 1 쪽을 옮겼다.
- 크립토 스크리너 09:20 → **09:35 KST**, 크립토 인사이트 09:20 → **09:50 KST** (서로도 15분 간격).
- KR 펀더멘털 18:00 → **18:20 KST** (investor-flow 18:10 과도 겹치지 않는다).
- `tests/scripts/test_dagu_at_batch_dags.py` 가 data-plane DAG 의 실제 `schedule` 을 읽어 ≥10분 간격을 고정한다.
  단 간격은 *시작 시각*이다. data-plane-kr 는 한 번에 약 70분 걸리므로 실행 구간은 여전히 겹친다 —
  이 PR 이 한 일은 "같은 순간 동시 시작"을 없앤 것이다.

**9번 주의 (interval 스케줄)**: Prefect 의 24시간 interval 은 배포 생성 시각이 anchor 라 고정 시각이 없다. 이 PR 은
06:30 KST 를 *임시*로 골랐다(07:00 DART 와 겹치지 않게). 등록 전에 §잡별 절차 9 에서 Prefect 의 실제 다음 실행
시각을 읽어 DAG 의 `schedule`·헤더(`dagu_schedule_kst`)·테스트 표를 같이 고친 뒤에 진행한다. 9번은 US 실적(finnhub)과
KR 공시(dart)를 함께 수집하므로 DART 수집이 깨진 동안(hk 3845)은 같은 증상을 보일 수 있다 — 이 이유로 9번도
3845 머지 후로 미루는 편이 안전하다(운영자 판단, 게이트 목록에는 넣지 않았다).

## 사전 조건 (이름만 적는다. 값은 이 레포에 없다)

NCP Dagu 워커(`worker_selector: host: ncp`, 기존 `prefect-error-watchdog` 와 같은 라벨)에서:

- **도구**: `docker`(root), `curl`, GNU `date`. 각 DAG 가 `docker run` 으로 auto_trader 이미지를 띄운다.
- **이미지 포인터**: `/root/at-run/deployed-digest` (`ghcr.io/mgh3326/auto_trader@sha256:…` 전체 참조, deploy 스크립트가 쓴다).
- **컨테이너 env 파일**: `/root/at-secrets/.env.api` — Prefect 래퍼가 `--env-file` 로 넘기던 파일과 같다.
  파일 안에 `TOSS_API_ENABLED`(6·7번), `CRYPTO_INSIGHT_COMMIT_ENABLED`(2번)가 Prefect 때와 같은 값으로 있어야 한다.
  Prefect 의 로컬 모드는 `TOSS_API_ENABLED=true` 를 export 했지만 docker 모드 명령에는 없었다 — 즉 지금 NCP 는 env 파일 값에 의존한다.
- **DAG 용 dotenv**: `/root/at-secrets/.env.dagu-at-batch` (권한 `0600`). 이름만:

  | 이름 | 용도 |
  |---|---|
  | `AT_BATCH_PING_URL_<잡>` | 위 표의 11개. healthchecks https 핑 URL(잡마다 별도 체크) |
  | `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` | commit 게이트(1·4·5·8번). Prefect 가 해석하던 값과 같게 둔다 |

  Dagu 는 워커의 프로세스 환경을 step 에 넘기지 않는다(`robin-prefect-automations` `deploy/dagu/README.md`, Dagu 2.21.0 에서 확인).
  그래서 DAG 가 `dotenv` 줄로 읽는다. 대안은 Dagu 설정의 `env_passthrough` 다. 🔴 Dagu 는 없는 dotenv 파일을 오류 없이
  넘기므로 첫 실행 로그에서 핸들러가 핑을 보냈는지 반드시 확인한다(핑 URL 이 없으면 `curl` 이 실패한다).
- **commit 게이트 값 확인**: Prefect 최근 COMPLETED 런의 결과에 `commit_gate_enabled` 가 있다(1·4·5·8번). 그 값과 dotenv 값을 같게 둔다.
  게이트가 꺼져 있으면(또는 dotenv 에 없으면) DAG 는 `--commit` 없이 dry-run 으로 돈다 — fail-closed 이고 Prefect 와 같은 규칙이다.
  2번은 게이트를 컨테이너 env 파일의 `CRYPTO_INSIGHT_COMMIT_ENABLED` 가 정한다(Prefect docker 모드도 이 값을 컨테이너에 넘기지 못했다).
- **Dagu 버전**: 로컬 Dagu 2.21.0 에서 `dagu validate` 통과. OCI 버전은 **확인 못 함(UNVERIFIED)**. `command` 가 deprecated 라는 경고는 기존 제안과 같다.

## 잡별 절차 (한 번에 한 잡)

각 잡마다 아래 1~6을 순서대로 한다. 건너뛰지 않는다.

1. **시각 고르기**: Dagu 첫 스케줄 시각과 Prefect 다음 시각이 모두 2시간 이상 남은 때 한다.
   (DAG 를 복사하는 순간 Dagu 스케줄이 살아난다. 쓰기는 upsert 라 멱등이지만 REST 호출이 두 번 나간다.)
2. **복사**: `deploy/dagu/at-batch/<파일>.yaml` 을 OCI coordinator 의 Dagu DAGs 디렉터리(`~/.config/dagu/dags`)에 복사한다.
   복사 직후 `dagu validate <파일>` 로 문법을 확인한다.
3. **수동 1회 실행**: `dagu start <DAG 이름>`(= 파일명에서 `.yaml` 제외). 워커 로그에서 확인할 것:
   각 step 이 `docker run` 으로 끝났고, 핸들러가 `onSuccess` 로 핑을 보냈다(실패 시 `onFailure`).
4. **Prefect 최근 COMPLETED 런과 대조**: 아래 표의 항목이 같은 범위·같은 의미여야 한다. 개수는 시각에 따라 다를 수 있으니 *형태와 committed 여부*를 본다.
5. **Prefect 스케줄 pause**: 대조가 맞을 때만. 스케줄 ID 는 먼저 읽는다.

   ```bash
   # PREFECT_API_URL 은 in-container 서버를 가리킨다. 컨테이너 이름 at-prefect-server 는 인벤토리 기준이며 UNVERIFIED — docker ps 로 확인한다.
   docker exec -e PREFECT_API_URL=http://127.0.0.1:4200/api at-prefect-server \
     prefect deployment schedule ls "<flow>/<deployment>"
   docker exec -e PREFECT_API_URL=http://127.0.0.1:4200/api at-prefect-server \
     prefect deployment schedule pause "<flow>/<deployment>" <schedule_id>
   ```

   🔴 Prefect 배포를 `deploy_*` 로 다시 등록하면 스케줄/paused 가 코드 값으로 되돌아간다. 전환 중에는 재등록하지 않는다.
6. **첫 스케줄 실행 확인**: 다음 Dagu 스케줄 실행 뒤 핑이 왔고(healthchecks 가 "up"), 결과가 Prefect 때와 같은지 본다. 이 확인 전에는 다음 잡으로 넘어가지 않는다
   (같은 상위 의존이 없는 잡끼리는 하루에 묶어도 된다: 1+2, 3+4).

| # | 대조 항목 (스크립트가 마지막에 내는 출력 ↔ Prefect 결과의 `summary`) | 이 잡만의 확인 |
|---|---|---|
| 1 | `snapshot_date`·`fetched`·`would_upsert`/`upserted`·`committed` | `committed` 가 Prefect 와 같은가(게이트) |
| 2 | `snapshots_built`·`committed`·`providers`·`warnings` | `committed` 가 Prefect 와 같은가(env 파일 값) |
| 3 | `committed N rows`(`committed_rows`) | `--commit` 항상 켜짐 |
| 4 | `coverage: would_upsert=… commit_allowed=…`·`blocked:` | 매일 실행(주말 포함) |
| 5 | `snapshots_built`·`symbols_resolved`·`dry_run` | 화–토 한정(UTC 월–금) |
| 6 | `Toss warnings sync completed: {…}` 의 `status` | `status` 가 `disabled` 면 env 파일의 `TOSS_API_ENABLED` 누락 |
| 7 | kr·us 각각 종료 코드 0 | 두 step 이 병렬. 한 시장 실패가 다른 시장을 막지 않는다 |
| 8 | `snapshots_built`·`symbols_resolved`·`COMMIT BLOCKED:` | 🔴 §전환 순서 2차. hk 3846 이후 |
| 9 | `ingest complete: succeeded=N failed=0 range=…` (두 target) | 🔴 먼저 `prefect deployment inspect "Market Events Daily/daily"` 의 다음 실행 시각을 읽어 `schedule`·헤더·테스트 표를 고친다(UTC 변환: KST−9시간) |
| 10 | 같음, 창 = 어제~오늘(KST) | 🔴 §전환 순서 2차. hk 3845 이후 |
| 11 | 같음, 창 = 오늘(KST) | 🔴 §전환 순서 2차. hk 3845 이후 |

## 롤백 (잡별)

1. `docker exec -e PREFECT_API_URL=http://127.0.0.1:4200/api at-prefect-server prefect deployment schedule resume "<flow>/<deployment>" <schedule_id>`
2. Dagu DAGs 디렉터리에서 그 DAG 파일을 지운다. 돌고 있는 실행이 있으면 먼저 `dagu stop <DAG 이름>`.
3. healthchecks 의 해당 체크를 일시 중지한다(안 하면 "안 옴" 알림이 온다).

`resume` 은 놓친 실행을 소급하지 않는다. 다음 스케줄 시각부터 다시 돈다. 두 쪽이 한동안 동시에 돌아도 쓰기는 upsert 라 멱등이다
(`source_event_id`/스냅샷 키). 롤백은 데이터를 되돌리지 않는다.

## 전환 순서

**1차 — 게이트 없음.** 위험이 낮고 매일 도는 것부터. 앞선 잡의 첫 스케줄 실행을 확인한 뒤 다음으로.

1. 6 `at-batch1-toss-warnings`, 7 `at-batch1-toss-symbol-master` (장 전 준비 잡. 실패하면 KR 승인 창에 바로 보인다)
2. 1 `at-batch1-crypto-screener`, 2 `at-batch1-crypto-insight` (이동된 시각 09:35 / 09:50 이 data-plane-crypto 와 안 겹치는지 로그로 확인)
3. 3 `at-batch1-kr-investor-flow`, 4 `at-batch1-kr-fundamentals` (18:10 / 18:20)
4. 5 `at-batch1-us-valuation`
5. 9 `at-batch1-market-events-daily` (interval anchor 확인 후; 위 주의 참고)

**2차 — 수정 PR 이 머지·배포된 뒤에만.** 그 전에는 DAG 를 복사하지 않고 Prefect 도 pause 하지 않는다
(Prefect 쪽이 지금 실패 중이므로 그대로 두어야 실패가 계속 보인다. 복사하면 Dagu 가 매일 `/fail` 만 보낸다).

- 8 `at-batch1-us-fundamentals` → **hk 3846**(US 펀더멘털 `payout_ratio NUMERIC(10,6)` 초과) 수정이 머지·배포된 뒤. 첫 수동 실행의 `COMMIT BLOCKED` 가 없고 전체가 적재되는 것을 확인한다.
- 10 `at-batch1-dart-0700` → **hk 3845**(DART 07:00, 진행 중인 당일 0건을 실패로 처리) 수정이 머지·배포된 뒤.
- 11 `at-batch1-dart-1830` → hk 3845 와 같은 수집기 경로이므로 10 이 정상으로 확인된 뒤.

## 알려진 차이 (Prefect 래퍼 대비)

- **알림**: Prefect 래퍼의 Discord 알림(`notify_market_events_status` — 실패 중복 억제·1시간 throttle·복구 메시지,
  Toss symbol master 실패 embed)은 이 DAG 들에 없다. 대신 healthchecks 핑(성공은 URL, 실패는 URL/`fail`)이고,
  **실행이 아예 안 온 경우도 healthchecks 의 기대 주기로 잡힌다**(Prefect 때는 없던 신호). Discord 알림이 필요하면 별도 후속이다.
  Prefect Error Watchdog 는 Prefect 만 본다 — 전환한 잡의 실패는 더는 거기에 보이지 않는다.
- **market events 행 수 검증**: Prefect 래퍼는 실행 전후 `market_events` 행 수를 세어 `inserted`/`updated` 를 검증하고
  `failed==0` 요약을 요구했다. DAG 는 스크립트 종료 코드(실패 파티션이 있으면 2)만 본다. 행 수 검증은 옮기지 않았다.
- **동시 실행 잠금**: Prefect 로컬 모드의 `mkdir` 잠금 대신 `max_active_runs: 1`.
- **재시도·시간 제한**: Prefect `retries=1` + 지연(60~300초), 스크립트 timeout(700~10800초)을 step 의 `retry_policy`/`timeout_sec` 로 그대로 옮겼다.
  timeout 으로 `docker` 클라이언트가 죽어도 컨테이너가 남을 수 있다 — Prefect 때와 같다.
- **`partially_succeeded` 함정**: Dagu 에서 `continue_on: failure` 를 쓰면 step 이 실패해도 DAG 가 `partially_succeeded` 로 끝나
  *성공* 핸들러가 돈다(로컬 Dagu 2.21.0 에서 확인). 그래서 9번의 두 step 은 서로 독립(병렬)이다 — Prefect 는 순차였지만
  한쪽 실패가 다른 쪽을 막지 않는 점은 같다.
- **commit 게이트 표현**: 명령 안의 `$(case … echo --commit;; esac)` 는 `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` 가
  `1/true/yes/on`(대소문자 무시)일 때만 `--commit` 을 낸다. 테스트가 이 문자열과 "리터럴 `--commit` 은 게이트 없는 잡에서만"을 고정한다.
