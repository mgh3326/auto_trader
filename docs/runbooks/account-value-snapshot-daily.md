# 계좌 가치 일별 스냅샷 DAG 런북 (hk 3872)

계좌 가치 스냅샷(`review.account_value_snapshots`)을 매일 07:40 KST 에 한 번 남기는 Dagu DAG 의
등록·확인·롤백 절차다. **이 파일과 DAG 는 제안일 뿐이고 아무것도 등록되어 있지 않다.** DAG 파일은
`deploy/dagu/at-daily/at-account-value-snapshot.yaml` 에 제안으로만 있고, 등록(복사)·첫 실행 확인은
이 런북을 운영자가 따라 할 때만 일어난다. 승인 근거: 운영자 결정
`hk:doc decision/2026-11-11/factortrail-p1` §5 (retro/v1 `retro_scoreboard` 와 이후 holdings
reconcile 이 이 스냅샷을 읽는다). 등록은 머지 후 운영자 단계이며, 배치 1 과 같은 dotenv 파일
(`/root/at-secrets/.env.dagu-at-batch`) 준비에 막혀 있다.

🔴 `deploy/dagu/**` 는 mergewake `deploy_approval` 대상이라 머지 후 롤아웃은 사람 승인 건으로 남는다.
이 런북의 어떤 단계도 그 승인을 대신하지 않는다.

## 대응표

시각은 KST ↔ UTC. OCI Dagu 스케줄러가 UTC 로 돌기 때문에 DAG 의 `schedule` 은 UTC 다.
선행 스케줄러는 없다 — 이 스냅샷은 지금까지 손으로만 돌았다(첫 `--commit` 2026-11-11 06:23 KST, 8행).

| DAG 파일 (`deploy/dagu/at-daily/`) | Dagu (UTC) | Dagu (KST) | 계좌 step | 핑 env 이름 |
|---|---|---|---|---|
| `at-account-value-snapshot` | `40 22 * * *` | 07:40 매일 | `kis` · `toss` · `upbit` (병렬) | `AT_BATCH_PING_URL_ACCOUNT_VALUE_SNAPSHOT` |

- 세 step 은 서로 `depends` 가 없다. Dagu 는 의존이 없는 step 을 병렬로 돌리므로 한 계좌가 실패해도
  나머지 두 계좌는 계속 돈다.
- 실패한 계좌가 하나라도 있으면 DAG 는 `failed` 로 끝나고 실패 핸들러가 `<URL>/fail` 을 보낸다.
  `continue_on: failure` 를 쓰지 않은 이유가 이것이다 — Dagu 2.21.0 에서 `continue_on` 은 DAG 를
  `partially_succeeded` 로 끝내 *성공* 핸들러를 부르므로, 실패가 핑으로 보이지 않는다.
- 계좌별 기대 행: `kis` 3행(kr·us·account), `toss` 3행(kr·us·account), `upbit` 2행(crypto·account) —
  합계 8행.

## 사전 조건 (이름만 적는다. 값은 이 레포에 없다)

NCP Dagu 워커(`worker_selector: host: ncp`)에서:

- **도구**: `docker`(root), `curl`. 각 step 이 `docker run` 으로 auto_trader 이미지를 띄운다.
- **이미지 포인터**: `/root/at-run/deployed-digest` (deploy 스크립트가 쓰는 전체 이미지 참조).
- **컨테이너 env 파일**: `/root/at-secrets/.env.api` — 배치 1 과 같은 파일이다. 계좌별 브로커
  자격증명(KIS·Toss·Upbit)이 그 안에 있어야 한다. 하나라도 없으면 그 계좌 step 만 `실패: <class>` 로
  끝나고 DAG 는 `failed` 다.
- **DAG 용 dotenv**: `/root/at-secrets/.env.dagu-at-batch` (권한 `0600`, 배치 1 과 공유). 이름만:

  | 이름 | 용도 |
  |---|---|
  | `AT_BATCH_PING_URL_ACCOUNT_VALUE_SNAPSHOT` | healthchecks https 핑 URL(성공은 그대로, 실패는 `/fail`) |

  이 이름이 dotenv 에 없으면 DAG 는 돌지만 핑이 실패한다(`curl` 이 URL 없이 실패). 🔴 Dagu 는 없는
  dotenv 파일을 오류 없이 넘기므로 첫 실행 로그에서 핸들러가 핑을 보냈는지 반드시 확인한다.
- **Dagu 버전**: 로컬 Dagu 2.21.0 에서 `dagu validate` 통과. OCI 버전은 **확인 못 함(UNVERIFIED)**.
- **쓰기 경로**: 이 DAG 는 DB 에 쓴다(`--commit`). 계좌 가치 스냅샷은 append-only 사실 행이고
  하루 한 번의 관측이다. 조회는 `scripts/account_value_report.py`, retro 는 `retro_scoreboard` 다.

## 등록 절차

1. **시각 고르기**: Dagu 첫 스케줄 시각(07:40 KST)이 2시간 이상 남은 때 한다. DAG 를 복사하는 순간
   Dagu 스케줄이 살아난다(쓰기는 멱등이지만 브로커 읽기 호출이 한 번 더 나간다).
2. **복사**: `deploy/dagu/at-daily/at-account-value-snapshot.yaml` 을 OCI coordinator 의 Dagu DAGs
   디렉터리(`~/.config/dagu/dags`)에 복사한다. 복사 직후 `dagu validate <파일>` 로 문법을 확인한다.
3. **수동 1회 실행**: `dagu start at-account-value-snapshot`(= 파일명에서 `.yaml` 제외). Dagu 로그에서
   확인할 것:
   - 세 step 이 모두 `docker run` 으로 끝났는가(종료 코드 0).
   - CLI 출력이 `{"mode": "commit", "snapshots": [...]}` 이고, 그날 계좌 8행이
     `"write_status": "inserted"` 이거나(첫 실행) 이미 그날 행이 있으면 `"already_present"` 인가.
     두 경우 모두 정상이다 — `already_present` 는 같은 날 재실행이 no-op 이라는 뜻이다.
   - 성공 핸들러가 핑을 보냈는가(`onSuccess`). 실패가 있으면 `onFailure` 가 `<URL>/fail` 을 보낸다.
4. **그날 행 확인**: 같은 날 두 번째 실행은 `already_present` 만 나와야 한다(중복 행 0). 새 행이
   생기면 `(account, market, day)` 키가 깨진 것이므로 스케줄을 켜지 말고 멈춘다.
5. **스케줄 유지**: 3·4 가 맞으면 그대로 둔다. 맞지 않으면 §롤백으로 되돌린다.

## 롤백

1. 돌고 있는 실행이 있으면 먼저 `dagu stop at-account-value-snapshot`.
2. Dagu DAGs 디렉터리에서 그 DAG 파일을 지운다. 스케줄이 사라진다.
3. healthchecks 의 해당 체크를 일시 중지한다(안 하면 "안 옴" 알림이 온다).

**행은 남긴다.** 스냅샷은 append-only 사실 행이고 지우거나 고치지 않는다. 롤백은 스케줄만 끊는다.
선행 스케줄러가 없으므로 되살릴 스케줄은 없다(배치 1 처럼 Prefect `schedule resume` 할 대상이 없다).

## 알려진 격차

- **흐름(flows) 없음**: DAG 는 `--commit` 만 준다. `--flow`/`--flows-complete` 는 운영자 수동 기록이고
  자동화하지 않았다. 그래서 기간 수익률은 계속 미측정(`flows_not_confirmed`)이다 — 스냅샷 행이 쌓여도
  `--flows-complete` 는 false 다. 외부 입출금이 있는 날은 운영자가 `--flow-id` 로 직접 남긴다.
- **KR 가격은 전일 종가**: 07:40 KST 는 KRX 개장 전이라 kr 행의 평가 가격은 직전 거래일 종가다.
  같은 날 15:30 종가가 아니다. US 행도 마지막 마감 세션 기준이다.
- **미측정 사유가 붙을 수 있다**: 첫 미리보기에서 account 행에 `cash_unknown`, 일부 종목에
  `mark_unknown` 이 관측됐다(`hk:doc decision/2026-11-11/factortrail-p1` §T5). 사유가 붙은 행도
  저장된다 — 미측정은 값이 아니라 이유로 남는다.
- **알림**: healthchecks 핑(성공은 URL, 실패는 URL/`fail`)뿐이다. 실행이 아예 안 온 경우도
  healthchecks 기대 주기로 잡힌다. Discord 알림은 없다.
- **한 계좌 실패 = 실행 실패**: 나머지 계좌는 계속 돌지만 DAG 는 `failed` 로 끝나 `/fail` 핑을 보낸다.
  어느 계좌가 실패했는지는 step 이름으로 로그에서 읽는다.
