# Phase 2 — n8n 디커미션 + OpenClaw→Agent 네이밍 정리 (설계 스펙, ROB-560)

- **날짜**: 2026-06-15
- **Linear**: ROB-560 (Medium) · 후속 of ROB-558(#1298 체결 알림 Python 이전)
- **워트리/브랜치**: `rob-560` @ `/Users/mgh3326/work/auto_trader.rob-560` (origin/main 기준)
- **구현 게이트**: 2c는 ROB-558 머지 후(같은 `openclaw_client.py` 수정). 2a/2b는 ROB-558과 무충돌.

---

## 1. 배경 / 동기

사용자 목표: **"n8n 관련 코드는 이제 정리하는게 맞아보여, 혼란만 주는 것 같아."** ROB-558에서 체결→n8n 경로를 제거했고, 남은 n8n/OpenClaw 잔재를 정돈한다.

### 1-1. 프로젝트 정체 (네이밍 근거)

- **OpenClaw** (github.com/openclaw/openclaw): self-hosted *personal AI assistant* 프레임워크. 게이트웨이가 메신저 채널 + AI 에이전트를 잇는 control plane. → auto_trader에서 OpenClaw는 **외부 AI 에이전트 게이트웨이**(분석 위임: `POST /hooks/agent`).
- **Hermes** (github.com/nousresearch/hermes-agent): Nous Research의 self-improving *AI agent* 프레임워크. → auto_trader에서 Hermes는 **watch-trigger 리뷰 알림**을 받는 현재의 구체 에이전트(`hermes_client`).

둘 다 **외부 AI 에이전트**다. auto_trader는 결정적 evidence/data 레이어이고 판단은 외부 에이전트에 위임한다. 따라서 OpenClaw 자리의 범용 개념은 **"Agent"** 다 (LLM 아님 — 벤더/모델 바뀌어도 유효).

### 1-2. 핵심 발견 (코드 grounded, Phase 2 디스커버리)

1. **OpenClaw ≠ Hermes** — 별개. watch-alert 조각은 이미 OpenClaw→Hermes 이전 완료(`investment_watch_scanner` → `hermes_client.send_review_trigger`가 live). OpenClaw로 hermes를 개명하면 **혼란 재발**.
2. **"n8n 삭제"는 두 부류**:
   - **죽은 HTTP 표면**: `/api/n8n/*` 라우터(19개) — 소비처 0 (n8n 은퇴, Prefect repo `grep '/api/n8n'` 0건). router-전용 서비스.
   - **⚠️ 살아있는데 이름만 `n8n_`**: 아래 표. 통째 삭제하면 **execution_ledger 정산 / 장중 리뷰 / `/invest/stocks` 종목상세**가 깨진다.
3. **OpenClaw 대부분 휴면**: `OPENCLAW_ENABLED=false`, `DAILY_SCAN_ENABLED=false` 기본 off. watch 메서드(`send_watch_alert`, `send_watch_alert_to_router`, `_send_market_alert` watch 분기)는 Hermes로 대체됨.

### 1-3. 살아있는 `n8n_*` 데이터 서비스 (개명 대상, 삭제 금지)

| 모듈 | 사용처(live) | 비고 |
|---|---|---|
| `app/services/n8n_filled_orders_service.py` (`fetch_filled_orders`) | `execution_ledger/reconciler.py`, `invest_view_model/stock_detail_orders_service.py`(`/invest/stocks`) | **체결 알림이 링크하는 그 페이지** |
| `app/services/n8n_pending_orders_service.py` (`fetch_pending_orders`) | `jobs/intraday_order_review.py` | 장중 리뷰 |
| `app/services/n8n_market_context_service.py` (`fetch_market_context`) | `n8n_pending_orders_service`(→ intraday 경유 live) | 기술지표 |
| `app/services/n8n_formatting.py` | 위 live 모듈들이 사용 | 포맷 유틸 |

> 정확한 live-vs-dead 경계는 플랜에서 모듈별 import 그래프로 확정한다(transitive 포함).

---

## 2. 네이밍 결정

| 대상 | 결정 | 새 이름(제안) |
|---|---|---|
| 살아있는 `n8n_*` **데이터** 서비스 | `n8n_` 접두사 제거 → **데이터 역할명** ("agent" 아님) | `filled_orders_service`, `pending_orders_service`, `market_context_service`, `order_brief_formatting`(=구 n8n_formatting) |
| 외부 **AI 에이전트** 게이트웨이 (`OpenClawClient`) | 범용 **Agent** 명 + 죽은 메서드 삭제 | `AgentGatewayClient` (`app/services/agent_gateway.py`) |
| OpenClaw config (`OPENCLAW_*`) | 범용 agent 네임스페이스 | `AGENT_GATEWAY_URL/_TOKEN/_ENABLED/_CALLBACK_*` |
| OpenClaw callback 라우터 (`/api/v1/openclaw/callback`) | agent 네임스페이스 | `/api/v1/agent/callback` (또는 유지 — §6 결정) |
| `StockAnalysisResult.model_name="openclaw-gpt"` | 신규 행만 영향 | `"agent-gpt"` 또는 유지(과거 행 보존) — §6 결정 |
| `hermes_client` | **유지** (현재 구체 에이전트, 정확) | 변경 없음 |

**왜 "hermes"로 통일하지 않는가**: Hermes는 이미 watch-trigger 리뷰 전용의 구체 개념(`hermes_client`)이다. OpenClaw(분석 게이트웨이)를 hermes로 개명하면 두 개념이 섞여 — 제거하려는 바로 그 혼란이 재발한다. 범용 `AgentGateway`는 OpenClaw→Hermes→미래 벤더 전환에도 유효하다.

---

## 3. 슬라이스 (각각 독립 PR)

### 2a — 살아있는 `n8n_*` 데이터 서비스 개명 (저위험, ROB-558 무충돌)
- 파일 rename + 모든 import 경로 갱신. **behavior 완전 불변** (순수 rename/이동).
- 대상: `n8n_filled_orders_service`→`filled_orders_service`, `n8n_pending_orders_service`→`pending_orders_service`, `n8n_market_context_service`→`market_context_service`, `n8n_formatting`→`order_brief_formatting`(이름은 플랜에서 확정).
- 테스트 파일도 rename + import 갱신. 동작 동일 검증.
- ROB-558과 다른 파일이라 충돌 없음 → ROB-558 머지 전에도 가능(단 순서 존중 시 머지 후).

### 2b — 죽은 n8n HTTP 표면 삭제 (operator-gated)
- `app/routers/n8n.py`(886줄)·`app/routers/n8n_scan.py`(94줄) + `main.py` 등록 제거.
- router-전용 서비스 삭제: `n8n_crypto_scan_service`, `n8n_news_service`, `n8n_trade_review_service`, `n8n_pending_snapshot_service`, `n8n_pending_review_service`, `n8n_daily_brief_service`(+`_portfolio`/`_rendering`), `n8n_kr_morning_report_service`, `n8n_sell_signal_service` 등 — **단, 2a에서 살아남은 데이터 서비스에 대한 의존만 끊고 그 데이터 서비스 자체는 유지**.
- 미들웨어 auth `/api/n8n/*` 분기(`app/middleware/auth.py`), `N8N_API_KEY` config, `docker-compose.n8n.yml`, `n8n/` 디렉터리(workflows, README) 삭제.
- n8n 테스트 파일 삭제(라우터/서비스 단위).
- **operator 게이트**: 삭제 전 `/api/n8n/*` 외부 소비처 0 확인(잔존 n8n 컨테이너/수동 호출/타 도구). uncertain으로 분류된 엔드포인트(daily-brief, trade-reviews, crypto-scan, kr-morning-report, news, sell-signal, scan/* 등)는 operator 확인 후 삭제.

### 2c — OpenClaw → AgentGateway 개명 + 죽은 메서드 삭제 (ROB-558 머지 후)
- `openclaw_client.py` → `agent_gateway.py`, `OpenClawClient` → `AgentGatewayClient`.
- **삭제**(Hermes 대체): `send_watch_alert`, `send_watch_alert_to_router`, `_send_market_alert`의 watch 분기, `N8N_WATCH_ALERT_WEBHOOK_URL`/`WATCH_ALERT_ROUTER_URL`(openclaw 경유분 — hermes_client가 watch 담당).
- **유지+개명**(휴면이나 살아있는 경로): `request_analysis`(스크리너), `send_scan_alert`(daily_scan). config `OPENCLAW_*`→`AGENT_GATEWAY_*`.
- callback 라우터·`model_name` 처리는 §6 결정.
- ROB-558이 같은 `openclaw_client.py`를 수정했으므로 **머지 후** 새 main 기준.

---

## 4. 안전 경계 / 비목표

- **behavior 불변(2a/2c 개명분)**: 순수 rename. 런타임 동작·payload·엔드포인트 동작 동일(2b의 삭제 제외).
- **삭제 금지 목록**: 살아있는 데이터 서비스(filled/pending/market_context), `hermes_client`, `investment_watch_scanner` 경로, execution_ledger reconciler.
- **operator-gated**: 2b의 모든 삭제는 외부 소비처 0 확인 후. uncertain 엔드포인트는 단독 확인.
- **마이그레이션 0** (DB 스키마 무변경). `model_name` 과거 행은 보존.
- **비목표**: hermes_client 통합(별도), OpenClaw 분석 게이트웨이의 in-process 대체, Prefect watch_alert_receiver 실배선.

---

## 5. 테스트 / 검증

- 2a: rename 후 `ruff`/`ty`/영향 테스트 그린 + `pytest --collect-only` import 무손상 + `grep -rn 'n8n_filled_orders_service\|n8n_pending_orders_service\|...'` 잔존 0(살아있는 것 한정).
- 2b: 삭제 후 collect-only 무손상 + `/api/n8n` 라우트 부재 + 살아있는 데이터 서비스 테스트 유지 그린.
- 2c: rename 후 그린 + `grep -rn 'openclaw\|OpenClaw\|OPENCLAW'` 잔존 0(과거 DB 행 제외) + screener/daily_scan 경로 테스트.

---

## 6. 미해결 (스펙 리뷰에서 결정)

1. **데이터 서비스 새 이름 확정**: `filled_orders_service` vs `order_fills_service`? `order_brief_formatting` vs `brief_formatting`?
2. **AgentGateway 새 이름**: `AgentGatewayClient`(추천) vs `AiAgentClient` vs `AutonomousAgentClient`.
3. **callback 라우터 경로** `/api/v1/openclaw/callback` → 개명(`/api/v1/agent/callback`, 외부 OpenClaw 콜백 URL 영향) vs 유지(하위호환). config `OPENCLAW_CALLBACK_URL` 영향.
4. **`model_name="openclaw-gpt"`**: 신규 행 `"agent-gpt"`로 변경 vs 유지(쿼리 일관성). 과거 행은 불변.
5. **request_analysis/send_scan_alert(휴면)**: 개명 유지 vs 완전 삭제(operator 사인오프). 삭제 시 callback 라우터·screener `request_report`·daily_scan alert도 함께.
6. **슬라이스 순서/PR 수**: 2a 단독 먼저(머지 전 가능) vs 전부 ROB-558 머지 후.
