# Retired Mac worker launchd 로그 회전 및 25GB 기존 파일 처리

> **2026-09-02 retired:** TaskIQ worker is now NCP `at-worker` (#2012). The
> Mac worker and its `worker-log-rotation` launchd plists are intentionally
> absent; do not install or arm them, because reviving the Mac worker creates
> competing TaskIQ consumers. The remaining material is historical incident
> guidance for already-retired files only.

ROB-1118은 TaskIQ worker의 launchd stderr가 무제한 증가한 사고를 다룬다.
애플리케이션은 `httpx`를 `WARNING` 이상으로 유지해 요청별 INFO 라인을
제거하고, launchd 작업은 60초마다 worker stdout/stderr의 크기를 확인한다.

## 정상 운영 상한

- 현재 파일별 회전 기준: 128MiB (`AUTO_TRADER_LOG_MAX_BYTES=134217728`)
- 보관: 파일별 gzip archive 4개
- 검사 주기: 60초
- 정상 상태의 명목상 파일별 최대 보관량: 현재 128MiB + archive 4×128MiB
  = 640MiB
- 실제 현재 파일은 검사 사이 최대 60초 동안의 유입량만큼 128MiB를
  초과할 수 있다. `httpx` INFO 억제가 1차 방어이고, 회전기가 2차 상한이다.

회전기는 `launchd`가 열어 둔 FD 문제를 피하기 위해 worker를 bootout하고,
`lsof`로 writer가 0인지 확인한 후 `newsyslog`를 실행한다. 그 다음 worker를
bootstrap/kickstart해 새 inode에 stdout/stderr를 연결한다. archive 개수와
새 현재 파일의 크기를 실행 후 다시 검사하며, 검증 실패는 non-zero로 남긴다.

## 기존 25GB 파일: 운영자 승인 절차

이 절차는 운영자가 maintenance window와 삭제/보관 승인을 받은 뒤 수행한다.
코드 배포나 자동 회전 작업이 기존 파일을 직접 삭제하거나 truncate하지
않도록 arming marker는 배포에서 만들지 않는다.

1. 전체 파일을 읽지 말고 메타데이터와 제한 샘플만 확인한다.

   ```bash
   log="$HOME/services/auto_trader/logs/com.robinco.auto-trader.worker.err.log"
   stat -f 'size=%z inode=%i modified=%Sm' "$log"
   /usr/sbin/lsof "$log"
   tail -n 200000 "$log" | awk '...운영 승인된 제한 집계...'
   ```

   25GB 파일에 `grep`, `rg`, `cat`, `wc -l`을 실행하지 않는다.

2. worker를 중지하고 모든 writer FD가 닫혔는지 확인한다.

   ```bash
   uid_num="$(id -u)"
   label="com.robinco.auto-trader.worker"
   launchctl bootout "gui/$uid_num/$label"
   /usr/sbin/lsof "$log"
   ```

   `lsof` 출력이 남아 있으면 진행하지 않는다. launchd 부모와 TaskIQ 자식이
   이전 inode를 계속 잡은 채 path만 rename/unlink하면 디스크 공간은
   반환되지 않는다. 열린 FD가 남은 상태의 truncate 역시 안전한 회전이나
   공간 반환 절차로 인정하지 않는다. writer는 이전 offset/inode를 계속
   사용할 수 있고 sparse 재증가·로그 유실을 만들 수 있으므로 금지한다.

3. 현재 파일을 같은 파일시스템의 명시적인 incident archive 이름으로
   rename하고 새 파일을 만든다. 이 단계는 보존만 하며 공간을 반환하지 않는다.

   ```bash
   stamp="$(date '+%Y%m%d-%H%M%S')"
   archive="$log.pre-rob1118.$stamp"
   mv "$log" "$archive"
   install -m 0640 /dev/null "$log"
   ```

4. worker를 다시 올리고 새 파일 inode를 잡았는지 확인한다.

   ```bash
   plist="$HOME/Library/LaunchAgents/$label.plist"
   launchctl bootstrap "gui/$uid_num" "$plist"
   launchctl enable "gui/$uid_num/$label"
   launchctl kickstart -k "gui/$uid_num/$label"
   /usr/sbin/lsof "$log"
   ```

   `lsof`의 inode와 `stat`의 새 inode가 같아야 한다. 새 로그에는 애플리케이션
   WARNING/ERROR가 남고 `[httpx][INFO] HTTP Request:`가 더 이상 쌓이지 않는지
   제한된 `tail`로 확인한다.

5. 25GB incident archive는 승인된 보존 정책에 따라 처리한다.

   - 보존 필요: 여유 공간이 있는 별도 볼륨으로 복사하고 checksum을 대조한
     후, 원본 archive 제거를 별도 승인한다.
   - 로컬 압축: 압축 중 추가 공간이 필요하므로 사전 free-space 계산과 승인을
     거친다.
   - 삭제 승인: 현재 로그가 아닌 `pre-rob1118` archive의 정확한 경로와
     checksum을 재확인한 후 운영자가 제거한다.

   어떤 경우에도 실행 중인 현재 파일을 `rm` 또는 `truncate`하지 않는다.

## 회전기 설치와 arming

기존 25GB archive 처리와 새 inode 검증이 끝난 뒤에만 실행한다. 프로덕션
배포와 launchd 등록은 별도 운영 절차다.

```bash
label="com.robinco.auto-trader.worker-log-rotation"
source_plist="$HOME/services/auto_trader/plists/$label.plist"
target_plist="$HOME/Library/LaunchAgents/$label.plist"
install -m 0644 "$source_plist" "$target_plist"
touch "$HOME/services/auto_trader/shared/log-rotation.enabled"
launchctl bootstrap "gui/$(id -u)" "$target_plist"
launchctl enable "gui/$(id -u)/$label"
```

배포 스크립트는 이 label을 자동 시작하지 않으며 marker도 만들지 않는다.

## 상한 검증

운영 파일에는 강제 회전을 실행하지 않는다. 임시 디렉터리에서 기준을 1KiB,
archive 2개로 낮춰 실제 `newsyslog`를 반복 실행하는 테스트가 상한을 검증한다.

```bash
uv run pytest -q tests/scripts/test_worker_log_rotation.py
```

운영 arming 뒤에는 read-only로 다음을 확인한다.

```bash
log_dir="$HOME/services/auto_trader/logs"
ls -lh "$log_dir"/com.robinco.auto-trader.worker.{err,out}.log*
launchctl print "gui/$(id -u)/com.robinco.auto-trader.worker-log-rotation"
```

archive는 각 current 파일당 최대 4개여야 하고, current가 128MiB를 넘은 채
다음 검사 주기 이후에도 유지되면 회전기 stderr와 launchd 상태를 조사한다.

## 2026-07-28 제한 샘플 조사

25GB 전체를 읽지 않고 마지막 200,000줄만 조사했다. 샘플 범위는
2026-07-23 01:30:00~2026-07-28 12:20:29 KST였으며, 그 안에서 최근 24시간
(2026-07-27 12:20:29~2026-07-28 12:20:29 KST)을 별도로 집계했다.

| 지표 | 최근 24시간 |
|---|---:|
| 전체 샘플 줄 | 50,882 |
| httpx HTTP 요청 INFO | 39,950 |
| HTTP 5xx | 811 (전부 HTTP 500, HTTP 요청의 2.03%) |
| KIS rate-limit heuristic 경고 | 809 |
| ConnectTimeout 요청 sequence | 10 |
| 기록된 ConnectTimeout retry attempt | 20 |

HTTP 500 endpoint는 `inquire-daily-itemchartprice` 445,
overseas `dailyprice` 158, `inquire-time-dailychartprice` 115,
overseas `inquire-time-itemchartprice` 82, 기타 11건이었다. 811건 중 최소
808건(99.63%)은 뒤 10줄 안의 `EGW00201 초당 거래건수를 초과` rate-limit
경고와 짝지어졌고, 같은 구간의 rate-limit 경고 총수는 809건이었다. 따라서
일반적인 upstream 5xx와 분리해 해석해야 한다.

ConnectTimeout 10 sequence는 모두 KIS `inquire_daily_itemchartprice`였다.
각 sequence가 5초 ConnectTimeout을 1/3, 2/3으로 기록했고 호출 간격은 약
15~16초였다. 이는 ROB-1116의 최악 trace가 기록한 같은 KIS endpoint,
`5.003s + 5.002s + 5.002s` 직렬 retry와 동일한 코드 경로·시간 형태다.
따라서 ROB-1118 로그의 이 부분은 별건이 아니라 ROB-1116 B 트랙의 현장
증거로 합쳐야 한다. 반면 HTTP 500 대부분은 ConnectTimeout이 아니라 KIS가
HTTP 500 body로 반환한 rate-limit 응답이다.
