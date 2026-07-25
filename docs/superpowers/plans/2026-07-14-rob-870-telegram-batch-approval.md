# ROB-870 Telegram Batch Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable Telegram “approve all” surface for manual order proposals while preserving every proposal's existing nonce, approval audit, revalidation, and individual message behavior.

**Architecture:** Add relational batch and membership records in the `review` schema, register successfully dispatched manual proposals into a ten-minute same-chat collection window, and render a summary after the second member. A batch click consumes only the batch trigger nonce, then processes each snapshotted proposal nonce through the existing `_handle_approve` path in an independent transaction so failures remain isolated.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async ORM, PostgreSQL advisory/row locks, Alembic, FastAPI Telegram webhook integration, pytest/pytest-asyncio, Ruff, ty.

## Global Constraints

- Runtime baseline is Python 3.13+ and all Python commands use `uv run`.
- Batch collection window is ten minutes from the first eligible member; new members do not extend it.
- Batch click TTL is ten minutes after the latest member, bounded by the earliest non-null member `valid_until`.
- Batch summary appears only at two or more members and is scoped to one Telegram chat.
- `loss_cut`, superseded/terminal, and already auto-approved proposals are excluded at registration and click time.
- A batch nonce is a single-use trigger only; every member's existing proposal nonce and approval audit remain mandatory.
- Each proposal executes in an independent transaction through existing `revalidate_and_submit`; one failure never stops later members.
- Existing individual approve/deny buttons and callbacks remain unchanged.
- ROB-861 shortfalls remain per-rung `needs_reconfirm` results with the existing fresh nonce/message behavior.
- Do not modify ROB-868 websocket or ROB-877 broker-gateway behavior.
- Keep repository imports internal to `service.py`; callers use `OrderProposalsService` only.
- Final gates are `uv run pytest tests/services/order_proposals/ -q` and `make lint`.

---

## File Map

- Create `alembic/versions/20260714_rob870_approval_batches.py`: additive batch tables, constraints, and indexes from the current head.
- Modify `app/models/order_proposals.py`: ORM rows for approval batches and members.
- Modify `app/models/__init__.py`: export the two new ORM types.
- Modify `tests/_schema_bootstrap.py`: bump persistent test-schema version for the new ORM tables.
- Modify `tests/services/order_proposals/test_models_smoke.py`: assert schema, columns, foreign keys, and uniqueness.
- Modify `app/services/order_proposals/approval_message.py`: `ba` callback support plus pending/final batch renderers.
- Modify `tests/services/order_proposals/test_approval_message.py`: pure callback/summary formatting tests.
- Modify `app/services/order_proposals/repository.py`: private CRUD, locked lookup, prefix resolution, membership, and update primitives.
- Modify `app/services/order_proposals/service.py`: eligibility rules, registration, TTL, nonce consumption, member snapshots/results, and summary audit.
- Modify `tests/services/order_proposals/test_service.py`: batch lifecycle and concurrency-facing invariants.
- Modify `app/services/order_proposals/dispatch.py`: register after individual send and send/edit the batch summary.
- Modify `tests/services/order_proposals/test_dispatch.py`: grouping windows, exclusions, summary send/edit, gate-off, and auto-approved behavior.
- Modify `app/services/order_proposals/telegram_callback.py`: batch callback orchestration over independent member sessions.
- Modify `tests/services/order_proposals/test_telegram_callback.py`: replay, TTL, partial failure, stale exclusion, individual edits, and ROB-861 mixture.
- Modify `docs/runbooks/order-proposals.md`: operator-facing batch contract and failure semantics.

---

### Task 1: Persist Approval Batches and Membership

**Files:**
- Create: `alembic/versions/20260714_rob870_approval_batches.py`
- Modify: `app/models/order_proposals.py`
- Modify: `app/models/__init__.py`
- Modify: `tests/_schema_bootstrap.py`
- Test: `tests/services/order_proposals/test_models_smoke.py`

**Interfaces:**
- Produces: `OrderProposalApprovalBatch` and `OrderProposalApprovalBatchMember` ORM models.
- Produces: database uniqueness on `batch_id`, `(batch_pk, proposal_pk)`, and `(proposal_pk, approval_nonce_snapshot)`.
- Consumes: existing `review.order_proposals.id` as the member foreign key.

- [ ] **Step 1: Write failing ORM contract tests**

Add imports and a test that asserts the exact table contracts:

```python
from app.models.order_proposals import (
    OrderProposalApprovalBatch,
    OrderProposalApprovalBatchMember,
)


@pytest.mark.unit
def test_approval_batch_models_are_durable_and_bound_to_proposals():
    batch = OrderProposalApprovalBatch.__table__
    member = OrderProposalApprovalBatchMember.__table__
    assert batch.schema == member.schema == "review"
    assert batch.name == "order_proposal_approval_batches"
    assert member.name == "order_proposal_approval_batch_members"
    assert {
        "batch_id", "chat_id", "window_started_at", "window_closes_at",
        "expires_at", "approval_nonce", "approval_nonce_used_at",
        "approved_by_telegram_user_id", "approved_at", "summary_message_id",
        "summary_dispatch_state", "summary_dispatch_lease_until",
    } <= set(batch.columns)
    assert {
        "batch_pk", "proposal_pk", "approval_nonce_snapshot",
        "approval_message_id", "result", "result_detail", "processed_at",
        "added_at",
    } <= set(member.columns)
    unique_names = {c.name for c in member.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_order_proposal_batch_member" in unique_names
    assert "uq_order_proposal_batch_member_nonce" in unique_names
```

- [ ] **Step 2: Run the model test and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_models_smoke.py::test_approval_batch_models_are_durable_and_bound_to_proposals -q`

Expected: collection fails because the two model classes do not exist.

- [ ] **Step 3: Add the ORM models and exports**

Add these model shapes after `OrderProposalRung`, using existing `Base`, `Mapped`, `mapped_column`, `JSONB`, `PG_UUID`, and timestamp conventions:

```python
class OrderProposalApprovalBatch(Base):
    __tablename__ = "order_proposal_approval_batches"
    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_order_proposal_approval_batches_batch_id"),
        CheckConstraint(
            "summary_dispatch_state IN ('idle','sending','sent')",
            name="order_proposal_approval_batches_summary_state",
        ),
        Index("ix_order_proposal_approval_batches_chat_window", "chat_id", "window_closes_at"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chat_id: Mapped[str] = mapped_column(Text, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    window_closes_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    approval_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    approval_nonce_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    approved_by_telegram_user_id: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    summary_message_id: Mapped[int | None] = mapped_column(BigInteger)
    summary_dispatch_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="idle")
    summary_dispatch_lease_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class OrderProposalApprovalBatchMember(Base):
    __tablename__ = "order_proposal_approval_batch_members"
    __table_args__ = (
        UniqueConstraint("batch_pk", "proposal_pk", name="uq_order_proposal_batch_member"),
        UniqueConstraint(
            "proposal_pk", "approval_nonce_snapshot",
            name="uq_order_proposal_batch_member_nonce",
        ),
        Index("ix_order_proposal_batch_members_batch_pk", "batch_pk"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_pk: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.order_proposal_approval_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposal_pk: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.order_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_nonce_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    approval_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result: Mapped[str | None] = mapped_column(Text)
    result_detail: Mapped[dict | None] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    added_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
```

Export both from `app/models/__init__.py`.

- [ ] **Step 4: Add the Alembic migration and bootstrap version bump**

Create revision `20260714_rob870_approval_batches` with
`down_revision = "20260713_rob848_paper_validation"`. Mirror the ORM types,
foreign keys, check, unique constraints, and indexes exactly. Downgrade in
reverse dependency order:

```python
def downgrade() -> None:
    op.drop_index(
        "ix_order_proposal_batch_members_batch_pk",
        table_name="order_proposal_approval_batch_members",
        schema="review",
    )
    op.drop_table("order_proposal_approval_batch_members", schema="review")
    op.drop_index(
        "ix_order_proposal_approval_batches_chat_window",
        table_name="order_proposal_approval_batches",
        schema="review",
    )
    op.drop_table("order_proposal_approval_batches", schema="review")
```

Increment `SCHEMA_BOOTSTRAP_VERSION` from `14` to `15` and document ROB-870 in
the adjacent version comments so persistent test databases rerun
`Base.metadata.create_all`.

- [ ] **Step 5: Run model and migration graph checks**

Run: `uv run pytest tests/services/order_proposals/test_models_smoke.py -q`

Expected: all tests pass.

Run: `uv run alembic heads`

Expected: exactly `20260714_rob870_approval_batches (head)`.

- [ ] **Step 6: Commit the persistence slice**

```bash
git add alembic/versions/20260714_rob870_approval_batches.py app/models/order_proposals.py app/models/__init__.py tests/_schema_bootstrap.py tests/services/order_proposals/test_models_smoke.py
git commit -m "feat(ROB-870): persist approval batches"
```

---

### Task 2: Define Batch Callback and Message Contracts

**Files:**
- Modify: `app/services/order_proposals/approval_message.py`
- Test: `tests/services/order_proposals/test_approval_message.py`

**Interfaces:**
- Produces: `build_batch_callback_data(*, batch_id: UUID, nonce: str) -> str`.
- Extends: `parse_callback_data(data: str) -> tuple[str, str, str]` with action `ba`.
- Produces: `build_batch_approval_message(*, batch, proposals) -> tuple[str, dict]`.
- Produces: `build_batch_result_message(*, proposals, results) -> str`.

- [ ] **Step 1: Write failing pure tests**

Add tests that use `SimpleNamespace` groups/rungs and assert:

```python
def test_batch_callback_round_trip_and_telegram_limit():
    batch_id = uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111")
    data = build_batch_callback_data(batch_id=batch_id, nonce="batch_nonce-1")
    assert parse_callback_data(data) == ("ba", "aaaaaaaa", "batch_nonce-1")
    assert len(data.encode()) <= 64


def test_batch_summary_lists_notional_and_account_subtotals():
    text, keyboard = build_batch_approval_message(
        batch=SimpleNamespace(
            batch_id=uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111"),
            approval_nonce="batch-nonce",
            expires_at=datetime(2026, 7, 14, 1, 30, tzinfo=UTC),
        ),
        proposals=[
            (_group("AAPL", "buy", "kis_live"), [_rung("1", "100")]),
            (_group("MSFT", "sell", "toss_live"), [_rung("2", "50")]),
        ],
    )
    assert "AAPL" in text and "MSFT" in text
    assert "kis_live" in text and "toss_live" in text
    assert "$200" in text
    assert keyboard["inline_keyboard"][0][0]["text"] == "전체 승인"
```

Add a result-render test with one approved, one `needs_reconfirm`, one stale,
and one error entry; assert all four categories appear and the keyboard is not
present in the result renderer.

- [ ] **Step 2: Run the pure tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_approval_message.py -q -k batch`

Expected: collection fails because the batch builders do not exist.

- [ ] **Step 3: Implement the callback parser extension**

Change `_ALLOWED_ACTIONS` and `_CALLBACK_PATTERN` to include `ba`, retain all
existing actions, and add a semantic wrapper:

```python
def build_batch_callback_data(*, batch_id: uuid.UUID, nonce: str) -> str:
    return build_callback_data(action="ba", proposal_id=batch_id, nonce=nonce)
```

Update `build_callback_data`'s validation message to list `ba` without changing
the existing compact `action:8-char-prefix:nonce` format.

- [ ] **Step 4: Implement pending and terminal renderers**

Use existing `_escape_markdown`, `_escape_inline_code`, `_format_decimal`, and
`_format_money`. Calculate notional as explicit rung `notional` when present,
otherwise `quantity * limit_price`; omit market orders from numeric totals.
Group subtotals by a redacted label consisting of `account_mode` plus the final
four characters of `broker_account_id` when present.

The pending renderer must produce:

```python
lines = ["*일괄 승인 대기*", f"- 제안: {len(proposals)}건", "", "*주문 목록*"]
# one escaped line per proposal and rung
# append total and account subtotal blocks
# append KST expiry line
keyboard = {"inline_keyboard": [[{
    "text": "전체 승인",
    "callback_data": build_batch_callback_data(
        batch_id=batch.batch_id,
        nonce=batch.approval_nonce,
    ),
}]]}
```

The terminal renderer groups result dictionaries by `approved`,
`needs_reconfirm`, `skipped`, and `failed`, includes symbol and rung result
labels, and returns text only so callers clear the button with an empty keyboard.

- [ ] **Step 5: Run message tests and full parser regressions**

Run: `uv run pytest tests/services/order_proposals/test_approval_message.py -q`

Expected: all tests pass, including existing `op`, `dn`, `lc`, and `vc` cases.

- [ ] **Step 6: Commit the pure contract slice**

```bash
git add app/services/order_proposals/approval_message.py tests/services/order_proposals/test_approval_message.py
git commit -m "feat(ROB-870): render batch approval messages"
```

---

### Task 3: Implement the Batch Lifecycle Service

**Files:**
- Modify: `app/services/order_proposals/repository.py`
- Modify: `app/services/order_proposals/service.py`
- Test: `tests/services/order_proposals/test_service.py`

**Interfaces:**
- Produces: `BatchRegistration` and `BatchMemberSnapshot` frozen dataclasses.
- Produces: `register_approval_batch_member(proposal_id: UUID, *, chat_id: str, approval_message_id: int, now: datetime, window_seconds: int = 600, ttl_seconds: int = 600) -> BatchRegistration | None`.
- Produces: `consume_approval_batch_nonce(batch_id: UUID, nonce: str, *, chat_id: str, telegram_user_id: str, now: datetime) -> tuple[OrderProposalApprovalBatch, list[BatchMemberSnapshot]]`.
- Produces: `record_approval_batch_summary(batch_id: UUID, *, message_id: int, now: datetime) -> OrderProposalApprovalBatch`.
- Produces: `release_approval_batch_summary_claim(batch_id: UUID, *, now: datetime) -> OrderProposalApprovalBatch`.
- Produces: `record_approval_batch_member_result(member_id: int, *, result: str, detail: dict[str, Any], now: datetime) -> OrderProposalApprovalBatchMember`.
- Produces: `get_approval_batch_display(batch_id: UUID) -> tuple[OrderProposalApprovalBatch, list[tuple[OrderProposal, list[OrderProposalRung]]]]`.
- Produces: `batch_member_block_reason(group, rungs, *, now) -> str | None`.

- [ ] **Step 1: Write failing service tests for registration and exclusions**

Cover these exact cases:

```python
registration = await service.register_approval_batch_member(
    group.proposal_id,
    chat_id="42",
    approval_message_id=1001,
    now=now,
)
assert registration is not None
assert registration.member_count == 1
assert registration.summary_action == "none"

second = await service.register_approval_batch_member(
    second_group.proposal_id,
    chat_id="42",
    approval_message_id=1002,
    now=now + timedelta(minutes=1),
)
assert second.batch.batch_id == registration.batch.batch_id
assert second.member_count == 2
assert second.summary_action == "send"
```

Add separate assertions that loss-cut, superseded, terminal, auto-approved,
used-nonce, no-pending-rung, expired-valid-until, and a different chat/window
do not join the first batch. Verify a proposal with a fresh replacement nonce
can join a later batch but the same nonce snapshot cannot be duplicated.

- [ ] **Step 2: Run registration tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_service.py -q -k approval_batch`

Expected: failures because lifecycle interfaces are absent.

- [ ] **Step 3: Add repository primitives**

Import the new models only in `repository.py`. Add methods with these exact
responsibilities:

```python
async def acquire_approval_batch_chat_lock(self, chat_id: str) -> None:
    await self._session.execute(
        select(func.pg_advisory_xact_lock(
            func.hashtextextended(f"order_proposals:approval_batch:{chat_id}", 0)
        ))
    )

async def get_open_approval_batch(
    self, *, chat_id: str, now: datetime, for_update: bool = False
) -> OrderProposalApprovalBatch | None:
    stmt = (
        select(OrderProposalApprovalBatch)
        .where(
            OrderProposalApprovalBatch.chat_id == chat_id,
            OrderProposalApprovalBatch.approval_nonce_used_at.is_(None),
            OrderProposalApprovalBatch.window_closes_at > now,
        )
        .order_by(OrderProposalApprovalBatch.id.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await self._session.execute(stmt)).scalar_one_or_none()

async def insert_approval_batch(self, **cols: Any) -> OrderProposalApprovalBatch:
    row = OrderProposalApprovalBatch(**cols)
    self._session.add(row)
    await self._session.flush()
    await self._session.refresh(row)
    return row

async def insert_approval_batch_member(
    self, **cols: Any
) -> OrderProposalApprovalBatchMember:
    row = OrderProposalApprovalBatchMember(**cols)
    self._session.add(row)
    await self._session.flush()
    await self._session.refresh(row)
    return row

async def list_approval_batch_members(
    self, batch_pk: int
) -> list[OrderProposalApprovalBatchMember]:
    stmt = (
        select(OrderProposalApprovalBatchMember)
        .where(OrderProposalApprovalBatchMember.batch_pk == batch_pk)
        .order_by(
            OrderProposalApprovalBatchMember.added_at,
            OrderProposalApprovalBatchMember.id,
        )
    )
    return list((await self._session.execute(stmt)).scalars().all())

async def get_approval_batch_by_id(
    self, batch_id: uuid.UUID, *, for_update: bool = False
) -> OrderProposalApprovalBatch | None:
    stmt = select(OrderProposalApprovalBatch).where(
        OrderProposalApprovalBatch.batch_id == batch_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await self._session.execute(stmt)).scalar_one_or_none()

async def resolve_approval_batch_id_prefix(
    self, batch_short: str
) -> uuid.UUID | None:
    stmt = (
        select(OrderProposalApprovalBatch.batch_id)
        .where(cast(OrderProposalApprovalBatch.batch_id, Text).like(f"{batch_short}%"))
        .limit(2)
    )
    matches = list((await self._session.execute(stmt)).scalars().all())
    return matches[0] if len(matches) == 1 else None

async def update_approval_batch(
    self, batch: OrderProposalApprovalBatch, **fields: Any
) -> OrderProposalApprovalBatch:
    for key, value in fields.items():
        setattr(batch, key, value)
    await self._session.flush()
    return batch

async def update_approval_batch_member(
    self, member: OrderProposalApprovalBatchMember, **fields: Any
) -> OrderProposalApprovalBatchMember:
    for key, value in fields.items():
        setattr(member, key, value)
    await self._session.flush()
    return member
```

`get_open_approval_batch` filters matching chat, unused nonce,
`window_closes_at > now`, and orders newest first. Prefix resolution casts UUID
to text and fails closed unless exactly one row matches.

- [ ] **Step 4: Implement service dataclasses and eligibility**

Add:

```python
@dataclass(frozen=True)
class BatchMemberSnapshot:
    member_id: int
    proposal_id: uuid.UUID
    approval_nonce: str
    approval_message_id: int


@dataclass(frozen=True)
class BatchRegistration:
    batch: OrderProposalApprovalBatch
    member_count: int
    summary_action: Literal["none", "send", "edit"]
```

`batch_member_block_reason` returns stable strings in this order:
`proposal_approval_block_reason`, `loss_cut_excluded`, `auto_approved_excluded`,
`proposal_expired`, `approval_nonce_missing`, `approval_nonce_used`, and
`no_pending_approval_rungs`.

Registration acquires the chat advisory lock, rechecks eligibility under the
proposal row lock, creates or joins a batch, snapshots the current proposal
nonce/message, recalculates `expires_at`, and returns `send` at member two,
`edit` for later members when a summary ID exists, otherwise `none`.

When a send is needed, registration atomically changes
`summary_dispatch_state` from `idle` to `sending` and sets a 30-second lease.
While that lease is live, concurrent registrants return `none`. An expired
`sending` lease can be reclaimed. `record_approval_batch_summary` writes the
message ID, changes state to `sent`, and clears the lease;
`release_approval_batch_summary_claim` changes a failed claim back to `idle`
and clears its lease. Existing summaries return `edit` while the batch window
is open.

- [ ] **Step 5: Write and run failing nonce/TTL tests**

Test consumption with the exact batch nonce, matching chat, and two members.
Then test replay, wrong nonce, wrong chat, expiry, ambiguous prefix, and fewer
than two members. Expected error reasons:

```text
approval_batch_nonce_mismatch
approval_batch_nonce_replay
approval_batch_chat_mismatch
approval_batch_expired
approval_batch_not_found
approval_batch_too_small
```

Run: `uv run pytest tests/services/order_proposals/test_service.py -q -k 'approval_batch and (nonce or expiry or prefix)'`

Expected: RED until consumption is implemented.

- [ ] **Step 6: Implement consumption, display, and result audit**

`consume_approval_batch_nonce` locks the batch, validates chat/nonce/replay/TTL,
loads at least two ordered members, writes approver/used timestamps, and returns
immutable `BatchMemberSnapshot` values. `record_approval_batch_member_result`
stores only JSON-safe bounded detail; it never mutates proposal/rung truth.
`record_approval_batch_summary` records message ID and resets delivery lease.
`release_approval_batch_summary_claim` makes a failed send retryable without
deleting membership.
`get_approval_batch_display` returns the batch plus ordered `(group, rungs)`
pairs for pure rendering.

- [ ] **Step 7: Run service tests**

Run: `uv run pytest tests/services/order_proposals/test_service.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit the lifecycle slice**

```bash
git add app/services/order_proposals/repository.py app/services/order_proposals/service.py tests/services/order_proposals/test_service.py
git commit -m "feat(ROB-870): manage approval batch lifecycle"
```

---

### Task 4: Add Batch Summary Dispatch

**Files:**
- Modify: `app/services/order_proposals/dispatch.py`
- Test: `tests/services/order_proposals/test_dispatch.py`

**Interfaces:**
- Consumes: `register_approval_batch_member`, `get_approval_batch_display`, `record_approval_batch_summary`.
- Consumes: `build_batch_approval_message`.
- Preserves: `send_proposal_for_approval(proposal_id: UUID, *, notifier: Any, now: datetime, service_factory: ServiceFactory = AsyncSessionLocal) -> int | None` return value as the individual message ID.

- [ ] **Step 1: Extend the notifier fake and write failing dispatch tests**

Add `edited_messages` and `edit_message` to `_FakeNotifier`, and make message IDs
increment per send so individual and summary IDs differ.

Test sequential dispatch:

```python
first_id = await send_proposal_for_approval(
    first.proposal_id,
    notifier=notifier,
    now=now,
    service_factory=_session_factory(db_session),
)
assert len(notifier.sent_messages) == 1  # individual only

second_id = await send_proposal_for_approval(
    second.proposal_id,
    notifier=notifier,
    now=now + timedelta(minutes=1),
    service_factory=_session_factory(db_session),
)
assert second_id != first_id
assert len(notifier.sent_messages) == 3  # second individual + batch summary
assert "전체 승인" in str(notifier.sent_messages[-1][1])

await send_proposal_for_approval(
    third.proposal_id,
    notifier=notifier,
    now=now + timedelta(minutes=2),
    service_factory=_session_factory(db_session),
)
assert notifier.edited_messages
assert "3건" in notifier.edited_messages[-1][2]
```

Add tests for different chat/window, loss-cut, superseded/terminal,
auto-approved, individual-send failure, and auto gate-off. Assert every
individual message/button remains present and unchanged.

- [ ] **Step 2: Run dispatch batch tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_dispatch.py -q -k batch`

Expected: no batch summary is sent or edited.

- [ ] **Step 3: Implement post-send registration and summary delivery**

After a successful individual send and `record_approval_dispatch`, call a new
private helper in the same transaction:

```python
async def _register_and_publish_batch_summary(
    *, service, proposal_id, message_id, chat_id, now, notifier
) -> None:
    registration = await service.register_approval_batch_member(
        proposal_id,
        chat_id=str(chat_id),
        approval_message_id=message_id,
        now=now,
    )
    if registration is None or registration.summary_action == "none":
        return
    batch, proposals = await service.get_approval_batch_display(
        registration.batch.batch_id
    )
    text, keyboard = build_batch_approval_message(batch=batch, proposals=proposals)
    if registration.summary_action == "send":
        summary_id = await notifier.send_approval_message(
            text, keyboard, chat_id=str(chat_id)
        )
        if summary_id is not None:
            await service.record_approval_batch_summary(
                batch.batch_id, message_id=summary_id, now=now
            )
    elif batch.summary_message_id is not None:
        await notifier.edit_message(
            chat_id, batch.summary_message_id, text, reply_markup=keyboard
        )
```

Catch Telegram exceptions, log them, clear a failed summary delivery claim,
and still commit the individual dispatch plus membership. Do not change the
function's individual message-ID return contract.

- [ ] **Step 4: Run dispatch tests and ROB-871 regression tests**

Run: `uv run pytest tests/services/order_proposals/test_dispatch.py -q`

Expected: all tests pass; auto-submitted proposals still send only their veto
notification and never join manual batches.

- [ ] **Step 5: Commit the dispatch slice**

```bash
git add app/services/order_proposals/dispatch.py tests/services/order_proposals/test_dispatch.py
git commit -m "feat(ROB-870): publish approval batch summaries"
```

---

### Task 5: Execute Batch Clicks with Isolated Member Transactions

**Files:**
- Modify: `app/services/order_proposals/telegram_callback.py`
- Test: `tests/services/order_proposals/test_telegram_callback.py`

**Interfaces:**
- Consumes: `parse_callback_data` action `ba`.
- Consumes: `consume_approval_batch_nonce`, member snapshots, and result audit.
- Reuses: `_handle_approve` without bypassing proposal nonce/audit/revalidation.
- Produces: `_handle_batch_approve(*, batch_short: str, nonce: str, now: datetime, service_factory: ServiceFactory, notifier: Any, chat_id: Any, message_id: int | None, telegram_user_id: str, revalidate_fn: RevalidateFn) -> dict[str, Any]`.

- [ ] **Step 1: Write failing replay/TTL and regression tests**

Seed a two-member batch through the service and call `handle_callback_update`
with its `ba` callback. Assert both proposal nonces become used and each
proposal gets `approved_at`/`approved_by_telegram_user_id`. Call the same update
again and assert `approval_batch_nonce_replay` with no extra revalidation.

Seed an expired batch and assert no proposal nonce is consumed. Re-run one
existing individual `op` test unchanged to guard callback regression.

- [ ] **Step 2: Run targeted callback tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_telegram_callback.py -q -k 'batch or valid_approve'`

Expected: `ba` resolves as a proposal callback or returns not found; batch
members are not processed.

- [ ] **Step 3: Add the top-level batch branch and nonce consumption**

Branch immediately after callback parsing, before proposal prefix resolution:

```python
if action == "ba":
    return await _handle_batch_approve(
        batch_short=subject_short,
        nonce=nonce,
        now=now,
        service_factory=service_factory,
        notifier=active_notifier,
        chat_id=chat_id,
        message_id=message_id,
        telegram_user_id=str(telegram_user_id or ""),
        revalidate_fn=revalidate_fn,
    )
```

`_handle_batch_approve` resolves and consumes the batch in an initial session,
commits the trigger audit before any broker work, then closes that transaction.

- [ ] **Step 4: Write failing partial-failure and stale-member tests**

Use three members and an injected `revalidate_fn` that returns an error for the
middle proposal but success for the first and third. Assert call order includes
all three, member results are persisted, and all three individual message IDs
are edited.

Add click-time cases where one member is superseded, terminal, loss-cut, or
already individually approved after batch creation. Assert only that member is
`skipped`, later members continue, and the individual nonce audit is not forged.

- [ ] **Step 5: Implement the isolated member loop**

For each `BatchMemberSnapshot`, open a new `service_factory` context. Re-read
the group/rungs and skip `loss_cut` or `auto_approved` before `_handle_approve`.
Otherwise call:

```python
result = await _handle_approve(
    session=member_session,
    service=member_service,
    proposal_id=member.proposal_id,
    nonce=member.approval_nonce,
    now=now,
    notifier=notifier,
    chat_id=chat_id,
    message_id=member.approval_message_id,
    callback_query_id=None,
    telegram_user_id=telegram_user_id,
    revalidate_fn=revalidate_fn,
)
```

Map handled approved results to `approved`, existing `needs_reconfirm` to
`needs_reconfirm`, nonce/lifecycle/eligibility misses to `skipped`, and
unexpected exceptions to `failed`. Roll back a failed member session before
recording its bounded failure detail, then continue.

After every member, persist its batch-observation result and commit. Finally
load display rows, render `build_batch_result_message`, and best-effort edit the
batch summary with `reply_markup={"inline_keyboard": []}`.

- [ ] **Step 6: Add the mixed ROB-861 test**

Inject outcomes so one proposal yields `needs_reconfirm` with
`reason="insufficient_buying_power"` while another yields
`submitted_resting`. Assert:

- the shortfall proposal has a fresh unused proposal nonce;
- its original message and new reconfirm message follow existing behavior;
- the other proposal completes;
- the batch summary contains both `재확인 필요` and `승인 완료` categories.

- [ ] **Step 7: Run the callback file**

Run: `uv run pytest tests/services/order_proposals/test_telegram_callback.py -q`

Expected: all batch and existing individual/loss-cut/veto tests pass.

- [ ] **Step 8: Commit the callback slice**

```bash
git add app/services/order_proposals/telegram_callback.py tests/services/order_proposals/test_telegram_callback.py
git commit -m "feat(ROB-870): execute batch approvals safely"
```

---

### Task 6: Document the Operator Contract and Verify the Whole Feature

**Files:**
- Modify: `docs/runbooks/order-proposals.md`

**Interfaces:**
- Documents: grouping, TTL, exclusions, partial failure, ROB-861 interaction,
  gate-off behavior, and unchanged individual path.
- Verifies: the complete order-proposal service surface and repository quality gates.

- [ ] **Step 1: Add the runbook section**

Document these exact facts near the existing Telegram callback section:

```markdown
### Manual approval batches (ROB-870)

- A successfully sent manual proposal joins the open batch for the same chat
  for up to 10 minutes from the first member.
- The summary appears at member two; individual messages and buttons remain.
- The batch trigger expires 10 minutes after the latest member, bounded by the
  earliest proposal validity deadline, and is single-use.
- Loss cuts, superseded/terminal proposals, and already auto-approved proposals
  are excluded. Gate-off manual fallback proposals may batch.
- Clicking the batch consumes each member's original nonce and runs the normal
  revalidation/submission transaction independently. Partial failure does not
  stop later members.
- ROB-861 buying-power misses stay `needs_reconfirm` only for affected rungs.
```

Also include the 2026-07-13 DB basis: valid 16 groups/17 rungs, manual 13/14,
cap 9/9, distance 3/3, actions 0/0, other multi-rung 1/2; explicitly state that
the structural value is not based on the temporary $150 canary cap.

- [ ] **Step 2: Run focused batch tests**

Run: `uv run pytest tests/services/order_proposals/test_models_smoke.py tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_service.py tests/services/order_proposals/test_dispatch.py tests/services/order_proposals/test_telegram_callback.py -q -k 'batch or individual or approve or loss_cut or buying_power or auto'`

Expected: all selected tests pass.

- [ ] **Step 3: Run the complete order-proposal suite**

Run: `uv run pytest tests/services/order_proposals/ -q`

Expected: all tests pass with zero failures.

- [ ] **Step 4: Run formatting, lint, and type checks**

Run: `make lint`

Expected: Ruff formatting/check and ty complete successfully.

- [ ] **Step 5: Inspect the final diff and migration graph**

Run: `git diff --check && uv run alembic heads && git status --short`

Expected: no whitespace errors, exactly one ROB-870 Alembic head, and only
intended ROB-870 files modified.

- [ ] **Step 6: Commit documentation/final mechanical fixes**

```bash
git add docs/runbooks/order-proposals.md
git add -u
git commit -m "docs(ROB-870): document batch approval operations"
```

---

### Task 7: Pre-PR Evidence and Shipping

**Files:**
- No product-code changes unless verification exposes a defect.
- PR body assembled from `docs/superpowers/specs/2026-07-14-rob-870-telegram-batch-approval-design.md` and fresh command output.

**Interfaces:**
- Produces: pushed `rob-870` branch and PR against `main`.
- Produces: PR evidence table separating cap from structural manual remainder.

- [ ] **Step 1: Run fresh completion verification**

Run:

```bash
uv run pytest tests/services/order_proposals/ -q
make lint
git diff "$(git merge-base origin/main HEAD)" HEAD --check
```

Expected: every command exits 0 with fresh output from this step.

- [ ] **Step 2: Review the final diff for safety invariants**

Confirm from the diff:

- no loss-cut batch entry path;
- batch nonce and proposal nonce are both consumed in their own scopes;
- member transactions commit independently;
- existing individual callback branches remain covered;
- no websocket or broker-gateway edits;
- no credentials, raw account IDs, or payload hashes are rendered.

- [ ] **Step 3: Prepare the PR body evidence table**

Include:

| Manual reason | Groups | Rungs | Structural? |
|---|---:|---:|---|
| Per-order cap above $150 | 9 | 9 | No — canary seed may rise |
| Distance below 3% | 3 | 3 | Yes |
| Replace/cancel action | 0 | 0 | Yes, absent in sampled session |
| Other: multi-rung fallback | 1 | 2 | Yes |
| **Total** | **13** | **14** | **Structural floor: 4 groups/5 rungs** |

State that raw DB count was 19/20, ROB-869 excluded 3/3, and the canonical
valid denominator is 16/17. Mention that gate-off/rollback periods route all
valid proposals through the manual batch-capable path.

- [ ] **Step 4: Ship with the repository ship workflow**

Use the `ship` skill to update the branch from `origin/main`, rerun required
checks, review the diff, push `rob-870`, and create a PR with base `main` and a
title beginning `feat(ROB-870):`.

- [ ] **Step 5: Report completion**

Report the proceed decision, canonical DB breakdown, test/lint results, commit
range, and PR number/link. If shipping is blocked by external authentication,
report the exact completed local state and command that failed.
