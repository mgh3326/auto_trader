# ROB-514 Watch Execution Plan Alert Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve watch-trigger re-review context by exposing structured planned action, trigger checklist, and optional recommendation attachment through the MCP and Hermes watch flows.

**Architecture:** Keep `max_action` as the canonical persisted execution-plan JSON and add a Hermes-facing `planned_action` projection. Wire that projection plus `trigger_checklist` into scanner and validity-review payloads, then expose the input contract in MCP descriptions and docs. Add an opt-in, fail-open `attach_recommendation` path to activation without changing broker/order behavior or DB schema.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async, FastMCP, pytest, `uv`.

---

## Decisions Locked

- Do not add a `planned_action` DB column.
- `planned_action` is derived from `alert.max_action`.
- `trigger_checklist` is a `string[]` input contract for new items.
- `attach_recommendation` defaults to `False`.
- Recommendation attachment is fail-open and must not prevent watch activation.
- No broker order submission, preview execution, scheduler activation, or DB migration in this slice.

## File Structure

| File | Role | Change |
|---|---|---|
| `app/schemas/investment_reports.py` | Item and activation contracts | Narrow checklist input, extend `MaxActionPayload`, add activation response metadata |
| `app/services/hermes_client.py` | Hermes payload contract | Add `PlannedAction`, projection helpers, payload fields |
| `app/jobs/investment_watch_scanner.py` | Trigger alert payload builder | Include planned action and checklist |
| `app/services/investment_reports/watch_validity_review.py` | Validity-review payload builder | Include planned action and checklist |
| `app/mcp_server/tooling/investment_reports_handlers.py` | MCP tool behavior and descriptions | Add `attach_recommendation`, shared recommendation helper, contract text |
| `app/mcp_server/README.md` | Public contract docs | Document watch checklist and execution-plan inputs |
| `docs/runbooks/watch-trigger-hermes-payload.md` | Hermes runbook | Document `planned_action` and checklist render expectations |
| `tests/test_investment_reports_schemas.py` | Schema tests | Checklist and max_action contract tests |
| `tests/test_hermes_client.py` | Payload tests | Projection and payload contract tests |
| `tests/test_investment_watch_scanner.py` | Scanner integration tests | Payload includes planned action and checklist |
| `tests/test_watch_validity_review.py` | Validity-review integration tests | Payload includes planned action and checklist |
| `tests/test_investment_reports_mcp.py` | MCP behavior tests | `attach_recommendation` success and fail-open |

---

## Task 1: Tighten Watch Item Input Contract

**Files:**
- Modify: `app/schemas/investment_reports.py`
- Test: `tests/test_investment_reports_schemas.py`

- [ ] **Step 1: Write failing schema tests**

Append these tests to `tests/test_investment_reports_schemas.py`.

```python
def test_trigger_checklist_requires_strings() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportItem(
            **_base_item_kwargs(
                client_item_key="watch-checklist",
                item_kind="watch",
                intent="buy_review",
                watch_condition={"metric": "price", "operator": "below", "threshold": "5"},
                valid_until="2026-12-31T00:00:00Z",
                trigger_checklist=["quote ok", {"not": "a string"}],
            )
        )
    assert "trigger_checklist" in str(exc_info.value)


def test_max_action_accepts_planned_action_fields() -> None:
    payload = MaxActionPayload(
        side="buy",
        quantity="1",
        amount_krw="980000",
        limit_price="975000",
        limit_price_hint="975000",
        ladder_level="1",
        account_mode="kis_mock",
    )

    dumped = payload.model_dump()
    assert dumped["quantity"] == Decimal("1")
    assert dumped["amount_krw"] == Decimal("980000")
    assert dumped["limit_price_hint"] == Decimal("975000")
    assert dumped["ladder_level"] == "1"
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py -k "trigger_checklist or planned_action_fields" -q
```

Expected: FAIL because non-string checklist entries are still accepted or because `amount_krw` / `limit_price_hint` are not typed fields.

- [ ] **Step 3: Implement minimal schema changes**

In `app/schemas/investment_reports.py`, update `MaxActionPayload`:

```python
class MaxActionPayload(BaseModel):
    """Structured order params a watch trigger proposes. Consumed by ROB-402.

    ``extra='allow'`` preserves legacy keys (e.g. ``notional_usd`` used by
    mock_preview). The live auto-execute block is enforced by ROB-402 on the
    (action_mode, account_mode) combination, not here.
    """

    side: ItemSideLiteral
    quantity: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    account_mode: AccountMode
    amount_krw: Decimal | None = None
    limit_price_hint: Decimal | None = None
    ladder_level: str | None = None

    model_config = ConfigDict(extra="allow")
```

Change `IngestReportItem.trigger_checklist`:

```python
trigger_checklist: list[str] = Field(default_factory=list)
```

Leave `InvestmentReportItemResponse.trigger_checklist` and
`InvestmentWatchAlertResponse.trigger_checklist` as `list[Any]` unless a focused
test proves historical rows can be safely narrowed too.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py -k "trigger_checklist or max_action" -q
```

Expected: PASS.

---

## Task 2: Add Hermes Planned Action Projection

**Files:**
- Modify: `app/services/hermes_client.py`
- Test: `tests/test_hermes_client.py`

- [ ] **Step 1: Write failing projection tests**

Update the imports in `tests/test_hermes_client.py`:

```python
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    planned_action_from_max_action,
    trigger_checklist_from_raw,
    price_guidance_from_watch_recommendation,
)
```

Append these tests:

```python
def test_planned_action_from_max_action_maps_canonical_keys() -> None:
    action = planned_action_from_max_action(
        {
            "side": "buy",
            "quantity": "1",
            "amount_krw": "980000",
            "limit_price": "975000",
            "ladder_level": "1",
        }
    )

    assert action is not None
    assert action.side == "buy"
    assert action.qty == Decimal("1")
    assert action.amount_krw == Decimal("980000")
    assert action.limit_price_hint == Decimal("975000")
    assert action.ladder_level == "1"


def test_planned_action_from_max_action_prefers_explicit_aliases() -> None:
    action = planned_action_from_max_action(
        {
            "side": "buy",
            "qty": "2",
            "quantity": "1",
            "amount_krw": "1900000",
            "limit_price_hint": "955000",
            "limit_price": "975000",
        }
    )

    assert action is not None
    assert action.qty == Decimal("2")
    assert action.limit_price_hint == Decimal("955000")


def test_planned_action_from_max_action_none_for_empty_or_malformed() -> None:
    assert planned_action_from_max_action({}) is None
    assert planned_action_from_max_action(None) is None
    assert planned_action_from_max_action({"side": "hold", "quantity": "1"}) is None
    assert planned_action_from_max_action({"side": "buy", "quantity": "oops"}) is None


def test_trigger_checklist_from_raw_returns_strings_only() -> None:
    assert trigger_checklist_from_raw(["quote", "thesis"]) == ["quote", "thesis"]
    assert trigger_checklist_from_raw(None) == []
    assert trigger_checklist_from_raw(["ok", {"bad": True}, 1]) == ["ok"]


def test_payload_accepts_planned_action_and_trigger_checklist() -> None:
    payload = _base_payload(
        planned_action={
            "side": "buy",
            "qty": "1",
            "amount_krw": "980000",
            "limit_price_hint": "975000",
            "ladder_level": "1",
        },
        trigger_checklist=["quote ok", "thesis ok"],
    )

    assert payload.planned_action is not None
    assert payload.planned_action.qty == Decimal("1")
    assert payload.trigger_checklist == ["quote ok", "thesis ok"]
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest tests/test_hermes_client.py -k "planned_action or trigger_checklist" -q
```

Expected: FAIL with missing imports or missing payload fields.

- [ ] **Step 3: Implement minimal projection helpers**

In `app/services/hermes_client.py`, add these imports if missing:

```python
from app.schemas.investment_reports import (
    ItemIntentLiteral,
    ItemSideLiteral,
    MarketLiteral,
    TargetKindLiteral,
    WatchActionModeLiteral,
    WatchClauseOpLiteral,
    WatchInvalidation,
    WatchMetricLiteral,
    WatchPriceRange,
)
```

Add this model and helpers after `PriceGuidance`:

```python
class PlannedAction(BaseModel):
    """ROB-514 - operator-facing execution plan derived from max_action."""

    side: ItemSideLiteral
    qty: Decimal | None = None
    amount_krw: Decimal | None = None
    limit_price_hint: Decimal | None = None
    ladder_level: str | None = None

    model_config = ConfigDict(extra="forbid")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def planned_action_from_max_action(max_action: dict[str, Any] | None) -> PlannedAction | None:
    """Project stored max_action into the Hermes planned_action contract.

    Fail-open: malformed optional context is omitted instead of blocking alert
    delivery.
    """
    if not isinstance(max_action, dict) or not max_action:
        return None
    try:
        return PlannedAction(
            side=max_action.get("side"),
            qty=_decimal_or_none(max_action.get("qty", max_action.get("quantity"))),
            amount_krw=_decimal_or_none(max_action.get("amount_krw")),
            limit_price_hint=_decimal_or_none(
                max_action.get("limit_price_hint", max_action.get("limit_price"))
            ),
            ladder_level=(
                str(max_action["ladder_level"])
                if max_action.get("ladder_level") not in (None, "")
                else None
            ),
        )
    except Exception:  # noqa: BLE001 - notification context is advisory
        logger.warning("max_action planned_action projection failed; omitting context")
        return None


def trigger_checklist_from_raw(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]
```

Add fields to `ReviewTriggerPayload`:

```python
planned_action: PlannedAction | None = None
trigger_checklist: list[str] | None = None
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_hermes_client.py -k "planned_action or trigger_checklist" -q
```

Expected: PASS.

---

## Task 3: Wire Planned Action Into Watch Trigger Payloads

**Files:**
- Modify: `app/jobs/investment_watch_scanner.py`
- Test: `tests/test_investment_watch_scanner.py`

- [ ] **Step 1: Write failing scanner payload assertions**

In `tests/test_investment_watch_scanner.py`, update
`test_trigger_payload_carries_links_guidance_and_price_guidance`.

After seeding the alert, set context fields:

```python
alert.max_action = {
    "side": "buy",
    "quantity": "1",
    "amount_krw": "980000",
    "limit_price": "975000",
    "ladder_level": "1",
}
alert.trigger_checklist = ["quote spread ok", "thesis still valid"]
await session.commit()
```

At the end of the test, add:

```python
assert payload.planned_action is not None
assert payload.planned_action.side == "buy"
assert payload.planned_action.qty == Decimal("1")
assert payload.planned_action.amount_krw == Decimal("980000")
assert payload.planned_action.limit_price_hint == Decimal("975000")
assert payload.planned_action.ladder_level == "1"
assert payload.trigger_checklist == ["quote spread ok", "thesis still valid"]
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/test_investment_watch_scanner.py::test_trigger_payload_carries_links_guidance_and_price_guidance -q
```

Expected: FAIL because the scanner does not populate the new fields.

- [ ] **Step 3: Implement scanner wiring**

In `app/jobs/investment_watch_scanner.py`, extend the import:

```python
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    planned_action_from_max_action,
    price_guidance_from_watch_recommendation,
    trigger_checklist_from_raw,
)
```

In `_upsert_event`, snapshot the alert context before commit/rollback:

```python
alert_max_action = dict(alert.max_action or {})
alert_trigger_checklist = list(alert.trigger_checklist or [])
```

Add fields to the `ReviewTriggerPayload(...)` call:

```python
planned_action=planned_action_from_max_action(alert_max_action),
trigger_checklist=trigger_checklist_from_raw(alert_trigger_checklist),
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_investment_watch_scanner.py::test_trigger_payload_carries_links_guidance_and_price_guidance -q
```

Expected: PASS.

---

## Task 4: Wire Planned Action Into Validity Review Payloads

**Files:**
- Modify: `app/services/investment_reports/watch_validity_review.py`
- Test: `tests/test_watch_validity_review.py`

- [ ] **Step 1: Write failing validity-review assertions**

In `tests/test_watch_validity_review.py`, update
`test_notify_payload_carries_links_and_price_guidance`. Set alert fields before
calling the service:

```python
alert.max_action = {
    "side": "buy",
    "quantity": "1",
    "amount_krw": "980000",
    "limit_price": "975000",
}
alert.trigger_checklist = ["quote spread ok", "thesis still valid"]
await session.commit()
```

Add assertions:

```python
assert payload.planned_action is not None
assert payload.planned_action.side == "buy"
assert payload.planned_action.qty == Decimal("1")
assert payload.planned_action.amount_krw == Decimal("980000")
assert payload.trigger_checklist == ["quote spread ok", "thesis still valid"]
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/test_watch_validity_review.py::test_notify_payload_carries_links_and_price_guidance -q
```

Expected: FAIL because validity review does not populate the new fields.

- [ ] **Step 3: Implement validity-review wiring**

In `app/services/investment_reports/watch_validity_review.py`, extend the import:

```python
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    planned_action_from_max_action,
    price_guidance_from_watch_recommendation,
    trigger_checklist_from_raw,
)
```

Add fields to the `ReviewTriggerPayload(...)` call in `_notify_review_required`:

```python
planned_action=planned_action_from_max_action(dict(alert.max_action or {})),
trigger_checklist=trigger_checklist_from_raw(list(alert.trigger_checklist or [])),
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_watch_validity_review.py::test_notify_payload_carries_links_and_price_guidance -q
```

Expected: PASS.

---

## Task 5: Add Opt-In Recommendation Attachment During Activation

**Files:**
- Modify: `app/schemas/investment_reports.py`
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
- Test: `tests/test_investment_reports_mcp.py`

- [ ] **Step 1: Write failing activation tests**

Append these tests to `tests/test_investment_reports_mcp.py`.

```python
@pytest.mark.asyncio
async def test_activate_watch_attach_recommendation_persists(
    session: AsyncSession, _stub_market_data
) -> None:
    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid,
        actor="operator",
        watch_condition={"metric": "price", "operator": "below", "threshold": 70000},
        valid_until=future_datetime().isoformat(),
        attach_recommendation=True,
    )

    assert response["success"] is True
    assert response["recommendation_attached"] is True
    assert response["recommendation_attach_error"] is None

    bundle_post = await investment_report_get_impl(created["report"]["report_uuid"])
    rec = bundle_post["items"][0]["watch_recommendation"]
    assert rec is not None
    assert rec["data_state"] == "ok"


@pytest.mark.asyncio
async def test_activate_watch_attach_recommendation_fails_open(
    session: AsyncSession, monkeypatch
) -> None:
    from app.mcp_server.tooling import investment_reports_handlers as h

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("market data unavailable")

    monkeypatch.setattr(h.market_data_service, "get_quote", _boom)

    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid,
        actor="operator",
        watch_condition={"metric": "price", "operator": "below", "threshold": 70000},
        valid_until=future_datetime().isoformat(),
        attach_recommendation=True,
    )

    assert response["success"] is True
    assert response["alert"]["source_item_uuid"] == watch_uuid
    assert response["recommendation_attached"] is False
    assert "market data unavailable" in response["recommendation_attach_error"]
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py -k "attach_recommendation" -q
```

Expected: FAIL because `investment_report_activate_watch_impl` does not accept
`attach_recommendation`.

- [ ] **Step 3: Extend schemas**

In `ActivateWatchRequest`, add:

```python
attach_recommendation: bool = False
```

In `InvestmentReportActivateWatchResponse`, add:

```python
recommendation_attached: bool | None = None
recommendation_attach_error: str | None = None
```

- [ ] **Step 4: Refactor recommendation computation into a helper**

In `app/mcp_server/tooling/investment_reports_handlers.py`, add this helper above
`investment_watch_recommend_impl`:

```python
async def _compute_watch_recommendation_json(
    *,
    symbol: str,
    market: str,
    valid_until: datetime | None,
) -> dict[str, Any]:
    if market not in _MARKET_MAP:
        raise ValueError(f"unsupported_market: {market}")

    md_symbol = _normalize_recommend_symbol(symbol, market)
    md_market = _MARKET_MAP[market]
    quote = await market_data_service.get_quote(symbol=md_symbol, market=md_market)
    reference_price = (
        Decimal(str(quote.price)) if getattr(quote, "price", None) is not None else None
    )
    candles = await market_data_service.get_ohlcv(
        symbol=md_symbol,
        market=md_market,
        period="day",
        count=LOOKBACK_DAYS + ATR_PERIOD + 6,
    )
    ordered = sorted(candles, key=lambda c: c.timestamp)
    payload = compute_watch_recommendation(
        WatchPolicyInput(
            reference_price=reference_price,
            best_bid=None,
            best_ask=None,
            daily_highs=[Decimal(str(c.high)) for c in ordered],
            daily_lows=[Decimal(str(c.low)) for c in ordered],
            daily_closes=[Decimal(str(c.close)) for c in ordered],
        ),
        computed_at=datetime.now(UTC),
        valid_until=valid_until,
    )
    if payload.data_state == "data_gap":
        raise ValueError("refusing to attach a data_gap recommendation")
    return payload.model_dump(mode="json")
```

Then update `investment_watch_recommend_impl` to use the helper so the policy
path cannot drift:

```python
rec_json = await _compute_watch_recommendation_json(
    symbol=symbol,
    market=market,
    valid_until=valid_until,
)
payload = WatchRecommendationPayload.model_validate(rec_json)
```

If importing `WatchRecommendationPayload` only for this validation adds churn,
instead check `rec_json.get("data_state") == "data_gap"` in the helper and keep
the existing return shape.

- [ ] **Step 5: Implement activation opt-in**

Update `investment_report_activate_watch_impl` signature:

```python
async def investment_report_activate_watch_impl(
    item_uuid: str,
    actor: str,
    idempotency_key: str | None = None,
    watch_condition: dict | None = None,
    valid_until: str | None = None,
    attach_recommendation: bool = False,
) -> dict:
```

Pass `attach_recommendation` into `ActivateWatchRequest.model_validate`.

Inside the DB session, before `await db.commit()`, after `item_row` is loaded,
add:

```python
recommendation_attached: bool | None = None
recommendation_attach_error: str | None = None

if request.attach_recommendation and item_row is not None:
    if item_row.watch_recommendation:
        recommendation_attached = True
    elif item_row.symbol is None:
        recommendation_attached = False
        recommendation_attach_error = "item symbol missing"
    else:
        try:
            rec_json = await _compute_watch_recommendation_json(
                symbol=item_row.symbol,
                market=alert_row.market,
                valid_until=item_row.valid_until,
            )
            await repo.update_item_watch_recommendation(item_row.id, rec_json)
            await db.flush()
            item_row = await repo.get_item_by_uuid(request.item_uuid)
            recommendation_attached = True
        except Exception as exc:  # noqa: BLE001 - attach is opt-in and fail-open
            recommendation_attached = False
            recommendation_attach_error = str(exc)
```

Build the response with the new fields:

```python
response = InvestmentReportActivateWatchResponse(
    alert=InvestmentWatchAlertResponse.model_validate(alert_row),
    item=InvestmentReportItemResponse.model_validate(item_row),
    recommendation_attached=recommendation_attached,
    recommendation_attach_error=recommendation_attach_error,
)
```

- [ ] **Step 6: Verify GREEN**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py -k "attach_recommendation or watch_recommend" -q
```

Expected: PASS.

---

## Task 6: Expose MCP and Documentation Contracts

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
- Modify: `app/mcp_server/README.md`
- Modify: `docs/runbooks/watch-trigger-hermes-payload.md`
- Test: `tests/test_investment_reports_mcp.py`
- Test: `tests/test_hermes_client.py`

- [ ] **Step 1: Write failing contract-text tests**

Append this test to `tests/test_investment_reports_mcp.py`:

```python
def test_create_description_mentions_watch_execution_plan_contract() -> None:
    from app.mcp_server.tooling.investment_reports_handlers import (
        ADD_ITEMS_DESCRIPTION,
        CREATE_DESCRIPTION,
    )

    combined = CREATE_DESCRIPTION + " " + ADD_ITEMS_DESCRIPTION
    assert "trigger_checklist" in combined
    assert "string[]" in combined
    assert "max_action" in combined
    assert "amount_krw" in combined
    assert "limit_price_hint" in combined
    assert "ladder_level" in combined
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_create_description_mentions_watch_execution_plan_contract -q
```

Expected: FAIL because the descriptions do not mention the new contract.

- [ ] **Step 3: Update tool descriptions and invalid item notes**

In `CREATE_DESCRIPTION`, after the watch item requirement sentence, add:

```python
"Watch execution context: trigger_checklist is string[] and is copied into "
"watch alert notifications. max_action is the structured execution-plan JSON; "
"supported keys include side, quantity or notional, amount_krw, limit_price, "
"limit_price_hint, ladder_level, and account_mode. planned_action in Hermes "
"payloads is derived from max_action; do not send planned_action as an item key. "
```

In `ADD_ITEMS_DESCRIPTION`, add:

```python
"For watch items, trigger_checklist string[] and max_action execution-plan "
"keys follow the same contract as investment_report_create."
```

In `_validate_report_items`, append to the `notes` string:

```python
" trigger_checklist must be string[]; watch execution plans belong in "
"max_action (side, quantity/notional, amount_krw, limit_price, "
"limit_price_hint, ladder_level, account_mode), not in planned_action."
```

- [ ] **Step 4: Update README**

In `app/mcp_server/README.md`, under
`### investment_report_create item contract`, add:

```markdown
Watch execution context fields:
- `trigger_checklist`: `string[]`; copied into watch alert notifications so the operator can re-check the trigger.
- `max_action`: structured watch execution-plan JSON. Supported keys include `side`, `quantity` or `notional`, optional `amount_krw`, optional `limit_price`, optional `limit_price_hint`, optional `ladder_level`, and optional `account_mode`.
- Do not send `planned_action` in item input. `planned_action` is derived from `max_action` when Hermes watch payloads are built.
```

- [ ] **Step 5: Update Hermes runbook**

In `docs/runbooks/watch-trigger-hermes-payload.md`, extend the JSON example:

```json
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
```

Add renderer requirements:

```markdown
5. Render `planned_action` near `price_guidance` when present. If null, do not invent quantity or amount.
6. Render each `trigger_checklist` string as an operator checklist. If empty, omit the checklist section.
```

- [ ] **Step 6: Verify GREEN**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_create_description_mentions_watch_execution_plan_contract -q
```

Expected: PASS.

---

## Task 7: Focused Integration Verification

**Files:**
- No new implementation files
- Verify changed behavior

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run pytest \
  tests/test_investment_reports_schemas.py \
  tests/test_hermes_client.py \
  tests/test_investment_watch_scanner.py \
  tests/test_watch_validity_review.py \
  tests/test_investment_reports_mcp.py \
  -k "trigger_checklist or max_action or planned_action or price_guidance or attach_recommendation or activate_watch" \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run type/lint checks for touched runtime files**

Run:

```bash
uv run ty check \
  app/schemas/investment_reports.py \
  app/services/hermes_client.py \
  app/jobs/investment_watch_scanner.py \
  app/services/investment_reports/watch_validity_review.py \
  app/mcp_server/tooling/investment_reports_handlers.py
```

Expected: PASS.

Run:

```bash
uv run ruff check \
  app/schemas/investment_reports.py \
  app/services/hermes_client.py \
  app/jobs/investment_watch_scanner.py \
  app/services/investment_reports/watch_validity_review.py \
  app/mcp_server/tooling/investment_reports_handlers.py \
  tests/test_investment_reports_schemas.py \
  tests/test_hermes_client.py \
  tests/test_investment_watch_scanner.py \
  tests/test_watch_validity_review.py \
  tests/test_investment_reports_mcp.py
```

Expected: PASS.

- [ ] **Step 3: Review diff for scope**

Run:

```bash
git diff --stat
git diff -- app/schemas/investment_reports.py app/services/hermes_client.py app/jobs/investment_watch_scanner.py app/services/investment_reports/watch_validity_review.py app/mcp_server/tooling/investment_reports_handlers.py
```

Expected: no DB migrations, no broker order submission changes, no scheduler changes.

---

## Plan Self Review

- Spec coverage: planned action projection, checklist payload, activation recommendation attachment, and docs are covered by Tasks 1-6.
- Type consistency: `planned_action_from_max_action`, `trigger_checklist_from_raw`, `PlannedAction`, and response field names are introduced before wiring tasks use them.
- Scope check: no migration, no order submission, no scheduler work.
- Verification: focused pytest, `ty`, and Ruff commands are included.
