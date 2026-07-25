# ROB-566 — watch 알림 Python(TradeNotifier) 통일 설계 스펙

- **날짜**: 2026-06-15
- **Linear**: ROB-566 (Medium) · 후속 of ROB-558(체결 Python 렌더)·ROB-560(n8n/OpenClaw 정리)
- **브랜치/워트리**: `rob-566` @ `/Users/mgh3326/work/auto_trader.rob-566` (origin/main)

## 1. 문제 / 동기

체결(fill) 알림은 ROB-558로 auto_trader 안에서 **Python `TradeNotifier`**가 직접 렌더(시장별 webhook). 그런데 **watch 알림만** 아직 `auto_trader → Prefect watch_alert_receiver(:18766) → Discord`로 한 홉 더 나간다. 사용자 지적: 일관성 없고 Prefect가 불필요하다.

### 리서치 결론 (코드/프로세스 실측)
- **Prefect watch 이벤트 `auto_trader.watch.triggered` 소비처 0** (dead). Prefect 경로의 유일한 오케스트레이션 기능이 죽은 코드 → 사실상 Discord 렌더러.
- prod **"Hermes" = Prefect 수신기**(`HERMES_WEBHOOK_URL`→:18766). watch 트리거 평가 LLM 없음(`ReviewTriggerPayload`는 outcome까지 계산된 닫힌 스냅샷). 향후 LLM 리뷰 로드맵 증거 없음.
- **TaskIQ 워커가 이미 TradeNotifier 설정**(`app/core/taskiq_broker.py` WorkerInitMiddleware, webhook 4개). watch 스캐너가 그 워커에서 돌므로 신규 인프라 0.
- 대가 없는 운영비: 별도 uvicorn/포트/토큰/plist/Prefect API 의존/네트워크 홉.

## 2. 핵심 통찰 — 이미 존재하는 seam: `HermesDeliveryResult`

스캐너의 alert 상태머신은 **전달 결과 3-way에만** 의존한다:
- `app/services/hermes_client.py:298` `HermesDeliveryResult(status: success|skipped|failed, http_status, reason)`
- `send_review_trigger(payload) -> HermesDeliveryResult` (line 330). `HERMES_ENABLED=False`면 `skipped`.
- 호출처 ①: `investment_watch_scanner.py:219` → `_record_delivery_outcome`(line 399): `delivery.status == "success"` 일 때만 `delivery_status="delivered"` + alert→`triggered`; 아니면 active 유지(재시도).
- 호출처 ②: `watch_validity_review.py:280` 동일 인터페이스.

→ **전송 수단(HTTP webhook vs in-process render)은 `send_review_trigger` 내부 디테일**이다. 결과 타입(`HermesDeliveryResult`)을 보존하면 **두 호출처와 상태머신은 무변경**. 이게 안전한 swap의 핵심.

## 3. 설계 (D안 — in-process 렌더 + 결과계약 보존)

### 3-1. 신규 렌더링 (체결 포맷터 동형)
- `app/monitoring/trade_notifier/formatters_discord.py`: `format_investment_watch_trigger(payload, *, display_name) -> DiscordEmbed`.
  - 제목 예: `🔔 워치 트리거 · {display_name} ({symbol})`, 색=시장/intents 기반.
  - 필드: 구분(metric/operator/threshold), 현재값(current_value), outcome(notified/review_required/preview_attached/executed → 한글), price_guidance(진입검토가/한도범위/무효화), 체결가 아님. **딥링크**: invest_links.report_path/stock_path → embed.url(클릭) + 필드. trigger_checklist(있으면). planned_action 요약(있으면).
- `formatters_telegram.py`: `format_investment_watch_trigger_telegram(...)` (마크다운 + 딥링크).

### 3-2. TradeNotifier 디스패치
- `TradeNotifier.notify_investment_watch(payload) -> bool`: `resolve` display_name → embed/telegram 빌드 → `_dispatch(embed, telegram_msg, market_type=payload.market)`. **시장별 채널 라우팅**(payload.market → DISCORD_WEBHOOK_KR/US/CRYPTO; 매핑 불가 시 alerts). (user 결정: 시장별 분리.) Telegram fallback은 `_dispatch`로 무료.

### 3-3. 전송 분기 (seam은 `send_review_trigger` 내부)
- 신규 config `WATCH_NOTIFY_TRANSPORT: Literal["hermes_webhook","python_direct"] = "hermes_webhook"` (default=현행, 안전).
- `HermesNotificationClient.send_review_trigger(payload)`:
  - `transport=="python_direct"` → `get_trade_notifier().notify_investment_watch(payload)` 호출, `True`→`HermesDeliveryResult(status="success", http_status=None)`, `False`→`HermesDeliveryResult(status="failed", reason="discord_render_failed")`. (webhook 안 탐.)
  - else(default) → 현행 HTTP POST 경로 그대로.
  - `HERMES_ENABLED` 의미: python_direct에선 무관(Discord webhook 설정이 게이트). 단 안전을 위해 python_direct일 때 webhook 미설정이면 `skipped` 반환(현행 skip 의미 보존, alert active 유지).
- **호출처 2곳·상태머신 무변경** (계속 `send_review_trigger`→`HermesDeliveryResult` 소비).

### 3-4. 컷오버 + Prefect 폐기
- 플래그 default=`hermes_webhook` → 머지해도 동작 불변. operator가 `WATCH_NOTIFY_TRANSPORT=python_direct`로 전환(병행 관찰: 잠시 양쪽 비교 후 Prefect 측 mute/중단).
- 패리티 확인 후 operator가 **Prefect watch 수신기 폐기**: launchd `dev.robinco.prefect.watch-alert-receiver.plist` 중단, `.env` WATCH_ALERT_* 정리, (Prefect repo) 죽은 `emit_watch_alert_event`/`watch_alert_router` 정리. ⚠️ Prefect repo는 별도 레포 — auto_trader PR 범위 밖, operator/cross-repo.
- ⚠️ 수신기 폐기 전 확인: `:18766`은 review-trigger + **news-relevance-judgment**도 받음 → news-relevance가 같은 포트/앱을 쓰면 watch만 떼고 앱은 유지(폐기 범위 정밀화는 operator).

## 4. 안전 경계 / 비목표
- **결과계약 보존**: `HermesDeliveryResult` 3-way 불변 → alert 상태머신/재시도/audit 컬럼 무변경.
- default 플래그=현행 → 머지 자체는 behavior 불변(opt-in 전환).
- **마이그레이션 0**, 브로커/주문 mutation 없음.
- 비목표: Prefect repo 코드 삭제(operator/cross-repo), `HermesNotificationClient` 개명(이름은 잔존 — 후속), news-relevance 경로 변경.

## 5. 테스트 계획
- `format_investment_watch_trigger`(discord/telegram): outcome별 라벨, 딥링크 embed.url, price_guidance/trigger_checklist 표기/생략, 시장별 색.
- `notify_investment_watch`: market→webhook 라우팅(kr/us/crypto/fallback), Telegram fallback, disabled no-op.
- `send_review_trigger` 분기: transport=python_direct → TradeNotifier 호출+결과 매핑(True→success/False→failed/webhook미설정→skipped); transport=hermes_webhook → 기존 HTTP 경로 불변(기존 테스트 유지).
- 회귀: 호출처 2곳·`_record_delivery_outcome` 무변경 확인(상태머신 테스트 그린).

## 6. 미해결 (스펙 리뷰)
1. Telegram fallback: python_direct에서 활성(추천) vs Discord-only 패리티.
2. embed 필드 구성 최종(price_guidance/checklist/planned_action 포함 범위).
3. 플래그 이름 `WATCH_NOTIFY_TRANSPORT` vs 단순 bool `WATCH_NOTIFY_PYTHON_DIRECT`.
4. Prefect 폐기 타이밍: 본 PR은 플래그만(operator 컷오버) vs 동시. (추천: 플래그만, operator 전환)
5. `HermesNotificationClient`/`HERMES_*` 네이밍 잔존 — 후속 개명 이슈로 분리(추천).
