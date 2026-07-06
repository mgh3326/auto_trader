---
description: watch 알림 발화 시 컨텍스트-보존 트리아지 (read-only 분석 + dry_run 제안). ROB-602.
---

# crypto-alert-triage

watch 알림이 발화해 너를 깨웠다. 너는 **신선한 세션**이지만, 매매 맥락은 지속 아티팩트에 산다.
아래 순서로 맥락을 복원하고, 발화를 분석하고, dry_run 제안까지만 낸다. **실주문 금지.**

## 입력

`$ARGUMENTS` 에 공백구분 key=value 로 이벤트 요약이 온다:
`event_uuid=... symbol=... market=... source_report_uuid=... metric=... operator=... threshold=... current_value=...`
먼저 이를 파싱한다. `market`이 비면 `crypto`로 간주.

## 1. 맥락 복원 (반드시 이 순서)

1. `get_operating_briefing(market=<market>)` — 현재 보유 / 미체결 주문(만료시각) / 활성 watch / 최신 리포트 / 최근 session_context.
2. `investment_report_get(report_uuid=<source_report_uuid>)` — 발화 item의 rationale / evidence_snapshot / trigger_checklist / max_action / watch_condition. "왜 이 watch를 걸었나"를 복원.
3. `session_context_get_recent(market=<market>, limit=10)` — 직전 트리아지·결정 핸드오프(decision/next_action/open_question). "지난번에 뭘 보고 뭘 미뤘나".

## 2. 분석

- 트리거가 여전히 유효한가? (현재가 vs threshold, 노이즈 여부)
- `trigger_checklist` 항목을 하나씩 점검.
- `max_action`(side/qty·notional/limit/ladder_level)이 지금도 타당한가? 포트폴리오 제약(미체결/현금/중복 반대주문) 반영.
- 손실매도 가드·현금 정책 등 memory/CLAUDE.md 정책과 충돌 없는가.

## 3. dry_run 미리보기 (read-only)

- 필요하면 `buy_ladder_fill_preview` / `sell_ladder_fill_preview` 등 **read-only preview**로 실행안을 시뮬레이션.
- **실주문 절대 금지**: place/modify/cancel/reconcile 도구는 권한으로 차단되어 있고, 호출해서도 안 된다. 너는 제안만 한다.

## 4. 출력 (둘 다 수행)

1. `session_context_append(entries=[{ "market": "<market>", "entry_type": "decision", "title": "<symbol> watch 트리아지", "body": "<핵심 판단 + 제안 dry_run + 다음 액션>", "refs": { "report_uuid": "<source_report_uuid>", "event_uuid": "<event_uuid>", "symbols": ["<symbol>"] }, "created_by": "crypto-alert-triage", "session_label": "alert-triage" }])` — 다음 신선 런이 읽을 핸드오프.
2. 마지막 assistant 메시지로 Discord와 Telegram 양쪽에 그대로 전달 가능한 간결 요약을 낸다(이게 `--output-format json`의 `.result`로 회신된다). 아래 섹션 제목을 유지한다:
   - `## 알림 요약`
     - 발화 symbol/market/조건/current_value를 한 줄로 요약.
     - 트리거가 여전히 유효한지 한 줄로 요약.
   - `## 제안 verdict`
     - `approve_dry_run`, `wait`, `reject`, `needs_human_review` 중 하나.
     - 핵심 근거 2~3개.
     - 제안 dry_run 실행안(side/수량/지정가)이 있으면 적고, 없으면 `dry_run 제안 없음`으로 적는다.
   - `## 결정 필요`
     - 운영자 확인이 필요한 경우에만 이 섹션을 포함한다.
     - 섹션을 포함할 때 마지막 줄은 다음 문장 그대로 쓴다: operator 세션에서: `session_context 최근 제안 승인 검토`

## 안전 계약

- READ-ONLY 분석 + dry_run 제안까지만. 실주문/리포트 mutation 금지(권한 차단됨).
- 불확실하면 보수적으로(no-action) 제안하고 그 이유를 적는다.
