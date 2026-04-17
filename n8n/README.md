# n8n Workflow Automation

auto_trader의 워크플로우 오케스트레이션을 담당하는 n8n 인스턴스.

## 구조

```
n8n/
├── data/          # n8n 런타임 데이터 (.gitignore)
├── workflows/     # 워크플로우 JSON export (Git 추적)
└── README.md
```

## 역할

- **알림 파이프라인**: 체결/분석 이벤트 수신 → Discord/Telegram 라우팅
- **헬스체크**: MCP 서버, WebSocket 연결 상태 모니터링
- **리포트**: 일일/주간 포트폴리오 요약 자동 발송
- **외부 시그널**: 경제 캘린더, 뉴스 등 수집

## 인증

n8n 2.x는 owner 기반 User Management를 사용합니다.
첫 접속 시 owner 계정 생성 화면이 표시됩니다.

- **내부 전용**: `N8N_LISTEN_ADDRESS=127.0.0.1`로 localhost만 바인딩
- 외부 접근: SSH 터널 사용 (`ssh -L 5678:localhost:5678 user@host`)

## 헬스체크

`QUEUE_HEALTH_CHECK_ACTIVE=true`로 `/healthz` 엔드포인트가 활성화되어 있습니다.

```bash
curl -f http://127.0.0.1:5678/healthz
```

Docker healthcheck + `scripts/healthcheck.sh` 모두 이 엔드포인트를 사용합니다.

## 워크플로우 관리

n8n UI에서 워크플로우 수정 후 export:

```bash
# 전체 워크플로우를 개별 JSON으로 export
docker exec auto_trader_n8n_prod n8n export:workflow \
  --all --separate --output=/home/node/.n8n/workflows/

# 호스트의 n8n/data/workflows/ → n8n/workflows/로 복사
cp n8n/data/workflows/*.json n8n/workflows/

# Git 커밋
git add n8n/workflows/ && git commit -m "chore: export n8n workflows"
```

import (복원 시):
```bash
docker exec auto_trader_n8n_prod n8n import:workflow \
  --input=/home/node/.n8n/workflows/
```

## 워크플로우 목록

### Paperclip Boss Action Queue
- **Export file**: `n8n/workflows/paperclip-boss-action-queue.json`
- **주기**: 10분 간격 + Manual Trigger
- **동작**: n8n 컨테이너 내부에서 `PAPERCLIP_CLI_COMMAND`(기본: baked-in `paperclipai`, fallback: `npx -y paperclipai`)로 issue / approval / agent 목록을 수집하고, heartbeat-runs HTTP API를 보강 호출한 뒤 Boss Action Queue 메시지를 Alfred Discord bot으로 전송
- **신호**: `manager_followup_needed`, `approval_revision_requested`, `active_issue_unassigned`, `heartbeat_missed`, `issue_review_needed`, `formal_approval_pending`, `misrouted_review`
- **필수 환경 변수**:
  - `N8N_DISCORD_BOT_TOKEN_ALFRED`
  - `PAPERCLIP_API_URL`
  - `PAPERCLIP_COMPANY_ID`
  - `PAPERCLIP_API_KEY`
  - `PAPERCLIP_BOSS_QUEUE_CHANNEL_ID`
- **중복 방지**: n8n workflow static data(`sentMap`)로 fingerprint + severity cooldown 관리
- **1차 alert tuning**:
  - `manager_followup_needed`: child update 직후 즉시 paging하지 않고 **10분 grace** 후에도 stale일 때만 알림
  - `active_issue_unassigned`: **20분 이상** assignee 없이 남은 active issue만 알림
  - `heartbeat_missed`: 우선 `CEO` / `CTO` / `Trader` / `Scout`만 감시, 나머지 역할 heartbeat는 v1에서 노이즈 방지를 위해 무시
  - `issue_review_needed`: `in_review` 상태 + user assignee + approval 없는 이슈. **30분 grace** 후 알림. 메시지에 역할 태그(`[Boss]`/`[CTO]` 등) 포함
  - `formal_approval_pending`: `in_review` 상태 + pending approval이 linked된 이슈. **30분 grace** 후 알림
  - `misrouted_review`: `in_review` 상태인데 agent만 할당 (user reviewer 없음). **30분 grace** 후 `medium` severity 알림
- **주의**:
  - 현재 workflow는 inline Node.js probe를 사용한다
  - `scripts/paperclip_cli_probe.py`는 host-side canonical/reference 구현이다
  - cold container 기준 `npx -y paperclipai` 3회 호출은 ~199초까지 늘어날 수 있어, 운영 이미지는 `Dockerfile.n8n`에서 `paperclipai`를 bake-in 하도록 전환했다

### ~~Paperclip Review/Blocked Notify~~ (deprecated)
- **폐기**: Boss Action Queue의 `issue_review_needed` / `formal_approval_pending` signal이 이 워크플로우의 역할을 대체합니다. 신규 배포 시 비활성화 권장.

### WebSocket Container Monitor
- **Export file**: `n8n/workflows/websocket-container-monitor.json`
- **Live workflow id**: `KyN2SJUCZ5QvOAKK`
- **주기**: 15분 간격
- **동작**: Upbit/KIS WebSocket heartbeat 파일을 읽어 비정상 상태 시 Discord 알림

### Paperclip Watch Alert
- **Export file**: `n8n/workflows/paperclip-watch-alert.json`
- **Trigger**: Webhook `POST /webhook/watch-alert`
- **동작**: auto_trader `OpenClawClient.send_watch_alert_to_n8n` ([ROB-173](/ROB/issues/ROB-173) / PR #540) 이 보내는 watch alert 페이로드를 받아 market (`crypto` / `kr` / `us`) 별 Discord 채널로 라우팅. `{market}:{symbol}:{condition_type}:{threshold}` fingerprint 를 workflow static data 에 저장해 **6시간 쿨다운** dedupe, 24시간 초과 엔트리는 GC.
- **응답 계약**:
  - 전송 성공 → `200 {"status":"sent","market":"...","sent_count":N,"deduped_count":M,...}`
  - 전 항목 dedupe hit → `200 {"status":"deduped","sent_count":0,"deduped_count":N,...}`
  - 잘못된 payload (unknown market, missing `triggered`, item 필드 누락) → `400 {"status":"error","error":"..."}`
  - Discord 노드 실패 (5xx / 타임아웃) → `500 {"status":"error","error":"discord_send_failed",...}` — auto_trader 가 재시도 후 record 보존
- **필수 credential (n8n UI 에서 수동 등록 후 import 된 workflow 에 매핑)**:

  | market  | credential 이름                        | 소스 env 값                |
  |---------|----------------------------------------|----------------------------|
  | crypto  | `Discord Webhook - Watch Alert Crypto` | `DISCORD_WEBHOOK_CRYPTO`   |
  | kr      | `Discord Webhook - Watch Alert KR`     | `DISCORD_WEBHOOK_KR`       |
  | us      | `Discord Webhook - Watch Alert US`     | `DISCORD_WEBHOOK_US`       |

  각 credential 은 n8n 의 `Discord Webhook account` 타입이며 `.env.prod` 에 저장된 URL 을 그대로 등록한다. Export 된 JSON 은 `REPLACE_WITH_DISCORD_WEBHOOK_*` placeholder id 를 가지므로 import 후 반드시 UI 에서 credential 을 재매핑할 것.
- **auto_trader 연동**: `.env.prod` 의 `N8N_WATCH_ALERT_WEBHOOK_URL=http://localhost:5678/webhook/watch-alert` 세팅 후 API 재시작. webhook URL 은 PR #540 / [ROB-173](/ROB/issues/ROB-173) 에서 서버측 수신 경로에 이미 연결되어 있다.
- **검증 (import 후 로컬)**:
  ```bash
  # 정상 전송 (crypto) → 200 sent
  curl -sS -X POST http://127.0.0.1:5678/webhook/watch-alert \
    -H 'content-type: application/json' \
    -d '{"alert_type":"watch","correlation_id":"00000000-0000-0000-0000-000000000001","as_of":"2026-04-17T00:00:00Z","market":"crypto","triggered":[{"symbol":"AAVE","condition_type":"rsi_above","threshold":70.0,"current":73.12}],"message":"Watch alerts (crypto)\n- AAVE rsi_above: current=73.1200, threshold=70.0000"}'

  # 동일 fingerprint 재전송 → 200 deduped
  # market 오타 → 400
  curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:5678/webhook/watch-alert \
    -H 'content-type: application/json' \
    -d '{"alert_type":"watch","correlation_id":"x","as_of":"2026-04-17T00:00:00Z","market":"nyse","triggered":[{"symbol":"AAPL","condition_type":"rsi_above","threshold":70,"current":80}],"message":"x"}'
  ```

## 런타임 계약

n8n은 낮부 전용으로 `127.0.0.1:5678`에 고정 바인딩됩니다.

## 배포 및 소유권 경계

**중요**: `n8n`은 `scripts/deploy.sh`와 별개로 관리됩니다.

- `deploy.sh`는 API stack (`docker-compose.prod.yml`)만 배포하고 검증합니다
- `n8n`은 별도 compose 파일로 수동/독립적으로 시작/중지/업데이트합니다
- `deploy.sh`는 `n8n`의 시작, 재시작, 또는 헬스체크를 수행하지 않습니다

## 배포

```bash
# 별도 compose 파일로 실행 (caddy와 동일 패턴)
docker compose -f docker-compose.n8n.yml up -d

# 로그 확인
docker compose -f docker-compose.n8n.yml logs -f

# 중지
docker compose -f docker-compose.n8n.yml down
```

`network_mode: host`이므로 auto_trader API/MCP와 localhost로 직접 통신 가능.
