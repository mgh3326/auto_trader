# 최소 운용 shell 계약 (T1)

상태: 계약 정의 문서. 구현·계좌 배정·브로커 호출을 의미하지 않는다.

작성 범위: 첫 paper run에 필요한 mutation-critical envelope와 최소 운용 shell.
69개 TBD를 모두 닫거나 공통 플랫폼을 새로 만드는 작업은 첫 paper run 이후로
연기한다. 수치가 정해지지 않은 값은 `TBD-OPERATOR` 또는 `UNKNOWN`으로
남기며 추정하지 않는다.

정본 대조:

- `answer-codexmock-paper-readiness-1526.md` R-3-3 및 R-1의 12개 material gap 표
- `orchmock-decisions-execution-envelope-20260803.md`
- `Documents/*/execution-envelope-*.md`
- 리포지토리 `CLAUDE.md`

## 현재 상태와 적용 경계

- 1계좌=1전략은 운영 규약이다. 현재 모든 실행 표면을 가로지르는 기술적
  singleton/writer 강제는 **없음 — 필요**.
- 오늘 실측한 기존 writer 후보가 있다. production의 `.env.prod.native:199`에
  `WATCH_AUTO_EXECUTE_MOCK_ENABLED=true`가 켜져 있고, watch 알림에 `max_action`이
  있으면 `kis_mock`에 자동 주문 intent를 쓴다. 따라서 이 watch 경로는 새 shell
  writer가 아니라도 **동일 계좌 scope에서 선점·등록해야 하는 기존 mutation writer
  후보**다. 현재 이 경로를 singleton lock에 편입했다는 증거는 **없음 — 필요**.
- 오늘 account canonicalization packet의 fresh read 결론은 후보 어느 곳에서도
  `writer=1`이 운영 강제된다는 증거가 없다는 것이다. 또한 Alpaca crypto paper는
  US equity와 **같은 physical account**를 공유하므로 crypto writer를 US writer와
  분리할 수 없다. 따라서 lock scope는 strategy나 asset class가 아니라
  `physical-account fingerprint/account_record_id`여야 한다.
- 현재 KIS reconcile은 구현이 있어도 `no schedule → paused`로 확인되었고,
  Binance loop는 foreground CLI이며 US runner는 확인되지 않았다. 아래 cadence는
  **계약**이지 현재 활성화된 scheduler가 아니다.
- 계좌의 cash/position/open-order 상태는 이 문서 작성에서 조회하지 않는다.
  계좌 clean 여부는 **UNKNOWN**이다.
- 이 문서는 broker별 얇은 adapter가 지켜야 할 공통 경계만 정한다. 기존
  default-disabled, `confirm=True`, host allowlist, fill-evidence-first 및 하드
  인바리언트를 완화하지 않는다.

## ENVELOPE_MUTATION_CRITICAL_FIELDS

아래는 값이 틀리면 잘못된 주문이 나가거나 기존 포지션을 잘못 바꿀 수 있는
필드만이다. 선택된 lane의 값이 봉인되지 않으면 mutation을 거부한다. 필드의
구체 수치·브로커별 측정값은 아직 일부 **TBD-OPERATOR/UNKNOWN**이다.

| 필드 | 계약 | 틀리면 무슨 일이 생기는가 |
|---|---|---|
| `account_record_id` | 주문 대상 물리 계좌의 정본 ID. 계좌 fingerprint와 함께 operator 승인 | 다른 계좌에 주문하거나 공유 계좌에 귀속을 잃는다 |
| `strategy_id`, `strategy_version`, `strategy_hash` | 해당 계좌의 유일한 전략 identity와 실행 산출물 hash | 다른 전략의 신호가 주문으로 변환되거나 재현성이 사라진다 |
| `symbol` | DB는 `.` 형식. 외부 형식 변환은 `app/core/symbol.py`만 사용 | 다른 종목으로 주문하거나 심볼 해석이 브로커마다 달라진다 |
| `side` 및 `intent` | `buy/sell`와 `entry/exit/reduce/cancel` 의미를 분리 | 매수·매도 방향 또는 신규 진입·청산 의미가 뒤집힌다 |
| `quantity` 또는 `notional` | 둘 중 정확히 하나만 사용. lot/step/minimum 적용 후 실현값도 재검증 | 의도보다 큰 수량, 최소주문 위반, cap 초과 주문이 된다 |
| `order_type`, `limit_price` | 시장가/지정가와 가격을 명시. 지정가면 가격 필수 | 예상하지 않은 가격으로 체결되거나 보호 가격 없이 주문된다 |
| `max_order_notional`, `max_position`, `max_gross_exposure` | 계좌·전략별 상한. 값 미봉인 시 주문 금지 | 한 건·한 종목·전체 익스포저가 위험 한도를 넘는다 |
| `daily_loss_limit`, `buying_power_buffer` | 일중 손실 중단선과 주문 가능 현금 여유 | 손실 중단 후 재진입하거나 현금 부족 주문을 낸다 |
| `session`, `bar_close`, `data_stale_cutoff` | 시장 세션, 확정 봉 시각, stale 판정 | 장외·미확정·오래된 신호로 주문한다 |
| `time_in_force`, `expiry` | day/GTC 등과 실제 만료 시각을 lane별로 봉인 | 의도보다 오래 남은 미체결이 뒤늦게 체결된다 |
| `exit_policy`, `reduce_only` | stop/target/time-exit의 소유자와 청산 여부; 청산은 신규 노출을 만들지 않음 | 청산 신호가 새 포지션을 열거나 잔여 노출을 방치한다 |
| `fee`, `spread`, `slippage`, `fx` guard | 주문 가능 여부·사이징에 영향을 주는 보수 비용 가정 | 비용을 무시해 cap·손익·최소 notional 판정이 틀린다 |
| `kill_state`, `operator_confirm`, `execution_surface` | kill이면 fail-closed; demo/mock/live 표면과 per-call 확인을 일치 | 정지 상태에서 재주문하거나 잘못된 브로커 표면으로 전송한다 |

공통적으로 submit 응답의 symbol/side/quantity/order type/`reduce_only` echo가
요청과 다르면 성공으로 취급하지 않는다. `filled`는 브로커 fill evidence 없이는
기록하지 않는다. 기존 하드 인바리언트(예: 1x, notional cap, 동시 포지션 상한)는
이 문서에서 값을 바꾸지 않는다.

## SINGLETON_MECHANISM

### 제안

모든 mutation adapter가 `account_record_id`로 동일한 PostgreSQL advisory lock을
획득한다. lock은 프로세스 수명 동안 유지하고, `pg_try_advisory_lock` 실패 시
주문하지 않고 `SINGLETON_BLOCKED`를 기록한다. 보조적으로 owner, process start,
heartbeat, lease expiry, strategy identity를 저장해 운영자가 누가 lock을 잡았는지
확인한다. DB 연결이 끊기거나 프로세스가 죽으면 advisory lock이 풀린다.

lock 획득 이후에만 broker preflight와 mutation을 수행하고, lock을 획득하지 않은
경로는 broker client를 호출할 수 없다. lease/heartbeat가 stale이면 자동으로
계속하지 않고 fail-closed한다. 모든 broker 표면이 같은 lock을 사용해야 하며,
표면별 별도 lock은 singleton으로 인정하지 않는다.

### 현재 상태

공통 `account_record_id` lock, fencing token, writer registry, 그리고 모든
mutation entrypoint에 대한 강제는 **없음 — 필요**. 따라서 현재는
`1계좌=1전략` 운영 규약만으로 writer cardinality를 보장한다. 이 문서만으로
paper activation을 허용하지 않는다.

특히 `WATCH_AUTO_EXECUTE_MOCK_ENABLED=true`인 production watch 경로는
`max_action`을 받아 `kis_mock` 주문 intent를 만들 수 있으므로 기존 writer 후보로
취급해야 한다. account-packet 실측상 이 후보를 포함해 어느 후보에서도 writer=1
운영 강제 증거가 없었다. Alpaca crypto와 US equity가 같은 physical account를
공유하는 사실 때문에 두 asset-class별 lock을 따로 두는 것은 singleton 증명이
아니다.

## RESTART_SEMANTICS

재기동은 “새로운 신호를 처음부터 다시 주문”하는 동작이 아니다.

1. 시작 즉시 mutation을 막고 singleton을 획득한다. 이어 account-wide open
   orders, positions, broker order/fill status를 조회해 broker truth를 만든다.
   ledger만으로 clean/filled를 판정하지 않는다.
2. 마지막 checkpoint는 `(account_record_id, strategy_id/version/hash,
   symbol, bar_close, decision_id, envelope_hash)`를 포함한다. `order/no_order`
   양쪽 결정을 저장하고, lifecycle이 reconcile되기 전에는 해당 decision을
   완료 checkpoint로 전진시키지 않는다.
3. 같은 `decision_id`/envelope hash의 결정은 재생할 수 있지만 submit은 한 번만
   허용한다. 이미 제출됐거나 제출 결과가 불확실하면 order ID·client ID·broker
   조회로 확인하고, 확인 전 재제출하지 않는다.
4. 미체결 주문이 남아 있으면 자동으로 대체 주문을 내지 않는다. 기존 주문을
   `OPEN/PENDING_RECONCILE`로 유지하고, 정책상 취소가 봉인되어 있고 operator
   확인이 있을 때만 cancel한다. 알 수 없는 주문·position mismatch는
   `ANOMALY`로 남기고 신규 mutation을 정지한다.
5. broker position과 open order가 정합하고 checkpoint가 확인된 뒤에만 새
   bar/decision을 평가한다. stale checkpoint, missing evidence, lock 실패,
   broker 조회 실패는 모두 fail-closed다.

현재 persisted decision checkpoint와 이 restart/replay 규칙의 공통 구현은
**없음 — 필요**. 특히 미체결·uncertain submit 복구는 현재 운용 증거가 없다.

## RECONCILE_CADENCE

이 cadence는 운영 계약이며 신규 TaskIQ/cron/Prefect 등록을 승인하지 않는다.
scheduleless CLI/감독 프로세스가 아래 시점을 호출할 수 있도록 adapter 계약으로
둔다.

- **startup/restart 전**: account-wide positions, open orders, recent fills,
  ledger를 대조. clean하지 않으면 mutation 금지.
- **각 mutation 직전**: lock, kill state, exposure, buying power, open orders를
  다시 확인한다.
- **submit 직후**: bounded order-status/fill poll. 시간 내 evidence가 없으면
  retry가 아니라 `PENDING_RECONCILE`/`ANOMALY`다.
- **정상 운용 중**: 최소 5분마다, 그리고 매 decision cycle 종료 시 account-wide
  positions/open orders/fills와 ledger를 대조한다. 정확한 주기는 lane operator가
  승인하기 전까지 `TBD-OPERATOR`로 본다.
- **cancel/kill 직후**: 취소 응답만 믿지 않고 open orders가 비었는지 확인한다.
- **장 마감 또는 UTC day 경계**: 미체결·포지션·손익·daily-loss baseline을
  대조하고 summary를 낸다. 24/7 crypto는 UTC 경계를 사용한다.

불일치 시 신규 주문을 중지하고 `ANOMALY` Discord를 보낸다. 현재 이 cadence를
자동으로 실행하는 공통 runner/supervisor는 **없음 — 필요**이며, KIS periodic
reconcile은 정본 상태상 paused다.

## DISCORD_SPEC

모든 메시지는 `event`, `severity`, `occurred_at`(timezone 포함), `market`,
`account_record_id`(비밀값 아님), `strategy_id/version`, `correlation_id`,
`decision_id`/`order_id`(있을 때), `state`, `action_required`를 포함한다. API key,
secret, token, webhook URL, 원시 인증 header는 절대 포함하지 않는다.

필수 event와 시점:

| event | 언제 | 최소 내용 |
|---|---|---|
| `STARTUP` | process 시작·재기동 | release/hash, checkpoint, singleton, broker truth 판정 |
| `HEARTBEAT` | cadence마다 | last decision, last reconcile, lock owner, stale 여부 |
| `NO_SIGNAL` | decision cycle에서 주문 없음 | bar_close, reason, checkpoint |
| `ORDER_INTENT` | mutation 직전 | symbol/side/qty 또는 notional, envelope hash, confirm 상태 |
| `SUBMITTED` / `FILL` | broker evidence 발생 시 | broker order ID, status, filled qty/price, evidence 시각 |
| `RECONCILE` | 대조 완료 | open orders/positions/fills 요약, match 여부 |
| `FAILURE` | 예외·broker/network/validation 실패 | fail-closed 상태, 원인 분류, 재시도 금지 여부 |
| `ANOMALY` | mismatch, uncertain submit, stale data, duplicate writer | 신규 주문 중지 여부와 operator 조치 |
| `STOPPED` | kill, shutdown, supervisor exit | 마지막 상태, 남은 주문/포지션, cancel 필요 여부 |
| `CANCEL` | 취소 요청·결과 | 요청자, order ID, broker evidence, 잔존 여부 |
| `DAILY_SUMMARY` | day/session 경계 | decision/submit/fill/reconcile counts, anomalies, PnL eligibility |

실패·정지·이상은 성공 메시지에 묻지 않고 반드시 별도 event로 보낸다. 현재
이 shell event 집합을 대상 paper runner에 일관되게 연결한 근거는 **없음 —
필요**. 기존 trade notifier/Discord formatter의 존재만으로 이 계약이 충족된
것으로 보지 않는다.

## MANUAL_KILL_PROCEDURE

사고 시 operator가 사용할 계약상 한 줄 명령은 다음이다.

```bash
paper-shell kill --account-record-id <ACCOUNT_RECORD_ID> --cancel-open-orders --confirm
```

명령은 kill state를 먼저 영구 기록하고 신규 mutation을 차단한 뒤, 해당 계좌의
미체결 주문을 cancel하고 broker truth로 취소 여부를 reconcile해야 한다. 취소
증거가 없거나 position이 남으면 `STOPPED`/`ANOMALY`를 보내고 사람의 추가
flatten 판단을 기다린다. 이 명령과 kill state/cancel/reconcile의 end-to-end
구현은 **없음 — 필요**이며, 위 명령은 현재 실행 가능한 명령이라고 주장하지
않는다. 구현 전에는 paper run acceptance를 통과로 부를 수 없다.

## ACCEPTANCE_CHECKLIST

첫 paper run을 “통과”라고 부르려면 선택된 한 계좌·한 전략에 대해 모두 증명해야
한다. 체크리스트는 계약이며 아직 실행 결과가 아니다.

- [ ] `account_record_id`, physical account fingerprint, strategy ownership,
  `strategy_id/version/hash`가 operator 승인 문서에 고정됨.
- [ ] clean startup snapshot이 broker read-only evidence로 존재함:
  cash, positions, open orders, starting NAV, 기존 잔고 disposition.
- [ ] mutation-critical envelope의 값과 envelope hash가 봉인됨. 남은 TBD는
  주문에 영향을 주지 않는다고 분류됨.
- [ ] 동일 계좌에 writer가 하나뿐임을 기술적으로 증명하고, 두 번째 writer가
  fail-closed 되는 것을 확인함.
- [ ] no-order shadow에서 신호·무신호 decision과 checkpoint가 남음.
- [ ] 합성 신호 1건에서 order intent → pre-submit gate → 최소 mock order까지
  귀속이 끊기지 않음. 주문 귀속 100%.
- [ ] partial fill, timeout/uncertain submit, cancel/expire를 재제출 없이
  evidence-first로 처리함. fill evidence 100%.
- [ ] 미체결 주문을 남긴 채 재기동해 duplicate submit이 없고, broker truth와
  checkpoint가 복구됨.
- [ ] startup/pre-submit/post-submit/periodic/cancel 후 reconcile이 실행되고,
  mismatch가 신규 mutation을 중지함.
- [ ] 한 줄 manual kill/cancel rehearsal에서 신규 주문이 멈추고 cancel 결과와
  잔존 position이 Discord에 기록됨.
- [ ] `STARTUP`, `HEARTBEAT`, `NO_SIGNAL`, order/fill, `RECONCILE`, `FAILURE`,
  `STOPPED`, `ANOMALY`, `DAILY_SUMMARY`가 secret 없이 수신됨.
- [ ] uptime, decision count, submit/fill/reconcile count, anomaly, PnL
  eligibility와 중단/승격 판정을 보존함.

현재 위 항목을 충족했다는 paper-run evidence는 **없음 — 필요**이다.

## GAPS_CLOSED / GAPS_LEFT_OPEN

아래에서 “닫음”은 이 문서가 계약 정의를 제공한다는 뜻이고, “운영 gap 닫힘”은
아니다. 이번 문서만으로 운영 gap을 완전히 닫은 항목은 **0개**다.

| # | R-1 material gap | 이 shell 계약의 판정 |
|---|---|---|
| 1 | 물리 계좌 identity·전용 전략·writer cardinality | **부분 정의 / 운영 미해소** — identity와 writer=1 조건은 명시했지만 계좌맵·fingerprint·기술 강제는 없음 |
| 2 | runtime 소유권 authorization | **열림** — strategy/account allow predicate 구현·실증 없음 |
| 3 | 실행 envelope | **부분 정의** — mutation-critical subset을 추렸지만 수치·operator 봉인·runtime guard 없음 |
| 4 | signal→order adapter·runner | **열림** — 구현 범위 밖, 현재 flagship runner 없음 |
| 5 | 청산·포지션 lifecycle | **부분 정의** — entry/partial/cancel/exit/reconcile 의미만 정의, 구현·실증 없음 |
| 6 | 기동·cadence·singleton·restart | **부분 정의** — 제안된 lock·restart·cadence는 있으나 공통 구현과 supervisor 없음 |
| 7 | reconcile 활성화·범위 | **부분 정의** — account-wide 범위와 시점은 정의, 실제 periodic activation은 없음 |
| 8 | clean baseline·자본/reset | **열림** — read-only snapshot과 disposition 요구만 정의, 실제 snapshot 없음 |
| 9 | 위험·비용·주문 정책 | **부분 정의** — cap/loss/cost/order 필드 요구, 계좌별 수치와 측정값은 TBD/UNKNOWN |
| 10 | decision-time 데이터·세션 | **부분 정의** — session/bar-finality/stale/TIF 요구, 시장별 운영값·calendar guard 없음 |
| 11 | monitoring·Discord·수동 개입 | **부분 정의** — 필수 event와 한 줄 명령을 정의, 대상 runner 연결·kill/cancel 구현 없음 |
| 12 | 운용 acceptance·성과 보존 | **부분 정의** — 첫 run checklist와 보존 지표를 정의, rehearsal/evidence는 없음 |

### GAPS_CLOSED

계약 수준에서 새로 정의한 것은 다음이다: mutation-critical 필드 경계,
singleton 제안, restart의 중복 방지 규칙, reconcile 최소 시점·account-wide
범위, Discord 필수 event, manual kill/cancel 명령 형태, 첫 paper acceptance
checklist. 이것은 구현 완료나 운영 승인으로 승격하지 않는다.

### GAPS_LEFT_OPEN

12개 gap 모두에 운영상 잔여가 있다. 특히 #1/#2/#6/#7/#11의 기술 강제·실행,
#8의 clean account truth, #12의 실제 rehearsal/evidence는 첫 paper run 전
필수다. #3/#9/#10의 lane별 수치와 broker 이전 측정도 생존자·계좌 확정 뒤
operator가 봉인해야 한다.

## SCOPE_CREEP / 변경 기록

```text
SCOPE_CREEP = NO
CODE_CHANGED = NO
BROKER_API_CALLED = NO
ACCOUNT_QUERIED = NO
PUBLIC_WRITE = NO
PR = 생성 예정 (문서 PR; merge 금지)
```
