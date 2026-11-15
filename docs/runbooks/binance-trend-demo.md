# TREND-TSMOM-v1 데모 러너 — NCP 수동 운영 런북

TREND-TSMOM-v1 Futures Demo 러너(hk 4753, 3796 CT-5a)를 NCP에서 운영자가 손으로
띄우고 관찰·정지하는 절차다. 같은 계정·같은 클라이언트를 쓰는 H5 런북
[binance-h5-ncp-manual-playbook.md](binance-h5-ncp-manual-playbook.md)의 형태를 따르며,
러너의 설계는 `docs/design/3796-crypto-trend-demo.md` §4가 정본이다.

**이 문서는 어떤 권한도 주지 않는다.** 병합은 러너를 시작하지 않는다. 별도 운영자
계약과 시작 승인이 있어야 하고, 시작 전에 24시간 정지 감시자와 장애 알림 담당자를
지명해야 한다(§5). live Binance 호스트·Spot Demo·testnet·다른 계정은 대상이 아니다 —
러너는 `https://demo-fapi.binance.com`만 쓴다.

## 1. 절대 규칙

1. **데모 플래그는 일회성 컨테이너에만 둔다.** `BINANCE_TREND_DEMO_ENABLED`,
   `BINANCE_FUTURES_DEMO_ENABLED`, `BINANCE_TREND_ALERT_ENABLED`를 해당
   `docker run`의 `-e NAME=true`로만 넘긴다(§3–§5). `docker run`의 `-e`가
   `--env-file`보다 나중에 적용되므로 파일 값은 지고, 러너는 정확히 `true`가
   아니면 시작을 거부한다.
2. **`/root/at-secrets/.env.api` 등 어떤 env 파일도 편집해 켜지 마라.** 공유 유닛
   (`at-api`, `at-worker`, MCP 유닛)이 같은 파일을 읽고 플래그는 off로 보여야 한다.
   §3에서 파일 해시를 기록하고 §8에서 비교한다.
3. **live 브로커 키는 필요 없다.** 컨테이너는 공유 env 파일에서 `DATABASE_URL`,
   Futures Demo 키 쌍, `DISCORD_WEBHOOK_ALERTS`만 읽는다. 다른 `--env-file`을
   추가하지 말고 명령줄에 비밀 값을 넣지 마라.
4. **supervisor·scheduler·자동 재시작 금지.** `--restart` 없음, `-d` 없음, cron
   항목 없음, systemd 유닛 없음, TaskIQ·Prefect 등록 없음. 러너는 사람이 시작한
   foreground tmux 창에서 산다.
5. **정지 순서는 watcher 먼저, runner 나중**(§7)이고 러너는 Ctrl-C로 멈춘다.
   `docker stop`은 SIGTERM이라 설계상 `stopped` 알림을 낸다.

## 2. 변수

명령 전에 새 셸·tmux 창마다 붙여 넣는다.

```bash
image="$(cat /root/at-run/deployed-digest)"
env_file=/root/at-secrets/.env.api
```

`deployed-digest`는 `scripts/deploy-ncp-pull.sh`가 마지막으로 승격한 이미지다
([ncp-pull-deploy.md](ncp-pull-deploy.md)). 배포 스크립트는 자기 유닛만 관리하므로
`at-trend-demo`·`at-trend-watch`가 떠 있으면 배포가 그것을 멈추거나 교체하지 않는다 —
러너는 자기가 시작한 이미지로 계속 돈다.

## 3. Preflight

다섯 검사가 모두 통과해야 한다. 하나라도 실패하면 멈추고 보고한다.

1. 배포 이미지에 세 스크립트가 있다(없으면 이 변경이 아직 배포되지 않은 것이다).

```bash
docker run --rm --network host --env-file "$env_file" "$image" /app/.venv/bin/python -m scripts.binance_trend_demo --help
docker run --rm --network host --env-file "$env_file" "$image" /app/.venv/bin/python -m scripts.binance_trend_truth_gate --help
docker run --rm --network host --env-file "$env_file" "$image" /app/.venv/bin/python -m scripts.binance_trend_heartbeat_watch --help
```

2. 공유 env 파일이 레인을 켜지 않았다. `=false` 줄만 있거나 아무 출력도 없어야
   한다. `=true`가 보이면 누군가 전역으로 켠 것이므로 멈춘다.

```bash
grep -E '^(BINANCE_TREND_DEMO_ENABLED|BINANCE_FUTURES_DEMO_ENABLED|BINANCE_TREND_ALERT_ENABLED)=' "$env_file"
```

3. env 파일 해시를 기록한다(§8에서 비교). 해시는 비밀이 아니다.

```bash
sha256sum "$env_file"
```

4. 남은 러너·watcher 컨테이너가 없다(출력이 없어야 한다).

```bash
docker ps -a --filter name=at-trend --format '{{.Names}} {{.Status}}'
```

5. **알림 경로 테스트.** ops Discord 채널로 1회 테스트 메시지가 간다. 아래 줄과
   exit 0, `TREND 알림 채널 테스트 (장애 아님)` 제목의 메시지를 확인한다.
   `"delivered": false`면 웹훅이 없거나 고장 난 것이다 — 알림 경로 없이는 §5를
   시작하지 않는다.

```bash
docker run --rm --network host --env-file "$env_file" -e BINANCE_TREND_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_trend_heartbeat_watch --send-test
```

```text
{"delivered": true, "event": "alert_test"}
```

## 4. 계정 truth gate (읽기 전용, 시작 전 PASS 필수)

Futures Demo 계정을 서명 GET으로, trend 상태 테이블을 SELECT로만 읽고 JSON 한 줄을
출력한다. 주문 전송·레버리지·마진 변경·행 쓰기가 없다. 러너처럼 두 데모 플래그와
`--confirm-demo`가 필요하다.

```bash
docker run --rm --network host --env-file "$env_file" -e BINANCE_TREND_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_trend_truth_gate --confirm-demo
```

exit 0과 `"verdict": "PASS"`, 네 개 검사의 통과를 기대한다. exit 2는
`"verdict": "FAIL"`이며 시작하지 않는다. 실패 검사는 `detail`에 원인을 적는다.
어느 쪽이든 그 줄 전체를 시작 기록에 붙인다.

계정을 읽기 전에 봉인된 envelope만 확인하려면 readiness 모드를 쓴다. HTTP·DB·
자격증명을 쓰지 않고 launch gate·상수·룩백·손절 비율만 출력한다.

```bash
docker run --rm --network host --env-file "$env_file" -e BINANCE_TREND_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_trend_demo --readiness --confirm-demo
```

| check | 통과 조건 | FAIL 시 |
| --- | --- | --- |
| `demo_host_identity` | 정확히 `https://demo-fapi.binance.com` | 클라이언트 origin 을 고친다. 이 레인은 호스트를 바꾸지 않는다 |
| `one_way_position_mode` | one-way 모드 | 데모 UI에서 one-way로 바꾼다 |
| `account_isolated_1x` | BTCUSDT/ETHUSDT/SOLUSDT/XRPUSDT 전부 isolated 1x BOTH readback | 데모 UI에서 마진·레버리지를 맞춘다. 이 레인은 설정을 바꾸지 않는다 |
| `account_flat_vs_trend_state` | 미해소 intent 0, `holding` 외 활성 신호 0, 계정 비영 위치가 전부 이 레인의 `holding` 과 수량·방향 일치, foreign 포지션 0 | 다른 전략·수동 포지션이 있으면 귀속이 불가능하다. 소유자를 찾아 데모 UI에서 정리한 뒤 다시 돈다 |

## 5. 시작

시작 승인이 기록에 있어야 한다. 러너 먼저, watcher 나중, 각각 별도 tmux 세션.

**러너.** 세션을 열고 §2 변수를 붙여 넣은 뒤 실행한다.

```bash
tmux new-session -s trend-runner
```

```bash
docker run --name at-trend-demo --network host --env-file "$env_file" -e BINANCE_TREND_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true -e BINANCE_TREND_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_trend_demo --loop --confirm-demo
```

Ctrl-b d로 detach한다. 컨테이너 이름이 고정이라 첫 러너가 있는 동안 두 번째 러너는
시작할 수 없고, 러너 자신도 liveness 잠금을 못 잡으면 `another TREND runner owns
the loop lock`으로 거부한다. `--rm`이 없으므로 죽은 러너의 로그는 지우기 전까지
`docker logs`에 남는다.

**watcher.** 러너가 첫 tick 줄(§6)을 출력한 뒤 두 번째 세션에서:

```bash
tmux new-session -s trend-watch
```

```bash
docker run --name at-trend-watch --network host --env-file "$env_file" -e BINANCE_TREND_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_trend_heartbeat_watch --loop --miss-minutes 10 --poll-seconds 60
```

watcher는 러너가 `--loop` 동안 잡고 있는 세션 advisory lock과 그 backend의
`pg_stat_activity.state_change`를 SELECT로만 읽는다. Binance 자격증명이 필요 없다.
**이 watcher가 러너를 한 번이라도 본 뒤** 잠금이 사라지거나 마지막 touch가 10분을
넘으면 1회 알린다. 한 번도 못 본 잠금 부재는 "아직 시작 안 함"과 구분할 수 없어
알리지 않고, DB 읽기 실패는 `absent`가 아니라 `unreadable`로 알린다. H5와 달리 이
레인에는 durable tick stamp가 없으므로 **본 뒤의 잠금 부재가 사망 신호**다 — 그래서
watcher는 러너의 첫 tick 뒤에 시작하고, 정지할 때는 watcher를 먼저 멈춘다. SIGKILL,
OOM kill, 호스트 상실을 잡는 유일한 경로다.

## 6. 시작 검증·T0·correlation id

```bash
docker ps --filter name=at-trend --format '{{.Names}} {{.Status}}'
docker logs --tail 5 at-trend-demo
docker logs --tail 3 at-trend-watch
```

둘 다 `Up`. 러너는 분당 JSON 한 줄을 출력한다. 예:

```text
{"decision_ts": 1790000000000, "events": [{"symbol": "BTCUSDT", "event": "no_signal", "detail": null}]}
```

`events`에 `blocked`, `*_uncertain`, `exposure_mismatch`,
`partial_entry_stop_breached`, `entry_aborted`/`exit_aborted`, 또는
`entry_refused:conflict_week_limit_reached`가 있으면 실패다: §9로 간다.
watcher는 `"verdict": "ok"` 줄을 출력한다.

플래그가 이 컨테이너들에만 있는지 증명한다. 첫 명령은 `=true` 세 줄, 둘째는
`=false` 또는 출력 없음, 해시는 §3과 같아야 한다.

```bash
docker inspect at-trend-demo --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(BINANCE_TREND_DEMO_ENABLED|BINANCE_FUTURES_DEMO_ENABLED|BINANCE_TREND_ALERT_ENABLED)='
docker inspect at-worker --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(BINANCE_TREND_DEMO_ENABLED|BINANCE_FUTURES_DEMO_ENABLED|BINANCE_TREND_ALERT_ENABLED)='
sha256sum "$env_file"
```

**T0.** 계약상 T0는 운영자가 시작한 첫 TREND 러너 프로세스다. 컨테이너 시작 시각과
이미지를 즉시 기록한다. 실패·재시작 뒤에도 T0를 재정의하지 않는다 — T0 변경은
director 결정이다.

```bash
docker inspect -f '{{.State.StartedAt}}' at-trend-demo
docker inspect -f '{{.Config.Image}}' at-trend-demo
```

**Correlation id.** 레인 identity는 `TREND-TSMOM-v1`, prefix는 `binance-trend:`다.
신호별 id는 신호가 생겨야 존재한다. tick 줄의 `events` 에 진입 이벤트가 보이면 그
신호 키는 `symbol|KST분|side|close text` 이고 correlation id는 그 키의 SHA-256 앞
24 hex다.

```bash
printf '%s' "$signal_key" | sha256sum | cut -c1-24
```

지정된 hk 기록에 T0(UTC), 위 이미지 참조, truth gate 줄 전체, 시작 전후 env 파일
해시, 알림 테스트 출력, lane identity·prefix, 이후 각 신호 키와 correlation id를
적는다.

## 7. 관찰과 계획 정지

첫 24시간 동안 시작 전에 지명한 관찰 담당자가 Discord를 보고 코인 세션마다 최소한
다음을 실행한다.

```bash
docker logs --since 1h at-trend-demo
docker logs --tail 3 at-trend-watch
```

**이 레인에는 거래소 측 손절이 없다.** 포지션이 열려 있으면 러너만이 hard stop(진입가
-10%, 1m 극값 감시), 신호 반전, 30일 time exit로 닫는다. 러너가 죽거나 멈추거나
blocked 되면 손절이 관찰되지 않는 상태다.

계획 정지, 이 순서로:

1. `tmux attach -t trend-watch`, Ctrl-C. watcher가 exit 0으로 끝난다.
2. `tmux attach -t trend-runner`, Ctrl-C. 러너는 exit 130이며 알림을 보내지 않는다.
3. 확인·정리:

```bash
docker inspect -f '{{.State.ExitCode}}' at-trend-demo
docker rm at-trend-demo at-trend-watch
tmux kill-session -t trend-watch
tmux kill-session -t trend-runner
```

4. truth gate(§4)를 다시 돈다. 열린 TREND 포지션이 보이면 러너만이 그것을 관리할 수
   있다: §9로 재시작하거나, 레인을 은퇴시키는 경우 §10의 manage-only tick으로 닫는다.
   데모 UI에서 손으로 닫지 마라 — state가 `holding`으로 남아 이후 모든 청산이 브로커
   position 대사에 실패한다. 감시 없이 두지 않는다.

러너를 먼저 멈추면 watcher가 10분 뒤 `heartbeat_missed`를 1회 보낸다. 예상된 동작이며
사고가 아니다.

## 8. 롤백

롤백은 §7의 계획 정지에 검사 하나를 더한 것이다. env 파일·유닛·스케줄을 건드린 것이
없으므로 되돌릴 설정이 없다.

```bash
sha256sum "$env_file"
```

해시가 §3 값과 같아야 한다. TREND 테이블·원장 행은 감사 증거로 남는다. 아무것도
drop·편집하지 않는다. "깨끗하게 시작"하려고 새 T0를 정하거나 TREND state를 지우지
마라 — 그 결정은 director 몫이다.

## 9. Incident response

알림은 kind를 말한다. 셋 다 손절 관찰이 깨졌다는 뜻이다.

| kind | 의미 | 첫 확인 |
| --- | --- | --- |
| `stopped` | 러너가 운영자 의도 없이 끝났다(SIGTERM·예외·취소) | `docker logs --tail 50 at-trend-demo`, §4 truth gate |
| `error` | tick이 `blocked`/불명/부분체결 정지 위반/conflict 주 3건 래치를 기록했다 | 해당 tick 줄의 `events`, §10 manage-only |
| `heartbeat_missed` | 러너 잠금이 없거나 마지막 touch가 오래됐다 | tmux·컨테이너 생존, `docker logs` |

공통: 알림 뒤에는 §4 truth gate를 먼저 돈다. 열린 포지션이 있으면 §7의 정지 순서를
지키고 §10 또는 재시작으로 관리한다. conflict 주 3건 래치는 주 경계로 풀리지 않는다 —
운영자가 `TrendStateService.resolve_conflicts`로 기록을 해소(식별자 필수)해야 풀린다.
상대 전략 배정·계정 분리는 운영자 결정이다.

## 10. manage-only (비상 관리)

신규 진입 없이 미해소 intent 정리·uncertain 차단·보유 포지션 관리(hard stop/신호
반전/time exit)만 한 번 수행한다. 루프가 돌고 있어도 tick lease가 직렬화한다.
`--loop`의 liveness 잠금은 잡지 않으므로 실행 중 러너와 공존한다.

```bash
docker run --rm --network host --env-file "$env_file" -e BINANCE_TREND_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_trend_demo --manage-only --confirm-demo
```

출력 `events`에 `entries_skipped_manage_only`가 있어야 한다. 이 모드에서 진입이
나오면 계약 위반이므로 즉시 멈추고 보고한다. 같은 명령에서 `--manage-only`를
`--once`로 바꾸면 정상 tick 1회(진입 포함)다.

```bash
docker run --rm --network host --env-file "$env_file" -e BINANCE_TREND_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_trend_demo --once --confirm-demo
```

## 11. 종료·잔여 위험

- 이 러너는 어떤 스케줄러에도 등록되지 않는다. `--loop`도 사람이 띄운 foreground
  프로세스다.
- 잔여 위험: watcher가 러너를 한 번도 보지 못한 채 러너가 죽으면(시작 전 또는 첫
  관측 전) 잠금 부재 알림이 나가지 않는다 — 운영 순서(러너 첫 tick → watcher)가 그
  구간을 막는다. 의도된 정지에서 watcher를 먼저 끄지 않으면 `heartbeat_missed`가 1회
  난다. 일시적 DB 오류는 러너를 죽이지 않고, 계속 실패하면 10분 뒤 stale 알림이 난다.
- 알림 전송 실패·지연은 러너 tick을 바꾸지 못한다(타임아웃 5초, 예외 삼킴, tick
  알림은 백그라운드 태스크).
