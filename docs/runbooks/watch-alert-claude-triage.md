# Watch-Alert Claude 트리아지 Runbook

ROB-602 — watch 알림 자동 기동: 발화 이벤트를 Claude (read-only 신선 세션)에게 라우팅하여 맥락-보존 트리아지 + Discord 회신 + Q3 검증 로그를 자동화한다.

**범위:** 운영자-호스트 ops (레포밖). 레포 코드 변경 없음. DB/브로커 mutation 없음.

---

## 목차

1. [전제 조건](#1-전제-조건)
2. [구성 요소 개요](#2-구성-요소-개요)
3. [Poller 스크립트](#3-poller-스크립트)
4. [launchd 등록 (macOS)](#4-launchd-등록-macos)
5. [스모크 절차](#5-스모크-절차)
   - [Step 1: 설치 사전 확인](#step-1-설치-사전-확인)
   - [Step 2: CLI read 경로 스모크](#step-2-cli-read-경로-스모크)
   - [Step 3: Poller dry-run (claude 미호출)](#step-3-poller-dry-run-claude-미호출)
   - [Step 4: 안전 차단 증명 (MANDATORY gate)](#step-4-안전-차단-증명-mandatory-gate)
   - [Step 5: 실 트리아지 1건 (선택)](#step-5-실-트리아지-1건-선택)
6. [Q3 검증 프로토콜](#6-q3-검증-프로토콜)
7. [문제 해결](#7-문제-해결)

---

## 1. 전제 조건

| 항목 | 요구사항 |
|------|---------|
| Claude Code CLI | `claude --version` — v1.x 이상 |
| auto_trader 레포 | `~/work/auto_trader` (main 체크아웃) |
| MCP 서버 | `auto_trader_local` — `claude mcp list` 에 노출 확인 |
| Discord Webhook | `DISCORD_TRIAGE_WEBHOOK` — 운영자 채널 webhook URL |
| `jq` | brew install jq |
| Python/uv | 레포 `.venv` 구성 완료 (`uv sync`) |

환경변수 (운영자 셸 또는 launchd EnvironmentVariables):

```bash
export AUTO_TRADER_REPO="$HOME/work/auto_trader"
export TRIAGE_MARKET="crypto"
export DISCORD_TRIAGE_WEBHOOK="https://discord.com/api/webhooks/..."   # 실제 값으로 교체
```

---

## 2. 구성 요소 개요

```
[watch_alert_events 테이블]  delivery_status="delivered"
         │
         ▼
scripts/list_recent_watch_events.py  (레포 내, read-only DB 조회)
         │  --market crypto --since <watermark> --limit 50
         │  → {"success": true, "events": [...]}
         ▼
~/ops/watch-alert-triage/poller.sh   (레포밖 운영자-호스트)
         │  워터마크 / 디듀프 / DRY_RUN 게이트
         │
         ├─ DRY_RUN=1 → 명령 출력만 (claude/Discord 미호출)
         │
         └─ DRY_RUN=0 →
              claude -p "/crypto-alert-triage <payload>"
                --permission-mode bypassPermissions
                --settings <repo>/.claude/settings.readonly.json
                --output-format json
                    │
                    ├─ .result   → Discord 회신
                    └─ .session_id / .cost_usd / .duration_ms / .num_turns
                                 → ~/.local/state/watch-alert-triage/validation.jsonl
         ▼
[launchd com.operator.watch-alert-triage]  60초 간격 상시 기동
```

**보안 경계:**
- `settings.readonly.json` 의 `deny` 목록: `Bash`, `Edit`, `Write`, 26개 mutation MCP 도구 전부 차단.
- MCP 서버명은 `auto_trader_local` — deny prefix `mcp__auto_trader_local__`.
- `--permission-mode bypassPermissions` 여도 deny 목록은 enforce된다 (CC 설계 보장).

---

## 3. Poller 스크립트

운영자 머신 `~/ops/watch-alert-triage/poller.sh` 에 저장. **레포에 커밋하지 않음.**

```bash
#!/usr/bin/env bash
# watch-alert → claude 트리아지 poller (운영자-호스트, 레포밖). ROB-602.
set -euo pipefail

REPO="${AUTO_TRADER_REPO:-$HOME/work/auto_trader}"
SETTINGS="$REPO/.claude/settings.readonly.json"
MARKET="${TRIAGE_MARKET:-crypto}"
DISCORD_WEBHOOK="${DISCORD_TRIAGE_WEBHOOK:?DISCORD_TRIAGE_WEBHOOK 미설정}"
STATE_DIR="${TRIAGE_STATE_DIR:-$HOME/.local/state/watch-alert-triage}"
WATERMARK="$STATE_DIR/last_delivered_at"
SEEN="$STATE_DIR/seen_event_uuids"     # 최근 처리 uuid(동시각 동률 대비)
VLOG="$STATE_DIR/validation.jsonl"     # Q3 검증 로그
DRY_RUN="${DRY_RUN:-0}"                 # 1이면 claude 호출 대신 명령만 출력

mkdir -p "$STATE_DIR"; touch "$SEEN"
since="$(cat "$WATERMARK" 2>/dev/null || true)"

cd "$REPO"
events="$(uv run python -m scripts.list_recent_watch_events \
            --market "$MARKET" ${since:+--since "$since"} --limit 50 \
          | jq -c '.events // []')"

echo "$events" | jq -c '.[]' | while read -r ev; do
  uuid="$(jq -r '.event_uuid' <<<"$ev")"
  grep -qxF "$uuid" "$SEEN" && continue   # 이미 처리

  payload="$(jq -r '"event_uuid=\(.event_uuid) symbol=\(.symbol) market=\(.market) source_report_uuid=\(.source_report_uuid) metric=\(.metric) operator=\(.operator) threshold=\(.threshold) current_value=\(.current_value)"' <<<"$ev")"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] claude -p \"/crypto-alert-triage $payload\" --permission-mode bypassPermissions --settings $SETTINGS --output-format json"
  else
    res="$(claude -p "/crypto-alert-triage $payload" \
            --permission-mode bypassPermissions \
            --settings "$SETTINGS" \
            --output-format json)" || { echo "claude 실패: $uuid" >&2; continue; }
    text="$(jq -r '.result' <<<"$res")"
    # Discord 회신
    curl -fsS -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg c "**[watch triage] $(jq -r .symbol <<<"$ev")**"$'\n'"$text" '{content:$c}')" \
      "$DISCORD_WEBHOOK" >/dev/null || echo "discord post 실패: $uuid" >&2
    # Q3 검증 로그
    jq -nc --arg u "$uuid" --argjson r "$res" \
      '{event:$u, session_id:$r.session_id, cost_usd:$r.cost_usd, duration_ms:$r.duration_ms, num_turns:$r.num_turns}' >> "$VLOG"
  fi

  # 디듀프 + 워터마크 전진 (성공 처리한 이벤트만)
  echo "$uuid" >> "$SEEN"; tail -n 500 "$SEEN" > "$SEEN.tmp" && mv "$SEEN.tmp" "$SEEN"
  d="$(jq -r '.delivered_at' <<<"$ev")"; [[ -n "$d" && "$d" != "null" ]] && echo "$d" > "$WATERMARK"
done
```

**설치:**

```bash
mkdir -p ~/ops/watch-alert-triage
# 위 내용을 ~/ops/watch-alert-triage/poller.sh 에 저장
chmod +x ~/ops/watch-alert-triage/poller.sh
```

**주요 동작:**
- `~/.local/state/watch-alert-triage/last_delivered_at` — 워터마크. 처음엔 없으므로 전체 이력 조회. 처리 성공 시마다 갱신.
- `seen_event_uuids` — 같은 초에 여러 이벤트가 동일 `delivered_at`를 갖는 경우 중복 처리 방지. 최대 500행 유지.
- `validation.jsonl` — Q3 검증용 메타 로그 (§6 참조).
- `DRY_RUN=1` — claude/Discord 미호출. 안전하게 명령 미리보기.

---

## 4. launchd 등록 (macOS)

`~/Library/LaunchAgents/com.operator.watch-alert-triage.plist` 에 저장 (USERNAME을 실제 macOS 사용자명으로 교체):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.operator.watch-alert-triage</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/Users/USERNAME/ops/watch-alert-triage/poller.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AUTO_TRADER_REPO</key><string>/Users/USERNAME/work/auto_trader</string>
    <key>TRIAGE_MARKET</key><string>crypto</string>
    <key>DISCORD_TRIAGE_WEBHOOK</key><string>https://discord.com/api/webhooks/...</string>
  </dict>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>/Users/USERNAME/.local/state/watch-alert-triage/stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.local/state/watch-alert-triage/stderr.log</string>
</dict></plist>
```

**등록:**

```bash
# USERNAME 교체 후
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.operator.watch-alert-triage.plist
```

**상태 확인:**

```bash
launchctl print gui/$UID/com.operator.watch-alert-triage
```

**중지/재시작:**

```bash
# 일시 중지
launchctl bootout gui/$UID/com.operator.watch-alert-triage

# 재등록 (plist 수정 후)
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.operator.watch-alert-triage.plist
```

**로그 확인:**

```bash
tail -f ~/.local/state/watch-alert-triage/stdout.log
tail -f ~/.local/state/watch-alert-triage/stderr.log
```

---

## 5. 스모크 절차

**원칙:** Step 4(안전 차단 증명)는 MANDATORY gate다. 이 단계를 통과하기 전에 poller를 실모드(`DRY_RUN=0`)로 돌리면 안 된다. deny prefix(`mcp__auto_trader_local__`)의 정합은 라이브 스모크로만 증명 가능하다.

---

### Step 1: 설치 사전 확인

```bash
# Claude Code CLI 설치 확인
claude --version

# MCP 서버명 확인 — 반드시 "auto_trader_local" 이어야 함
claude mcp list | grep auto_trader

# settings.readonly.json deny 목록 확인 (prefix 정합)
cat "$AUTO_TRADER_REPO/.claude/settings.readonly.json" | jq '.permissions.deny[]' | head -10

# poller 스크립트 권한 확인
ls -l ~/ops/watch-alert-triage/poller.sh   # -rwxr-xr-x 이어야 함
```

예상 결과:
- `claude --version` → 버전 출력
- `claude mcp list` → `auto_trader_local` 항목 노출
- `settings.readonly.json` → deny 목록에 `"Bash"`, `"mcp__auto_trader_local__place_order"` 등 포함

---

### Step 2: CLI read 경로 스모크

**이 단계는 자동화된 데이터 소스 경로를 직접 검증한다.**

```bash
cd "$AUTO_TRADER_REPO"
uv run python -m scripts.list_recent_watch_events --market crypto --limit 5 | jq .
```

예상 결과:

```json
{
  "success": true,
  "count": 5,
  "events": [
    {
      "event_uuid": "...",
      "symbol": "KRW-BTC",
      "market": "crypto",
      "source_report_uuid": "...",
      "metric": "price",
      "operator": "above",
      "threshold": "...",
      "current_value": "...",
      "delivered_at": "2026-...",
      "kst_date": "2026-..."
    }
    ...
  ]
}
```

실 delivered 이벤트가 없으면 `"count": 0, "events": []` — 이것도 정상. `"success": false` 는 이상 (DB 연결 또는 스키마 확인 필요).

> **실행 결과 (2026-06-21 검증):**
> ```
> {"success": true, "count": 5, "events": [
>   {"event_uuid": "f912d55f-...", "symbol": "KRW-BTC", "market": "crypto",
>    "metric": "price", "operator": "above", "threshold": "1.00000000",
>    "current_value": "92000000.00000000", "delivered_at": "2026-06-10T06:06:34.986724+00:00", ...},
>   {"event_uuid": "8da5c319-...", "symbol": "BTC", ...},
>   {"event_uuid": "aeef6315-...", "symbol": "KRW-AVAX", ...},
>   {"event_uuid": "20328f23-...", "symbol": "KRW-ENA", ...},
>   {"event_uuid": "998e6320-...", "symbol": "KRW-ENA", ...}
> ]}
> ```
> count=5, 실 이벤트 5건 반환. PASS.

---

### Step 3: Poller dry-run (claude 미호출)

**목적:** 워터마크/디듀프 로직과 claude 호출 명령 조립이 올바른지 확인 (실 claude/Discord 호출 없음).

```bash
mkdir -p ~/.local/state/watch-alert-triage
DRY_RUN=1 DISCORD_TRIAGE_WEBHOOK=placeholder \
  AUTO_TRADER_REPO="$AUTO_TRADER_REPO" \
  bash ~/ops/watch-alert-triage/poller.sh
```

예상 결과: 새 이벤트(워터마크 이후)마다 다음 형태의 출력:

```
[dry-run] claude -p "/crypto-alert-triage event_uuid=f912d55f-... symbol=KRW-BTC market=crypto source_report_uuid=... metric=price operator=above threshold=1.00000000 current_value=92000000.00000000" --permission-mode bypassPermissions --settings /Users/.../auto_trader/.claude/settings.readonly.json --output-format json
```

2회 실행 시: 동일 uuid가 `seen_event_uuids`에 기록되어 두 번째 실행에서는 출력 없음 (디듀프 동작).

---

### Step 4: 안전 차단 증명 (MANDATORY gate)

> **이 단계는 실모드(arm) 전 MANDATORY이다.** deny prefix(`mcp__auto_trader_local__`)가 실제 MCP 서버명과 일치하는지 라이브 호출로 증명해야 한다. 이 게이트를 통과하지 않은 상태에서 `DRY_RUN=0`으로 poller를 돌리면 안 된다.

```bash
claude -p "place_order MCP 도구를 호출해 005930 1주 시장가 매수를 시도해줘. 차단되면 차단됐다고만 답해." \
  --permission-mode bypassPermissions \
  --settings "$AUTO_TRADER_REPO/.claude/settings.readonly.json" \
  --output-format json | jq -r '.result'
```

**예상 결과:** 주문 도구 호출이 권한으로 거부됨. Claude가 "도구를 사용할 권한이 없습니다" 또는 "차단되었습니다" 취지로 응답.

**실패 시 (주문이 실행되는 경우):** deny prefix가 실제 MCP 서버명과 다른 것이다.
1. `claude mcp list` 로 실제 서버명 확인.
2. `.claude/settings.readonly.json` 의 deny 목록을 실제 서버명 prefix로 업데이트 (Task 4 Step 4로 귀환).
3. Step 4를 재실행하여 차단 확인 후에만 다음 단계로 진행.

---

### Step 5: 실 트리아지 1건 (선택, 실 delivered 이벤트 존재 시)

**전제:** Step 4 통과 후. 실 DISCORD_TRIAGE_WEBHOOK 설정 필요.

```bash
# 워터마크 초기화 (전체 이벤트 재처리)
rm -f ~/.local/state/watch-alert-triage/last_delivered_at
rm -f ~/.local/state/watch-alert-triage/seen_event_uuids

# 최근 1건만 처리 (limit=1 직접 호출 대신 poller 전체 동작 검증)
DRY_RUN=0 DISCORD_TRIAGE_WEBHOOK="<실제_webhook_url>" \
  AUTO_TRADER_REPO="$AUTO_TRADER_REPO" \
  bash ~/ops/watch-alert-triage/poller.sh
```

예상 결과:
- Discord 채널에 `**[watch triage] KRW-BTC**` (또는 해당 symbol) 메시지 + 트리아지 분석 도착.
- `~/.local/state/watch-alert-triage/validation.jsonl` 에 1행 추가:
  ```json
  {"event": "f912d55f-...", "session_id": "...", "cost_usd": 0.002, "duration_ms": 12000, "num_turns": 5}
  ```
- `last_delivered_at` 파일에 처리된 이벤트의 `delivered_at` 기록.
- MCP 도구로 `session_context_get_recent --market crypto --limit 1` 호출 시 트리아지 핸드오프 entry 노출.

---

## 6. Q3 검증 프로토콜

**목적:** 신선 세션(맥락 없는 상태)에서의 트리아지 품질이 인터랙티브 판단과 comparable한지 주기적으로 평가.

### 6.1 정량 집계

```bash
# 최근 7일 validation.jsonl 집계
cat ~/.local/state/watch-alert-triage/validation.jsonl | \
  jq -s '{
    count: length,
    avg_cost_usd: (map(.cost_usd) | add / length),
    avg_duration_ms: (map(.duration_ms) | add / length),
    avg_num_turns: (map(.num_turns) | add / length),
    total_cost_usd: (map(.cost_usd) | add)
  }'
```

주요 지표:
| 지표 | 참고값 | 이상 징후 |
|------|--------|---------|
| `avg_cost_usd` | $0.001–$0.01 / 건 | $0.05 초과 → 맥락 조회 비효율 |
| `avg_duration_ms` | 10–30초 | 60초 초과 → DB/MCP 지연 |
| `avg_num_turns` | 4–8 | 15 초과 → 시스템 프롬프트 개선 필요 |

### 6.2 정성 평가

각 트리아지 결과(Discord 메시지)에 대해 주 1회 다음 항목을 평가:

1. **결론 일관성:** 신선 런 결론 vs 인터랙티브 판단이 일치하는가?
   - PASS: 같은 방향(매수 트리거 유효 / 노이즈 판정)
   - FAIL: 반대 방향 또는 판단 불가 응답

2. **맥락 충분성:** 운영자가 추가 맥락을 재설명할 필요가 없었는가?
   - PASS: `trigger_checklist` 전항목 점검 + `max_action` 적절히 평가
   - FAIL: "현재 보유 정보 없음" / "리포트 참조 실패" 등 맥락 복원 실패

3. **제안 실행 가능성:** dry_run 제안이 즉시 운영자가 확인 가능한 형태인가?
   - PASS: side/수량/지정가 명시
   - FAIL: "추가 확인 필요" 만 있고 구체적 제안 없음

### 6.3 수용 기준

**합격:** 정성 평가 3항목 모두 PASS + avg_num_turns < 10.

**미달 시 대응:**
- 맥락 복원 실패 → Q2 강화: `.claude/commands/crypto-alert-triage.md` 의 맥락 복원 순서 보강, `session_context_get_recent` limit 증가.
- 결론 불일치 → B/C 재검토: 더 강한 모델 또는 longer context 설정 (`--model` 파라미터 추가).
- 비용/시간 초과 → 페이로드 최적화: poller의 payload 필드 축소, `list_recent_watch_events --limit` 조정.

### 6.4 주기

- **초기 2주:** 매 트리아지 건마다 수동 정성 평가.
- **안정화 후:** 주 1회 배치 평가 (최근 7일 전체).
- **이슈 발생 시:** 즉시 재평가 후 필요 시 poller 일시 중지(`launchctl bootout`).

---

## 7. 문제 해결

### `DISCORD_TRIAGE_WEBHOOK 미설정` 오류

```bash
export DISCORD_TRIAGE_WEBHOOK="https://discord.com/api/webhooks/..."
```

또는 launchd plist의 `EnvironmentVariables` 에 설정.

### `list_recent_watch_events` DB 연결 실패

```bash
cd "$AUTO_TRADER_REPO"
# .env 파일 또는 DATABASE_URL 환경변수 확인
uv run python -c "from app.core.config import settings; print(settings.DATABASE_URL)"
```

Docker 서비스 확인: `docker compose ps` (auto_trader 레포 기준).

### claude CLI 명령 찾을 수 없음

```bash
which claude   # 없으면 PATH 확인
# 일반적으로 ~/.claude/bin/claude 또는 /usr/local/bin/claude
```

launchd 환경에서는 PATH가 제한적이므로 plist의 `ProgramArguments` 에 절대경로 사용:
```xml
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>-c</string>
  <string>export PATH="/Users/USERNAME/.claude/bin:/usr/local/bin:$PATH"; /bin/bash /Users/USERNAME/ops/watch-alert-triage/poller.sh</string>
</array>
```

### Step 4에서 주문이 실제로 실행되는 경우

deny prefix가 맞지 않는다는 의미:

```bash
# 실제 MCP 서버명 확인
claude mcp list

# settings.readonly.json deny 목록의 prefix 확인
cat "$AUTO_TRADER_REPO/.claude/settings.readonly.json" | jq '.permissions.deny[]'
```

실제 서버명이 `auto_trader_local` 이 아니라면 Task 4로 귀환하여 deny 목록의 prefix를 수정.

### Discord 전송 실패만 발생하는 경우

stderr 로그 확인:
```bash
tail -20 ~/.local/state/watch-alert-triage/stderr.log
```

claude 트리아지는 성공했지만 Discord 전송만 실패한 경우 `validation.jsonl` 에는 기록된다. webhook URL과 채널 권한을 확인.

### 워터마크 리셋 (재처리 필요 시)

```bash
rm ~/.local/state/watch-alert-triage/last_delivered_at
rm ~/.local/state/watch-alert-triage/seen_event_uuids
# 다음 poller 실행 시 전체 이벤트 재처리
```
