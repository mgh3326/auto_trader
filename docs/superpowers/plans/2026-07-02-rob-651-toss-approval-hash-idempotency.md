# ROB-651 P6-A — Toss approval-hash + content-based clientOrderId Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind `toss_preview_order` → `toss_place_order` with a content-based approval token (TTL 5min, fail-closed on mismatch) and replace uuid4 `clientOrderId` with a deterministic content+trading-day-salt idempotency key.

**Architecture:** A new pure module `toss_approval.py` holds all hash/token/salt primitives (no DB, no network, `now` injected → fully unit-testable). `orders_toss_variants.py` wires it into preview (emit token + deterministic clientOrderId) and place (verify token under a config-gated rollout mode + deterministic clientOrderId). The `toss_live_order_ledger` gains an additive nullable `approval_hash` column storing the content digest of every placed order.

**Tech Stack:** Python 3.13, FastMCP tools, SQLAlchemy async ORM, Alembic, pytest (`uv run pytest`).

## Global Constraints

- **Scope: Toss only.** KIS/Upbit shared-path port is P6-B (ROB-653) — do not touch shared `_place_order_impl`.
- **Rollout default = `optional`.** Existing `toss_place_order` calls without `approval_hash` must keep working (back-compat).
- **Deterministic clientOrderId is always ON** (independent of `approval_hash` presence/mode).
- **Migration is additive + nullable only.** `down_revision = "20260702_rob641"` (current head). One migration.
- **`record_send` replay contract unchanged**: query-first on `client_order_id`; existing row returned as-is, approval_hash never overwritten on replay.
- **No new infra dependency** on the place path (stateless token; no Redis).
- **Trust model** (issue §5): token is accident-prevention, not adversary defense — single operator confirm-as-approval.
- Constants: `APPROVAL_TTL_SECONDS = 300`, token version `"p6a1"`, digest prefix `"p6a"`, clientOrderId prefix `"tossp6"`.
- Run tests with `uv run pytest`. Lint with `uv run ruff format app/ tests/ && uv run ruff check app/ tests/` before final commit (CI lints `app/` **and** `tests/`).

---

## File Structure

- **Create** `app/mcp_server/tooling/toss_approval.py` — pure primitives (canonical, digest, clientOrderId, trading-day salt, token encode/decode/verify). No DB/network.
- **Create** `tests/test_toss_approval.py` — pure unit tests for the module.
- **Modify** `app/models/review.py` — add `approval_hash` column to `TossLiveOrderLedger`.
- **Create** `alembic/versions/20260702_rob651_toss_approval_hash.py` — additive nullable column.
- **Modify** `app/services/toss_live_order_ledger_service.py` — `record_send(..., approval_hash=None)`.
- **Modify** `app/mcp_server/tooling/toss_live_ledger.py` — `record_toss_place_order(..., approval_hash=None)` pass-through.
- **Modify** `app/core/config.py` — add `toss_approval_hash_mode: str = "optional"`.
- **Modify** `app/mcp_server/tooling/orders_toss_variants.py` — wire preview + place.
- **Modify** `tests/test_mcp_toss_order_variants.py` — tool-level tests.
- **Modify** `tests/test_rob538_toss_live_ledger_schema.py` — ledger column/record_send test.

---

## Task 1: Pure approval module (`toss_approval.py`)

**Files:**
- Create: `app/mcp_server/tooling/toss_approval.py`
- Test: `tests/test_toss_approval.py`

**Interfaces:**
- Consumes: `app.core.timezone.KST`.
- Produces:
  - `build_canonical_payload(*, market: str, symbol: str, side: str, order_type: str, time_in_force: str, quantity: str | None, price: str | None, order_amount: str | None) -> dict[str, Any]` — `quantity/price/order_amount` are already-stringified wire values (post tick-snap) or `None`.
  - `derive_approval_digest(canonical: dict) -> str` → `"p6a-<16hex>"`.
  - `trading_day_salt(market: str, now: datetime) -> str` → ISO date; `us` → ET, else KST.
  - `derive_client_order_id(canonical: dict, *, market: str, now: datetime, rung: str | int | None = None) -> str` → `"tossp6-<16hex>"`.
  - `encode_approval_token(canonical: dict, *, now: datetime) -> str` → `"p6a1.<b64url>"`.
  - `decode_approval_token(token: str) -> tuple[int, dict]` → `(iat_epoch, previewed_canonical)`; raises `ValueError` on bad token.
  - `verify_approval_token(token: str, placing_canonical: dict, *, now: datetime) -> ApprovalResult`.
  - `ApprovalResult` dataclass: `ok: bool`, `error_code: str | None`, `message: str | None`, `diff: dict | None`, `digest: str | None`.
  - `APPROVAL_TTL_SECONDS: int = 300`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_toss_approval.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.timezone import KST
from app.mcp_server.tooling.toss_approval import (
    APPROVAL_TTL_SECONDS,
    build_canonical_payload,
    decode_approval_token,
    derive_approval_digest,
    derive_client_order_id,
    encode_approval_token,
    trading_day_salt,
    verify_approval_token,
)

_ET = ZoneInfo("America/New_York")


def _canon(**overrides):
    base = dict(
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity="10",
        price="70000",
        order_amount=None,
    )
    base.update(overrides)
    return build_canonical_payload(**base)


def test_canonical_uppercases_and_is_stable():
    a = _canon()
    b = _canon()
    assert a == b
    assert a["side"] == "BUY"
    assert a["orderType"] == "LIMIT"
    assert derive_approval_digest(a) == derive_approval_digest(b)


def test_canonical_price_change_changes_digest():
    assert derive_approval_digest(_canon(price="70000")) != derive_approval_digest(
        _canon(price="70100")
    )


def test_amount_based_buy_hashes_wire_payload():
    canon = _canon(quantity=None, price=None, order_amount="1000000")
    digest = derive_approval_digest(canon)
    assert digest.startswith("p6a-")
    assert len(digest) == len("p6a-") + 16


def test_trading_day_salt_kr_uses_kst_us_uses_et():
    # 2026-07-02 23:30 KST == 2026-07-02 10:30 ET (same US calendar date here)
    now = datetime(2026, 7, 2, 23, 30, tzinfo=KST)
    assert trading_day_salt("kr", now) == "2026-07-02"
    assert trading_day_salt("us", now) == now.astimezone(_ET).date().isoformat()


def test_client_order_id_deterministic_same_day():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canon = _canon()
    assert derive_client_order_id(canon, market="kr", now=now) == derive_client_order_id(
        canon, market="kr", now=now
    )


def test_client_order_id_changes_next_trading_day():
    canon = _canon()
    today = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    tomorrow = datetime(2026, 7, 3, 10, 0, tzinfo=KST)
    assert derive_client_order_id(canon, market="kr", now=today) != derive_client_order_id(
        canon, market="kr", now=tomorrow
    )


def test_client_order_id_rung_discriminator_splits():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canon = _canon()
    base = derive_client_order_id(canon, market="kr", now=now)
    r2 = derive_client_order_id(canon, market="kr", now=now, rung=2)
    assert base != r2
    assert r2.startswith("tossp6-")


def test_client_order_id_is_safe_segment():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    cid = derive_client_order_id(_canon(), market="kr", now=now)
    assert cid.replace("-", "").replace("_", "").isalnum()
    assert len(cid) <= 40


def test_token_roundtrip_and_verify_ok():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    canon = _canon()
    token = encode_approval_token(canon, now=now)
    assert token.startswith("p6a1.")
    iat, decoded = decode_approval_token(token)
    assert decoded == canon
    result = verify_approval_token(token, canon, now=now)
    assert result.ok is True
    assert result.digest == derive_approval_digest(canon)


def test_verify_mismatch_returns_diff():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    token = encode_approval_token(_canon(price="70000"), now=now)
    result = verify_approval_token(token, _canon(price="70100"), now=now)
    assert result.ok is False
    assert result.error_code == "approval_hash_mismatch"
    assert "price" in result.diff
    assert result.diff["price"] == {"previewed": "70000", "placing": "70100"}


def test_verify_expired_after_ttl():
    issued = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    token = encode_approval_token(_canon(), now=issued)
    later = issued + timedelta(seconds=APPROVAL_TTL_SECONDS + 1)
    result = verify_approval_token(token, _canon(), now=later)
    assert result.ok is False
    assert result.error_code == "approval_expired"


def test_verify_within_ttl_ok():
    issued = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    token = encode_approval_token(_canon(), now=issued)
    later = issued + timedelta(seconds=APPROVAL_TTL_SECONDS - 1)
    assert verify_approval_token(token, _canon(), now=later).ok is True


def test_verify_invalid_token():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    result = verify_approval_token("not-a-token", _canon(), now=now)
    assert result.ok is False
    assert result.error_code == "invalid_approval_hash"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_toss_approval.py -q`
Expected: FAIL / collection error — `ModuleNotFoundError: app.mcp_server.tooling.toss_approval`.

- [ ] **Step 3: Write the module**

Create `app/mcp_server/tooling/toss_approval.py`:

```python
"""ROB-651 P6-A — Toss approval-hash + content-based clientOrderId primitives.

Pure helpers (no DB, no network, ``now`` injected) shared by
``toss_preview_order`` and ``toss_place_order`` so a previewed order and the
placed order are bound to the same canonical content, and the Toss
``clientOrderId`` is a deterministic content + trading-day-salt idempotency key
instead of a fresh uuid4 per call.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.timezone import KST

APPROVAL_TOKEN_VERSION = "p6a1"
APPROVAL_DIGEST_PREFIX = "p6a"
CLIENT_ORDER_ID_PREFIX = "tossp6"
APPROVAL_TTL_SECONDS = 300

_ET = ZoneInfo("America/New_York")


@dataclass
class ApprovalResult:
    ok: bool
    error_code: str | None = None
    message: str | None = None
    diff: dict[str, Any] | None = None
    digest: str | None = None


def build_canonical_payload(
    *,
    market: str,
    symbol: str,
    side: str,
    order_type: str,
    time_in_force: str,
    quantity: str | None,
    price: str | None,
    order_amount: str | None,
) -> dict[str, Any]:
    """Canonical order content shared by preview and place.

    ``quantity``/``price``/``order_amount`` must already be the stringified
    wire values (post tick-snap) or ``None`` so preview and place derive an
    identical digest. ``clientOrderId`` and ``confirmHighValueOrder`` are
    intentionally excluded (the former is derived from this; the latter is an
    operator flag, not economic intent).
    """
    return {
        "market": market,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
        "quantity": quantity,
        "price": price,
        "orderAmount": order_amount,
    }


def _canonical_json(canonical: dict[str, Any]) -> str:
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def derive_approval_digest(canonical: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(canonical).encode()).hexdigest()[:16]
    return f"{APPROVAL_DIGEST_PREFIX}-{digest}"


def trading_day_salt(market: str, now: datetime) -> str:
    """ISO trading-day date. ``us`` → America/New_York (DST-aware), else KST."""
    tz = _ET if market == "us" else KST
    return now.astimezone(tz).date().isoformat()


def derive_client_order_id(
    canonical: dict[str, Any],
    *,
    market: str,
    now: datetime,
    rung: str | int | None = None,
) -> str:
    salt = trading_day_salt(market, now)
    disc = "" if rung is None else str(rung)
    blob = f"{_canonical_json(canonical)}|{salt}|{disc}".encode()
    digest = hashlib.sha256(blob).hexdigest()[:16]
    return f"{CLIENT_ORDER_ID_PREFIX}-{digest}"


def encode_approval_token(canonical: dict[str, Any], *, now: datetime) -> str:
    payload = json.dumps(
        {"iat": int(now.timestamp()), "canon": canonical},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    blob = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{APPROVAL_TOKEN_VERSION}.{blob}"


def decode_approval_token(token: str) -> tuple[int, dict[str, Any]]:
    version, _, blob = token.partition(".")
    if version != APPROVAL_TOKEN_VERSION or not blob:
        raise ValueError("unsupported approval token")
    pad = "=" * (-len(blob) % 4)
    raw = base64.urlsafe_b64decode(blob + pad)
    obj = json.loads(raw)
    iat = int(obj["iat"])
    canon = obj["canon"]
    if not isinstance(canon, dict):
        raise ValueError("malformed approval token payload")
    return iat, canon


def _diff_canonical(
    previewed: dict[str, Any], placing: dict[str, Any]
) -> dict[str, Any]:
    keys = set(previewed) | set(placing)
    return {
        key: {"previewed": previewed.get(key), "placing": placing.get(key)}
        for key in sorted(keys)
        if previewed.get(key) != placing.get(key)
    }


def verify_approval_token(
    token: str, placing_canonical: dict[str, Any], *, now: datetime
) -> ApprovalResult:
    try:
        iat, previewed = decode_approval_token(token)
    except Exception:
        return ApprovalResult(
            ok=False,
            error_code="invalid_approval_hash",
            message=(
                "approval_hash is not a valid approval token; re-preview required"
            ),
        )
    if int(now.timestamp()) - iat > APPROVAL_TTL_SECONDS:
        return ApprovalResult(
            ok=False,
            error_code="approval_expired",
            message="approval_hash expired; re-preview required",
        )
    if previewed != placing_canonical:
        return ApprovalResult(
            ok=False,
            error_code="approval_hash_mismatch",
            message="placing order does not match previewed order",
            diff=_diff_canonical(previewed, placing_canonical),
        )
    return ApprovalResult(ok=True, digest=derive_approval_digest(placing_canonical))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_toss_approval.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/toss_approval.py tests/test_toss_approval.py
git commit -m "feat(ROB-651): pure Toss approval-hash + clientOrderId primitives

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Ledger column + migration + service pass-through

**Files:**
- Modify: `app/models/review.py` (class `TossLiveOrderLedger`, after `report_item_uuid` column ~`app/models/review.py:537`)
- Create: `alembic/versions/20260702_rob651_toss_approval_hash.py`
- Modify: `app/services/toss_live_order_ledger_service.py:66-155` (`record_send`)
- Modify: `app/mcp_server/tooling/toss_live_ledger.py:55` (`record_toss_place_order`)
- Test: `tests/test_rob538_toss_live_ledger_schema.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `TossLiveOrderLedger.approval_hash: Mapped[str | None]` (Text, nullable).
  - `record_send(..., approval_hash: str | None = None)` — sets column on insert; **never** overwrites on replay.
  - `record_toss_place_order(..., approval_hash: str | None = None)` — pass-through.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_rob538_toss_live_ledger_schema.py` (append at end of file):

```python
def test_toss_live_order_ledger_has_approval_hash_column():
    from app.models.review import TossLiveOrderLedger

    col = TossLiveOrderLedger.__table__.columns["approval_hash"]
    assert col.nullable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob538_toss_live_ledger_schema.py::test_toss_live_order_ledger_has_approval_hash_column -q`
Expected: FAIL — `KeyError: 'approval_hash'`.

- [ ] **Step 3: Add the model column**

In `app/models/review.py`, inside `TossLiveOrderLedger`, immediately after the `report_item_uuid` column line:

```python
    approval_hash: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rob538_toss_live_ledger_schema.py::test_toss_live_order_ledger_has_approval_hash_column -q`
Expected: PASS.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/20260702_rob651_toss_approval_hash.py`:

```python
"""add approval_hash to toss_live_order_ledger (ROB-651)

Revision ID: 20260702_rob651
Revises: 20260702_rob641
Create Date: 2026-07-02

Additive nullable column storing the content digest (``p6a-<16hex>``) of the
placed order's canonical payload — the approval-hash binding between
toss_preview_order and toss_place_order (ROB-651 P6-A).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260702_rob651"
down_revision: str | Sequence[str] | None = "20260702_rob641"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "toss_live_order_ledger",
        sa.Column("approval_hash", sa.Text(), nullable=True),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column(
        "toss_live_order_ledger", "approval_hash", schema="review"
    )
```

- [ ] **Step 6: Verify migration chains from head (offline check)**

Run: `uv run alembic heads`
Expected: single head `20260702_rob651` (no multi-head). If `alembic` needs a DB and is unavailable, instead grep-confirm `down_revision` matches the prior head:
Run: `grep -h "^revision\|^down_revision" alembic/versions/20260702_rob651_toss_approval_hash.py`
Expected: `revision = "20260702_rob651"`, `down_revision = "20260702_rob641"`.

- [ ] **Step 7: Add `approval_hash` to `record_send`**

In `app/services/toss_live_order_ledger_service.py`, add the parameter to `record_send`'s signature (after `report_item_uuid: str | uuid.UUID | None = None,`):

```python
        approval_hash: str | None = None,
```

And in the `TossLiveOrderLedger(...)` constructor call (after `report_item_uuid=parse_report_item_uuid(report_item_uuid),`):

```python
            approval_hash=approval_hash,
```

The replay branch (`if existing is not None: ... return existing`) is left untouched — a replayed row keeps its original `approval_hash`.

- [ ] **Step 8: Add `approval_hash` to `record_toss_place_order`**

In `app/mcp_server/tooling/toss_live_ledger.py`, add to `record_toss_place_order`'s signature (after `report_item_uuid: str | None,`):

```python
    approval_hash: str | None = None,
```

And pass it through in the `record_send(...)` call (after `report_item_uuid=report_item_uuid,`):

```python
            approval_hash=approval_hash,
```

- [ ] **Step 9: Write the record_send behavior tests**

Add to `tests/test_rob538_toss_live_ledger_schema.py`:

```python
@pytest.mark.asyncio
async def test_record_send_stores_approval_hash(toss_ledger_session):
    # toss_ledger_session: existing async-session fixture in this file.
    from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

    svc = TossLiveOrderLedgerService(toss_ledger_session)
    row = await svc.record_send(
        operation_kind="place",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=None,
        price=None,
        order_amount=None,
        currency="KRW",
        client_order_id="tossp6-deadbeefdeadbeef",
        broker_order_id="BRK-1",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response=None,
        approval_hash="p6a-abc123abc123abc1",
    )
    assert row.approval_hash == "p6a-abc123abc123abc1"


@pytest.mark.asyncio
async def test_record_send_replay_keeps_original_approval_hash(toss_ledger_session):
    from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

    svc = TossLiveOrderLedgerService(toss_ledger_session)
    common = dict(
        operation_kind="place",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=None,
        price=None,
        order_amount=None,
        currency="KRW",
        client_order_id="tossp6-cafecafecafecafe",
        broker_order_id="BRK-2",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response=None,
    )
    first = await svc.record_send(**common, approval_hash="p6a-original00000000")
    replay = await svc.record_send(**common, approval_hash="p6a-different0000000")
    assert replay.id == first.id
    assert replay.approval_hash == "p6a-original00000000"
```

> **NOTE for implementer:** Confirm the async-session fixture name in this file (grep `def .*session` / `@pytest.fixture` in `tests/test_rob538_toss_live_ledger_schema.py`). If it differs from `toss_ledger_session`, use the existing fixture name. If the file has no DB-session fixture, reuse the session fixture used by `test_rob538_toss_live_ledger_schema.py`'s existing insert/replay tests (this file already exercises `record_send`).

- [ ] **Step 10: Run the ledger tests**

Run: `uv run pytest tests/test_rob538_toss_live_ledger_schema.py -q`
Expected: PASS (new column test + both record_send tests + pre-existing tests).

- [ ] **Step 11: Commit**

```bash
git add app/models/review.py alembic/versions/20260702_rob651_toss_approval_hash.py \
        app/services/toss_live_order_ledger_service.py \
        app/mcp_server/tooling/toss_live_ledger.py \
        tests/test_rob538_toss_live_ledger_schema.py
git commit -m "feat(ROB-651): toss_live_order_ledger.approval_hash column + record_send pass-through

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Wire `toss_preview_order`

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py` (`toss_preview_order` `:642-765`)
- Test: `tests/test_mcp_toss_order_variants.py`

**Interfaces:**
- Consumes (Task 1): `build_canonical_payload`, `derive_client_order_id`, `encode_approval_token`, `APPROVAL_TTL_SECONDS`.
- Produces: `toss_preview_order` response now contains `approval_hash` (token str), `approval_expires_at` (ISO KST str), and `payload_preview["clientOrderId"]` is the deterministic id. New optional param `rung: str | int | None = None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_toss_order_variants.py` (follow the file's existing async test + monkeypatch style for `_client_context`/warnings; grep existing preview tests for the fixture pattern and reuse it):

```python
@pytest.mark.asyncio
async def test_preview_emits_approval_hash_and_deterministic_client_order_id(
    monkeypatch,
):
    from datetime import datetime

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    fixed = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    monkeypatch.setattr(otv, "now_kst", lambda: fixed)
    # reuse the file's existing helper that stubs _client_context + warnings guard;
    # if none exists, monkeypatch otv._client_context and otv.check_warnings_guard.

    res1 = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
    )
    res2 = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
    )
    assert res1["success"] is True
    assert res1["approval_hash"].startswith("p6a1.")
    assert res1["approval_expires_at"]  # ISO string
    cid = res1["payload_preview"]["clientOrderId"]
    assert cid.startswith("tossp6-")
    # deterministic: identical params + same trading day -> identical id + token payload
    assert res2["payload_preview"]["clientOrderId"] == cid


@pytest.mark.asyncio
async def test_preview_rung_discriminator_changes_client_order_id(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    # stub client/warnings as above

    base = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
    )
    r2 = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr", rung=2,
    )
    assert (
        base["payload_preview"]["clientOrderId"]
        != r2["payload_preview"]["clientOrderId"]
    )
```

> **NOTE:** `toss_preview_order` calls `_client_context()` (network). Reuse whatever stub the existing preview tests in this file use. Grep `toss_preview_order` in the test file for the established monkeypatch pattern; do not invent a new one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_toss_order_variants.py -q -k "approval_hash or rung_discriminator"`
Expected: FAIL — `KeyError: 'approval_hash'` / `TypeError: unexpected keyword argument 'rung'`.

- [ ] **Step 3: Add the timezone import + module seam**

In `app/mcp_server/tooling/orders_toss_variants.py`, extend the timezone import and add the approval module import near the existing imports (after line 20-36 block):

```python
from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.toss_approval import (
    APPROVAL_TTL_SECONDS,
    build_canonical_payload,
    derive_approval_digest,
    derive_client_order_id,
    encode_approval_token,
    verify_approval_token,
)
```

(`now_kst` referenced as a module attribute so tests can `monkeypatch.setattr(otv, "now_kst", ...)`. `KST` is used to format `approval_expires_at` and add the TTL.)

- [ ] **Step 4: Rewrite the preview payload/response block**

In `toss_preview_order`, add `rung: str | int | None = None` to the signature (after `account_type: str | None = None,`).

Replace the payload-construction block (currently starting `payload: dict[str, Any] = {"clientOrderId": _new_client_order_id(), ...}` through the conditional `quantity`/`price`/`orderAmount` adds) with:

```python
    quantity_str = _stringify_decimal(quantity_dec)
    price_str = _stringify_decimal(price_dec)
    order_amount_str = _stringify_decimal(order_amount_dec)

    canonical = build_canonical_payload(
        market=mkt,
        symbol=symbol,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        quantity=quantity_str,
        price=price_str,
        order_amount=order_amount_str,
    )
    now = now_kst()
    client_order_id = derive_client_order_id(
        canonical, market=mkt, now=now, rung=rung
    )
    approval_hash = encode_approval_token(canonical, now=now)
    approval_expires_at = (
        now + timedelta(seconds=APPROVAL_TTL_SECONDS)
    ).astimezone(KST).isoformat()

    payload: dict[str, Any] = {
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
    }
    if quantity_str is not None:
        payload["quantity"] = quantity_str
    if price_str is not None:
        payload["price"] = price_str
    if order_amount_str is not None:
        payload["orderAmount"] = order_amount_str
```

Add `timedelta` to the `datetime` import at the top: change `from datetime import date, datetime` to `from datetime import date, datetime, timedelta`.

Then add the two new keys to the `response = { ... }` dict (alongside `"payload_preview": payload,`):

```python
        "approval_hash": approval_hash,
        "approval_expires_at": approval_expires_at,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_toss_order_variants.py -q -k "approval_hash or rung_discriminator"`
Expected: PASS.

- [ ] **Step 6: Run the full Toss variants suite (no regressions)**

Run: `uv run pytest tests/test_mcp_toss_order_variants.py tests/test_mcp_toss_order_variants_rob561.py -q`
Expected: PASS. If a pre-existing test asserted `clientOrderId` was a 32-char uuid hex, update it to assert the `tossp6-` deterministic prefix (this is the intended contract change; note it in the commit).

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-651): toss_preview_order emits approval_hash token + deterministic clientOrderId

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire `toss_place_order` + rollout mode gate

**Files:**
- Modify: `app/core/config.py:184+` (`Settings`) — add `toss_approval_hash_mode`
- Modify: `app/mcp_server/tooling/orders_toss_variants.py` (`_toss_place_order_impl` `:766-982`, `toss_place_order` `:983-1035`)
- Test: `tests/test_mcp_toss_order_variants.py`

**Interfaces:**
- Consumes (Task 1): `build_canonical_payload`, `derive_client_order_id`, `derive_approval_digest`, `verify_approval_token`. (Task 2): `record_toss_place_order(..., approval_hash=...)`.
- Produces: `_toss_place_order_impl` + `toss_place_order` accept `approval_hash: str | None = None` and `rung: str | int | None = None`. Fail-closed error codes on the response `"error_code"`: `invalid_approval_hash`, `approval_expired`, `approval_hash_mismatch` (with `"diff"`), `approval_hash_required`.

- [ ] **Step 1: Add the config flag**

In `app/core/config.py`, in `Settings` next to the other `toss_*` fields (~line 246):

```python
    toss_approval_hash_mode: str = "optional"  # off | optional | warn | required
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_mcp_toss_order_variants.py`:

```python
@pytest.mark.asyncio
async def test_place_dry_run_matching_hash_passes(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    # stub _client_context + warnings as the existing place tests do

    prev = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
    )
    res = await otv.toss_place_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
        dry_run=True, approval_hash=prev["approval_hash"],
    )
    assert res["success"] is True
    # placed clientOrderId matches previewed (idempotent)
    assert res["client_order_id"] == prev["payload_preview"]["clientOrderId"]


@pytest.mark.asyncio
async def test_place_mismatched_hash_fails_closed_with_diff(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))

    prev = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
    )
    res = await otv.toss_place_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70100", market="kr",  # price differs
        dry_run=True, approval_hash=prev["approval_hash"],
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_mismatch"
    assert "price" in res["diff"]


@pytest.mark.asyncio
async def test_place_expired_hash_requires_repreview(monkeypatch):
    from datetime import datetime, timedelta

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    issued = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    monkeypatch.setattr(otv, "now_kst", lambda: issued)
    prev = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
    )
    monkeypatch.setattr(otv, "now_kst", lambda: issued + timedelta(seconds=301))
    res = await otv.toss_place_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr",
        dry_run=True, approval_hash=prev["approval_hash"],
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_expired"


@pytest.mark.asyncio
async def test_place_optional_mode_without_hash_passes(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    monkeypatch.setattr(otv.settings, "toss_approval_hash_mode", "optional", raising=False)
    res = await otv.toss_place_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr", dry_run=True,
    )
    assert res["success"] is True


@pytest.mark.asyncio
async def test_place_required_mode_without_hash_fails_closed(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    monkeypatch.setattr(otv.settings, "toss_approval_hash_mode", "required", raising=False)
    res = await otv.toss_place_order(
        symbol="005930", side="buy", order_type="limit",
        quantity="10", price="70000", market="kr", dry_run=True,
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_required"
```

> **NOTE:** These place tests are `dry_run=True`, so `_live_mutation_disabled_error` and the broker `execute_order` path are not reached — no broker stub needed beyond `_client_context` which dry-run does not enter. Verify by reading `_toss_place_order_impl`: the `dry_run` early return happens **before** `execute_order`. The approval-gate must be placed **before** that dry-run return so dry-run exercises it.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_toss_order_variants.py -q -k "place_dry_run_matching or mismatched_hash or expired_hash or optional_mode or required_mode"`
Expected: FAIL — `TypeError: unexpected keyword argument 'approval_hash'`.

- [ ] **Step 4: Add params + approval gate to `_toss_place_order_impl`**

Add to the signature (after `account_type: str | None = None,`, before `client_order_id_override: str | None = None,`):

```python
    approval_hash: str | None = None,
    rung: str | int | None = None,
```

Replace the payload-construction block (currently `payload: dict[str, Any] = {"clientOrderId": client_order_id_override or _new_client_order_id(), ...}` and the conditional field adds) with the canonical-first version:

```python
    quantity_str = _stringify_decimal(quantity_dec)
    price_str = _stringify_decimal(price_dec)
    order_amount_str = _stringify_decimal(order_amount_dec)

    canonical = build_canonical_payload(
        market=mkt,
        symbol=symbol,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        quantity=quantity_str,
        price=price_str,
        order_amount=order_amount_str,
    )
    now = now_kst()
    client_order_id = client_order_id_override or derive_client_order_id(
        canonical, market=mkt, now=now, rung=rung
    )
    ledger_approval_hash = derive_approval_digest(canonical)

    payload: dict[str, Any] = {
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
    }
    if quantity_str is not None:
        payload["quantity"] = quantity_str
    if price_str is not None:
        payload["price"] = price_str
    if order_amount_str is not None:
        payload["orderAmount"] = order_amount_str
    if confirm_high_value_order:
        payload["confirmHighValueOrder"] = True
```

Then, immediately **after** `base_response = {...}` and the existing `_client_order_id_error` guard, and **before** the `if dry_run:` return, insert the approval gate:

```python
    mode = getattr(settings, "toss_approval_hash_mode", "optional")
    if mode != "off":
        if approval_hash is not None:
            result = verify_approval_token(approval_hash, canonical, now=now)
            if not result.ok:
                err = {
                    "success": False,
                    **base_response,
                    "error": result.message,
                    "error_code": result.error_code,
                }
                if result.diff is not None:
                    err["diff"] = result.diff
                return err
        elif mode == "required":
            return {
                "success": False,
                **base_response,
                "error": (
                    "toss_place_order requires approval_hash "
                    "(TOSS_APPROVAL_HASH_MODE=required). Re-preview and pass "
                    "approval_hash from toss_preview_order."
                ),
                "error_code": "approval_hash_required",
            }
        elif mode == "warn":
            logger.warning(
                "toss_place_order called without approval_hash "
                "(mode=warn) symbol=%s side=%s",
                symbol,
                side,
            )
```

Finally, pass the digest into the ledger write. In the `record_toss_place_order(...)` call inside `execute_order`, add (after `report_item_uuid=report_item_uuid,`):

```python
                approval_hash=ledger_approval_hash,
```

> **NOTE:** `now_kst` is already imported (Task 3). `client_order_id_override` stays a real parameter but `toss_place_order` keeps passing `None`, so the deterministic id is always used from the public tool. The two dry-run/confirm return paths already spread `base_response` which carries `client_order_id`.

- [ ] **Step 5: Thread the new params through `toss_place_order`**

In `toss_place_order`'s signature add `approval_hash: str | None = None,` and `rung: str | int | None = None,` (after `account_type: str | None = None,`), and pass them into the `_toss_place_order_impl(...)` call (add alongside the existing kwargs, keeping `client_order_id_override=None`):

```python
        approval_hash=approval_hash,
        rung=rung,
```

- [ ] **Step 6: Run the new place tests**

Run: `uv run pytest tests/test_mcp_toss_order_variants.py -q -k "place_dry_run_matching or mismatched_hash or expired_hash or optional_mode or required_mode"`
Expected: PASS.

- [ ] **Step 7: Run the full Toss suite + ledger + module (no regressions)**

Run: `uv run pytest tests/test_mcp_toss_order_variants.py tests/test_mcp_toss_order_variants_rob561.py tests/test_toss_approval.py tests/test_rob538_toss_live_ledger_schema.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/core/config.py app/mcp_server/tooling/orders_toss_variants.py \
        tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-651): toss_place_order approval_hash verification + rollout mode gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Idempotency integration test + docs + lint

**Files:**
- Test: `tests/test_mcp_toss_order_variants.py`
- Modify: `CLAUDE.md` (Toss section — document `approval_hash` + `rung` + `TOSS_APPROVAL_HASH_MODE`)
- Modify: `env.example` (add `TOSS_APPROVAL_HASH_MODE=optional` comment line)

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: acceptance-criteria coverage test; operator-facing docs.

- [ ] **Step 1: Write the idempotency (same-day vs next-day) test**

Add to `tests/test_mcp_toss_order_variants.py`:

```python
@pytest.mark.asyncio
async def test_client_order_id_same_day_stable_next_day_new(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST
    from app.mcp_server.tooling import orders_toss_variants as otv

    def _prev():
        return otv.toss_preview_order(
            symbol="005930", side="buy", order_type="limit",
            quantity="10", price="70000", market="kr",
        )

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    day1_a = await _prev()
    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 15, 0, tzinfo=KST))
    day1_b = await _prev()
    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 3, 10, 0, tzinfo=KST))
    day2 = await _prev()

    cid1a = day1_a["payload_preview"]["clientOrderId"]
    cid1b = day1_b["payload_preview"]["clientOrderId"]
    cid2 = day2["payload_preview"]["clientOrderId"]
    assert cid1a == cid1b  # same trading day -> broker/ledger dedupe key
    assert cid1a != cid2   # next trading day -> new order allowed
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_mcp_toss_order_variants.py -q -k "same_day_stable_next_day"`
Expected: PASS.

- [ ] **Step 3: Document in CLAUDE.md**

In `CLAUDE.md`, under the "토스증권 Open API (ROB-529)" section, append a bullet:

```markdown
- **ROB-651 (P6-A)**: `toss_preview_order`가 정규화(tick-snap) 이후 `approval_hash`(self-contained 토큰, TTL 5분) + `approval_expires_at`를 반환. `toss_place_order(approval_hash=...)`는 자기 파라미터로 canonical을 재계산해 불일치/만료 시 fail-closed(`error_code` + `diff`). 롤아웃 `TOSS_APPROVAL_HASH_MODE ∈ {off,optional,warn,required}`(기본 `optional`, 백컴팻). `clientOrderId`는 uuid4 → 결정적 `tossp6-<sha16>(canonical|거래일salt|rung)` 멱등키(KR=KST/US=ET 거래일; 같은 거래일 동일주문 dedupe, 익일 신규). 같은 날 진짜 동일 두 번째 주문은 `rung` discriminator로 분리. 컬럼: `review.toss_live_order_ledger.approval_hash`(digest). 공유경로(KIS/Upbit)는 ROB-653 P6-B.
```

- [ ] **Step 4: Document in env.example**

In `env.example`, near the Toss env vars, add:

```bash
# ROB-651 P6-A — Toss preview→place approval-hash 강제 수준 (off|optional|warn|required)
TOSS_APPROVAL_HASH_MODE=optional
```

- [ ] **Step 5: Lint the whole change**

Run: `uv run ruff format app/ tests/ && uv run ruff check app/ tests/`
Expected: no errors; if `ruff check` reports fixable issues, run `uv run ruff check --fix app/ tests/` and re-run.

- [ ] **Step 6: Full targeted regression run**

Run:
```bash
uv run pytest tests/test_toss_approval.py tests/test_mcp_toss_order_variants.py \
  tests/test_mcp_toss_order_variants_rob561.py tests/test_rob538_toss_live_ledger_schema.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_mcp_toss_order_variants.py CLAUDE.md env.example
git commit -m "test(ROB-651): idempotency same-day/next-day coverage + docs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §1 canonical / two derived values → Task 1 (`build_canonical_payload`, `derive_approval_digest`, `derive_client_order_id`) ✅
- §2 stateless token / TTL / diff → Task 1 (`encode/decode/verify_approval_token`) + Task 4 (gate) ✅
- §3 deterministic clientOrderId always ON → Task 3 (preview) + Task 4 (place, `client_order_id_override or derive_...`) ✅
- §4 rollout config gate → Task 4 (`toss_approval_hash_mode` off/optional/warn/required) ✅
- §5 ledger additive column + migration + record_send/wrapper + replay-unchanged → Task 2 ✅
- §6 tool surface (preview response keys, place params, `rung`) → Tasks 3 & 4 ✅
- Acceptance: mismatch fail-closed + diff (Task 4 test), same-day/next-day (Task 5 test), TTL expiry (Task 4 test), back-compat optional (Task 4 test) ✅

**Placeholder scan:** No TBD/TODO/"handle edge cases" placeholders; every code step carries full code.

**Type consistency:** `approval_hash` names two related values by design and this is called out in the spec/plan — the tool **param/response** `approval_hash` is the *token* (`p6a1.…`); the **ledger column** `approval_hash` stores the *digest* (`p6a-…`, from `derive_approval_digest`). `derive_client_order_id`, `verify_approval_token`, `ApprovalResult.error_code` names match across Tasks 1/3/4. `now_kst` is imported and referenced as a module attribute in Tasks 3–5 so monkeypatch works.

**Open confirmation for implementer (does not block):** fixture name in `tests/test_rob538_toss_live_ledger_schema.py` (Task 2 Step 9) and the existing `_client_context`/warnings monkeypatch pattern in `tests/test_mcp_toss_order_variants.py` (Tasks 3–5) — reuse existing, do not invent.
