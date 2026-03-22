# Discord 액션 버튼 미체결 주문 Cancel/Modify Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 미체결 주문 AI 리뷰 메시지에서 Discord 버튼으로 `취소` 또는 `가격 수정` 액션을 실행할 수 있게 하되, OpenClaw inline buttons 경로를 우선 검증하고 실패 시 `auto_trader` FastAPI interaction endpoint로 폴백한다.

**Architecture:** 기본 경로는 OpenClaw가 Discord Components v2 버튼을 렌더링하고, 버튼 클릭 이벤트를 받아 기존 MCP order tools(`cancel_order`, `modify_order`)를 호출하는 방식이다. `auto_trader`는 이 경로에서 기존 `/api/n8n/pending-review`와 MCP 도구 계약을 재사용하고, 필요한 경우에만 pending-review 응답을 버튼 친화적으로 보강한다. OpenClaw가 버튼 콜백 또는 modal 입력을 안정적으로 제공하지 못하면, `auto_trader`에 Discord interaction endpoint를 추가해 서명 검증 후 기존 MCP order tools를 호출하는 얇은 폴백 레이어를 만든다.

**Tech Stack:** FastAPI, Pydantic v2, existing OpenClaw webhook/callback integration, existing MCP order tools, pytest, Discord Interactions API

---

## 전제 / 결정 기준

- OpenClaw 워크플로와 Discord 컴포넌트 설정은 이 저장소 밖에서 변경 가능하다고 가정한다.
- `cancel_order`는 `order_id` 중심, `modify_order`는 `order_id + symbol (+ market) + new_price/new_quantity` 중심 계약을 유지한다.
- `modify` 액션의 사용자 입력은 OpenClaw modal 또는 Discord interaction modal에서 새 가격을 받는 쪽을 기본으로 한다.
- 버튼 `custom_id`에는 장문 JSON을 넣지 않는다. 먼저 짧은 식별자(`action`, `market`, `order_id`, `symbol` 또는 opaque token`)만 전달 가능한지 검증한다.
- Option A가 아래 조건을 모두 만족하면 Option B는 구현하지 않는다.
  - `discord.capabilities.inlineButtons` 활성화 가능
  - 버튼 클릭 콜백에서 `order_id`/`symbol`/`market` 전달 가능
  - 수정 액션에서 새 가격 입력 경로(modal 또는 후속 prompt) 확보 가능
  - OpenClaw가 기존 MCP 도구를 직접 호출 가능

## 변경 범위 요약

| 영역 | 파일 | 목적 |
|------|------|------|
| External | OpenClaw Discord workflow / capability config | inline buttons 가능 여부 검증 |
| Existing API | `app/routers/n8n.py` | pending-review 응답 계약 유지 또는 최소 보강 |
| Existing service | `app/services/n8n_pending_review_service.py` | 버튼 콜백에 필요한 action metadata 보강 가능 지점 |
| Existing schema | `app/schemas/n8n/pending_review.py` | action metadata 필드 추가 시 계약 반영 |
| Existing MCP | `app/mcp_server/tooling/orders_registration.py` | existing tool surface 유지 확인 |
| Existing MCP | `app/mcp_server/tooling/orders_modify_cancel.py` | cancel/modify edge-case 회귀 검증 |
| Existing docs | `app/mcp_server/README.md` | 버튼 경로에서 사용할 tool contract 문서화 |
| Fallback API | `app/core/config.py` | Discord interaction env 추가 |
| Fallback API | `app/routers/discord_interactions.py` | Discord interaction 수신 endpoint |
| Fallback service | `app/services/discord_interaction_service.py` | interaction 검증/라우팅/MCP 호출 |
| App wiring | `app/main.py` | fallback router 등록 |
| Tests | `tests/test_mcp_order_tools.py` | cancel/modify contract 회귀 방지 |
| Tests | `tests/test_n8n_trade_review.py` 또는 새 테스트 파일 | pending-review action metadata 검증 |
| Tests | `tests/test_discord_interactions.py` | fallback endpoint 서명/라우팅/응답 검증 |
| Dependency (conditional) | `pyproject.toml` | Discord 서명 검증용 라이브러리 추가 시 반영 |

---

### Task 1: Option A 가능 여부를 먼저 확정

**Files:**
- Inspect: `app/services/n8n_pending_review_service.py`
- Inspect: `app/schemas/n8n/pending_review.py`
- Inspect: `app/mcp_server/tooling/orders_registration.py`
- Inspect: `app/mcp_server/README.md`
- External: OpenClaw Discord workflow / capability config

**Step 1: OpenClaw 버튼 capability probe 수행**

OpenClaw 쪽에서 가장 작은 실험 메시지를 만든다.

```json
{
  "text": "pending-order-action-probe",
  "components": [
    {
      "type": "button",
      "label": "취소",
      "style": "danger",
      "callback": {"type": "echo", "payload": {"action": "cancel", "order_id": "TEST-1"}}
    },
    {
      "type": "button",
      "label": "가격 수정",
      "style": "primary",
      "callback": {"type": "echo", "payload": {"action": "modify", "order_id": "TEST-1"}}
    }
  ]
}
```

Expected: Discord에 버튼이 렌더링되고, 클릭 이벤트가 OpenClaw에서 수신된다.

**Step 2: 콜백 payload shape 기록**

버튼 클릭 시 OpenClaw가 실제로 넘겨주는 값을 기록한다.

- `custom_id` 또는 callback payload 길이 제한
- message/thread/channel 식별자 포함 여부
- modal 또는 추가 입력 UI 지원 여부
- callback ack deadline(몇 초 안에 응답해야 하는지)

Expected: `action`, `order_id`, 선택적으로 `symbol`, `market`을 안정적으로 전달할 수 있다는 증거 확보

**Step 3: pending-review가 버튼 생성에 필요한 식별자를 이미 제공하는지 대조**

Run:

```bash
uv run python -m pytest tests/test_n8n_trade_review.py -k "compute_fill_probability" -v
```

그리고 `app/services/n8n_pending_review_service.py` / `app/schemas/n8n/pending_review.py`를 기준으로 아래 필드 존재 여부를 체크한다.

- `order_id`
- `symbol`
- `market`
- `order_price`
- `current_price`
- `remaining_qty`

Expected: Option A에서 필요한 기본 식별자는 이미 존재한다.

**Step 4: MCP order tool이 버튼 경로 입력만으로 충분한지 확인**

Run:

```bash
uv run pytest tests/test_mcp_order_tools.py -k "cancel_order or modify_order" -v
```

Expected: 기존 테스트가 PASS 하며, 특히 다음 계약이 확인된다.

- `cancel_order(order_id, market=...)`가 버튼 클릭 경로에 충분한지
- `modify_order(order_id, symbol, market, new_price)`가 수정 액션에 충분한지

**Step 5: Go / No-Go 결정**

- Go to Task 2 if inline buttons + callback + MCP direct call이 성립
- Go to Task 4 if OpenClaw가 버튼 렌더링/콜백/modals 중 하나라도 제공하지 못함

**Step 6: Commit (문서/운영 결정만 있으면 생략 가능)**

```bash
git add docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md
git commit -m "docs: add discord pending order action button plan"
```

---

### Task 2: Option A에서 필요한 최소 auto_trader 계약만 보강

**Files:**
- Modify: `app/services/n8n_pending_review_service.py` (필요한 경우만)
- Modify: `app/schemas/n8n/pending_review.py` (필요한 경우만)
- Test: `tests/test_n8n_trade_review.py` 또는 새 pending-review 테스트 파일
- Modify: `app/mcp_server/README.md`

**Step 1: failing test 작성**

OpenClaw 버튼 워크플로가 compact action context를 요구하는 경우에만 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_fetch_pending_review_exposes_button_action_context(monkeypatch):
    from app.services.n8n_pending_review_service import fetch_pending_review

    monkeypatch.setattr(
        "app.services.n8n_pending_review_service.fetch_pending_orders",
        AsyncMock(
            return_value={
                "orders": [
                    {
                        "order_id": "US-1",
                        "symbol": "AAPL",
                        "market": "us",
                        "raw_symbol": "AAPL",
                        "side": "sell",
                        "order_price": 210.0,
                        "current_price": 198.0,
                        "gap_pct": 6.1,
                        "gap_pct_fmt": "+6.1%",
                        "amount_krw": 300000,
                        "quantity": 2,
                        "remaining_qty": 2,
                        "created_at": "2026-03-22T00:30:00+09:00",
                        "age_days": 2,
                        "currency": "USD",
                    }
                ],
                "errors": [],
            }
        ),
    )

    result = await fetch_pending_review(market="us")
    order = result["orders"][0]

    assert order["action_context"]["cancel"] == {
        "order_id": "US-1",
        "market": "us",
        "symbol": "AAPL",
    }
    assert order["action_context"]["modify"]["order_id"] == "US-1"
```

**Step 2: 테스트 실패 확인**

Run:

```bash
uv run pytest tests/test_n8n_trade_review.py -k "button_action_context" -v
```

Expected: FAIL - action metadata 없음

**Step 3: 최소 구현**

`fetch_pending_review()`에서 OpenClaw가 바로 버튼 payload를 만들 수 있을 만큼만 action context를 추가한다.

```python
action_context = {
    "cancel": {
        "order_id": order_id,
        "market": market,
        "symbol": symbol,
    },
    "modify": {
        "order_id": order_id,
        "market": market,
        "symbol": symbol,
        "current_price": current_price,
        "order_price": order_price,
        "remaining_qty": remaining_qty,
    },
}
```

원칙:

- 이미 내려가는 상위 필드와 중복되지 않으면 추가하지 않는다.
- OpenClaw가 기존 필드만으로 충분하면 이 Task 전체를 건너뛴다.
- `custom_id`에 넣을 최종 문자열은 OpenClaw 쪽에서 조립하고, API는 원시 식별자만 내려준다.

**Step 4: schema 반영**

`N8nPendingReviewItem`에 optional field 추가:

```python
action_context: dict[str, dict[str, object]] | None = Field(None)
```

**Step 5: 테스트 통과 확인**

Run:

```bash
uv run pytest tests/test_n8n_trade_review.py -k "button_action_context or compute_fill_probability" -v
```

Expected: PASS

**Step 6: MCP 문서 보강**

`app/mcp_server/README.md`에 버튼 경로용 실행 예시를 추가한다.

```md
- Discord button flows typically call:
  - `cancel_order(order_id="...", market="...")`
  - `modify_order(order_id="...", symbol="...", market="...", new_price=123.45, dry_run=false)`
```

**Step 7: Commit**

```bash
git add app/services/n8n_pending_review_service.py app/schemas/n8n/pending_review.py app/mcp_server/README.md tests/
git commit -m "feat: expose pending review action context for discord buttons"
```

---

### Task 3: Option A용 MCP 회귀 방어 추가

**Files:**
- Modify: `tests/test_mcp_order_tools.py`
- Inspect: `app/mcp_server/tooling/orders_modify_cancel.py`
- Inspect: `app/mcp_server/tooling/orders_registration.py`
- Modify: `app/mcp_server/README.md` (필요 시만)

**Step 1: failing regression test 추가**

버튼 경로에서 가장 많이 쓰일 입력 모양을 테스트로 박는다.

```python
@pytest.mark.asyncio
async def test_cancel_order_button_flow_requires_only_order_id_and_market(monkeypatch):
    tools = build_tools()
    # monkeypatch provider
    result = await tools["cancel_order"](order_id="US-CAN-1", market="us")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_modify_order_button_flow_uses_kr_path_and_executes_modify(monkeypatch):
    tools = build_tools()
    result = await tools["modify_order"](
        order_id="KR-MOD-1",
        symbol="005930",
        market="kr",
        new_price=61000,
        dry_run=True,
    )
    assert result["success"] is True
```

**Step 2: 테스트 실행**

Run:

```bash
uv run pytest tests/test_mcp_order_tools.py -k "button_flow or cancel_order_us_auto_lookup_symbol_and_exchange_when_symbol_missing or modify_order" -v
```

Expected: 신규 테스트는 FAIL 또는 아직 없음

**Step 3: 최소 수정**

필요할 때만 아래를 수정한다.

- `cancel_order_impl()`가 `market="us"` 버튼 경로에서 symbol 없이도 안정적으로 동작하도록 에러 메시지/lookup fallback 보강
- `modify_order_impl()`가 버튼 경로에서 필요한 필드 부족 시 명확한 오류를 돌려주도록 에러 텍스트 정리

원칙:

- 새 MCP tool을 만들지 않는다.
- public tool 이름/기본값을 바꾸지 않는다.

**Step 4: 테스트 통과 확인**

Run:

```bash
uv run pytest tests/test_mcp_order_tools.py -k "button_flow or cancel_order or modify_order" -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_mcp_order_tools.py app/mcp_server/tooling/orders_modify_cancel.py app/mcp_server/README.md
git commit -m "test: lock mcp order tool contract for discord button flows"
```

---

### Task 4: Option B 폴백용 Discord interaction endpoint 추가

**Files:**
- Modify: `app/core/config.py`
- Create: `app/routers/discord_interactions.py`
- Create: `app/services/discord_interaction_service.py`
- Modify: `app/main.py`
- Test: `tests/test_discord_interactions.py`
- Modify: `pyproject.toml` (Discord 서명 검증용 dependency가 필요할 때만)

**Step 1: failing router test 작성**

```python
def test_discord_interactions_reject_invalid_signature(client):
    response = client.post(
        "/api/discord/interactions",
        headers={
            "X-Signature-Ed25519": "bad",
            "X-Signature-Timestamp": "123",
        },
        content=b"{}",
    )
    assert response.status_code == 401


def test_discord_ping_returns_pong(client, signed_payload):
    response = client.post(
        "/api/discord/interactions",
        headers=signed_payload.headers,
        content=signed_payload.body_for({"type": 1}),
    )
    assert response.status_code == 200
    assert response.json()["type"] == 1
```

**Step 2: failing action-routing test 작성**

```python
def test_cancel_button_invokes_cancel_order(client, signed_payload, monkeypatch):
    mock_dispatch = AsyncMock(return_value={"content": "주문 취소 완료"})
    monkeypatch.setattr(
        "app.services.discord_interaction_service.dispatch_component_interaction",
        mock_dispatch,
    )

    response = client.post(
        "/api/discord/interactions",
        headers=signed_payload.headers,
        content=signed_payload.body_for(
            {
                "type": 3,
                "data": {"custom_id": "cancel:us:US-1:AAPL"},
            }
        ),
    )

    assert response.status_code == 200
    mock_dispatch.assert_awaited_once()
```

**Step 3: 테스트 실패 확인**

Run:

```bash
uv run pytest tests/test_discord_interactions.py -v
```

Expected: FAIL - module/route missing

**Step 4: config 추가**

`app/core/config.py`에 fallback endpoint용 env 추가:

```python
DISCORD_INTERACTIONS_PUBLIC_KEY: str = ""
DISCORD_INTERACTIONS_ENABLED: bool = False
```

**Step 5: 서명 검증 구현**

`discord_interaction_service.py`에 raw body 기반 서명 검증 추가:

```python
def verify_discord_request(
    public_key: str,
    signature: str,
    timestamp: str,
    body: bytes,
) -> bool:
    ...
```

주의:

- body를 JSON 파싱하기 전에 raw bytes 그대로 검증한다.
- 표준 Discord `PING (type=1)` 응답을 먼저 구현한다.
- 서명 검증 라이브러리가 없으면 `pyproject.toml`에 최소 dependency만 추가한다.

**Step 6: action dispatcher 구현**

`custom_id`를 파싱해 existing MCP tool 호출로 매핑한다.

```python
cancel:us:US-1:AAPL
modify:kr:KR-1:005930
```

원칙:

- 라우터는 얇게 유지한다.
- 실제 cancel/modify 호출은 service layer에서 수행한다.
- modify는 modal submit 또는 follow-up interaction에서 `new_price`를 추출한다.
- 응답 메시지는 ephemeral로 제한한다.

**Step 7: router 등록**

`app/main.py`에 router include 추가:

```python
app.include_router(discord_interactions.router)
```

**Step 8: 테스트 통과 확인**

Run:

```bash
uv run pytest tests/test_discord_interactions.py -v
```

Expected: PASS

**Step 9: Commit**

```bash
git add app/core/config.py app/routers/discord_interactions.py app/services/discord_interaction_service.py app/main.py tests/test_discord_interactions.py pyproject.toml
git commit -m "feat: add discord interaction fallback for pending order actions"
```

---

### Task 5: End-to-End 검증 및 운영 문서 정리

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md` (결정 결과 메모)
- External: OpenClaw workflow docs / secrets config

**Step 1: Option A happy-path E2E 검증**

검증 시나리오:

1. `/api/n8n/pending-review`에서 실제 미체결 주문 하나를 고른다.
2. OpenClaw가 그 주문으로 버튼이 달린 Discord 메시지를 보낸다.
3. `취소` 클릭 시 기존 `cancel_order`가 실행된다.
4. `가격 수정` 클릭 시 새 가격 입력 후 `modify_order`가 실행된다.

성공 조건:

- Discord 상에서 버튼 렌더링
- 잘못된 주문/이미 체결된 주문이면 사용자 친화적 오류 노출
- 성공/실패 결과가 thread 또는 ephemeral 응답으로 남음

**Step 2: Option B happy-path E2E 검증 (fallback 구현 시만)**

Run relevant tests first:

```bash
uv run pytest tests/test_discord_interactions.py tests/test_mcp_order_tools.py -v
```

그 다음 Discord Application 설정에서 interaction URL을 실제 endpoint로 연결하고 아래를 검증한다.

- `PING` 성공
- invalid signature 401
- cancel button -> `cancel_order`
- modify modal -> `modify_order`

**Step 3: 운영 문서 업데이트**

문서에 아래를 명시한다.

- Option A 사용 시 필요한 OpenClaw capability flag
- 버튼 callback payload contract
- `custom_id` 또는 action token 규약
- Option B 사용 시 필요한 env vars
- rollback 방법: 버튼 제거하고 기존 텍스트 리뷰만 유지

**Step 4: 최종 검증 명령**

```bash
uv run pytest tests/test_mcp_order_tools.py -k "cancel_order or modify_order" -v
uv run pytest tests/test_n8n_trade_review.py -k "pending_review or compute_fill_probability" -v
```

Fallback 구현까지 갔다면:

```bash
uv run pytest tests/test_discord_interactions.py -v
```

Expected: 모두 PASS

**Step 5: Commit**

```bash
git add app/mcp_server/README.md docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md
git commit -m "docs: finalize discord pending order action button rollout notes"
```

---

## 구현 순서 권장

1. Task 1로 OpenClaw inline buttons feasibility를 먼저 확정한다.
2. 가능하면 Task 2와 Task 3만 수행하고 종료한다.
3. 막히는 지점이 증명되면 그때만 Task 4로 넘어간다.
4. 마지막에 Task 5로 E2E와 운영 문서를 닫는다.

## 구현 결과

**Date:** 2026-03-22
**Status:** ⚠ Repository-side changes completed; live OpenClaw/Discord capability verification is still required in an environment with credentials.

### Task 1 결과
- `fetch_pending_review()`가 버튼 생성에 필요한 기본 식별자(`order_id`, `symbol`, `market`, `order_price`, `current_price`, `remaining_qty`)를 제공함은 저장소 기준으로 확인됨
- MCP order tools는 버튼 경로 입력 형태에 대해 저장소 테스트 기준으로 작동함
- 다만 이 작업 디렉터리의 현재 셸에는 `OPENCLAW_*` / `DISCORD_*` 환경 변수가 없어, OpenClaw inline buttons live probe는 여기서 재현하지 못함

### Task 2 결과
- `action_context` 필드 추가 완료:
  - `app/services/n8n_pending_review_service.py`: cancel/modify용 action_context 생성
  - `app/schemas/n8n/pending_review.py`: `N8nPendingReviewItem`에 `action_context` 필드 추가
  - `tests/test_n8n_trade_review.py`: `test_fetch_pending_review_exposes_button_action_context` 추가

### Task 3 결과
- MCP button flow 테스트 추가:
  - `test_cancel_order_button_flow_requires_only_order_id_and_market`
  - `test_modify_order_button_flow_uses_kr_path_and_executes_modify`
- KR modify 버튼 테스트는 이후 보강되어 실제 `equity_kr` + `dry_run=False` 수정 경로를 타도록 유지해야 함

### Task 4 결과
- **보류**: live OpenClaw capability evidence가 없으므로 Option A 확정 전까지 Discord interaction fallback 판단을 유보

### Task 5 결과
- 저장소 기준 검증:
  - `tests/test_mcp_order_tools.py`: `45 passed`
  - `tests/test_n8n_trade_review.py`: `23 passed`
- MCP 문서 업데이트는 필요하지만, public signature와 예시가 정확히 일치하는지 재검토가 필요함

### OpenClaw 통합 가이드

**Button callback payload contract:**
```json
{
  "action": "cancel|modify",
  "order_id": "US-1",
  "market": "us|kr|crypto",
  "symbol": "AAPL"
}
```

**MCP tool 호출 예시:**
- Cancel: `cancel_order(order_id="US-1", market="us")` — symbol 자동 조회됨
- Modify: `modify_order(order_id="US-1", symbol="AAPL", market="us", new_price=195.5, dry_run=false)`

**API 응답 예시 (`/api/n8n/pending-review`):**
```json
{
  "orders": [{
    "order_id": "US-1",
    "symbol": "AAPL",
    "market": "us",
    "action_context": {
      "cancel": {"order_id": "US-1", "market": "us", "symbol": "AAPL"},
      "modify": {"order_id": "US-1", "market": "us", "symbol": "AAPL", "current_price": 198.0, "order_price": 210.0, "remaining_qty": 2}
    }
  }]
}
```

## 확인 체크리스트

- `discord.capabilities.inlineButtons`가 실제로 켜져 있는가
- `reusable: true` 또는 equivalent callback persistence가 필요한가
- 버튼 콜백에서 `order_id`/`symbol`/`market`이 손실 없이 전달되는가
- `cancel_order` / `modify_order`가 버튼 경로 입력만으로 안정적으로 호출되는가
- 수정 액션에서 사용자가 새 가격을 입력하는 UX가 OpenClaw 또는 Discord fallback에 존재하는가
- 실패 시 사용자가 이해할 수 있는 오류가 Discord에 남는가
- 위 항목들의 live evidence가 스크린샷, payload sample, workflow log 형태로 저장되었는가

Plan complete and saved to `docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

**Which approach?**
