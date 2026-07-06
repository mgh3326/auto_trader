---
description: 체결(fill) 이벤트, 특히 매도 체결 후 현금 재배치 판단을 read-only 트리아지로 자동 기동해 dry-run 제안을 남긴다. ROB-755.
---

# fill-event-triage

체결(fill) 이벤트가 들어왔다. 너는 **신선한 세션**이지만, 매매 맥락은 지속 아티팩트에 산다.
아래 순서로 맥락을 복원하고, 매도/매수 분기에서 적절히 분석하고, dry_run 제안까지만 낸다. **실주문 금지.**

## 입력

`$ARGUMENTS` 에 공백구분 key=value 로 체결 요약이 온다:
`ledger_id=... event_key=... broker=... account_mode=... market=... symbol=... side=... filled_qty=... filled_price=... filled_notional=... currency=... filled_at=... correlation_id=...`
먼저 이를 파싱한다. `market`이 비면 `crypto`로 간주.

## 1. 맥락 복원 (반드시 이 순서)

1. `get_operating_briefing(market=<market>)` — 현재 보유 / 미체결 주문(만료시각) / 활성 watch / 최신 리포트 / 최근 session_context.
2. `get_cash_balance(...)` — **매도 체결 후 현금/주문가능금액 재확인 (핵심)**. 특히 sell fill에서 어떤 통화로 얼마가 풀렸는지, 주문가능금액의 변화까지 같이 본다.
3. `get_portfolio_allocation(include_cash=true)` — 활성 MCP profile에 해당 도구가 노출되어 있는 경우에만 호출한다. 매도 후 포트폴리오 비중 변화 / 현금 비율의 타겟 괴리를 점검한다.
4. `session_context_get_recent(market=<market>, limit=10)` — 직전 트리아지·결정 핸드오프(decision/next_action/open_question). "지난번에 뭘 보고 뭘 미뤘나".
5. `session_context_append(...)` — refs에 `{event_key, ledger_id, correlation_id, symbols}` 포함해 다음 신선 런이 읽을 핸드오프를 적재.

## 2. 분석

`side` 값에 따라 분기한다. 두 분기 모두 read-only 검토이며, 어떤 분기에서도 실주문을 호출하지 않는다.

### 분기 A — 매도(sell) 체결

풀린 현금과 그 재배치 판단에 집중한다.

- 어떤 현금/통화가 풀렸는가? (`filled_notional` × `currency`, 수수료/슬리피지 고려)
- 매도 후 현재 포트폴리오가 타겟 대비 **under / over** 어디에 있는가? (`get_portfolio_allocation` 결과의 비중 vs 타겟)
- 기존 후보(latest report) / 활성 watch / 직전 `session_context` 중 매도 자금으로 **재검토**해야 할 것이 있는가?
- 운영자가 검토할 **dry-run buy / redeploy 제안**은 무엇인가? (재배치 후보 종목, 분할 수량, 지정가 구간)

### 분기 B — 매수(buy) 체결

체결된 트랜치의 완결성과 잔여 주문 가정의 유효성에 집중한다.

- 의도된 트랜치(tranche)가 실제로 다 체결되었는가? (`filled_qty` vs 의도 수량)
- 남은 rung(미체결 주문) 가정이 여전히 유효한가? (지정가, 노트, 손절선, max_action)
- 운영자가 남은 주문을 **pause / tighten / leave** 중 어떻게 해야 하는가? 그 근거는 무엇인가?

## 3. dry_run 미리보기 (read-only)

- 필요하면 `buy_ladder_fill_preview` / `sell_ladder_fill_preview` 등 **read-only preview**로 재배치/잔여 주문을 시뮬레이션한다.
- **실주문 절대 금지**: `place_order` / `modify_order` / `cancel_order` / `reconcile` / 리포트·watch mutation 도구는 권한으로 차단되어 있고, 호출해서도 안 된다. 너는 제안만 한다.

## 4. 출력 (둘 다 수행)

1. `session_context_append(entries=[{ "market": "<market>", "entry_type": "decision", "title": "<symbol> fill 트리아지", "body": "<핵심 판단 + 제안 dry_run + 다음 액션>", "refs": { "event_key": "<event_key>", "ledger_id": "<ledger_id>", "correlation_id": "<correlation_id>", "symbols": ["<symbol>"] }, "created_by": "fill-event-triage", "session_label": "fill-triage" }])` — 다음 신선 런이 읽을 핸드오프.
2. 마지막 assistant 메시지로 **Discord용 간결 요약**을 낸다(이게 `--output-format json`의 `.result`로 회신된다):
   - 한 줄 결론(예: "BTC 매도 체결 — KRW 1.2M 풀림, ETH 재배치 dry_run 권장"),
   - 핵심 근거 2~3개 (현금 변화, 비중 괴리, 후보/리포트 컨텍스트),
   - 제안 dry_run 실행안(side/수량/지정가) 또는 남은 주문 가이드,
   - 운영자 확인 필요 사항(실주문은 사람이 확정).

## 안전 계약

- READ-ONLY 분석 + dry_run 제안까지만. place/modify/cancel/reconcile/report/watch mutation 금지(권한 차단됨).
- 불확실하면 보수적으로(no-action) 제안하고 그 이유를 적는다. 현금 비중이 명확하면 redeploy 후보를 같이 제시하되, 마지막 결정은 운영자 몫으로 남긴다.
