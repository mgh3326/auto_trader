# Watch Trigger → Hermes Payload Contract (ROB-265 Plan 4 + ROB-500)

auto_trader가 watch 발화/유효성 재검토 시 `HERMES_WEBHOOK_URL`로 POST하는
`ReviewTriggerPayload`(`app/services/hermes_client.py`) 계약 문서.
Hermes Discord 렌더러가 이 문서를 기준으로 카드를 구성한다.

## ROB-500 + ROB-514 추가 필드 (additive, 모두 optional)

```json
{
  "invest_links": {
    "report_path": "/invest/reports/70019e8d-1ee6-493f-adeb-5d9301d5ea48",
    "stock_path": "/invest/stocks/crypto/KRW-BTC",
    "event_anchor": "/invest/reports/70019e8d-…#watch-event-f912d55f-…",
    "alert_anchor": "/invest/reports/70019e8d-…#watch-alert-5e32ec11-…"
  },
  "operator_action_guidance": {
    "headline": "알림 전용 — 자동 주문 없음, 필요 시 수동 검토",
    "requires_operator_review": false,
    "order_behavior": "none"
  },
  "price_guidance": {
    "entry_review_below_price": "100",
    "suggested_limit_price_range": {"low": "95", "high": "100"},
    "max_chase_price": "102",
    "invalidation": {"kind": "price_below", "price": "80", "text": null}
  },
  "planned_action": {
    "side": "buy",
    "qty": "1",
    "amount_krw": "980000",
    "limit_price_hint": "975000",
    "ladder_level": "1"
  },
  "trigger_checklist": [
    "Check latest quote spread",
    "Confirm thesis still valid"
  ]
}
```

- 링크는 **path-only** — Hermes가 Invest base URL을 prepend.
- `event_anchor`는 스캐너 발화 경로에만 존재 (validity review는 `alert_anchor`만).
- `price_guidance: null` 이면 **"가격 가이드 없음"으로 표시**한다. Hermes가
  가격을 추론/생성하는 것은 금지.
- 익절/매도 목표 필드는 계약에 없다. 렌더러가 임의 생성하지 않는다 (locked scope).

## Hermes Discord 렌더러 요구사항 (ROB-500 §4 + ROB-514)

1. 카드 상단: `operator_action_guidance.headline` + 발화 조건
   (`symbol metric operator threshold`, `current_value`).
2. 그 다음: `invest_links` (event_anchor 우선, 없으면 alert_anchor → report_path,
   stock_path는 보조 context link).
3. 그 다음: `price_guidance` 4개 값 (또는 "가격 가이드 없음").
4. 하단 `Trace` 섹션: event/alert/report/item UUID + correlation_id.
5. Render `planned_action` near `price_guidance` when present. If null, do not invent quantity or amount.
6. Render each `trigger_checklist` string as an operator checklist. If empty, omit the checklist section.

## 배포 순서

새 필드는 additive-optional이다. Hermes 수신측이 unknown field를 strict 거부하는
경우 auto_trader 배포 → Hermes 업데이트 사이에 delivery가 `failed`로 기록되지만,
alert는 `active`로 남아 다음 스캔 루프가 재시도하므로 유실은 없다 (Plan 4
semantics). 그래도 권장 순서는 **Hermes(수신 tolerant + 렌더러) 먼저 → auto_trader**.
