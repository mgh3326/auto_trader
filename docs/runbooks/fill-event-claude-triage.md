# Fill-Event Claude 트리아지 Runbook

ROB-755 — 체결(fill) 이벤트 자동 기동: websocket 동기화된 `execution_ledger` 행을 Claude (read-only 신선 세션)에게 라우팅하여 맥락-보존 트리아지(매도 시 현금 재배치, 매수 시 잔여 주문 점검) + Discord 회신 + Q3 검증 로그를 자동화한다.

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
| Discord Webhook | `DISCORD_FILL_TRIAGE_WEBHOOK` — 운영자 채널 webhook URL |
| `jq` | brew install jq |
| Python/uv | 레포 `.venv` 구성 완료 (`uv sync`) |

환경변수 (운영자 셸 또는 launchd EnvironmentVariables):

```bash
export AUTO_TRADER_REPO="$HOME/work/auto_trader"
export FILL_TRIAGE_MARKET="crypto"
export DISCORD_FILL_TRIAGE_WEBHOOK="https://discord.com/api/webhooks/..."   # 실제 값으로 교체
```

선택 환경변수 (기본값이 합리적이라 보통은 export 불필요):

```bash
export FILL_TRIAGE_STATE_DIR="$HOME/.local/state/fill-event-triage"   # 기본
export DRY_RUN="0"                                                   # 1이면 claude/Discord 미호출
```

---

## 2. 구성 요소 개요

```
[execution_ledger 테이블]  source='websocket' 인 신규 row
          │
          ▼
scripts/list_recent_fill_events.py  (레포 내, read-only DB 조회)
          │  --market crypto --source websocket --after-id <watermark> --limit 50
          │  → {"success": true, "fills": [...]}
          ▼
~/ops/fill-event-triage/poller.sh   (레포밖 운영자-호스트)
          │  워터마크(execution_ledger.id) / 디듀프 / DRY_RUN 게이트
          │
          ├─ DRY_RUN=1 → 명령 출력만 (claude/Discord 미호출)
          │
          └─ DRY_RUN=0 →
               claude -p "/fill-event-triage <payload>"
                 --permission-mode bypassPermissions
                 --settings <repo>/.claude/settings.readonly.json
                 --output-format json
                     │
                     ├─ .result   → Discord 회신
                     └─ .session_id / .cost_usd / .duration_ms / .num_turns
                                  → ~/.local/state/fill-event-triage/validation.jsonl
          ▼
[launchd com.operator.fill-event-triage]  60초 간격 상시 기동
```

**보안 경계:**
- `settings.readonly.json` 의 `deny` 목록: `Bash`, `Edit`, `Write`, 그리고 모든 mutation MCP 도구(place_order / cancel_order / modify_order / reconcile / report mutation / ladder preview 등) 차단.
- MCP 서버명은 `auto_trader_local` — deny prefix `mcp__auto_trader_local__`.
- `--permission-mode bypassPermissions` 여도 deny 목록은 enforce된다 (CC 설계 보장).

**워터마크 특성:**
- watermark는 `execution_ledger.id` (정수 PK, 단조 증가). 타임스탬프가 아니다.
- websocket이 동일 체결을 재방문해도 `id`는 동일 → poller는 한 번만 처리하고 seen-set으로 디듀프까지 한 번 더.
- 즉, 같은 체결 row에 대해 두 번 발화하지 않는다.

---

## 3. Poller 스크립트

운영자 머신 `~/ops/fill-event-triage/poller.sh` 에 저장. **레포에 커밋하지 않음.**

```bash
#!/usr/bin/env bash
# fill-event → claude 트리아지 poller (운영자-호스트, 레포밖). ROB-755.
set -euo pipefail

REPO="${AUTO_TRADER_REPO:-$HOME/work/auto_trader}"
SETTINGS="$REPO/.claude/settings.readonly.json"
MARKET="${FILL_TRIAGE_MARKET:-crypto}"
DISCORD_WEBHOOK="${DISCORD_FILL_TRIAGE_WEBHOOK:?DISCORD_FILL_TRIAGE_WEBHOOK 미설정}"
STATE_DIR="${FILL_TRIAGE_STATE_DIR:-$HOME/.local/state/fill-event-triage}"
WATERMARK="$STATE_DIR/last_ledger_id"          # execution_ledger.id 워터마크
SEEN="$STATE_DIR/seen_ledger_ids"              # 최근 처리 id(워터마크 동률 대비)
VLOG="$STATE_DIR/validation.jsonl"             # Q3 검증 로그
DRY_RUN="${DRY_RUN:-0}"                         # 1이면 claude/Discord 미호출

mkdir -p "$STATE_DIR"; touch "$SEEN"
last_id="$(cat "$WATERMARK" 2>/dev/null || true)"

cd "$REPO"
fills="$(uv run python -m scripts.list_recent_fill_events \
            --market "$MARKET" \
            --source websocket \
            ${last_id:+--after-id "$last_id"} \
            --limit 50 \
          | jq -c '.fills // []')"

echo "$fills" | jq -c '.[]' | while read -r fill; do
  lid="$(jq -r '.ledger_id' <<<"$fill")"
  # id가 seen에 있으면(exit 0) continue; 없으면(exit 1) && 단락→계속 진행 (set -e 미발동)
  grep -qxF "$lid" "$SEEN" && continue   # 이미 처리

  payload="$(jq -r \
    '"ledger_id=\(.ledger_id) event_key=\(.event_key) broker=\(.broker) account_mode=\(.account_mode) market=\(.market) symbol=\(.symbol) side=\(.side) filled_qty=\(.filled_qty) filled_price=\(.filled_price) filled_notional=\(.filled_notional) currency=\(.currency) filled_at=\(.filled_at) correlation_id=\(.correlation_id // "")"' \
    <<<"$fill")"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] claude -p \"/fill-event-triage $payload\" --permission-mode bypassPermissions --settings $SETTINGS --output-format json"
  else
    # Claude 시도 — 실패 시 break(배치 중단, 다음 폴에서 재시도).
    # fills는 execution_ledger.id 오름차순으로 들어오므로 break로 배치를 끊어
    # 워터마크가 실패 fill을 건너뛰고 후속 fill을 처리하는 일을 막는다 (at-least-once).
    res="$(claude -p "/fill-event-triage $payload" \
            --permission-mode bypassPermissions \
            --settings "$SETTINGS" \
            --output-format json)" || { echo "claude 실패(배치 중단, 다음 폴에서 재시도): $lid" >&2; break; }
    text="$(jq -r '.result' <<<"$res")"
    # Discord POST — 성공해야만 Q3 로그·seen·워터마크를 갱신한다.
    # 실패 시 break로 배치 중단 (at-least-once).
    curl -fsS -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg c "**[fill triage] $(jq -r .symbol <<<"$fill")**"$'\n'"$text" '{content:$c}')" \
      "$DISCORD_WEBHOOK" >/dev/null \
      || { echo "discord post 실패(배치 중단, 다음 폴에서 재시도): $lid" >&2; break; }
    # Q3 검증 로그 — Discord 성공 "직후"에만 기록.
    # Claude는 성공했으나 Discord가 실패한 채로 재시도되면 동일 ledger_id가 중복
    # 기록되는 것을 막기 위함(§6.1 집계는 ledger_id 기준 합산만 하므로 중복이
    # 들어가면 비용/시간 평균이 부풀어진다).
    jq -nc --arg lid "$lid" --argjson r "$res" \
      '{ledger_id:$lid, session_id:$r.session_id, cost_usd:$r.cost_usd, duration_ms:$r.duration_ms, num_turns:$r.num_turns}' >> "$VLOG"
  fi

  # 디듀프 + 워터마크 전진 (claude + Discord 모두 성공한 이벤트만)
  echo "$lid" >> "$SEEN"; tail -n 500 "$SEEN" > "$SEEN.tmp" && mv "$SEEN.tmp" "$SEEN"
  echo "$lid" > "$WATERMARK"
done
```

**설치:**

```bash
mkdir -p ~/ops/fill-event-triage
# 위 내용을 ~/ops/fill-event-triage/poller.sh 에 저장
chmod +x ~/ops/fill-event-triage/poller.sh
```

**주요 동작:**
- `~/.local/state/fill-event-triage/last_ledger_id` — 워터마크 (가장 큰 `execution_ledger.id`). 처음엔 없으므로 전체 이력 조회. 처리 성공 시마다 갱신.
- `seen_ledger_ids` — 같은 watermark(=`id`)를 갖는 동시각 row가 다수일 수 있는 케이스 대비(예: 동일 ID로 동일 초에 여러 source에서 upsert된 경우) 중복 처리 방지. 최대 500행 유지.
- `validation.jsonl` — Q3 검증용 메타 로그 (§6 참조).
- `DRY_RUN=1` — claude/Discord 미호출. 안전하게 명령 미리보기.
- `at-least-once` 보장: `claude -p` 또는 `curl` Discord가 실패하면 `seen`/`last_ledger_id`를 모두 갱신하지 않고 다음 사이클에서 동일 row를 다시 트리아지한다.

---

## 4. launchd 등록 (macOS)

`~/Library/LaunchAgents/com.operator.fill-event-triage.plist` 에 저장 (USERNAME을 실제 macOS 사용자명으로 교체):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.operator.fill-event-triage</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/Users/USERNAME/ops/fill-event-triage/poller.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AUTO_TRADER_REPO</key><string>/Users/USERNAME/work/auto_trader</string>
    <key>FILL_TRIAGE_MARKET</key><string>crypto</string>
    <key>DISCORD_FILL_TRIAGE_WEBHOOK</key><string>https://discord.com/api/webhooks/...</string>
  </dict>
  <key>WorkingDirectory</key><string>/Users/USERNAME/work/auto_trader</string>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>/Users/USERNAME/.local/state/fill-event-triage/stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.local/state/fill-event-triage/stderr.log</string>
</dict></plist>
```

**등록:**

```bash
# USERNAME 교체 후
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.operator.fill-event-triage.plist
```

**상태 확인:**

```bash
launchctl print gui/$UID/com.operator.fill-event-triage
```

**중지/재시작:**

```bash
# 일시 중지
launchctl bootout gui/$UID/com.operator.fill-event-triage

# 재등록 (plist 수정 후)
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.operator.fill-event-triage.plist
```

**로그 확인:**

```bash
tail -f ~/.local/state/fill-event-triage/stdout.log
tail -f ~/.local/state/fill-event-triage/stderr.log
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
ls -l ~/ops/fill-event-triage/poller.sh   # -rwxr-xr-x 이어야 함
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
uv run python -m scripts.list_recent_fill_events --market crypto --source websocket --limit 5 | jq .
```

예상 결과:

```json
{
  "success": true,
  "count": 5,
  "fills": [
    {
      "ledger_id": 1234,
      "event_key": "execution_ledger:1234",
      "broker": "upbit",
      "account_mode": "live",
      "market": "crypto",
      "symbol": "KRW-BTC",
      "side": "sell",
      "filled_qty": "0.0123",
      "filled_price": "92000000.00000000",
      "filled_notional": "1131600.00000000",
      "currency": "KRW",
      "filled_at": "2026-...",
      "correlation_id": "..."
    }
    ...
  ]
}
```

실 websocket-소스 fill이 없으면 `"count": 0, "fills": []` — 이것도 정상. `"success": false` 는 이상 (DB 연결 또는 스키마 확인 필요).

---

### Step 3: Poller dry-run (claude 미호출)

**목적:** 워터마크/디듀프 로직과 claude 호출 명령 조립이 올바른지 확인 (실 claude/Discord 호출 없음).

```bash
mkdir -p ~/.local/state/fill-event-triage
DRY_RUN=1 DISCORD_FILL_TRIAGE_WEBHOOK=placeholder \
  AUTO_TRADER_REPO="$AUTO_TRADER_REPO" \
  bash ~/ops/fill-event-triage/poller.sh
```

예상 결과: 새 fill(워터마크 이후)마다 다음 형태의 출력:

```
[dry-run] claude -p "/fill-event-triage ledger_id=1234 event_key=execution_ledger:1234 broker=upbit account_mode=live market=crypto symbol=KRW-BTC side=sell filled_qty=0.0123 filled_price=92000000.00000000 filled_notional=1131600.00000000 currency=KRW filled_at=2026-... correlation_id=..." --permission-mode bypassPermissions --settings /Users/.../auto_trader/.claude/settings.readonly.json --output-format json
```

2회 실행 시: 동일 `ledger_id`가 `seen_ledger_ids`에 기록되어 두 번째 실행에서는 출력 없음 (디듀프 동작).

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

### Step 5: 실 트리아지 1건 (선택, 실 websocket fill 존재 시)

**전제:** Step 4 통과 후. 실 `DISCORD_FILL_TRIAGE_WEBHOOK` 설정 필요.

> **경고:** 워터마크를 리셋하면 `--limit 50` 범위의 모든 fill이 한 번에 재처리된다(각각 claude 비용 + Discord 발송). 먼저 Step 2로 `count`를 확인하고 재처리 건수를 파악하라.

```bash
# 워터마크 초기화 (전체 fill 재처리)
rm -f ~/.local/state/fill-event-triage/last_ledger_id
rm -f ~/.local/state/fill-event-triage/seen_ledger_ids

# 최근 fill 처리 (limit=1 직접 호출 대신 poller 전체 동작 검증)
DRY_RUN=0 DISCORD_FILL_TRIAGE_WEBHOOK="<실제_webhook_url>" \
  AUTO_TRADER_REPO="$AUTO_TRADER_REPO" \
  bash ~/ops/fill-event-triage/poller.sh
```

예상 결과:
- Discord 채널에 `**[fill triage] KRW-BTC**` (또는 해당 symbol) 메시지 + 트리아지 분석 도착.
- `~/.local/state/fill-event-triage/validation.jsonl` 에 1행 추가:
  ```json
  {"ledger_id": "1234", "session_id": "...", "cost_usd": 0.002, "duration_ms": 12000, "num_turns": 5}
  ```
- `last_ledger_id` 파일에 처리된 fill의 `ledger_id` 기록.
- MCP 도구로 `session_context_get_recent --market crypto --limit 1` 호출 시 트리아지 핸드오프 entry 노출.

---

## 6. Q3 검증 프로토콜

**목적:** 신선 세션(맥락 없는 상태)에서의 트리아지 품질이 인터랙티브 판단과 comparable한지 주기적으로 평가.

### 6.1 정량 집계

```bash
# 최근 7일 validation.jsonl 집계
# cost_usd null(API 에러 응답 등)은 제외 후 평균·합산 (null이 섞이면 add가 null을 반환)
cat ~/.local/state/fill-event-triage/validation.jsonl | \
  jq -s '
    . as $all |
    ($all | map(select(.cost_usd != null))) as $with_cost |
    {
      count: ($all | length),
      avg_cost_usd: (if ($with_cost | length) > 0 then ($with_cost | map(.cost_usd) | add / length) else null end),
      avg_duration_ms: (map(.duration_ms) | add / length),
      avg_num_turns: (map(.num_turns) | add / length),
      total_cost_usd: (if ($with_cost | length) > 0 then ($with_cost | map(.cost_usd) | add) else null end)
    }
  '
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
   - PASS: 같은 방향(sell → 현금 재배치 제안 / buy → 잔여 주문 가이드)
   - FAIL: 반대 방향 또는 판단 불가 응답

2. **맥락 충분성:** 운영자가 추가 맥락을 재설명할 필요가 없었는가?
   - PASS: `get_operating_briefing` / `get_cash_balance` / `session_context_get_recent` 결과가 트리아지에 반영
   - FAIL: "현재 보유 정보 없음" / "리포트 참조 실패" 등 맥락 복원 실패

3. **제안 실행 가능성:** dry_run 제안이 즉시 운영자가 확인 가능한 형태인가?
   - PASS: 매도 시 재배치 종목/수량/지정가 명시 / 매수 시 잔여 rung 가이드 명확
   - FAIL: "추가 확인 필요" 만 있고 구체적 제안 없음

### 6.3 수용 기준

**합격:** 정성 평가 3항목 모두 PASS + avg_num_turns < 10.

**미달 시 대응:**
- 맥락 복원 실패 → Q2 강화: `.claude/commands/fill-event-triage.md` 의 맥락 복원 순서 보강, `session_context_get_recent` limit 증가.
- 결론 불일치 → 모델/컨텍스트 재검토: `--model` 파라미터 추가 또는 longer context 설정.
- 비용/시간 초과 → 페이로드 최적화: poller의 payload 필드 축소, `list_recent_fill_events --limit` 조정.

### 6.4 주기

- **초기 2주:** 매 트리아지 건마다 수동 정성 평가.
- **안정화 후:** 주 1회 배치 평가 (최근 7일 전체).
- **이슈 발생 시:** 즉시 재평가 후 필요 시 poller 일시 중지(`launchctl bootout`).

---

## 7. 문제 해결

### `DISCORD_FILL_TRIAGE_WEBHOOK 미설정` 오류

```bash
export DISCORD_FILL_TRIAGE_WEBHOOK="https://discord.com/api/webhooks/..."
```

또는 launchd plist의 `EnvironmentVariables` 에 설정.

### `list_recent_fill_events` 가 빈 fills를 반환

워터마크(`last_ledger_id`) 이후에 새 websocket-source fill이 없는 것이다. 정상.

```bash
# 현재 워터마크 확인
cat ~/.local/state/fill-event-triage/last_ledger_id

# 워터마크 무시하고 최근 5건 직접 조회 (DB에 row는 있는지)
cd "$AUTO_TRADER_REPO"
uv run python -m scripts.list_recent_fill_events --market crypto --source websocket --limit 5 | jq '.count, .fills[].ledger_id'
```

`count=0` 이면 DB 자체에 websocket-source fill이 없음 (실 운영 트래픽 미발생 또는 websocket 미기동).

### `claude -p` 호출이 실패하는 경우

```bash
# stderr 로그 확인
tail -20 ~/.local/state/fill-event-triage/stderr.log
# "claude 실패: <ledger_id>" 가 보이면 해당 row는 다음 사이클에서 재시도됨

# claude CLI 직접 호출 테스트 (해당 ledger_id로)
claude -p "/fill-event-triage ledger_id=... event_key=... broker=... account_mode=... market=crypto symbol=KRW-BTC side=sell filled_qty=... filled_price=... filled_notional=... currency=KRW filled_at=... correlation_id=..." \
  --permission-mode bypassPermissions \
  --settings "$AUTO_TRADER_REPO/.claude/settings.readonly.json" \
  --output-format json | jq -r '.result'
```

claude 실패 시 해당 ledger_id는 seen-set에 들어가지 않으므로 **다음 사이클에 재시도된다(at-least-once)**. 일시적 API 오류라면 자연 회복. 영구적 오류라면 `claude --version` / 인증 상태 / MCP 서버 노출을 확인.

### Discord webhook이 4xx/5xx를 반환하는 경우

```bash
# curl 직접 호출로 응답 확인
curl -fsS -o /dev/null -w "%{http_code}\n" \
  -H 'Content-Type: application/json' \
  -d '{"content":"[fill-triage-smoke] webhook test"}' \
  "$DISCORD_FILL_TRIAGE_WEBHOOK"
```

200이 아니면: webhook URL / 채널 권한 / rate limit (Discord는 채널당 분당 ~30 메시지) 확인.

curl 실패 시 해당 ledger_id는 seen-set에 들어가지 않으므로 **다음 사이클에 재시도된다(at-least-once)**. webhook이 영구적으로 잘못 설정된 경우 매 사이클마다 claude 재호출 비용이 발생하므로 `stderr.log` 의 `discord post 실패(재시도 예정)` 메시지를 지속 모니터링하라.

### Step 4에서 주문이 실제로 실행되는 경우

deny prefix가 맞지 않는다는 의미:

```bash
# 실제 MCP 서버명 확인
claude mcp list

# settings.readonly.json deny 목록의 prefix 확인
cat "$AUTO_TRADER_REPO/.claude/settings.readonly.json" | jq '.permissions.deny[]'
```

실제 서버명이 `auto_trader_local` 이 아니라면 Task 4로 귀환하여 deny 목록의 prefix를 수정.

### 워터마크 리셋 (재처리 필요 시)

```bash
rm ~/.local/state/fill-event-triage/last_ledger_id
rm ~/.local/state/fill-event-triage/seen_ledger_ids
# 다음 poller 실행 시 전체 fill 재처리
```