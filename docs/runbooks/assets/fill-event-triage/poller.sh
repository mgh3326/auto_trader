#!/usr/bin/env bash
# fill-event → claude 트리아지 poller (운영자-호스트, 레포밖). ROB-755/ROB-926.
# 런북(docs/runbooks/fill-event-claude-triage.md) 기반 + 호스트 적응 3건:
#  (a) MARKET 미설정 시 --market 생략 = 전 시장 (kr/us/crypto)
#  (b) webhook은 prod env 파일에서 로드 (plist에 시크릿 미기재)
#  (c) claude -p는 operator 워크스페이스에서 실행 (repo에 MCP 설정 없음;
#      커맨드 2종은 operator .claude/commands→repo 심링크, deny-list는 절대경로)
#
# ROB-926: SOURCE/BROKER/ACCOUNT_MODE도 (a)와 동일하게 env로 조건부 전달한다.
# env 미설정 시 각각 websocket(기본)/생략/생략이라 기존 websocket 인스턴스와
# 바이트 수준 동일 동작(무회귀). reconciler-source(Toss 등) 체결은 별도
# FILL_TRIAGE_STATE_DIR 인스턴스로 이 스크립트를 재사용해 워터마크를 분리한다.
set -euo pipefail

REPO="${AUTO_TRADER_REPO:-$HOME/work/auto_trader}"
OPERATOR_WS="${OPERATOR_WS:-$HOME/services/auto_trader-operator}"
SETTINGS="$REPO/.claude/settings.readonly.json"
MARKET="${FILL_TRIAGE_MARKET:-}"
SOURCE="${FILL_TRIAGE_SOURCE:-websocket}"
BROKER="${FILL_TRIAGE_BROKER:-}"
ACCOUNT_MODE="${FILL_TRIAGE_ACCOUNT_MODE:-}"
PROD_ENV="$HOME/services/auto_trader/shared/.env.prod.native"
DISCORD_FILL_TRIAGE_WEBHOOK="${DISCORD_FILL_TRIAGE_WEBHOOK:-$(grep '^DISCORD_WEBHOOK_ALERTS=' "$PROD_ENV" | head -1 | cut -d= -f2-)}"
: "${DISCORD_FILL_TRIAGE_WEBHOOK:?DISCORD_FILL_TRIAGE_WEBHOOK 미설정(env/prod env 파일 확인)}"
STATE_DIR="${FILL_TRIAGE_STATE_DIR:-$HOME/.local/state/fill-event-triage}"
WATERMARK="$STATE_DIR/last_ledger_id"
SEEN="$STATE_DIR/seen_ledger_ids"
VLOG="$STATE_DIR/validation.jsonl"
DRY_RUN="${DRY_RUN:-0}"
export ENV_FILE="${ENV_FILE:-.env.prod}"   # repo CLI가 prod DB를 보도록

mkdir -p "$STATE_DIR"; touch "$SEEN"
last_id="$(cat "$WATERMARK" 2>/dev/null || true)"

cd "$REPO"
args=(--source "$SOURCE" --limit 50)
[[ -n "$MARKET" ]] && args+=(--market "$MARKET")
[[ -n "$BROKER" ]] && args+=(--broker "$BROKER")
[[ -n "$ACCOUNT_MODE" ]] && args+=(--account-mode "$ACCOUNT_MODE")
[[ -n "$last_id" ]] && args+=(--after-id "$last_id")
if ! response="$(uv run python -m scripts.list_recent_fill_events "${args[@]}")"; then
  error="$(jq -r '.error // "unknown error"' <<<"$response" 2>/dev/null || printf '%s' "$response")"
  echo "list_recent_fill_events failed: $error" >&2
  exit 1
fi
if [[ "$(jq -r '.success' <<<"$response")" != "true" ]]; then
  echo "list_recent_fill_events failed: $(jq -r '.error // "unknown error"' <<<"$response")" >&2
  exit 1
fi
fills="$(jq -c '.fills // []' <<<"$response")"

echo "$fills" | jq -c '.[]' | while read -r fill; do
  lid="$(jq -r '.ledger_id' <<<"$fill")"
  grep -qxF "$lid" "$SEEN" && continue   # 이미 처리

  payload="$(jq -r \
    '"ledger_id=\(.ledger_id) event_key=\(.event_key) broker=\(.broker) account_mode=\(.account_mode) market=\(.market) symbol=\(.symbol) side=\(.side) filled_qty=\(.filled_qty) filled_price=\(.filled_price) filled_notional=\(.filled_notional) currency=\(.currency) filled_at=\(.filled_at) correlation_id=\(.correlation_id // "")"' \
    <<<"$fill")"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] claude -p \"/fill-event-triage $payload\" --permission-mode bypassPermissions --settings $SETTINGS --output-format json"
    continue   # DRY_RUN은 상태 전진 없음 (런북 결함 수정)
  else
    if ! (cd "$OPERATOR_WS" && claude -p "/fill-event-triage $payload" \
            --permission-mode bypassPermissions \
            --settings "$SETTINGS" \
            --output-format json) > "$STATE_DIR/last_claude_out.json" 2>> "$STATE_DIR/claude_err.log"; then
      echo "claude 실패(배치 중단, 다음 폴에서 재시도): $lid — 출력 꼬리:" >&2
      tail -c 600 "$STATE_DIR/last_claude_out.json" >&2 || true
      break
    fi
    res="$(cat "$STATE_DIR/last_claude_out.json")"
    text="$(jq -r '.result' <<<"$res")"
    curl -fsS -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg c "**[fill triage] $(jq -r .symbol <<<"$fill")**"$'\n'"$text" '{content:$c}')" \
      "$DISCORD_FILL_TRIAGE_WEBHOOK" >/dev/null \
      || { echo "discord post 실패(배치 중단, 다음 폴에서 재시도): $lid" >&2; break; }
    jq -nc --arg lid "$lid" --argjson r "$res" \
      '{ledger_id:$lid, session_id:$r.session_id, cost_usd:$r.cost_usd, duration_ms:$r.duration_ms, num_turns:$r.num_turns}' >> "$VLOG"
  fi

  echo "$lid" >> "$SEEN"; tail -n 500 "$SEEN" > "$SEEN.tmp" && mv "$SEEN.tmp" "$SEEN"
  echo "$lid" > "$WATERMARK"
done
