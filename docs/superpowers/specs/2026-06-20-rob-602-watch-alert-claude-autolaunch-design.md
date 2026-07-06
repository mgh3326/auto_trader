# ROB-602 — watch-alert 발화 → Claude Code 컨텍스트-보존 자동 기동 (설계/결정 기록)

- **Linear**: [ROB-602](https://linear.app/mgh3326/issue/ROB-602) (Spike)
- **작성일**: 2026-06-20
- **상태**: 설계 확정 (decision + design doc). 구현은 후속 이슈.
- **세션 스코프**: 방향 결정 + 6개 열린질문 해소 + 본 설계문서 + Linear 업데이트. **이번 세션에 코드 변경 없음.**

---

## 1. 문제

watch 알림이 발화하면 현재 **Discord로만**(사람에게) 통지된다. 운영자는 이 알림이
**Claude Code(에이전트)에게도 닿아서**, 그동안 쌓은 매매 맥락(보유/미체결/watch/전략 흐름)을
유지한 채 분석·다음 액션을 제안하길 원한다.

핵심 난점: 알림에서 `claude -p "..."`(headless print)를 그냥 호출하면 **새 세션이라
인터랙티브 세션의 누적 맥락을 잃는다.** "맥락을 어떻게 보존해서 깨울지"가 미해결이었다.

본 spike의 결론: **맥락의 ~90%는 인터랙티브 세션이 아니라 지속 아티팩트
(`get_operating_briefing` / `investment_report` / `session_context` / `memory` / `CLAUDE.md`)에
이미 산다.** 따라서 신선한 런이 이것들을 부트스트랩으로 읽으면 충분하다 — 비싼 세션 resume이 불필요.

---

## 2. 결정 요약 (TL;DR)

| # | 결정 항목 | 확정 | 근거 |
|---|---|---|---|
| 1 | 기동 방향 | **A안: 신선 `claude -p` + 지속 아티팩트 부트스트랩** | 맥락 ~90%가 지속 아티팩트에 존재(코드 확증). B(resume)는 운영 세션이 커서 토큰 폭탄, C(클라우드 routine)는 ≥1h 간격+auto-approve라 실시간 부적합 |
| 2 | 기동 글루 위치 | **운영자-호스트 poller + 레포 read surface** | 운영자의 CC 환경(memory/skills/MCP)은 운영자 머신에 삶 → 서버측 Prefect 워커에서 기동하면 다른 환경. scanner가 5분 cadence라 폴링 지연 무의미 |
| 3 | 출력 경로 | **Discord 회신 + session_context 적재** | session_context append가 A안의 "신선=맥락손실" 약점을 자가치유(다음 신선 런이 연속성 회복, resume 토큰비용 없이) |
| 4 | 안전 게이트 | **하드 read-only 프로파일** | 알림 기동 런은 분석+dry_run까지만. 실주문은 사람이 인터랙티브 세션에서 확정 |
| 5 | 부트스트랩 패키징 | **슬래시 커맨드 파일** (`.claude/commands/crypto-alert-triage.md`) | poller가 명시 호출하므로 풀 스킬의 자동발동 기계장치 불필요. 가볍고·버전관리·리뷰 가능 |
| 6 | 네이티브 vs 레포밖 | **poller는 레포밖 ops 스크립트, 레포는 read-only 추가만** | auto_trader 안전경계(순수 read, 브로커 mutation 없음) 유지 |

---

## 3. 검증된 빌딩블록 (코드 grounding, 2026-06-20)

본 spike는 5개 빌딩블록을 실제 코드에서 검증했다. 전부 실재(하나만 partial).

| 블록 | 상태 | 핵심 파일 |
|---|---|---|
| **ROB-516** session_context | ✅ 완전구현 | `app/mcp_server/tooling/session_context_tools.py:28-91`, `app/models/session_context.py:25-107`, `app/services/session_context.py:19-73`. `review.operator_session_context` 테이블. entry_type 8종(plan/decision/deferred/rejected_candidate/constraint/open_question/next_action/handoff_note), market(kr/us/crypto)+account_scope 필터. TTL 없음 |
| **ROB-413/500** watch-alert 파이프라인 | ✅ 가동중 | `app/jobs/investment_watch_scanner.py:120-231`(Prefect 5분), `app/services/hermes_client.py:255-380`(`ReviewTriggerPayload`), `app/monitoring/trade_notifier/notifier.py:288-304`(Discord/Telegram) |
| **ROB-514** watch 실행계획 | ✅ 완전구현 | `app/models/investment_reports.py:329-334,536-541`(`max_action`+`trigger_checklist` JSONB), `app/schemas/investment_reports.py:203-228`. alert payload(`ReviewTriggerPayload.planned_action`+`trigger_checklist`)에 재검토 맥락 내장 |
| **briefing + report** | ✅ 완전구현 | `app/mcp_server/tooling/operating_briefing.py:242-378`(`get_operating_briefing`), `app/mcp_server/tooling/investment_reports_handlers.py:697-705`(`investment_report_get`), `:936-968`(`investment_report_context_get`) |
| **부트스트랩 자료** (skill/hook) | ⚠️ partial | infra(briefing 툴/session_context/안전패턴)는 존재. `/crypto-alert-triage` 커맨드, SessionStart hook은 **미존재**(`.claude/skills/` 비어있음, `.claude/hooks/`엔 check-gstack.sh만) → 본 설계의 신규 작업물 |

---

## 4. 아키텍처

5개 컴포넌트. 기존 watch-alert 파이프라인 위에 read surface 1개 + 운영자-호스트 글루를 얹는다.

```
[기존] InvestmentWatchScanner (Prefect 5분) → 조건충족 → InvestmentWatchEvent 생성
        → Hermes/TradeNotifier → Discord (delivery_status='delivered')
                                          │
[신규①] 레포 read surface ←──────────────┘  (이벤트가 DB에 delivered로 적재됨)
        investment_watch_events_list_recent(delivery_status='delivered', since)   ※순수 read
                                          │
[신규②] 운영자-호스트 poller (launchd/cron ~1분, 레포밖 ops 스크립트)
        last-seen delivered_at 워터마크 + event_uuid 디듀프
        새 fire마다 ↓
[신규③+④] claude -p --settings .claude/settings.readonly.json \
              "/crypto-alert-triage <event 요약>"                                 ※하드 read-only
                                          │
[신규③] 부트스트랩 (.claude/commands/crypto-alert-triage.md):
        1) get_operating_briefing(market)        — 보유+미체결+활성watch+최신리포트+session_context
        2) investment_report_get(source_report_uuid) — trigger_checklist/max_action/근거 복원
        3) session_context_get_recent(market)    — 직전 트리아지/결정 핸드오프
        4) 발화 분석 + dry_run 미리보기(ladder preview 등 read-only)
        5) 출력 ↓
[신규⑤] 출력 경로:
        a) session_context_append(entry_type='decision', refs={alert_uuid,event_uuid,symbols})
        b) --output-format json | jq .result → Discord 회신(운영자가 받은 그 알림 채널)
        → 사람이 인터랙티브 세션에서 실주문 확정
```

---

## 5. 컴포넌트 상세 명세

### ① `investment_watch_events_list_recent` — read surface (레포, 신규, 순수 read)

폴러가 "새로 발화한 watch 이벤트"를 감지·디듀프하기 위한 유일한 read 표면. 현재 인프라는 95% 존재하나
글로벌 "since 타임스탬프 이후 delivered 이벤트" 조회 표면이 없다(기존 표면은 전부 report-scoped).

- **위치(추가)**: 핸들러 `app/mcp_server/tooling/investment_reports_handlers.py`, repository
  메서드 `app/services/investment_reports/repository.py`
- **재사용**: `InvestmentWatchEvent`(`app/models/investment_reports.py:577-718`) + 기존 인덱스
  `ix_investment_watch_events_delivery_status_created` (delivery_status, created_at)
- **인터페이스**:
  ```
  investment_watch_events_list_recent(
      market: str | None = None,           # kr|us|crypto, None=전체
      since_timestamp: str | None = None,  # ISO8601, delivered_at >= since
      limit: int = 50,                     # 1..500 clamp
  ) -> {success: bool, count: int, events: [InvestmentWatchEventResponse]}
  ```
- **필터 규칙**: `WHERE delivery_status='delivered' AND delivered_at >= :since` ORDER BY `delivered_at ASC`.
  `delivered` 게이트 = Hermes가 실제 Discord에 전달 성공한 fire만 노출(skipped/failed 제외 — 폴러가
  미전달 알림을 잘못 깨우지 않게).
- **디듀프 키**: `event_uuid`(자연 유니크). 보조: `idempotency_key`(`alert_uuid:kst_date:threshold_key`,
  하루 1회 동일 fire 보장).
- **페이로드 한계 (중요)**: 이벤트 행에는 `trigger_checklist`/`max_action`/`price_guidance`/`planned_action`이
  **없다**(alert/item에 삶). 폴러/트리아지는 이벤트의 `source_report_uuid`로 ②③ 단계에서
  `investment_report_get`을 호출해 재검토 맥락을 후속 조회한다(부트스트랩이 어차피 하는 일).
- **안전경계**: 순수 read. 브로커/주문/감시 mutation 없음. 모든 DB 접근은 repository 경유.

### ② alert poller (레포밖 ops 스크립트, 신규)

운영자 머신(운영자의 `~/.claude` 환경이 사는 곳)에서 도는 launchd/cron 데몬.

- **위치**: **레포 밖**(운영 스크립트). auto_trader 런타임에 결합 안 함.
- **주기**: ~1분(scanner 5분 cadence보다 빠르므로 지연 무의미).
- **상태**: 마지막으로 처리한 `delivered_at` 워터마크 + 최근 처리한 `event_uuid` 링버퍼(동일
  타임스탬프 동률/시계 오차 대비).
- **동작**: ① 호출 → 새 이벤트별로 ③ 기동(`claude -p --settings <readonly> "/crypto-alert-triage <요약>"`)
  → 결과를 ⑤b로 Discord 회신.
- **실패 처리**: claude 런 실패(CLI 에러/MCP down) 시 워터마크를 미처리 이벤트 너머로 전진시키지 않거나
  event_uuid seen-set으로 재시도(구현 때 확정). 절대 silent skip 금지(로깅).
- **의존**: ①, `claude` CLI, `jq`.

### ③ `/crypto-alert-triage` — 부트스트랩 슬래시 커맨드 (레포, 신규)

- **위치(추가)**: `.claude/commands/crypto-alert-triage.md`
- **입력(인자)**: 이벤트 요약 — `event_uuid`, `symbol`, `market`, `source_report_uuid`,
  `metric`/`operator`/`threshold`, `current_value`.
- **시퀀스**: §7 참조.
- **소비자**: 1차 = poller(자동 호출). 2차(보너스) = 운영자 수동 호출.
- **의존**: `get_operating_briefing`, `investment_report_get`, `session_context_get_recent`,
  `session_context_append`, read-only 분석/preview 도구.

### ④ read-only 안전 프로파일 (레포, 신규 설정)

- **강제점**: MCP 서버는 공유 long-running 프로세스라 프로파일을 per-alert 전환 불가
  (`app/mcp_server/profiles.py`의 `MCP_PROFILE`은 부팅 시 1회). → **per-invocation 강제는
  Claude Code `--settings <deny-list 파일>`** 가 정답.
- **위치(추가)**: `.claude/settings.readonly.json` (또는 동등 권한 파일).
- **deny-list(26 mutation 도구)**:
  ```
  place_order, cancel_order, modify_order,
  kis_live_place_order, kis_live_cancel_order, kis_live_modify_order, kis_live_reconcile_orders,
  kis_mock_place_order, kis_mock_cancel_order, kis_mock_modify_order,
  toss_place_order, toss_modify_order, toss_cancel_order, toss_reconcile_orders,
  alpaca_paper_submit_order, alpaca_paper_cancel_order,
  kiwoom_mock_place_order, kiwoom_mock_cancel_order, kiwoom_mock_modify_order,
  live_reconcile_orders,
  investment_report_create, investment_report_add_items, investment_report_update,
  investment_report_decide_item, investment_report_activate_watch, investment_report_set_status
  ```
- **KEEP(허용)**: 모든 read/조사 도구, `*_get_order_history`, `get_holdings/get_position/get_portfolio_*`,
  `buy_ladder_fill_preview`/`sell_ladder_fill_preview`(read-only preview),
  `investment_report_list/get/context_get/delta_get`, `investment_watch_recommend`,
  `session_context_append`(핸드오프 적재는 허용 — 주문 mutation 아님), `session_context_get_recent`.
- **⚠️ 구현 주의**: Claude Code 권한 deny 구문은 실제 포맷(`mcp__<server>__<tool>` 패턴)으로 변환 필요.
  위 도구명은 논리명 — 구현 때 실제 MCP 서버명 prefix 확정.
- **심층방어(선택)**: MCP 서버를 제한 프로파일(예: `hermes-paper-kis`)로 띄우면 live 브로커 도구가 아예
  등록 안 됨. 단 이는 해당 서버의 **모든** 세션에 영향 → 운영 전체 결정이라 별도 검토.

### ⑤ 출력 글루

- **a) session_context 적재(③ 내부)**: `session_context_append(entries=[{market, account_scope,
  entry_type:'decision'|'next_action', title, body, refs:{alert_uuid, item_uuid, report_uuid,
  event_uuid, symbols}}])`. → 다음 신선 런이 §7 step3에서 읽어 연속성 회복(자가치유 루프).
- **b) Discord 회신(②)**: `claude -p ... --output-format json | jq -r .result` → 운영자가 알림을 받은
  그 Discord 채널/스레드로 회신.

---

## 6. 컨텍스트 복원 시퀀스 (the heart)

`/crypto-alert-triage`가 신선 세션에서 ~90% 맥락을 복원하는 순서. 각 콜이 복원하는 것:

| 단계 | 콜 | 복원하는 맥락 |
|---|---|---|
| 1 | `get_operating_briefing(market)` | 현재 보유 + 미체결 주문(만료시각 포함) + 활성 watch + 최신 리포트 요약 + 최근 session_context |
| 2 | `investment_report_get(source_report_uuid)` | 발화한 item의 `rationale`/`evidence_snapshot`/`trigger_checklist`/`max_action`/`watch_condition` — "왜 이 watch를 걸었나" |
| 3 | `session_context_get_recent(market, limit)` | 직전 트리아지·결정 핸드오프(`decision`/`next_action`/`open_question`) — "지난번에 뭘 봤고 뭘 미뤘나" |
| 4 | 분석 | 트리거 여전히 유효? max_action/trigger_checklist 점검. 미체결/현금 등 포트폴리오 제약 반영 |
| 5 | dry_run preview | `*_ladder_fill_preview` 등 read-only로 실행안 미리보기(실주문 없음) |
| 6 | 출력 | ⑤a session_context append + ⑤b Discord 회신 |

step3의 session_context 읽기 + step6의 적재가 맞물려 **연속성이 누적**된다 — 신선 런이지만 직전
트리아지의 결론을 읽으므로 resume의 토큰비용 없이 "지난 맥락"을 회복.

---

## 7. 안전 모델

- 알림 기동 런 = **READ-ONLY 분석 + dry_run 제안까지만**. 실주문은 사람이 인터랙티브 세션에서 확정.
- 강제: ④ `--settings` deny-list(26 mutation 도구 차단). 트리아지는 물리적으로 주문 불가.
- 기존 게이트와 정합: 모든 주문 mutation은 `dry_run=False` AND `confirm=True` 명시 필요 — 트리아지는
  이를 절대 설정하지 않음 + deny-list가 이중 차단.
- auto-approve routine(C안)을 안 쓰므로 auto-approve 안전 우려 자체가 없음.

---

## 8. 열린질문 6개 해소 매핑

| 열린질문 | 해소 |
|---|---|
| Q1 기동 글루 | ② 운영자-호스트 poller + ① read surface (scanner 직접 셸아웃 아님) |
| Q2 부트스트랩 표준 | ③ 슬래시 커맨드 1개(풀 스킬 아님). SessionStart hook/스냅샷 습관은 후속(YAGNI) |
| Q3 비용/충실도 | §9 검증 프로토콜로 실측 |
| Q4 출력 경로 | ⑤ Discord 회신 + session_context 적재 |
| Q5 권한/안전 | ④ 하드 read-only deny-list. 실주문 사람 확정 |
| Q6 네이티브 vs 스크립트 | ② 레포밖 ops, 레포는 ① read-only 추가만 |

---

## 9. 비용/충실도 검증 프로토콜 (Q3 — 후속 구현 후 실측)

A안의 "신선 손실"이 실제로 유의미한지 첫 실발화 후 실측한다.

- **정량**: 트리아지당 출력 토큰 수 + wall time(`--output-format json`의 usage).
- **정성**: 신선 런의 결론이 운영자가 인터랙티브 세션에서 내릴 판단과 일치하는가? 빠진 맥락은?
- **수용 기준**: 운영자가 트리아지 런에 맥락을 재설명할 필요가 거의 없으면 ~90% 복원 달성.
- **에스컬레이션**: 결론이 유의하게 어긋나면 → Q2 강화(인터랙티브 세션이 매 결정 끝에 session_context
  스냅샷 적재를 표준화) 또는 B/C 재검토.

---

## 10. 비범위 / 후속 구현 이슈 제안

이번 세션 산출물 = 본 설계문서 + Linear 업데이트. 구현은 후속. 권장 분할:

- **Impl-1 (레포, read-only)**: ① `investment_watch_events_list_recent` MCP 도구 + repository 메서드.
  TDD, 마이그레이션 없음(기존 모델/인덱스 재사용).
- **Impl-2 (레포, 설정+커맨드)**: ④ `.claude/settings.readonly.json` deny-list + ③
  `/crypto-alert-triage` 커맨드 + ⑤a session_context 적재.
- **Impl-3 (레포밖 ops)**: ② poller(launchd/cron) + ⑤b Discord 회신. 운영자-호스트 배포.
- **Validation**: §9 프로토콜(첫 실발화 후).

---

## 11. 리스크 & 주의사항

- **CC 권한 deny 구문**: 논리 도구명 → 실제 `mcp__<server>__<tool>` 포맷 변환을 구현 때 확정.
- **이벤트 행 페이로드 한계**: `trigger_checklist`/`max_action`은 이벤트에 없음 → 트리아지가
  `investment_report_get`으로 후속 조회(설계상 자연 처리).
- **공유 MCP 서버**: per-invocation 프로파일 전환 불가 → Layer 2(CC settings deny-list)가 주 강제점.
  Layer 1(제한 프로파일 서버)은 선택적 심층방어이나 전 세션 영향.
- **폴러 디듀프**: `delivered_at` 워터마크 + `event_uuid` seen-set 병행(동시각 동률 대비).
- **delivered 게이트**: skipped/failed 이벤트는 노출 안 함(미전달 알림으로 잘못 기동 방지).
- **멱등**: `idempotency_key`로 하루/threshold당 1 fire 보장 → 중복 기동 없음.
- **실행환경**: poller는 운영자 머신에서 돌아야 운영자 `~/.claude`(memory/skills/MCP)를 사용. 서버측
  실행 시 다른 환경이 되어 맥락 복원 충실도 저하.

---

## 12. 관련 이슈

- ROB-516 (session_context 저장소) — 부트스트랩 읽기/쓰기 1급 경로
- ROB-514 (watch 매수 실행계획 — max_action/trigger_checklist) — 재검토 맥락 페이로드
- ROB-413 / ROB-500 (watch-alert → Discord router/메시지) — 발화 원천
- ROB-453 (체결→알림→재트랜치 dry_run 루프, human-in-loop) — 인접 패턴
