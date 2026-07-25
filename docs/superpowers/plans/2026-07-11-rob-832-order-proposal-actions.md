# ROB-832 Order Proposal Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add fail-closed `replace` and `cancel` order proposal actions with broker-bound snapshots, Telegram before/after approval, confirmed cancellation, and replacement lineage.

**Architecture:** Preserve the ROB-816 ledger, nonce/lease callback, state machine, and place-order path. Add a pure target snapshot type and proposal-internal broker gateway; action-specific revalidation composes read, preview, cancel, confirm, and submit while persistence stays inside `OrderProposalsService`.

**Tech Stack:** Python 3.13, SQLAlchemy 2, Alembic, PostgreSQL JSONB, FastMCP, pytest/pytest-asyncio, Ruff, ty, uv.

## Global Constraints

- Base is `main` at `c2db536e`.
- `NULL/place` is backward-compatible place behavior; place keeps multiple rungs.
- Replace/cancel require exactly one rung and one target broker order.
- Replace is: fresh evidence → fresh preview/guards → cancel → broker confirmation → submit.
- Cancel is: fresh exact snapshot match → cancel → broker confirmation; never submit.
- Cancellation failure or missing/ambiguous confirmation forbids replacement submission.
- Manual/unattributed broker orders are valid targets.
- Unsupported `account_mode × market × action` combinations fail at create time.
- Independent ladder proposals remain allowed and send individual Telegram messages.
- Broker and Telegram dependencies are mocked/injected in tests; no live tests/orders.
- ROB-832 section B is out of scope.
- `make lint` must be clean; open a PR but do not merge.

## File Map

- Create `alembic/versions/20260711_rob832_order_proposal_actions.py`.
- Create `app/services/order_proposals/target_order.py`.
- Create `app/services/order_proposals/broker_gateway.py`.
- Modify the proposal model, payload, service, state machine, revalidation, renderer, dispatch, callback, and MCP tool modules.
- Update `app/mcp_server/README.md` and `docs/runbooks/order-proposals.md`.
- Add focused tests under `tests/services/order_proposals/` and `tests/test_mcp_order_proposal_tools.py`.

---

### Task 1: Add action persistence and hash binding

**Files:**
- Create: `alembic/versions/20260711_rob832_order_proposal_actions.py`
- Modify: `app/models/order_proposals.py`
- Modify: `app/services/order_proposals/payload.py`
- Test: `tests/services/order_proposals/test_models_smoke.py`
- Test: `tests/services/order_proposals/test_payload.py`

**Interfaces:**
- Produces nullable `OrderProposal.action` and `target_broker_order_id`.
- Extends `compute_proposal_payload_hash` with action, target ID, and target snapshot.

- [ ] **Step 1: Write failing schema and hash tests**

```python
def test_order_proposal_has_action_columns():
    columns = OrderProposal.__table__.columns
    assert columns["action"].nullable
    assert columns["target_broker_order_id"].nullable
    check = next(
        c for c in OrderProposal.__table__.constraints
        if getattr(c, "name", None)
        == "ck_order_proposals_order_proposals_action"
    )
    assert "action IS NULL" in str(check.sqltext)
    for action in ("place", "replace", "cancel"):
        assert f"'{action}'" in str(check.sqltext)


def test_payload_hash_binds_target_identity_and_remaining_qty():
    common = {
        "symbol": "KRW-AVAX",
        "market": "crypto",
        "account_mode": "upbit",
        "order_type": "limit",
        "rungs": [ProposalRungSpec(0, "sell", "3.5", "43000", None)],
    }
    snapshot = {
        "broker_order_id": "old-1",
        "symbol": "KRW-AVAX",
        "side": "sell",
        "order_type": "limit",
        "limit_price": "42000",
        "remaining_quantity": "3.5",
        "status": "open",
        "observed_at": "2026-07-11T17:23:00+09:00",
    }
    hashes = {
        compute_proposal_payload_hash(**common),
        compute_proposal_payload_hash(
            **common, action="replace",
            target_broker_order_id="old-1",
            target_order_snapshot=snapshot,
        ),
        compute_proposal_payload_hash(
            **common, action="replace",
            target_broker_order_id="old-2",
            target_order_snapshot={**snapshot, "broker_order_id": "old-2"},
        ),
        compute_proposal_payload_hash(
            **common, action="replace",
            target_broker_order_id="old-1",
            target_order_snapshot={**snapshot, "remaining_quantity": "3.4"},
        ),
    }
    assert len(hashes) == 4
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/services/order_proposals/test_models_smoke.py \
  tests/services/order_proposals/test_payload.py -q
```

Expected: missing columns and unexpected hash keyword arguments.

- [ ] **Step 3: Implement migration and ORM fields**

```python
revision = "20260711_rob832_actions"
down_revision = "20260711_rob816_exit_binding"


def upgrade() -> None:
    op.add_column(
        "order_proposals", sa.Column("action", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "order_proposals",
        sa.Column("target_broker_order_id", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        "order_proposals_action", "order_proposals",
        "action IS NULL OR action IN ('place','replace','cancel')",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "order_proposals_action", "order_proposals",
        type_="check", schema="review",
    )
    op.drop_column(
        "order_proposals", "target_broker_order_id", schema="review"
    )
    op.drop_column("order_proposals", "action", schema="review")
```

Add the same CHECK to the ORM plus:

```python
action: Mapped[str | None] = mapped_column(Text)
target_broker_order_id: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 4: Extend the hash with backward-compatible defaults**

```python
def compute_proposal_payload_hash(
    *, symbol: str, market: str, account_mode: str, order_type: str,
    rungs: Sequence[ProposalRungSpec],
    action: str | None = None,
    target_broker_order_id: str | None = None,
    target_order_snapshot: dict[str, str | None] | None = None,
    exit_intent: str | None = None,
    exit_reason: str | None = None,
    retrospective_id: int | None = None,
    approval_issue_id: str | None = None,
) -> str:
    canonical = {
        "symbol": symbol,
        "market": market,
        "account_mode": account_mode,
        "order_type": order_type,
        "action": action or "place",
        "target_broker_order_id": target_broker_order_id,
        "target_order_snapshot": target_order_snapshot,
        "exit_intent": exit_intent,
        "exit_reason": exit_reason,
        "retrospective_id": retrospective_id,
        "approval_issue_id": approval_issue_id,
        "rungs": [
            {
                "rung_index": r.rung_index,
                "side": r.side,
                "quantity": r.quantity,
                "limit_price": r.limit_price,
                "notional": r.notional,
            }
            for r in sorted(rungs, key=lambda r: r.rung_index)
        ],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run pytest tests/services/order_proposals/test_models_smoke.py \
  tests/services/order_proposals/test_payload.py -q
uv run alembic heads
git add alembic/versions/20260711_rob832_order_proposal_actions.py \
  app/models/order_proposals.py app/services/order_proposals/payload.py \
  tests/services/order_proposals/test_models_smoke.py \
  tests/services/order_proposals/test_payload.py
git commit -m "feat(ROB-832): add proposal action schema"
```

---

### Task 2: Define canonical target evidence and broker adapters

**Files:**
- Create: `app/services/order_proposals/target_order.py`
- Create: `app/services/order_proposals/broker_gateway.py`
- Test: `tests/services/order_proposals/test_target_order.py`
- Test: `tests/services/order_proposals/test_broker_gateway.py`

**Interfaces:**
- Produces `TargetOrderSnapshot.from_broker_order`, `.from_payload`, `.to_payload`, `.matches_approved`.
- Produces `fetch_target_order` and `cancel_target_order`.

- [ ] **Step 1: Write failing snapshot and gateway tests**

```python
def test_snapshot_normalizes_manual_broker_order():
    snapshot = TargetOrderSnapshot.from_broker_order(
        {
            "order_id": "manual-upbit-1",
            "symbol": "KRW-AVAX",
            "side": "sell",
            "status": "partial",
            "remaining_qty": 3.500000,
            "ordered_price": 42000.0,
            "order_type": "limit",
        },
        observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
    )
    assert snapshot.status == "open"
    assert snapshot.remaining_quantity == "3.5"
    assert snapshot.limit_price == "42000"


@pytest.mark.asyncio
async def test_fetch_accepts_unattributed_open_order():
    async def fake_history(**kwargs):
        return {
            "orders": [{
                "order_id": "manual-1",
                "symbol": "KRW-AVAX",
                "side": "sell",
                "status": "pending",
                "remaining_qty": 2,
                "ordered_price": 41000,
                "order_type": "limit",
            }],
            "errors": [],
        }
    snapshot = await fetch_target_order(
        order_id="manual-1", symbol="KRW-AVAX", market="crypto",
        account_mode="upbit", now=datetime.now(UTC),
        history_fn=fake_history,
    )
    assert snapshot.broker_order_id == "manual-1"
```

Also test remaining drift, zero/multiple matches, broker errors, unsupported modes, cancel routing, and cancelled evidence.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/services/order_proposals/test_target_order.py \
  tests/services/order_proposals/test_broker_gateway.py -q
```

Expected: import failures.

- [ ] **Step 3: Implement the pure snapshot**

```python
@dataclass(frozen=True)
class TargetOrderSnapshot:
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    limit_price: str | None
    remaining_quantity: str
    status: str
    observed_at: str

    @classmethod
    def from_broker_order(
        cls, row: Mapping[str, Any], *, observed_at: datetime
    ) -> "TargetOrderSnapshot":
        status = str(row.get("status") or "").lower()
        return cls(
            broker_order_id=str(row.get("order_id") or "").strip(),
            symbol=str(row.get("symbol") or "").strip(),
            side=str(row.get("side") or "").lower(),
            order_type=str(row.get("order_type") or "limit").lower(),
            limit_price=canonical_decimal(row.get("ordered_price")),
            remaining_quantity=canonical_decimal(
                row.get("remaining_qty")
            ) or "0",
            status=(
                "open"
                if status in {"pending", "partial", "open"}
                else status
            ),
            observed_at=observed_at.isoformat(),
        )

    def to_payload(self) -> dict[str, str | None]:
        return asdict(self)

    def matches_approved(self, fresh: "TargetOrderSnapshot") -> bool:
        fields = (
            "broker_order_id", "symbol", "side", "order_type",
            "limit_price", "remaining_quantity", "status",
        )
        return all(
            getattr(self, field) == getattr(fresh, field)
            for field in fields
        )
```

Validate required identity, side/order-type bags, timezone awareness, and positive remaining quantity for open snapshots. `from_payload` reconstructs the same canonical object.

- [ ] **Step 4: Implement injected broker adapters**

```python
SUPPORTED_TARGET_ACTIONS = frozenset({
    ("kis_live", "equity_kr"),
    ("kis_live", "equity_us"),
    ("upbit", "crypto"),
})


async def fetch_target_order(
    *, order_id: str, symbol: str, market: str, account_mode: str,
    now: datetime, history_fn: Callable[..., Any] | None = None,
) -> TargetOrderSnapshot:
    if (account_mode, market) not in SUPPORTED_TARGET_ACTIONS:
        raise OrderProposalError(
            f"target order lookup unsupported for {account_mode}/{market}"
        )
    if history_fn is None:
        from app.mcp_server.tooling.orders_history import get_order_history_impl
        history_fn = get_order_history_impl
    result = await _maybe_await(history_fn(
        symbol=symbol, status="all", order_id=order_id,
        market=market, limit=20, is_mock=False,
    ))
    matches = [
        row for row in result.get("orders", [])
        if str(row.get("order_id")) == order_id
    ]
    if len(matches) != 1:
        raise OrderProposalError(
            "target broker order not found uniquely"
        )
    return TargetOrderSnapshot.from_broker_order(
        matches[0], observed_at=now
    )


async def cancel_target_order(
    *, order_id: str, symbol: str, market: str, account_mode: str,
    cancel_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if (account_mode, market) not in SUPPORTED_TARGET_ACTIONS:
        raise OrderProposalError(
            f"cancel unsupported for {account_mode}/{market}"
        )
    if cancel_fn is None:
        from app.mcp_server.tooling.orders_modify_cancel import cancel_order_impl
        cancel_fn = cancel_order_impl
    return await _maybe_await(cancel_fn(
        order_id=order_id, symbol=symbol,
        market=market, is_mock=False,
    ))
```

Do not treat a missing post-cancel order as confirmation.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run pytest tests/services/order_proposals/test_target_order.py \
  tests/services/order_proposals/test_broker_gateway.py -q
git add app/services/order_proposals/target_order.py \
  app/services/order_proposals/broker_gateway.py \
  tests/services/order_proposals/test_target_order.py \
  tests/services/order_proposals/test_broker_gateway.py
git commit -m "feat(ROB-832): normalize proposal target evidence"
```

---

### Task 3: Enforce create contracts and MCP read-only preflight

**Files:**
- Modify: `app/services/order_proposals/service.py`
- Modify: `app/mcp_server/tooling/order_proposal_tools.py`
- Test: `tests/services/order_proposals/test_service.py`
- Test: `tests/test_mcp_order_proposal_tools.py`

**Interfaces:**
- Extends `create_proposal` with action, target ID, and target snapshot.
- Extends public `order_proposal_create` with `action="place"` and optional target ID.

- [ ] **Step 1: Write failing service/MCP tests**

```python
@pytest.mark.asyncio
async def test_place_still_allows_multiple_rungs(db_session):
    group = await OrderProposalsService(db_session).create_proposal(
        symbol="005930", market="equity_kr", account_mode="kis_live",
        side="buy", order_type="limit", proposer="p", action="place",
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("70000"), None),
            RungInput(1, "buy", Decimal("1"), Decimal("69000"), None),
        ],
    )
    assert group.action == "place"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_target_actions_require_exactly_one_rung(
    db_session, action
):
    with pytest.raises(OrderProposalError, match="exactly one rung"):
        await OrderProposalsService(db_session).create_proposal(
            symbol="KRW-AVAX", market="crypto", account_mode="upbit",
            side="sell", order_type="limit", proposer="p", action=action,
            target_broker_order_id="old-1",
            target_order_snapshot=_snapshot_payload(),
            rungs=[
                RungInput(0, "sell", Decimal("1"), Decimal("42000"), None),
                RungInput(1, "sell", Decimal("1"), Decimal("41000"), None),
            ],
        )
```

Add tests for missing/forbidden target, unsupported tuple, cancel snapshot mismatch, two independent replace proposals, manual target preflight, lookup failure with no insert, and place never fetching a target.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/services/order_proposals/test_service.py \
  tests/test_mcp_order_proposal_tools.py -q
```

- [ ] **Step 3: Implement action validation**

```python
_ACTION_CAPABILITIES = {
    "place": _SUBMITTABLE_ACCOUNT_MODE_MARKETS,
    "replace": SUPPORTED_TARGET_ACTIONS,
    "cancel": SUPPORTED_TARGET_ACTIONS,
}


def _validate_action_contract(
    *,
    action: str | None,
    account_mode: str,
    market: str,
    symbol: str,
    side: str,
    order_type: str,
    rungs: list[RungInput],
    target_broker_order_id: str | None,
    target_order_snapshot: dict[str, str | None] | None,
) -> tuple[str, TargetOrderSnapshot | None]:
    normalized = action or "place"
    if normalized not in _ACTION_CAPABILITIES:
        raise OrderProposalError(
            "action must be one of: place, replace, cancel"
        )
    if (account_mode, market) not in _ACTION_CAPABILITIES[normalized]:
        raise OrderProposalError(
            "unsupported account_mode/market/action: "
            f"{account_mode}/{market}/{normalized}"
        )
    if normalized == "place":
        if target_broker_order_id is not None or target_order_snapshot is not None:
            raise OrderProposalError(
                "place proposal cannot target a broker order"
            )
        return normalized, None
    if len(rungs) != 1:
        raise OrderProposalError(
            f"{normalized} proposal requires exactly one rung"
        )
    if not target_broker_order_id or target_order_snapshot is None:
        raise OrderProposalError(
            f"{normalized} requires target broker evidence"
        )
    snapshot = TargetOrderSnapshot.from_payload(target_order_snapshot)
    if snapshot.broker_order_id != target_broker_order_id:
        raise OrderProposalError(
            "target broker order id does not match snapshot"
        )
    if (
        snapshot.symbol != symbol
        or snapshot.side != side
        or snapshot.order_type != order_type
    ):
        raise OrderProposalError(
            "target broker evidence conflicts with proposal"
        )
    if normalized == "cancel":
        rung = rungs[0]
        if (
            canonical_decimal(rung.quantity)
            != snapshot.remaining_quantity
            or canonical_decimal(rung.limit_price) != snapshot.limit_price
            or rung.side != snapshot.side
        ):
            raise OrderProposalError(
                "cancel rung must equal target broker snapshot"
            )
    return normalized, snapshot
```

Hash and persist the normalized action/target. Merge `target_order_snapshot` into `source_asof`.

- [ ] **Step 4: Implement MCP preflight**

```python
normalized_action = action or "place"
target_snapshot = None
if normalized_action in {"replace", "cancel"}:
    if not target_broker_order_id:
        raise ValueError(
            f"{normalized_action} requires target_broker_order_id"
        )
    target_snapshot = await fetch_target_order(
        order_id=target_broker_order_id,
        symbol=symbol,
        market=market,
        account_mode=account_mode,
        now=now_kst(),
    )
```

Pass the snapshot to the service. Add action/target to `_group_dict`, create response, and MCP description. Commit before Telegram dispatch.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run pytest tests/services/order_proposals/test_service.py \
  tests/test_mcp_order_proposal_tools.py -q
git add app/services/order_proposals/service.py \
  app/mcp_server/tooling/order_proposal_tools.py \
  tests/services/order_proposals/test_service.py \
  tests/test_mcp_order_proposal_tools.py
git commit -m "feat(ROB-832): validate target proposal creation"
```

---

### Task 4: Render initial action before/after messages

**Files:**
- Modify: `app/services/order_proposals/approval_message.py`
- Modify: `app/services/order_proposals/dispatch.py`
- Test: `tests/services/order_proposals/test_approval_message.py`
- Test: `tests/services/order_proposals/test_dispatch.py`

**Interfaces:**
- Produces `build_action_diff(group, rungs)`.
- Explicit `diff=` remains NEEDS_RECONFIRM and takes precedence.

- [ ] **Step 1: Write failing rendering tests**

```python
def test_replace_message_renders_target_before_new_rung_after():
    group = _group(
        action="replace", target_broker_order_id="old-1",
        source_asof={"target_order_snapshot": _snapshot_payload()},
    )
    text, _ = build_approval_message(
        group=group,
        rungs=[_rung(quantity="3.5", limit_price="43000")],
    )
    assert "replace" in text
    assert "old-1" in text
    assert "변경 전: 수량 3.5 / 가격 ₩42,000" in text
    assert "변경 후: 수량 3.5 / 가격 ₩43,000" in text
    assert "재확인" not in text


def test_cancel_message_renders_zero_remaining_after():
    group = _group(
        action="cancel", target_broker_order_id="old-1",
        source_asof={"target_order_snapshot": _snapshot_payload()},
    )
    text, _ = build_approval_message(
        group=group, rungs=[_snapshot_rung()]
    )
    assert "변경 후: 수량 0" in text
```

Add dispatch coverage for the initial outbound message and preserve explicit reconfirm tests.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/services/order_proposals/test_approval_message.py \
  tests/services/order_proposals/test_dispatch.py -q
```

- [ ] **Step 3: Implement the reusable diff**

```python
def build_action_diff(
    *, group: Any, rungs: Sequence[Any]
) -> dict | None:
    action = getattr(group, "action", None) or "place"
    if action == "place":
        return None
    source = getattr(group, "source_asof", None) or {}
    before = source.get("target_order_snapshot")
    if not isinstance(before, Mapping) or len(rungs) != 1:
        return None
    after = (
        {"quantity": "0", "limit_price": before.get("limit_price")}
        if action == "cancel"
        else {
            "quantity": getattr(rungs[0], "quantity", None),
            "limit_price": getattr(rungs[0], "limit_price", None),
        }
    )
    return {
        "before": {
            "quantity": before.get("remaining_quantity"),
            "limit_price": before.get("limit_price"),
        },
        "after": after,
    }
```

Use:

```python
explicit_reconfirm = diff is not None
effective_diff = diff or build_action_diff(group=group, rungs=rungs)
```

Render action and escaped target ID; only explicit diffs use the reconfirm title. Reuse `_format_diff_side`.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run pytest tests/services/order_proposals/test_approval_message.py \
  tests/services/order_proposals/test_dispatch.py -q
git add app/services/order_proposals/approval_message.py \
  app/services/order_proposals/dispatch.py \
  tests/services/order_proposals/test_approval_message.py \
  tests/services/order_proposals/test_dispatch.py
git commit -m "feat(ROB-832): render proposal action diffs"
```

---

### Task 5: Record confirmed cancel lifecycle

**Files:**
- Modify: `app/services/order_proposals/state_machine.py`
- Modify: `app/services/order_proposals/service.py`
- Test: `tests/services/order_proposals/test_state_machine.py`
- Test: `tests/services/order_proposals/test_service.py`

**Interfaces:**
- Produces legal `submitting -> cancelled`.
- Produces `record_cancelled`.

- [ ] **Step 1: Write failing transition/writer tests**

```python
def test_confirmed_cancel_terminalizes_submitting():
    assert_rung_transition("submitting", "cancelled")


@pytest.mark.asyncio
async def test_record_cancelled_retains_target_id(db_session):
    service, group = await _create_cancel_proposal(db_session)
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(
            group.proposal_id, 0, new_state=state
        )
    rung = await service.record_cancelled(
        group.proposal_id, 0,
        broker_order_id="old-1", now=datetime.now(UTC),
    )
    assert rung.state == "cancelled"
    assert rung.broker_order_id == "old-1"
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/services/order_proposals/test_state_machine.py \
  tests/services/order_proposals/test_service.py -q
```

- [ ] **Step 3: Add transition and writer**

```python
async def record_cancelled(
    self, proposal_id: uuid.UUID, rung_index: int, *,
    broker_order_id: str, now: datetime,
) -> OrderProposalRung:
    self._require_timezone_aware(now)
    return await self.transition_rung(
        proposal_id, rung_index, new_state="cancelled",
        broker_order_id=broker_order_id,
        validated_at=now, updated_at=now,
    )
```

Add `cancelled` to the `submitting` allowed set only.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run pytest tests/services/order_proposals/test_state_machine.py \
  tests/services/order_proposals/test_service.py -q
git add app/services/order_proposals/state_machine.py \
  app/services/order_proposals/service.py \
  tests/services/order_proposals/test_state_machine.py \
  tests/services/order_proposals/test_service.py
git commit -m "feat(ROB-832): record confirmed proposal cancellation"
```

---

### Task 6: Implement strict replace/cancel orchestration

**Files:**
- Modify: `app/services/order_proposals/revalidation.py`
- Test: `tests/services/order_proposals/test_revalidation.py`

**Interfaces:**
- Extends `revalidate_and_submit` with injected target fetch and cancel functions.
- Produces `RungOutcome(result="cancelled")`.
- Preserves place and accepted-vs-filled behavior.

- [ ] **Step 1: Write failing replace ordering test**

```python
@pytest.mark.asyncio
async def test_replace_confirms_cancel_before_new_submit(db_session):
    service, group = await _create_replace_proposal(db_session)
    events = []
    snapshots = iter([
        _target_snapshot(status="open"),
        _target_snapshot(status="cancelled"),
    ])
    async def fetch_target_fn(**kwargs):
        snap = next(snapshots)
        events.append(f"fetch:{snap.status}")
        return snap
    async def cancel_target_fn(**kwargs):
        events.append("cancel")
        return {"success": True}
    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            events.append("preview")
            return {
                "success": True, "approval_hash": "fresh",
                "price": "43000", "quantity": "3.5",
            }
        events.append("submit")
        return {
            "success": True, "status": "resting",
            "broker_order_id": "new-1",
        }

    outcomes = await revalidate_and_submit(
        service=service, proposal_id=group.proposal_id,
        now=datetime.now(UTC), place_order_fn=place_order_fn,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )
    assert events == [
        "fetch:open", "preview", "cancel",
        "fetch:cancelled", "submit",
    ]
    assert outcomes[0].result == "submitted_resting"
```

- [ ] **Step 2: Write failing fail-closed and cancel-only tests**

Add separate tests for:

- target price drift and remaining-quantity drift: `rejected`, no cancel;
- preview guard and normalization diff: no cancel, existing result;
- cancel explicit rejection: `rejected`, no submit;
- cancel exception/confirmation exception/open confirmation/missing evidence: `unverified`, no submit;
- cancel action: fetch-open → cancel → fetch-cancelled, no preview/submit;
- manual target: no session-lineage lookup.

The key no-submit assertion is:

```python
async def forbidden_submit(**kwargs):
    if kwargs["dry_run"]:
        return _matching_preview()
    raise AssertionError(
        "replacement submit requires confirmed cancellation"
    )
```

- [ ] **Step 3: Run RED**

```bash
uv run pytest tests/services/order_proposals/test_revalidation.py -q
```

- [ ] **Step 4: Preserve place behavior behind action dispatch**

Rename existing `_revalidate_rung` to `_revalidate_place_rung` without behavior changes, run the old tests green, then dispatch:

```python
action = group.action or "place"
if action == "replace":
    outcome = await _revalidate_replace_rung(
        service=service,
        group=group,
        rung=rung,
        now=now,
        place_order_fn=place_order_fn,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
        correlation_mint=correlation_mint,
    )
elif action == "cancel":
    outcome = await _revalidate_cancel_rung(
        service=service,
        group=group,
        rung=rung,
        now=now,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )
else:
    outcome = await _revalidate_place_rung(
        service=service,
        group=group,
        rung=rung,
        now=now,
        place_order_fn=place_order_fn,
        correlation_mint=correlation_mint,
    )
```

- [ ] **Step 5: Implement exact target comparison**

```python
def _target_mismatch_reason(
    approved: TargetOrderSnapshot,
    fresh: TargetOrderSnapshot,
) -> str | None:
    if fresh.status != "open":
        return f"target_not_open:{fresh.status}"
    if Decimal(fresh.remaining_quantity) <= 0:
        return "target_has_no_remaining_quantity"
    for field in (
        "broker_order_id", "symbol", "side", "order_type",
        "limit_price", "remaining_quantity",
    ):
        if getattr(approved, field) != getattr(fresh, field):
            return f"target_snapshot_mismatch:{field}"
    return None
```

Transient fetch failure returns `revalidating -> pending_approval`. Deterministic mismatch transitions to `rejected`; add and test `revalidating -> rejected` if needed.

- [ ] **Step 6: Implement cancel and confirmation gate**

After target validation, replace runs the existing fresh dry-run. Only a matching preview proceeds. Both actions then transition to `approved -> submitting`:

```python
try:
    cancel_result = await _maybe_await(cancel_target_fn(
        order_id=group.target_broker_order_id,
        symbol=group.symbol,
        market=group.market,
        account_mode=group.account_mode,
    ))
except Exception as exc:
    await service.record_unverified(
        proposal_id, rung_index,
        reason=f"cancel_exception:{exc}", now=now,
    )
    return RungOutcome(
        rung_index, "unverified", {"error": str(exc)}
    )

if cancel_result.get("success") is not True:
    await service.record_rejected(
        proposal_id, rung_index,
        reason=str(cancel_result.get("error") or "cancel_rejected"),
        now=now,
    )
    return RungOutcome(
        rung_index, "error", {"error": "cancel_rejected"}
    )

try:
    confirmed = await _maybe_await(fetch_target_fn(
        order_id=group.target_broker_order_id,
        symbol=group.symbol,
        market=group.market,
        account_mode=group.account_mode,
        now=now,
    ))
except Exception as exc:
    await service.record_unverified(
        proposal_id, rung_index,
        reason=f"cancel_confirmation_error:{exc}", now=now,
    )
    return RungOutcome(
        rung_index, "unverified", {"error": str(exc)}
    )

if confirmed.status != "cancelled":
    await service.record_unverified(
        proposal_id, rung_index,
        reason=f"cancel_unconfirmed:{confirmed.status}", now=now,
    )
    return RungOutcome(
        rung_index, "unverified",
        {"error": "cancel_unconfirmed"},
    )
```

Cancel calls `record_cancelled`. Replace calls the existing submit and `_classify_submit` only after this block. The preview uses the existing arguments so quote, profit floor, balance, Paperclip, and sector guards rerun.

- [ ] **Step 7: Run GREEN, regression, and commit**

```bash
uv run pytest tests/services/order_proposals/test_revalidation.py -q
uv run pytest tests/services/order_proposals -q
git add app/services/order_proposals/revalidation.py \
  tests/services/order_proposals/test_revalidation.py
git commit -m "feat(ROB-832): confirm cancel before proposal replacement"
```

---

### Task 7: Integrate Telegram result reporting

**Files:**
- Modify: `app/services/order_proposals/telegram_callback.py`
- Test: `tests/services/order_proposals/test_telegram_callback.py`

**Interfaces:**
- Consumes `RungOutcome(result="cancelled")`.
- Preserves nonce, lease, reconfirm rotation, and commit-before-notify.

- [ ] **Step 1: Write failing callback tests**

```python
def test_result_summary_labels_confirmed_cancel():
    summary = _build_result_summary([
        RungOutcome(0, "cancelled", {})
    ])
    assert "취소 확인" in summary
```

Add a callback test using the existing event-session fake to assert commit occurs before Telegram edit for `cancelled`.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/services/order_proposals/test_telegram_callback.py -q
```

- [ ] **Step 3: Add minimal label**

```python
_RESULT_LABELS = {
    "submitted_acked": "체결 대기(접수)",
    "submitted_resting": "주문 유지(대기)",
    "guard_blocked": "가드에 의해 차단됨",
    "unverified": "확인 불가(수동 확인 필요)",
    "error": "오류",
    "needs_reconfirm": "재확인 필요",
    "cancelled": "취소 확인",
}
```

Do not create another callback branch; orchestration remains in `revalidate_and_submit`.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run pytest tests/services/order_proposals/test_telegram_callback.py -q
git add app/services/order_proposals/telegram_callback.py \
  tests/services/order_proposals/test_telegram_callback.py
git commit -m "feat(ROB-832): report confirmed proposal cancellations"
```

---

### Task 8: Document and verify the complete change

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/runbooks/order-proposals.md`

**Interfaces:**
- Documents public parameters, supported tuples, evidence, lineage, and recovery.

- [ ] **Step 1: Update MCP reference**

Document:

```text
order_proposal_create adds:
  action="place"
  target_broker_order_id=None

place   — target absent; one or more rungs.
replace — target required; exactly one new-order rung.
cancel  — target required; exactly one exact snapshot rung; no new submit.
```

List only `kis_live/equity_kr`, `kis_live/equity_us`, and `upbit/crypto` as supported target-action tuples.

- [ ] **Step 2: Update runbook and lineage SQL**

```sql
SELECT
  proposal_id, action, target_broker_order_id, lifecycle_state,
  source_asof->'target_order_snapshot' AS approved_target_snapshot
FROM review.order_proposals
WHERE proposal_id = :proposal_id;

SELECT rung_index, state, broker_order_id, correlation_id, void_reason
FROM review.order_proposal_rungs
WHERE proposal_pk = (
  SELECT id FROM review.order_proposals
  WHERE proposal_id = :proposal_id
);
```

Explain original ID vs replacement rung ID and require reconcile before retrying `unverified`.

- [ ] **Step 3: Run focused verification**

```bash
uv run pytest tests/services/order_proposals \
  tests/test_mcp_order_proposal_tools.py \
  tests/test_mcp_order_tools.py -q
uv run alembic heads
uv run alembic check
make lint
```

Expected: zero failures, one Alembic head, and clean lint.

- [ ] **Step 4: Run non-live repository gate**

```bash
uv run pytest -m "not live" -q
```

Expected: zero failures. If it times out, report exact completed selectors; do not claim success without exit 0.

- [ ] **Step 5: Review scope and commit docs**

```bash
git diff --check main...HEAD
git diff --stat main...HEAD
git status --short
git log --oneline main..HEAD
git add app/mcp_server/README.md docs/runbooks/order-proposals.md \
  docs/superpowers/specs/2026-07-11-rob-832-order-proposal-actions-design.md
git commit -m "docs(ROB-832): document proposal mutation actions"
```

Verify no section B policy changes, no approve/submit MCP tool, no live fixtures, place multi-rung coverage, manual-order support, and no replacement submit before cancellation confirmation.

## PR Preparation Checklist

After verification, use the ship workflow to push and open a PR without merging. Include:

```markdown
## Operator smoke checklist (no live orders in CI/dev)

- [ ] Inspect the additive action/target migration and confirm one Alembic head.
- [ ] Confirm proposal and Telegram feature flags before process restart.
- [ ] With broker functions mocked, create replace/cancel proposals for known open-order fixtures; creation performs reads only.
- [ ] Verify one Telegram message per target with target ID and before→after diff.
- [ ] Force cancel rejection and confirmation timeout; replacement submit count stays zero.
- [ ] Verify a manual/unattributed order can be targeted when broker evidence matches.
- [ ] For a future separately authorized live smoke, inspect fresh broker evidence before click and verify original cancellation before looking for a replacement ID.
- [ ] For `unverified`, stop retries and reconcile using original target ID and replacement correlation ID.
```

Do not merge. Report PR number/URL and exact test/lint evidence.
