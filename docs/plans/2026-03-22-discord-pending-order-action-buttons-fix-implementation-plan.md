# Discord Pending Order Action Buttons Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Discord 미체결 주문 버튼 작업의 남은 품질 이슈를 닫는다. 구체적으로는 허상에 가까운 KR modify 회귀 테스트를 실제 코드 경로를 검증하도록 고치고, MCP 문서를 실제 시그니처와 일치시키며, OpenClaw `inlineButtons` capability를 실제 Discord까지 포함해 증거 기반으로 검증한다.

**Architecture:** 저장소 내부 수정은 작게 가져간다. 테스트는 `modify_order_impl()`의 `equity_kr` + `dry_run=False` 경로를 직접 타게 수정하고, README는 public contract를 한 곳에서만 정의하도록 정리한다. 외부 검증은 OpenClaw probe 메시지 전송, Discord 렌더링 확인, 클릭 payload 캡처, 실제 MCP 호출 확인을 순차적으로 수행한다. 이 검증 결과에 따라 Option A 유지 또는 Option B 폴백 착수 여부를 명시적으로 결정한다.

**Tech Stack:** Python 3.13+, pytest, FastMCP order tools, existing OpenClaw webhook integration, Discord Components v2, curl/httpx

---

## 변경 범위 요약

| 파일/영역 | 변경 내용 |
|-----------|-----------|
| `tests/test_mcp_order_tools.py` | KR modify 버튼 플로우 테스트를 실제 KR 경로로 교체 |
| `app/mcp_server/README.md` | `modify_order` 시그니처 중복/불일치 정리 |
| `docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md` | 실제 capability 검증 결과와 결론 반영 |
| External: OpenClaw workflow/config | `discord.capabilities.inlineButtons` probe, callback payload 확인 |
| External: Discord channel/thread | 버튼 렌더링, 클릭 응답, modal/후속 입력 가능 여부 검증 |

---

### Task 1: KR modify 버튼 회귀 테스트를 실제 코드 경로로 수정

**Files:**
- Modify: `tests/test_mcp_order_tools.py`
- Inspect: `app/mcp_server/tooling/orders_modify_cancel.py:631-856`

**Step 1: failing test 작성**

기존 허상 테스트를 실제 KR 경로를 타도록 바꾼다. 핵심은 `market="kr"` + `dry_run=False` + `inquire_korea_orders()`/`modify_korea_order()` mock이다.

```python
@pytest.mark.asyncio
async def test_modify_order_button_flow_uses_kr_path_and_executes_modify(monkeypatch):
    tools = build_tools()
    received = {}

    class FakeKIS:
        async def inquire_korea_orders(self):
            return [
                {
                    "odno": "KR-BTN-1",
                    "pdno": "005930",
                    "ord_qty": "10",
                    "ord_unpr": "60000",
                    "sll_buy_dvsn_cd": "02",
                    "ord_gno_brno": "06010",
                }
            ]

        async def modify_korea_order(
            self, order_id, symbol, quantity, price, krx_fwdg_ord_orgno=None
        ):
            received.update(
                {
                    "order_id": order_id,
                    "symbol": symbol,
                    "quantity": quantity,
                    "price": price,
                    "orgno": krx_fwdg_ord_orgno,
                }
            )
            return {"odno": "KR-BTN-2"}

    _patch_kis_client(monkeypatch, lambda: FakeKIS())

    result = await tools["modify_order"](
        order_id="KR-BTN-1",
        symbol="005930",
        market="kr",
        new_price=61000,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["status"] == "modified"
    assert result["new_order_id"] == "KR-BTN-2"
    assert received == {
        "order_id": "KR-BTN-1",
        "symbol": "005930",
        "quantity": 10,
        "price": 61000,
        "orgno": "06010",
    }
```

**Step 2: 기존 테스트 제거 또는 교체**

현재 테스트는 `dry_run=True`라서 early return만 검증한다. 중복 가치를 줄이고 오해를 막기 위해 기존 버튼 플로우 KR 테스트는 삭제하거나 위 테스트로 완전히 교체한다.

**Step 3: 테스트 실행**

Run:

```bash
uv run pytest tests/test_mcp_order_tools.py -k "modify_order_button_flow or modify_order_kr_uppercase_fields" -v
```

Expected: PASS

**Step 4: broader regression 실행**

Run:

```bash
uv run pytest tests/test_mcp_order_tools.py -k "button_flow or cancel_order or modify_order" -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_mcp_order_tools.py
git commit -m "test: make kr button modify flow hit real modify path"
```

---

### Task 2: MCP README 시그니처 중복/불일치 정리

**Files:**
- Modify: `app/mcp_server/README.md`

**Step 1: failing review checklist 작성**

README에서 아래 2가지를 한 번에 만족하도록 수정한다.

- `modify_order` 시그니처는 한 번만 정의
- Discord 버튼 예시는 canonical signature의 사용 예로만 설명

수정 후 목표 형태:

```md
- `modify_order(order_id, symbol, market=None, new_price=None, new_quantity=None)`
  - Discord button flows: `modify_order(order_id="...", symbol="...", market="...", new_price=123.45, dry_run=false)`
```

또는 `dry_run`이 public signature 설명에 반드시 포함되어야 한다면 실제 등록 시그니처와 맞춰 전체 항목을 다시 적는다.

**Step 2: 문서 수정**

중복된 `modify_order` bullet을 제거한다. `cancel_order`도 필요하면 동일한 스타일로 예시만 남긴다.

**Step 3: diff 검토**

Run:

```bash
git diff -- app/mcp_server/README.md
```

Expected: `modify_order` 항목이 한 번만 나오고, 예시는 그 아래 하위 설명으로만 남음

**Step 4: Commit**

```bash
git add app/mcp_server/README.md
git commit -m "docs: align modify_order readme signature with actual contract"
```

---

### Task 3: OpenClaw inlineButtons 실제 capability probe 준비

**Files:**
- Inspect: `app/services/openclaw_client.py`
- Inspect: `app/core/config.py`
- External: OpenClaw Discord workflow/config
- Optional notes update: `docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md`

**Step 1: probe payload 확정**

아래와 같은 최소 probe 메시지를 준비한다.

```json
{
  "message": "pending-order-action-probe",
  "name": "auto-trader:scan",
  "sessionKey": "auto-trader:probe:inline-buttons",
  "wakeMode": "now",
  "components": [
    {
      "type": "button",
      "label": "취소",
      "style": "danger",
      "callback": {
        "action": "cancel",
        "order_id": "TEST-ORDER-1",
        "market": "us",
        "symbol": "AAPL"
      }
    },
    {
      "type": "button",
      "label": "가격 수정",
      "style": "primary",
      "callback": {
        "action": "modify",
        "order_id": "TEST-ORDER-1",
        "market": "us",
        "symbol": "AAPL"
      }
    }
  ]
}
```

OpenClaw 스펙이 다르면 그 스펙에 맞게 equivalent payload를 만든다. 목적은 “렌더링 + 콜백 수신 여부”만 검증하는 것이다.

**Step 2: capability flag 확인**

OpenClaw 설정에서 아래를 확인한다.

- `discord.capabilities.inlineButtons` 활성화 여부
- 버튼 callback/reusable session 지원 여부
- modal 또는 후속 prompt 지원 여부

Expected: 설정 스크린샷 또는 설정 값 로그 확보

**Step 3: probe 전송**

OpenClaw webhook에 실제 probe 메시지를 전송한다.

예시:

```bash
curl -X POST "$OPENCLAW_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENCLAW_TOKEN" \
  -d @/tmp/openclaw-inline-buttons-probe.json
```

Expected: 2xx 응답

**Step 4: Discord 렌더링 확인**

`#trading-alerts` 또는 테스트 채널에서 아래를 기록한다.

- 버튼 2개가 실제 보이는지
- label/style이 예상대로 렌더링되는지
- thread/channel target이 맞는지

Expected: 스크린샷 또는 운영 로그 확보

**Step 5: callback payload 캡처**

버튼을 각각 클릭하고 OpenClaw가 실제로 받는 값을 캡처한다.

필수 확인 항목:

- `order_id`
- `market`
- `symbol`
- 메시지/스레드 식별자
- payload 길이 제한 또는 custom_id 제약
- callback ack 시간 제약

Expected: payload example을 문서에 기록

**Step 6: modify 입력 UX 확인**

`가격 수정` 버튼 클릭 시 아래 중 무엇이 가능한지 증거를 남긴다.

- modal로 숫자 입력 가능
- 후속 prompt/답장으로 입력 가능
- 입력 UI가 없어 불가능

Expected: 가능/불가능을 명확히 판정

**Step 7: Commit (문서 반영이 있을 때만)**

```bash
git add docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md
git commit -m "docs: record openclaw inline button capability probe results"
```

---

### Task 4: 실제 MCP 호출 가능 여부 검증

**Files:**
- External: OpenClaw workflow
- Inspect: `app/mcp_server/README.md`
- Inspect: `tests/test_mcp_order_tools.py`
- Optional notes update: `docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md`

**Step 1: cancel 버튼 -> MCP 호출 시나리오 검증**

실제 또는 sandbox 환경에서 버튼 클릭 후 OpenClaw가 다음 형태 호출을 만들 수 있는지 확인한다.

```text
cancel_order(order_id="...", market="...")
```

Expected: symbol 없이도 충분하다는 운영 증거 확보

**Step 2: modify 버튼 -> MCP 호출 시나리오 검증**

새 가격 입력이 가능한 경우 OpenClaw가 다음 형태 호출을 만들 수 있는지 확인한다.

```text
modify_order(order_id="...", symbol="...", market="...", new_price=..., dry_run=false)
```

Expected: `symbol`과 `new_price`를 안정적으로 전달 가능

**Step 3: failure mode 기록**

아래 중 하나라도 막히면 정확히 적는다.

- callback payload에서 `symbol` 누락
- modal 미지원
- 버튼 클릭 이벤트 미수신
- OpenClaw가 MCP tool direct call 불가

Expected: Option A blocker 목록 생성

**Step 4: 결과 판정**

판정 기준:

- 모두 가능: Option A 유지, Option B 미구현
- 일부 불가이지만 우회 가능: 우회 설계 문서화
- 핵심 불가: Option B 폴백 구현 플랜 재개

**Step 5: Commit (문서 반영이 있을 때만)**

```bash
git add docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md
git commit -m "docs: record mcp call viability for openclaw button flow"
```

---

### Task 5: 최종 결론 문서화

**Files:**
- Modify: `docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md`

**Step 1: 검증 결과 표 추가**

다음 표를 실제 값으로 채운다.

```md
| Check | Result | Evidence |
|------|--------|----------|
| inlineButtons enabled | yes/no | config / screenshot |
| button render in Discord | yes/no | screenshot |
| cancel callback payload complete | yes/no | payload sample |
| modify input supported | yes/no | modal/prompt sample |
| MCP direct call possible | yes/no | workflow log |
```

**Step 2: 최종 추천 업데이트**

문서 마지막 recommendation을 실제 결과에 따라 갱신한다.

- `Proceed with Option A`
- `Proceed with Option A + workaround`
- `Abort Option A and start Option B`

**Step 3: 최종 검증 명령**

Run:

```bash
uv run pytest tests/test_mcp_order_tools.py -k "button_flow or cancel_order or modify_order" -v
git diff -- app/mcp_server/README.md tests/test_mcp_order_tools.py docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md
```

Expected: 테스트 PASS, 문서 diff가 사람이 읽기에 명확함

**Step 4: Commit**

```bash
git add tests/test_mcp_order_tools.py app/mcp_server/README.md docs/plans/2026-03-22-discord-pending-order-action-buttons-implementation-plan.md
git commit -m "docs: close pending order button fix and capability validation"
```

---

## 실행 순서

1. Task 1로 허상 테스트를 먼저 제거한다.
2. Task 2로 README contract를 정리한다.
3. Task 3과 Task 4로 OpenClaw 실제 capability와 MCP 호출 가능성을 검증한다.
4. Task 5에서 결론을 문서화하고 Option A 유지/포기 여부를 확정한다.

## 성공 기준

- KR modify 버튼 회귀 테스트가 실제 `equity_kr` + `dry_run=False` 경로를 검증한다.
- MCP README가 실제 public contract와 일치한다.
- OpenClaw inlineButtons가 실제 Discord 렌더링과 callback payload까지 증거 기반으로 검증된다.
- `cancel_order`와 `modify_order`를 버튼 플로우에서 실제로 호출할 수 있는지 명확히 판정된다.
- Option A 지속 여부가 문서에 명시된다.

Plan complete and saved to `docs/plans/2026-03-22-discord-pending-order-action-buttons-fix-implementation-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

**Which approach?**
