# Phase 2 · Slice 2c — OpenClaw → AgentGateway 개명 + 죽은 watch 메서드 삭제 Implementation Plan (ROB-560)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. 개명 + 죽은코드 삭제 — 게이트는 **collect-only 무손상 + screener/daily_scan 경로 테스트 그린 + `grep openclaw` 잔존 0(과거 DB 행 제외)**.

**Goal:** 외부 AI 에이전트 게이트웨이 `OpenClawClient` → `AgentGatewayClient`로 개명(범용 Agent 명), Hermes로 대체된 죽은 watch 메서드 삭제, 휴면 분석 경로(`request_analysis`/`send_scan_alert`)는 개명 유지.

**Architecture:** OpenClaw·Hermes 둘 다 외부 AI 에이전트. watch-alert는 이미 `hermes_client`로 이전됨 → openclaw watch 메서드는 죽은 코드. 분석 게이트웨이(screener)·스캔 알림(daily_scan)은 휴면이나 살아있는 경로 → 범용 이름으로 보존.

**⚠️ 게이트:** **ROB-558(#1298) 머지 후** 시작. 본 슬라이스는 ROB-558이 수정한 `openclaw_client.py`를 같이 수정 → 머지 후 새 main 기준(아래 라인/메서드는 post-558 상태 기준). 외부 에이전트 callback URL 변경은 operator 조율(Task 5).

**스펙:** `docs/superpowers/specs/2026-06-15-phase2-n8n-decommission-agent-naming-design.md` §3 (2c), §6.2(AgentGatewayClient)·§6.5(휴면=개명유지)

### 결정 (확정)
- 이름: `AgentGatewayClient` (`app/services/agent_gateway.py`), config `OPENCLAW_* → AGENT_GATEWAY_*`.
- 휴면 분석 경로: **개명 유지**(`request_analysis`, `send_scan_alert`).
- 죽은 watch: **삭제**(`send_watch_alert`, `send_watch_alert_to_router`, `_send_market_alert` watch 분기, `WatchAlertDeliveryResult`, `_resolve_watch_alert_url`, config `WATCH_ALERT_ROUTER_URL`/`N8N_WATCH_ALERT_WEBHOOK_URL`).
- callback path: 신규 `/api/v1/agent/callback` + 구 `/api/v1/openclaw/callback` **백워드-호환 alias** 유지(operator가 에이전트 config 전환 후 별도 제거). (§6.3 기본값)
- `model_name`: **"openclaw-gpt" 유지**(과거 행 쿼리 일관성; §6.4 기본값). 신규 행도 동일.

---

## Task 1: 죽은 watch 메서드/심볼 삭제

**Files:** `app/services/openclaw_client.py` (아직 개명 전)

- [ ] **Step 1: 삭제**

`openclaw_client.py`에서 제거:
- `class WatchAlertDeliveryResult` (+ `__post_init__`)
- `_resolve_watch_alert_url()` 함수
- `OpenClawClient.send_watch_alert_to_router()` 메서드
- `OpenClawClient.send_watch_alert()` (deprecated) 메서드
- `_send_market_alert()` 내 watch 분기/`category=="watch"` 관련 분기(스캔 경로만 남김). 메서드가 scan 전용이 되면 시그니처 단순화 가능(단 `send_scan_alert` 동작 불변 유지).

- [ ] **Step 2: 죽은 watch 참조 0 확인**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-558   # (post-558 worktree; 실제로는 머지된 main 기준)
grep -rn 'send_watch_alert\|send_watch_alert_to_router\|WatchAlertDeliveryResult\|_resolve_watch_alert_url' app/ websocket_monitor.py scripts/
```
Expected: 0 (테스트 제외 — 테스트는 Task 6에서 정리).

- [ ] **Step 3: 임시 테스트 통과 확인(개명 전)**

Run: `uv run pytest tests/test_openclaw_client.py -q 2>&1 | tail -3`
Expected: watch 테스트 제거/실패 → Task 6에서 정리. (이 시점 RED 허용; Task 6에서 GREEN)

---

## Task 2: config 개명 (OPENCLAW_* → AGENT_GATEWAY_*) + 죽은 watch config 삭제

**Files:** `app/core/config.py`, `env.example`

- [ ] **Step 1: config 키 개명**

`app/core/config.py`에서:
```
OPENCLAW_WEBHOOK_URL          -> AGENT_GATEWAY_URL
OPENCLAW_TOKEN                -> AGENT_GATEWAY_TOKEN
OPENCLAW_ENABLED             -> AGENT_GATEWAY_ENABLED
OPENCLAW_CALLBACK_URL        -> AGENT_GATEWAY_CALLBACK_URL
OPENCLAW_CALLBACK_TOKEN      -> AGENT_GATEWAY_CALLBACK_TOKEN
OPENCLAW_SCREENER_CALLBACK_URL -> AGENT_GATEWAY_SCREENER_CALLBACK_URL
```
**삭제**(죽은 watch): `WATCH_ALERT_ROUTER_URL`, `N8N_WATCH_ALERT_WEBHOOK_URL`.

- [ ] **Step 2: env.example 동기화**

env.example의 `OPENCLAW_*` → `AGENT_GATEWAY_*` 개명, `WATCH_ALERT_ROUTER_URL`/`N8N_WATCH_ALERT_WEBHOOK_URL` 삭제.
> ⚠️ operator: 운영 `.env.prod.native`의 `OPENCLAW_*` 키도 동일 개명 필요(런북/체크리스트에 기재).

---

## Task 3: 파일/클래스 개명 (openclaw_client → agent_gateway)

**Files:**
- Move: `app/services/openclaw_client.py` → `app/services/agent_gateway.py`
- Modify: 내부 심볼 + 모든 importer

- [ ] **Step 1: git mv + 클래스/심볼 개명**

```bash
git mv app/services/openclaw_client.py app/services/agent_gateway.py
```
`agent_gateway.py` 내부:
- `class OpenClawClient` → `class AgentGatewayClient`
- `_build_openclaw_message` → `_build_agent_message`, `_build_openclaw_retrying` → `_build_agent_retrying`, `OPENCLAW_RETRY_*` → `AGENT_GATEWAY_RETRY_*`
- `settings.OPENCLAW_*` → `settings.AGENT_GATEWAY_*` (Task 2 키명)
- docstring "OpenClaw Gateway" → "External AI agent gateway (formerly OpenClaw)"

- [ ] **Step 2: importer 갱신**

```bash
grep -rl 'openclaw_client\|OpenClawClient' app/ tests/ websocket_monitor.py scripts/ \
  | xargs sed -i '' -e 's/openclaw_client/agent_gateway/g' -e 's/OpenClawClient/AgentGatewayClient/g'
```
주요 live importer: `app/jobs/daily_scan.py`(`self._openclaw = AgentGatewayClient()` — 변수명 `_openclaw`는 선택적으로 `_agent`로 정리), `app/services/screener_service.py`(`_get_openclaw`→선택적 `_get_agent`). 변수/헬퍼명 정리는 동작 불변 범위에서.

- [ ] **Step 3: 잔존 확인**

Run: `grep -rn 'openclaw_client\|OpenClawClient' app/ tests/ websocket_monitor.py scripts/`
Expected: 0.

---

## Task 4: daily_scan / screener 변수·주석 정리 (선택, 혼란 제거)

**Files:** `app/jobs/daily_scan.py`, `app/services/screener_service.py`

- [ ] **Step 1: alert_mode 리터럴 `"openclaw_only"` 처리**

`daily_scan.py`의 `alert_mode: Literal["both","telegram_only","openclaw_only","none"]` — `"openclaw_only"` → `"agent_only"`로 개명(호출처/테스트 동기화). 외부에서 이 문자열을 주입하는 곳(operator config/test) 확인 후 일괄.
> 외부 주입이 불확실하면 이번엔 리터럴 유지하고 주석만 정정(동작 안전). 플랜 실행자가 호출처 grep로 결정.

- [ ] **Step 2: 검증**

Run: `uv run pytest tests/ -k "daily_scan or screener" -q 2>&1 | tail -3`
Expected: 그린.

---

## Task 5: callback 라우터 개명 + 백워드-호환 alias

**Files:**
- Move: `app/routers/openclaw_callback.py` → `app/routers/agent_callback.py`
- Modify: `app/main.py`(라우터 import/등록), 라우트 path

- [ ] **Step 1: git mv + path 추가**

```bash
git mv app/routers/openclaw_callback.py app/routers/agent_callback.py
```
- 라우트: `@router.post("/api/v1/agent/callback")` 신규 + `@router.post("/api/v1/openclaw/callback")` **alias 유지**(동일 핸들러). config는 `AGENT_GATEWAY_CALLBACK_TOKEN`.
- `model_name="openclaw-gpt"` **유지**(§6.4).
- `main.py`의 `openclaw_callback` import/등록 → `agent_callback`.

- [ ] **Step 2: 검증**

Run: `uv run python -c "import app.main"` + 두 path 모두 라우트 테이블 존재 확인(테스트 Task 6).
> ⚠️ operator: 외부 에이전트의 callback URL을 신규 path로 전환 후, 후속 PR에서 alias 제거.

---

## Task 6: 테스트 개명/정리

**Files:** `tests/test_openclaw_client.py`, `tests/test_openclaw_callback.py`, `tests/test_openclaw_callback_auth.py`

- [ ] **Step 1: 개명 + watch 테스트 삭제**

```bash
git mv tests/test_openclaw_client.py tests/test_agent_gateway.py
git mv tests/test_openclaw_callback.py tests/test_agent_callback.py
git mv tests/test_openclaw_callback_auth.py tests/test_agent_callback_auth.py
```
- `test_agent_gateway.py`: import/심볼 개명(`OpenClawClient`→`AgentGatewayClient` 등), **watch 테스트(send_watch_alert/send_watch_alert_to_router) 삭제**.
- `test_agent_callback*.py`: path를 `/api/v1/agent/callback`로 갱신 + alias `/api/v1/openclaw/callback` 동작 테스트 1건 추가.
- config 참조 `OPENCLAW_*`→`AGENT_GATEWAY_*` 일괄.

- [ ] **Step 2: 잔존 확인 + 테스트**

```bash
grep -rn 'openclaw\|OpenClaw\|OPENCLAW' app/ tests/ websocket_monitor.py scripts/ | grep -vi 'openclaw-gpt'
uv run pytest tests/test_agent_gateway.py tests/test_agent_callback.py tests/test_agent_callback_auth.py -q 2>&1 | tail -3
```
Expected: grep 0(과거 model_name `openclaw-gpt` 제외), 테스트 그린.

---

## Task 7: 전체 검증

- [ ] **Step 1: 린트/타입/collect/앱import**

```bash
uv run ruff format app/ tests/ ; uv run ruff check app/ tests/ ; uv run ty check app/
uv run pytest tests/ --collect-only -q 2>&1 | tail -3
uv run python -c "import app.main"
```
Expected: 클린, collect 에러 0.

- [ ] **Step 2: 살아있는 경로 회귀**

Run: `uv run pytest tests/ -k "agent_gateway or agent_callback or daily_scan or screener" -q 2>&1 | tail -3`
Expected: 그린.

- [ ] **Step 3: 커밋**

```bash
git add -A
git commit -m "refactor(ROB-560): rename OpenClaw -> AgentGateway, drop Hermes-superseded watch methods

OpenClawClient -> AgentGatewayClient (app/services/agent_gateway.py),
OPENCLAW_* -> AGENT_GATEWAY_* config. Delete dead watch methods
(send_watch_alert[_to_router], superseded by hermes_client). Keep dormant
analysis paths (request_analysis/send_scan_alert) under generic agent name.
Callback path /api/v1/agent/callback (+ /openclaw alias for cutover).

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## 자체 점검 (작성자 — 완료)
- **스펙 커버리지:** 죽은 watch 삭제(Task1)·config 개명(Task2)·파일/클래스 개명(Task3)·소비처(Task4)·callback(Task5)·테스트(Task6). §6.2/§6.5 결정 반영.
- **타입 일관성:** `AgentGatewayClient`·`AGENT_GATEWAY_*` 전 Task 일치. 백워드-호환 callback alias.
- **플레이스홀더:** 없음. 라인 번호는 post-558 기준(머지 후 재확인).

## 미해결 (구현 중/operator)
- 운영 `.env.prod.native` OPENCLAW_* → AGENT_GATEWAY_* 전환(operator, 런북).
- 외부 에이전트 callback URL 전환 후 `/api/v1/openclaw/callback` alias 제거(후속).
- `alert_mode="openclaw_only"` 리터럴 외부 주입처 확인 후 `agent_only` 전환 여부.
- request_analysis/send_scan_alert 장기적으로 Hermes/in-process 대체 시 완전 삭제(별도).
