# ROB-103 Watch Order Intent MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an approval-required `OrderPreviewLine` branch to the watch alert flow so triggered watches can produce KIS-mock order intent previews (audited in a new ledger) without ever submitting orders.

**Architecture:** Per-watch action policy is encoded in the existing Redis hash payload (JSON) — `notify_only` keeps the current alert flow, `create_order_intent` calls a new service that builds a ROB-100 `OrderPreviewLine`, applies a KRW-denominated cap (FX-converted for US), and inserts a `previewed`/`failed` row into `review.watch_order_intent_ledger`. KST-day idempotency is enforced via a partial unique index. The n8n batched alert payload gains an additive `intents` block.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async (Mapped/mapped_column), Alembic, FastAPI, FastMCP, pytest + pytest-asyncio (strict mode), Pydantic v2, Redis 7, PostgreSQL 16. Reuse `app/schemas/execution_contracts.py` (ROB-100), `app/services/exchange_rate_service.py`, `app/core/timezone.now_kst`.

**Spec:** `docs/superpowers/specs/2026-05-04-rob-103-watch-order-intent-mvp-design.md`

**Project conventions baked in:**
- Review-schema models live in `app/models/review.py` (alongside `AlpacaPaperOrderLedger`, `KISMockOrderLedger`).
- Partial unique indexes use SQLAlchemy `Index(..., unique=True, postgresql_where=text(...))` (no raw SQL).
- Service-only writes — no direct SQL `INSERT/UPDATE/DELETE` outside the ledger service.
- Pytest-asyncio runs in strict mode; class-based tests work without `@pytest.mark.asyncio` on each method, but module-level tests must mark each.
- Commit style: `feat(ROB-103): ...` / `test(ROB-103): ...` / `chore(ROB-103): ...`. End every commit body with `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

---

## File structure

| File | Status | Responsibility |
|---|---|---|
| `app/services/watch_intent_policy.py` | new | Pure parser/validator: raw Redis payload → `WatchActionPolicy` value object; raises `WatchPolicyError` on bad input. |
| `app/services/watch_order_intent_preview_builder.py` | new | Pure builder: `(WatchActionPolicy, watch row, triggered value, fx quote\|None, kst date)` → `IntentBuildResult` (success line+basket, or structured failure). |
| `app/services/watch_order_intent_service.py` | new | Only writer to `watch_order_intent_ledger`. Calls FX, calls builder, inserts row, handles partial-unique conflict → `dedupe_hit`. |
| `app/models/review.py` | modified | Add `WatchOrderIntentLedger` ORM class. |
| `alembic/versions/<ts>_add_watch_order_intent_ledger.py` | new | Create table + indexes (including partial unique index) inside `review` schema. |
| `app/services/watch_alerts.py` | modified | Extend `add_watch` to accept policy fields, validate via `parse_policy`, persist as JSON. `list_watches` surfaces parsed policy fields per row. |
| `app/services/openclaw_client.py` | modified | `send_watch_alert_to_n8n` accepts and emits an additive `intents` array. |
| `app/jobs/watch_scanner.py` | modified | Branch on `policy.action`; aggregate `intents`; decide watch deletion per outcome; pass `intents` to client. |
| `app/mcp_server/tooling/watch_alerts_registration.py` | modified | Extend `manage_watch_alerts` `add` action with optional policy kwargs. |
| `app/routers/watch_order_intent_ledger.py` | new | Read-only `GET` endpoints for the ledger. |
| `app/mcp_server/tooling/watch_order_intent_ledger_read.py` | new | Read-only MCP tools `watch_order_intent_ledger_list_recent` / `_get`. |
| `app/main.py` | modified | Register the new router. |
| `app/mcp_server/server.py` (or wherever existing MCP tool registrations chain) | modified | Register the new MCP read tools. |
| `docs/runbooks/watch-order-intent-ledger.md` | new | Operator runbook (mirrors `docs/runbooks/alpaca-paper-ledger.md`). |
| `tests/test_watch_intent_policy.py` | new | Unit tests for the parser. |
| `tests/test_watch_order_intent_preview_builder.py` | new | Unit tests for the builder. |
| `tests/test_watch_order_intent_service.py` | new | DB-integration tests for the service. |
| `tests/test_watch_alerts.py` | modified | Add backward-compat + new validation cases. |
| `tests/test_watch_scanner.py` | modified | Add previewed/failed/dedupe_hit/notify_only cases. |
| `tests/test_mcp_watch_alerts.py` | modified | Add policy kwargs cases. |
| `tests/test_watch_order_intent_ledger_router.py` | new | Router GET tests. |
| `tests/test_mcp_watch_order_intent_ledger.py` | new | MCP tool tests. |

---

## Pre-flight: branch + worktree sanity

- [ ] **Step 0.1: Confirm we're on a worktree branch off main**

Run: `git status -sb`
Expected: branch is `aquamarine-canvas` (or a fresh feature branch); working tree clean except for the spec doc commit.

- [ ] **Step 0.2: Confirm Alembic head and pick `down_revision`**

Run:
```bash
python3 - <<'PY'
import re, glob
revs = {}
for p in glob.glob("alembic/versions/*.py"):
    with open(p) as f:
        c = f.read()
    m = re.search(r"^revision\s*[:=].*?['\"](\w+)['\"]", c, re.MULTILINE)
    d = re.search(r"^down_revision\s*[:=].*?['\"](\w+)['\"]", c, re.MULTILINE)
    if m:
        revs[m.group(1)] = d.group(1) if d else None
referenced = {v for v in revs.values() if v}
heads = [r for r in revs if r not in referenced]
print("heads:", heads)
PY
```
Expected: a list of head revision IDs. Pick the head that contains the most recent ledger/review-schema migration (today: `d4e5f6a7b8c9` from `normalize_alpaca_paper_ledger_taxonomy`). If multiple heads exist, that is normal for this repo — chain off the head related to `review` schema; if none, pick the alphabetically first head and confirm with the user before proceeding.

Record the chosen head as `<DOWN_REV>`. Use it in Task 4.

---

## Task 1: Watch intent policy parser (pure)

**Files:**
- Create: `app/services/watch_intent_policy.py`
- Test: `tests/test_watch_intent_policy.py`

- [ ] **Step 1.1: Write the failing tests**

Create `tests/test_watch_intent_policy.py`:

```python
from __future__ import annotations

import json

import pytest

from app.services.watch_intent_policy import (
    IntentPolicy,
    NotifyOnlyPolicy,
    WatchPolicyError,
    parse_policy,
)


def _payload(**fields: object) -> str:
    return json.dumps({"created_at": "2026-05-04T00:00:00+09:00", **fields})


class TestParsePolicyBackwardCompat:
    def test_legacy_created_at_only_payload_is_notify_only(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload='{"created_at":"2026-05-04T00:00:00+09:00"}',
        )
        assert isinstance(policy, NotifyOnlyPolicy)

    def test_missing_payload_is_notify_only(self) -> None:
        policy = parse_policy(
            market="crypto",
            target_kind="asset",
            condition_type="price_above",
            raw_payload=None,
        )
        assert isinstance(policy, NotifyOnlyPolicy)

    def test_unparsable_payload_is_notify_only(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload="not-json",
        )
        assert isinstance(policy, NotifyOnlyPolicy)

    def test_action_notify_only_explicit(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload=_payload(action="notify_only"),
        )
        assert isinstance(policy, NotifyOnlyPolicy)


class TestParsePolicyNotifyOnlyStrict:
    @pytest.mark.parametrize(
        "extra",
        [
            {"side": "buy"},
            {"quantity": 1},
            {"notional_krw": 100000},
            {"limit_price": 70000},
            {"max_notional_krw": 1500000},
        ],
    )
    def test_notify_only_with_extra_field_rejected(self, extra: dict) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="notify_only", **extra),
            )
        assert excinfo.value.code == "notify_only_must_be_bare"


class TestParsePolicyIntentMarketCondition:
    def test_crypto_market_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="crypto",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="buy", quantity=1),
            )
        assert excinfo.value.code == "intent_market_unsupported"

    @pytest.mark.parametrize("condition_type", ["rsi_above", "rsi_below", "trade_value_above", "trade_value_below"])
    def test_non_price_condition_rejected(self, condition_type: str) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type=condition_type,
                raw_payload=_payload(action="create_order_intent", side="buy", quantity=1),
            )
        assert excinfo.value.code == "intent_condition_unsupported"


class TestParsePolicyIntentSideAndSizing:
    def test_side_required(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", quantity=1),
            )
        assert excinfo.value.code == "intent_side_invalid"

    def test_side_invalid_value(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="long", quantity=1),
            )
        assert excinfo.value.code == "intent_side_invalid"

    def test_quantity_and_notional_both_present_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", quantity=1, notional_krw=100000
                ),
            )
        assert excinfo.value.code == "intent_sizing_xor"

    def test_neither_quantity_nor_notional_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="buy"),
            )
        assert excinfo.value.code == "intent_sizing_xor"

    def test_us_with_notional_krw_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="us",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", notional_krw=1000000
                ),
            )
        assert excinfo.value.code == "intent_us_notional_krw_unsupported"

    @pytest.mark.parametrize("bad_qty", [0, -1, 0.5, "1"])
    def test_quantity_must_be_positive_integer(self, bad_qty: object) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="buy", quantity=bad_qty),
            )
        assert excinfo.value.code == "intent_quantity_invalid"

    def test_limit_price_non_positive_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", quantity=1, limit_price=0
                ),
            )
        assert excinfo.value.code == "intent_limit_price_invalid"

    def test_max_notional_non_positive_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", quantity=1, max_notional_krw=0
                ),
            )
        assert excinfo.value.code == "intent_max_notional_invalid"

    def test_notional_krw_non_positive_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", notional_krw=0
                ),
            )
        assert excinfo.value.code == "intent_notional_krw_invalid"


class TestParsePolicyIntentSuccess:
    def test_kr_quantity_success(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload=_payload(
                action="create_order_intent", side="buy", quantity=1, max_notional_krw=1500000
            ),
        )
        assert isinstance(policy, IntentPolicy)
        assert policy.side == "buy"
        assert policy.quantity == 1
        assert policy.notional_krw is None
        assert policy.limit_price is None
        assert policy.max_notional_krw == 1500000

    def test_kr_notional_krw_success(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload=_payload(
                action="create_order_intent", side="buy", notional_krw=1000000
            ),
        )
        assert isinstance(policy, IntentPolicy)
        assert policy.notional_krw == 1000000
        assert policy.quantity is None

    def test_us_quantity_success(self) -> None:
        policy = parse_policy(
            market="us",
            target_kind="asset",
            condition_type="price_above",
            raw_payload=_payload(
                action="create_order_intent", side="sell", quantity=10, limit_price=190.5
            ),
        )
        assert isinstance(policy, IntentPolicy)
        assert policy.side == "sell"
        assert policy.quantity == 10
        assert policy.limit_price == 190.5
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watch_intent_policy.py -v`
Expected: every test errors with `ModuleNotFoundError: app.services.watch_intent_policy` (or the dataclass imports fail).

- [ ] **Step 1.3: Implement `app/services/watch_intent_policy.py`**

```python
"""Pure policy parser for watch intent payloads (ROB-103).

This module is a leaf:
- No DB, Redis, HTTP, settings, or logging side effects beyond a debug log on malformed JSON.
- Inputs are primitive / JSON-string; outputs are value objects.
- Trigger-time and add-time both call ``parse_policy``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

logger = logging.getLogger(__name__)

_PRICE_CONDITIONS: Final[frozenset[str]] = frozenset({"price_above", "price_below"})
_INTENT_MARKETS: Final[frozenset[str]] = frozenset({"kr", "us"})
_SIDES: Final[frozenset[str]] = frozenset({"buy", "sell"})


class WatchPolicyError(ValueError):
    """Validation error raised by :func:`parse_policy`.

    ``code`` is a stable string used by the MCP tool surface and tests.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class NotifyOnlyPolicy:
    action: Literal["notify_only"] = "notify_only"


@dataclass(frozen=True, slots=True)
class IntentPolicy:
    action: Literal["create_order_intent"]
    side: Literal["buy", "sell"]
    quantity: int | None
    notional_krw: Decimal | None
    limit_price: Decimal | None
    max_notional_krw: Decimal | None


WatchActionPolicy = NotifyOnlyPolicy | IntentPolicy


def _to_decimal_positive(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise WatchPolicyError(code)
    if not isinstance(value, (int, float, Decimal, str)):
        raise WatchPolicyError(code)
    try:
        decimal_value = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal raises a few different types
        raise WatchPolicyError(code) from exc
    if decimal_value <= 0:
        raise WatchPolicyError(code)
    return decimal_value


def _to_positive_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise WatchPolicyError(code)
    if not isinstance(value, int):
        raise WatchPolicyError(code)
    if value <= 0:
        raise WatchPolicyError(code)
    return value


def parse_policy(
    *,
    market: str,
    target_kind: str,
    condition_type: str,
    raw_payload: str | None,
) -> WatchActionPolicy:
    if not raw_payload:
        return NotifyOnlyPolicy()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        logger.debug("watch payload is not JSON, treating as notify_only")
        return NotifyOnlyPolicy()
    if not isinstance(payload, dict):
        return NotifyOnlyPolicy()

    action = payload.get("action") or "notify_only"

    if action == "notify_only":
        for forbidden in ("side", "quantity", "notional_krw", "limit_price", "max_notional_krw"):
            if forbidden in payload:
                raise WatchPolicyError("notify_only_must_be_bare")
        return NotifyOnlyPolicy()

    if action != "create_order_intent":
        raise WatchPolicyError("action_unsupported")

    if market not in _INTENT_MARKETS:
        raise WatchPolicyError("intent_market_unsupported")
    if condition_type not in _PRICE_CONDITIONS:
        raise WatchPolicyError("intent_condition_unsupported")

    side = payload.get("side")
    if side not in _SIDES:
        raise WatchPolicyError("intent_side_invalid")

    has_quantity = "quantity" in payload
    has_notional = "notional_krw" in payload
    if has_quantity == has_notional:
        raise WatchPolicyError("intent_sizing_xor")

    if has_notional and market == "us":
        raise WatchPolicyError("intent_us_notional_krw_unsupported")

    quantity: int | None = None
    if has_quantity:
        quantity = _to_positive_int(payload["quantity"], "intent_quantity_invalid")

    notional_krw: Decimal | None = None
    if has_notional:
        notional_krw = _to_decimal_positive(
            payload["notional_krw"], "intent_notional_krw_invalid"
        )

    limit_price: Decimal | None = None
    if "limit_price" in payload:
        limit_price = _to_decimal_positive(
            payload["limit_price"], "intent_limit_price_invalid"
        )

    max_notional_krw: Decimal | None = None
    if "max_notional_krw" in payload:
        max_notional_krw = _to_decimal_positive(
            payload["max_notional_krw"], "intent_max_notional_invalid"
        )

    return IntentPolicy(
        action="create_order_intent",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        notional_krw=notional_krw,
        limit_price=limit_price,
        max_notional_krw=max_notional_krw,
    )


__all__ = [
    "IntentPolicy",
    "NotifyOnlyPolicy",
    "WatchActionPolicy",
    "WatchPolicyError",
    "parse_policy",
]
```

- [ ] **Step 1.4: Run tests, expect green**

Run: `uv run pytest tests/test_watch_intent_policy.py -v`
Expected: all 25 cases pass.

- [ ] **Step 1.5: Commit**

```bash
git add app/services/watch_intent_policy.py tests/test_watch_intent_policy.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): add watch intent policy parser

Pure parser/validator that turns a Redis watch payload into a
NotifyOnlyPolicy or IntentPolicy. Backward compatible with the legacy
created_at-only payload and the strict validation rules from the
ROB-103 spec.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 2: Preview builder (pure)

**Files:**
- Create: `app/services/watch_order_intent_preview_builder.py`
- Test: `tests/test_watch_order_intent_preview_builder.py`

- [ ] **Step 2.1: Write the failing tests**

Create `tests/test_watch_order_intent_preview_builder.py`:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.watch_intent_policy import IntentPolicy
from app.services.watch_order_intent_preview_builder import (
    IntentBuildFailure,
    IntentBuildSuccess,
    build_preview,
)


def _watch(market: str = "kr", symbol: str = "005930", threshold: Decimal = Decimal("70000")) -> dict:
    return {
        "market": market,
        "target_kind": "asset",
        "symbol": symbol,
        "condition_type": "price_below",
        "threshold": threshold,
        "threshold_key": str(threshold),
    }


def _intent_policy(**overrides: object) -> IntentPolicy:
    base = dict(
        action="create_order_intent",
        side="buy",
        quantity=1,
        notional_krw=None,
        limit_price=None,
        max_notional_krw=None,
    )
    base.update(overrides)
    return IntentPolicy(**base)  # type: ignore[arg-type]


class TestKrQuantitySuccess:
    def test_basic_buy_uses_threshold_as_limit_price(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=1, max_notional_krw=Decimal("1500000")),
            watch=_watch(),
            triggered_value=Decimal("68500"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        line = result.preview_line
        assert line.symbol == "005930"
        assert line.market == "kr"
        assert line.side == "buy"
        assert line.account_mode == "kis_mock"
        assert line.execution_source == "watch"
        assert line.lifecycle_state == "previewed"
        assert line.quantity == Decimal("1")
        assert line.limit_price == Decimal("70000")
        assert line.notional == Decimal("70000")
        assert line.currency == "KRW"
        assert line.guard.execution_allowed is False
        assert line.guard.approval_required is True
        assert result.notional_krw_evaluated == Decimal("70000")
        assert result.fx_usd_krw_used is None

    def test_static_limit_price_override_used(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=2, limit_price=Decimal("69000")),
            watch=_watch(),
            triggered_value=Decimal("68500"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        assert result.preview_line.limit_price == Decimal("69000")
        assert result.preview_line.notional == Decimal("138000")
        assert result.notional_krw_evaluated == Decimal("138000")


class TestKrNotionalKrwSizing:
    def test_notional_krw_resolves_to_floor_quantity(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=None, notional_krw=Decimal("141000")),
            watch=_watch(threshold=Decimal("70000")),
            triggered_value=Decimal("68000"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        # floor(141000 / 70000) == 2
        assert result.preview_line.quantity == Decimal("2")
        assert result.preview_line.notional == Decimal("140000")

    def test_notional_krw_floor_below_one_is_qty_zero_failure(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=None, notional_krw=Decimal("69999")),
            watch=_watch(threshold=Decimal("70000")),
            triggered_value=Decimal("68000"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildFailure)
        assert result.blocked_by == "qty_zero"


class TestUsWithFxAndCap:
    def test_us_quantity_uses_fx_for_krw_evaluation(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=10, max_notional_krw=Decimal("3000000")),
            watch=_watch(market="us", symbol="AAPL", threshold=Decimal("180")),
            triggered_value=Decimal("181"),
            fx_quote=Decimal("1400"),
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildSuccess)
        line = result.preview_line
        assert line.currency == "USD"
        assert line.limit_price == Decimal("180")
        assert line.notional == Decimal("1800")
        # 10 * 180 * 1400 = 2_520_000
        assert result.notional_krw_evaluated == Decimal("2520000")
        assert result.fx_usd_krw_used == Decimal("1400")

    def test_us_without_fx_quote_is_fx_unavailable_failure(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=10, max_notional_krw=Decimal("3000000")),
            watch=_watch(market="us", symbol="AAPL", threshold=Decimal("180")),
            triggered_value=Decimal("181"),
            fx_quote=None,
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildFailure)
        assert result.blocked_by == "fx_unavailable"

    def test_cap_blocked_failure(self) -> None:
        result = build_preview(
            policy=_intent_policy(quantity=10, max_notional_krw=Decimal("100000")),
            watch=_watch(market="us", symbol="AAPL", threshold=Decimal("180")),
            triggered_value=Decimal("181"),
            fx_quote=Decimal("1400"),
            kst_date="2026-05-04",
        )
        assert isinstance(result, IntentBuildFailure)
        assert result.blocked_by == "max_notional_krw_cap"
        # Failure still records evaluated KRW so the ledger row carries it
        assert result.notional_krw_evaluated == Decimal("2520000")
        assert result.fx_usd_krw_used == Decimal("1400")
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watch_order_intent_preview_builder.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 2.3: Implement `app/services/watch_order_intent_preview_builder.py`**

```python
"""Pure preview builder for watch order intents (ROB-103).

Inputs are value objects + primitives. Outputs are an
``IntentBuildSuccess`` (full ROB-100 ``OrderPreviewLine``/``OrderBasketPreview``
plus KRW evaluation) or an ``IntentBuildFailure`` describing the reason.

No I/O. No DB, Redis, HTTP, settings, or logging side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Final, Literal

from app.schemas.execution_contracts import (
    ExecutionGuard,
    ExecutionReadiness,
    OrderBasketPreview,
    OrderPreviewLine,
)
from app.services.watch_intent_policy import IntentPolicy

ACCOUNT_MODE: Final[Literal["kis_mock"]] = "kis_mock"
EXECUTION_SOURCE: Final[Literal["watch"]] = "watch"


@dataclass(frozen=True, slots=True)
class IntentBuildSuccess:
    preview_line: OrderPreviewLine
    basket: OrderBasketPreview
    notional_krw_evaluated: Decimal
    fx_usd_krw_used: Decimal | None


@dataclass(frozen=True, slots=True)
class IntentBuildFailure:
    blocked_by: str
    blocking_reasons: list[str]
    notional_krw_evaluated: Decimal | None
    fx_usd_krw_used: Decimal | None
    quantity: Decimal | None
    limit_price: Decimal | None
    currency: str | None


IntentBuildResult = IntentBuildSuccess | IntentBuildFailure


def _resolve_limit_price(policy: IntentPolicy, watch: dict) -> Decimal:
    if policy.limit_price is not None:
        return policy.limit_price
    return Decimal(str(watch["threshold"]))


def _resolve_quantity(policy: IntentPolicy, limit_price: Decimal) -> Decimal | None:
    if policy.quantity is not None:
        return Decimal(policy.quantity)
    assert policy.notional_krw is not None  # parser guarantees XOR
    raw = (policy.notional_krw / limit_price).to_integral_value(rounding=ROUND_FLOOR)
    if raw < 1:
        return None
    return raw


def _failure(
    *,
    blocked_by: str,
    quantity: Decimal | None,
    limit_price: Decimal | None,
    currency: str | None,
    notional_krw_evaluated: Decimal | None,
    fx_usd_krw_used: Decimal | None,
    extra_reasons: list[str] | None = None,
) -> IntentBuildFailure:
    reasons = [blocked_by]
    if extra_reasons:
        reasons.extend(extra_reasons)
    return IntentBuildFailure(
        blocked_by=blocked_by,
        blocking_reasons=reasons,
        notional_krw_evaluated=notional_krw_evaluated,
        fx_usd_krw_used=fx_usd_krw_used,
        quantity=quantity,
        limit_price=limit_price,
        currency=currency,
    )


def build_preview(
    *,
    policy: IntentPolicy,
    watch: dict,
    triggered_value: Decimal,  # noqa: ARG001 — recorded by the service, not used here yet
    fx_quote: Decimal | None,
    kst_date: str,  # noqa: ARG001 — service uses for idempotency_key
) -> IntentBuildResult:
    market = watch["market"]
    currency = "KRW" if market == "kr" else "USD"
    limit_price = _resolve_limit_price(policy, watch)

    quantity = _resolve_quantity(policy, limit_price)
    if quantity is None:
        return _failure(
            blocked_by="qty_zero",
            quantity=None,
            limit_price=limit_price,
            currency=currency,
            notional_krw_evaluated=None,
            fx_usd_krw_used=None,
        )

    native_notional = quantity * limit_price

    if market == "us":
        if fx_quote is None:
            return _failure(
                blocked_by="fx_unavailable",
                quantity=quantity,
                limit_price=limit_price,
                currency=currency,
                notional_krw_evaluated=None,
                fx_usd_krw_used=None,
            )
        notional_krw_evaluated = native_notional * fx_quote
        fx_used: Decimal | None = fx_quote
    else:
        notional_krw_evaluated = native_notional
        fx_used = None

    if (
        policy.max_notional_krw is not None
        and notional_krw_evaluated > policy.max_notional_krw
    ):
        return _failure(
            blocked_by="max_notional_krw_cap",
            quantity=quantity,
            limit_price=limit_price,
            currency=currency,
            notional_krw_evaluated=notional_krw_evaluated,
            fx_usd_krw_used=fx_used,
        )

    guard = ExecutionGuard(
        execution_allowed=False,
        approval_required=True,
        blocking_reasons=[],
        warnings=[],
    )
    line = OrderPreviewLine(
        symbol=watch["symbol"],
        market=market,
        side=policy.side,
        account_mode=ACCOUNT_MODE,
        execution_source=EXECUTION_SOURCE,
        lifecycle_state="previewed",
        quantity=quantity,
        limit_price=limit_price,
        notional=native_notional,
        currency=currency,
        guard=guard,
        rationale=[
            f"watch trigger {watch['condition_type']} threshold={watch['threshold']}",
            f"sizing_source={'notional_krw' if policy.quantity is None else 'quantity'}",
        ],
        correlation_id=None,
    )
    basket = OrderBasketPreview(
        account_mode=ACCOUNT_MODE,
        execution_source=EXECUTION_SOURCE,
        readiness=ExecutionReadiness(
            account_mode=ACCOUNT_MODE,
            execution_source=EXECUTION_SOURCE,
            is_ready=False,
            guard=guard,
        ),
        lines=[line],
        basket_warnings=[],
    )
    return IntentBuildSuccess(
        preview_line=line,
        basket=basket,
        notional_krw_evaluated=notional_krw_evaluated,
        fx_usd_krw_used=fx_used,
    )


__all__ = [
    "ACCOUNT_MODE",
    "EXECUTION_SOURCE",
    "IntentBuildFailure",
    "IntentBuildResult",
    "IntentBuildSuccess",
    "build_preview",
]
```

- [ ] **Step 2.4: Run tests, expect green**

Run: `uv run pytest tests/test_watch_order_intent_preview_builder.py -v`
Expected: 7 cases pass.

- [ ] **Step 2.5: Commit**

```bash
git add app/services/watch_order_intent_preview_builder.py tests/test_watch_order_intent_preview_builder.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): add watch order intent preview builder

Pure builder that turns a parsed IntentPolicy plus a triggered watch
row into a ROB-100 OrderPreviewLine / OrderBasketPreview, evaluates
the KRW-denominated cap (FX-converted for US), and returns a
structured IntentBuildFailure with blocked_by codes for qty_zero,
fx_unavailable, and max_notional_krw_cap.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 3: ORM model in `app/models/review.py`

**Files:**
- Modify: `app/models/review.py` (append a new class)

- [ ] **Step 3.1: Append `WatchOrderIntentLedger` to `app/models/review.py`**

Add this class at the end of the file (after `AlpacaPaperOrderLedger` and any other classes — pick the bottom):

```python
class WatchOrderIntentLedger(Base):
    """Watch-driven order intent audit ledger (ROB-103).

    All writes go through ``app.services.watch_order_intent_service``.
    Direct SQL ``INSERT/UPDATE/DELETE`` is forbidden.
    """

    __tablename__ = "watch_order_intent_ledger"
    __table_args__ = (
        Index(
            "uq_watch_intent_previewed_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("lifecycle_state = 'previewed'"),
        ),
        Index("ix_watch_intent_kst_date", "kst_date"),
        Index("ix_watch_intent_market_symbol", "market", "symbol"),
        Index("ix_watch_intent_state_created_at", "lifecycle_state", "created_at"),
        CheckConstraint(
            "lifecycle_state IN ('previewed','failed')",
            name="watch_intent_ledger_lifecycle_state",
        ),
        CheckConstraint("side IN ('buy','sell')", name="watch_intent_ledger_side"),
        CheckConstraint(
            "account_mode = 'kis_mock'", name="watch_intent_ledger_account_mode"
        ),
        CheckConstraint(
            "execution_source = 'watch'", name="watch_intent_ledger_execution_source"
        ),
        CheckConstraint(
            "currency IS NULL OR currency IN ('KRW','USD')",
            name="watch_intent_ledger_currency",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    correlation_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    condition_type: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    threshold_key: Mapped[str] = mapped_column(Text, nullable=False)

    action: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    execution_source: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)

    quantity: Mapped[float | None] = mapped_column(Numeric(18, 8))
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 8))
    notional: Mapped[float | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str | None] = mapped_column(Text)

    notional_krw_input: Mapped[float | None] = mapped_column(Numeric(18, 2))
    max_notional_krw: Mapped[float | None] = mapped_column(Numeric(18, 2))
    notional_krw_evaluated: Mapped[float | None] = mapped_column(Numeric(18, 2))
    fx_usd_krw_used: Mapped[float | None] = mapped_column(Numeric(18, 4))

    approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    execution_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    blocking_reasons: Mapped[list] = mapped_column(JSONB, nullable=False)
    blocked_by: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)

    preview_line: Mapped[dict] = mapped_column(JSONB, nullable=False)
    triggered_value: Mapped[float | None] = mapped_column(Numeric(18, 8))
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

Notes:
- `Numeric` columns are typed as `float | None` to match this codebase's existing convention in `review.py` (the CLAUDE.md and surrounding ledger classes use `float`); SQLAlchemy still hands back `Decimal` at runtime — the service code will be careful to wrap as needed.
- `text("...")` for `postgresql_where` is already imported at the top of `review.py`.

- [ ] **Step 3.2: Smoke-test the import**

Run: `uv run python -c "from app.models.review import WatchOrderIntentLedger; print(WatchOrderIntentLedger.__tablename__)"`
Expected: prints `watch_order_intent_ledger`.

- [ ] **Step 3.3: Commit**

```bash
git add app/models/review.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): add WatchOrderIntentLedger ORM model

Adds the audit ledger model under the review schema. Uses a partial
unique index on idempotency_key scoped to lifecycle_state='previewed'
so failed rows do not block dedupe and previewed rows enforce
KST-day idempotency.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 4: Alembic migration

**Files:**
- Create: `alembic/versions/<timestamp>_add_watch_order_intent_ledger.py`

- [ ] **Step 4.1: Create the migration file**

Pick a slug like `g4h5i6j7k8l9` (12 hex chars, must not collide — `python -c "import secrets; print(secrets.token_hex(6))"` produces a fresh one). Name the file `alembic/versions/<slug>_add_watch_order_intent_ledger.py`.

Write:

```python
"""add watch_order_intent_ledger to review schema

Revision ID: <SLUG>
Revises: <DOWN_REV>
Create Date: 2026-05-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "<SLUG>"
down_revision: str | Sequence[str] | None = "<DOWN_REV>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watch_order_intent_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("condition_type", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(18, 8), nullable=False),
        sa.Column("threshold_key", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("execution_source", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("limit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("notional", sa.Numeric(18, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("notional_krw_input", sa.Numeric(18, 2), nullable=True),
        sa.Column("max_notional_krw", sa.Numeric(18, 2), nullable=True),
        sa.Column("notional_krw_evaluated", sa.Numeric(18, 2), nullable=True),
        sa.Column("fx_usd_krw_used", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "execution_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "blocking_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("blocked_by", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "preview_line",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("triggered_value", sa.Numeric(18, 8), nullable=True),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("correlation_id", name="uq_watch_intent_correlation_id"),
        sa.CheckConstraint(
            "lifecycle_state IN ('previewed','failed')",
            name="watch_intent_ledger_lifecycle_state",
        ),
        sa.CheckConstraint("side IN ('buy','sell')", name="watch_intent_ledger_side"),
        sa.CheckConstraint(
            "account_mode = 'kis_mock'", name="watch_intent_ledger_account_mode"
        ),
        sa.CheckConstraint(
            "execution_source = 'watch'", name="watch_intent_ledger_execution_source"
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency IN ('KRW','USD')",
            name="watch_intent_ledger_currency",
        ),
        schema="review",
    )

    op.create_index(
        "ix_watch_intent_kst_date",
        "watch_order_intent_ledger",
        ["kst_date"],
        schema="review",
    )
    op.create_index(
        "ix_watch_intent_market_symbol",
        "watch_order_intent_ledger",
        ["market", "symbol"],
        schema="review",
    )
    op.create_index(
        "ix_watch_intent_state_created_at",
        "watch_order_intent_ledger",
        ["lifecycle_state", "created_at"],
        schema="review",
    )
    op.create_index(
        "uq_watch_intent_previewed_idempotency",
        "watch_order_intent_ledger",
        ["idempotency_key"],
        unique=True,
        schema="review",
        postgresql_where=text("lifecycle_state = 'previewed'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_watch_intent_previewed_idempotency",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_watch_intent_state_created_at",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_watch_intent_market_symbol",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_watch_intent_kst_date",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_table("watch_order_intent_ledger", schema="review")
```

Replace `<SLUG>` with the 12-char hex slug and `<DOWN_REV>` with the head you recorded in Step 0.2. Filename must use the same `<SLUG>`.

- [ ] **Step 4.2: Apply the migration locally**

Run: `uv run alembic upgrade head`
Expected: `INFO  [alembic.runtime.migration] Running upgrade <DOWN_REV> -> <SLUG>, add watch_order_intent_ledger to review schema`. If alembic complains about multiple heads, run `uv run alembic upgrade heads` (note the trailing `s`) — that is documented project behavior.

- [ ] **Step 4.3: Verify the table and partial unique index exist**

Run:
```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\\d review.watch_order_intent_ledger" | head -40
```
Expected: shows the table columns and the four indexes including `uq_watch_intent_previewed_idempotency` with predicate `WHERE (lifecycle_state = 'previewed'::text)`.

If you do not have the env vars handy, fall back to:
```bash
docker compose exec -T postgres psql -U auto_trader -d auto_trader -c "\\d review.watch_order_intent_ledger" | head -40
```

- [ ] **Step 4.4: Commit**

```bash
git add alembic/versions/<slug>_add_watch_order_intent_ledger.py
git commit -m "$(cat <<'EOF'
chore(ROB-103): add watch_order_intent_ledger migration

Creates review.watch_order_intent_ledger with check constraints,
JSONB columns for preview_line/detail/blocking_reasons, and a
partial unique index on idempotency_key for previewed rows so
failed rows do not block dedupe.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 5: Service writer (DB integration)

**Files:**
- Create: `app/services/watch_order_intent_service.py`
- Test: `tests/test_watch_order_intent_service.py`

This task uses the real Postgres test database. Follow CLAUDE.md's existing async test patterns; this codebase already has DB-backed async tests in `tests/services/` for the proximity service — use the same fixture style.

- [ ] **Step 5.1: Locate an existing DB session fixture**

Run: `grep -rn "AsyncSession\|get_db\|conftest" tests/conftest.py tests/services/conftest.py 2>/dev/null | head -20`
Goal: identify the fixture that yields an `AsyncSession` against the test DB. We will reuse it.

- [ ] **Step 5.2: Write the failing tests**

Create `tests/test_watch_order_intent_service.py`:

```python
from __future__ import annotations

from decimal import Decimal
from typing import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import WatchOrderIntentLedger
from app.services.watch_intent_policy import IntentPolicy
from app.services.watch_order_intent_service import (
    IntentEmissionResult,
    WatchOrderIntentService,
)


def _intent_policy(**overrides: object) -> IntentPolicy:
    base = dict(
        action="create_order_intent",
        side="buy",
        quantity=1,
        notional_krw=None,
        limit_price=None,
        max_notional_krw=Decimal("1500000"),
    )
    base.update(overrides)
    return IntentPolicy(**base)  # type: ignore[arg-type]


def _watch(market: str = "kr", symbol: str = "005930") -> dict:
    return {
        "market": market,
        "target_kind": "asset",
        "symbol": symbol,
        "condition_type": "price_below",
        "threshold": Decimal("70000"),
        "threshold_key": "70000",
    }


class FakeFx:
    def __init__(self, value: Decimal | None = Decimal("1400")) -> None:
        self.value = value
        self.calls = 0

    async def get_quote(self) -> Decimal | None:
        self.calls += 1
        return self.value


@pytest.mark.asyncio
async def test_emit_intent_kr_success_writes_previewed_row(
    db_session: AsyncSession,
) -> None:
    service = WatchOrderIntentService(db_session, fx_provider=FakeFx())
    result = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(),
        triggered_value=Decimal("68000"),
        kst_date="2026-05-04",
        correlation_id="corr-1",
    )
    assert isinstance(result, IntentEmissionResult)
    assert result.status == "previewed"
    rows = (
        await db_session.execute(select(WatchOrderIntentLedger))
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.lifecycle_state == "previewed"
    assert row.market == "kr"
    assert row.symbol == "005930"
    assert row.side == "buy"
    assert row.account_mode == "kis_mock"
    assert row.execution_source == "watch"
    assert row.idempotency_key == (
        "kr:asset:005930:price_below:70000:create_order_intent:buy:2026-05-04"
    )
    assert row.kst_date == "2026-05-04"
    assert row.preview_line["lifecycle_state"] == "previewed"


@pytest.mark.asyncio
async def test_emit_intent_dedupe_returns_dedupe_hit(
    db_session: AsyncSession,
) -> None:
    service = WatchOrderIntentService(db_session, fx_provider=FakeFx())
    first = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(),
        triggered_value=Decimal("68000"),
        kst_date="2026-05-04",
        correlation_id="corr-first",
    )
    assert first.status == "previewed"

    second = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(),
        triggered_value=Decimal("67000"),
        kst_date="2026-05-04",
        correlation_id="corr-second",
    )
    assert second.status == "dedupe_hit"
    assert second.correlation_id == "corr-first"
    assert second.idempotency_key == first.idempotency_key

    rows = (
        await db_session.execute(select(WatchOrderIntentLedger))
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_emit_intent_failed_does_not_block_subsequent_previewed(
    db_session: AsyncSession,
) -> None:
    fx = FakeFx(value=None)
    service = WatchOrderIntentService(db_session, fx_provider=fx)
    failed = await service.emit_intent(
        watch=_watch(market="us", symbol="AAPL"),
        policy=_intent_policy(quantity=1, max_notional_krw=Decimal("3000000")),
        triggered_value=Decimal("181"),
        kst_date="2026-05-04",
        correlation_id="corr-fail",
    )
    assert failed.status == "failed"
    assert failed.blocked_by == "fx_unavailable"

    fx.value = Decimal("1400")
    succeeded = await service.emit_intent(
        watch=_watch(market="us", symbol="AAPL"),
        policy=_intent_policy(quantity=1, max_notional_krw=Decimal("3000000")),
        triggered_value=Decimal("181"),
        kst_date="2026-05-04",
        correlation_id="corr-succeed",
    )
    assert succeeded.status == "previewed"

    rows = (
        await db_session.execute(
            select(WatchOrderIntentLedger).order_by(WatchOrderIntentLedger.created_at)
        )
    ).scalars().all()
    assert [r.lifecycle_state for r in rows] == ["failed", "previewed"]


@pytest.mark.asyncio
async def test_emit_intent_cap_blocked_records_failed_row(
    db_session: AsyncSession,
) -> None:
    service = WatchOrderIntentService(db_session, fx_provider=FakeFx())
    result = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(quantity=100, max_notional_krw=Decimal("100000")),
        triggered_value=Decimal("68000"),
        kst_date="2026-05-04",
        correlation_id="corr-cap",
    )
    assert result.status == "failed"
    assert result.blocked_by == "max_notional_krw_cap"

    row = (
        await db_session.execute(select(WatchOrderIntentLedger))
    ).scalars().one()
    assert row.lifecycle_state == "failed"
    assert row.blocked_by == "max_notional_krw_cap"
    assert row.notional_krw_evaluated is not None
```

- [ ] **Step 5.3: Run tests, expect failures**

Run: `uv run pytest tests/test_watch_order_intent_service.py -v`
Expected: ImportError on `WatchOrderIntentService`.

- [ ] **Step 5.4: Implement `app/services/watch_order_intent_service.py`**

```python
"""Watch order intent ledger writer (ROB-103).

This is the only writer to ``review.watch_order_intent_ledger``. It
- calls ``app.services.exchange_rate_service.get_usd_krw_quote`` for US watches,
- defers to ``watch_order_intent_preview_builder.build_preview`` for the
  pure preview/cap/FX evaluation,
- inserts a single ledger row,
- handles the partial-unique-index conflict by reading back the existing
  ``previewed`` row and returning ``dedupe_hit``.

It must never call any broker submit endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import WatchOrderIntentLedger
from app.services.exchange_rate_service import get_usd_krw_quote
from app.services.watch_intent_policy import IntentPolicy
from app.services.watch_order_intent_preview_builder import (
    ACCOUNT_MODE,
    EXECUTION_SOURCE,
    IntentBuildFailure,
    IntentBuildSuccess,
    build_preview,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IntentEmissionResult:
    status: Literal["previewed", "failed", "dedupe_hit"]
    ledger_id: int | None
    correlation_id: str | None
    idempotency_key: str
    market: str
    symbol: str
    side: str
    quantity: Decimal | None
    limit_price: Decimal | None
    blocked_by: str | None
    reason: str | None

    def to_alert_dict(self) -> dict:
        return {
            "market": self.market,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": float(self.quantity) if self.quantity is not None else None,
            "limit_price": float(self.limit_price) if self.limit_price is not None else None,
            "status": self.status,
            "ledger_id": self.ledger_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
        }


class FxProvider(Protocol):
    async def get_quote(self) -> Decimal | None: ...


class _DefaultFxProvider:
    async def get_quote(self) -> Decimal | None:
        try:
            rate = await get_usd_krw_quote()
        except Exception as exc:
            logger.warning("FX quote fetch failed: %s", exc)
            return None
        if rate is None:
            return None
        return Decimal(str(rate))


def _build_idempotency_key(watch: dict, side: str, kst_date: str) -> str:
    return ":".join(
        [
            str(watch["market"]),
            str(watch["target_kind"]),
            str(watch["symbol"]),
            str(watch["condition_type"]),
            str(watch["threshold_key"]),
            "create_order_intent",
            side,
            kst_date,
        ]
    )


class WatchOrderIntentService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        fx_provider: FxProvider | None = None,
    ) -> None:
        self._db = db
        self._fx = fx_provider or _DefaultFxProvider()

    async def emit_intent(
        self,
        *,
        watch: dict,
        policy: IntentPolicy,
        triggered_value: Decimal,
        kst_date: str,
        correlation_id: str,
    ) -> IntentEmissionResult:
        idempotency_key = _build_idempotency_key(watch, policy.side, kst_date)

        fx_quote: Decimal | None = None
        if watch["market"] == "us":
            fx_quote = await self._fx.get_quote()

        result = build_preview(
            policy=policy,
            watch=watch,
            triggered_value=triggered_value,
            fx_quote=fx_quote,
            kst_date=kst_date,
        )

        if isinstance(result, IntentBuildFailure):
            return await self._insert_failed(
                watch=watch,
                policy=policy,
                triggered_value=triggered_value,
                kst_date=kst_date,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                failure=result,
            )

        return await self._insert_or_dedupe_previewed(
            watch=watch,
            policy=policy,
            triggered_value=triggered_value,
            kst_date=kst_date,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            success=result,
        )

    async def _insert_or_dedupe_previewed(
        self,
        *,
        watch: dict,
        policy: IntentPolicy,
        triggered_value: Decimal,
        kst_date: str,
        correlation_id: str,
        idempotency_key: str,
        success: IntentBuildSuccess,
    ) -> IntentEmissionResult:
        line = success.preview_line
        row = WatchOrderIntentLedger(
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=watch["market"],
            target_kind=watch["target_kind"],
            symbol=watch["symbol"],
            condition_type=watch["condition_type"],
            threshold=watch["threshold"],
            threshold_key=watch["threshold_key"],
            action="create_order_intent",
            side=policy.side,
            account_mode=ACCOUNT_MODE,
            execution_source=EXECUTION_SOURCE,
            lifecycle_state="previewed",
            quantity=line.quantity,
            limit_price=line.limit_price,
            notional=line.notional,
            currency=line.currency,
            notional_krw_input=policy.notional_krw,
            max_notional_krw=policy.max_notional_krw,
            notional_krw_evaluated=success.notional_krw_evaluated,
            fx_usd_krw_used=success.fx_usd_krw_used,
            approval_required=True,
            execution_allowed=False,
            blocking_reasons=[],
            blocked_by=None,
            detail={"basket_preview": success.basket.model_dump(mode="json")},
            preview_line=line.model_dump(mode="json"),
            triggered_value=triggered_value,
            kst_date=kst_date,
        )
        self._db.add(row)
        try:
            await self._db.flush()
        except IntegrityError:
            await self._db.rollback()
            existing = (
                await self._db.execute(
                    select(WatchOrderIntentLedger).where(
                        WatchOrderIntentLedger.idempotency_key == idempotency_key,
                        WatchOrderIntentLedger.lifecycle_state == "previewed",
                    )
                )
            ).scalars().one()
            return IntentEmissionResult(
                status="dedupe_hit",
                ledger_id=existing.id,
                correlation_id=existing.correlation_id,
                idempotency_key=idempotency_key,
                market=existing.market,
                symbol=existing.symbol,
                side=existing.side,
                quantity=existing.quantity,
                limit_price=existing.limit_price,
                blocked_by=None,
                reason="already_previewed_today",
            )
        await self._db.commit()
        return IntentEmissionResult(
            status="previewed",
            ledger_id=row.id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=row.market,
            symbol=row.symbol,
            side=row.side,
            quantity=line.quantity,
            limit_price=line.limit_price,
            blocked_by=None,
            reason=None,
        )

    async def _insert_failed(
        self,
        *,
        watch: dict,
        policy: IntentPolicy,
        triggered_value: Decimal,
        kst_date: str,
        correlation_id: str,
        idempotency_key: str,
        failure: IntentBuildFailure,
    ) -> IntentEmissionResult:
        preview_payload = {
            "lifecycle_state": "failed",
            "blocked_by": failure.blocked_by,
            "blocking_reasons": failure.blocking_reasons,
            "quantity": str(failure.quantity) if failure.quantity is not None else None,
            "limit_price": (
                str(failure.limit_price) if failure.limit_price is not None else None
            ),
            "currency": failure.currency,
        }
        row = WatchOrderIntentLedger(
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=watch["market"],
            target_kind=watch["target_kind"],
            symbol=watch["symbol"],
            condition_type=watch["condition_type"],
            threshold=watch["threshold"],
            threshold_key=watch["threshold_key"],
            action="create_order_intent",
            side=policy.side,
            account_mode=ACCOUNT_MODE,
            execution_source=EXECUTION_SOURCE,
            lifecycle_state="failed",
            quantity=failure.quantity,
            limit_price=failure.limit_price,
            notional=None,
            currency=failure.currency,
            notional_krw_input=policy.notional_krw,
            max_notional_krw=policy.max_notional_krw,
            notional_krw_evaluated=failure.notional_krw_evaluated,
            fx_usd_krw_used=failure.fx_usd_krw_used,
            approval_required=True,
            execution_allowed=False,
            blocking_reasons=failure.blocking_reasons,
            blocked_by=failure.blocked_by,
            detail={"failure_input": {"sizing_source": "notional_krw" if policy.quantity is None else "quantity"}},
            preview_line=preview_payload,
            triggered_value=triggered_value,
            kst_date=kst_date,
        )
        self._db.add(row)
        await self._db.commit()
        return IntentEmissionResult(
            status="failed",
            ledger_id=row.id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            market=row.market,
            symbol=row.symbol,
            side=row.side,
            quantity=failure.quantity,
            limit_price=failure.limit_price,
            blocked_by=failure.blocked_by,
            reason=failure.blocked_by,
        )


__all__ = [
    "FxProvider",
    "IntentEmissionResult",
    "WatchOrderIntentService",
]
```

- [ ] **Step 5.5: Run service tests**

Run: `uv run pytest tests/test_watch_order_intent_service.py -v`
Expected: 4 cases pass.

If a fixture for `db_session` is missing, mirror the patterns used in `tests/services/test_watch_proximity.py` (or whichever existing async DB-backed test you found in Step 5.1) — usually a `db_session` fixture in `tests/conftest.py` or `tests/services/conftest.py`. Add the new test under whatever directory matches the existing fixture scope.

- [ ] **Step 5.6: Commit**

```bash
git add app/services/watch_order_intent_service.py tests/test_watch_order_intent_service.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): add watch order intent ledger service

Service-only writer for review.watch_order_intent_ledger. Calls the
pure preview builder, fetches USD/KRW for US watches, inserts a
previewed or failed row, and handles the partial-unique-index
conflict by returning a dedupe_hit pointing at the existing previewed
row. Never calls any broker submit endpoint.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 6: Watch alerts service — payload extension

**Files:**
- Modify: `app/services/watch_alerts.py`
- Modify: `tests/test_watch_alerts.py`

- [ ] **Step 6.1: Write the failing tests (additions to `tests/test_watch_alerts.py`)**

Append to `tests/test_watch_alerts.py`:

```python
import json

import pytest

from app.services.watch_alerts import WatchAlertService


@pytest.mark.asyncio
async def test_add_watch_intent_payload_round_trips() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    result = await service.add_watch(
        market="kr",
        symbol="005930",
        condition_type="price_below",
        threshold=70000,
        action="create_order_intent",
        side="buy",
        quantity=1,
        max_notional_krw=1500000,
    )
    assert result["created"] is True

    rows = await service.list_watches("kr")
    watch = rows["kr"][0]
    assert watch["action"] == "create_order_intent"
    assert watch["side"] == "buy"
    assert watch["quantity"] == 1
    assert watch["notional_krw"] is None
    assert watch["max_notional_krw"] == 1500000


@pytest.mark.asyncio
async def test_add_watch_rejects_create_order_intent_for_crypto() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    with pytest.raises(ValueError) as excinfo:
        await service.add_watch(
            market="crypto",
            symbol="BTC",
            condition_type="price_below",
            threshold=90000000,
            action="create_order_intent",
            side="buy",
            quantity=1,
        )
    assert "intent_market_unsupported" in str(excinfo.value)


@pytest.mark.asyncio
async def test_legacy_payload_lists_as_notify_only() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    # Simulate a row written by the pre-ROB-103 code path.
    field = "asset:005930:price_below:70000"
    fake_redis._hashes["watch:alerts:kr"] = {
        field: json.dumps({"created_at": "2026-05-04T00:00:00+09:00"}),
    }

    rows = await service.list_watches("kr")
    watch = rows["kr"][0]
    assert watch["action"] == "notify_only"
    assert "side" in watch and watch["side"] is None
```

- [ ] **Step 6.2: Run tests, expect failures**

Run: `uv run pytest tests/test_watch_alerts.py -v`
Expected: the new tests fail because `add_watch` does not yet accept policy kwargs and `list_watches` does not surface them.

- [ ] **Step 6.3: Modify `app/services/watch_alerts.py`**

In the `add_watch` method, add optional kwargs for the policy and persist a richer JSON payload. Specifically:

1. At the top of the file, add the import:
   ```python
   from app.services.watch_intent_policy import (
       IntentPolicy,
       NotifyOnlyPolicy,
       WatchPolicyError,
       parse_policy,
   )
   ```

2. Replace the existing `add_watch` signature and body with:

```python
    async def add_watch(
        self,
        market: str,
        symbol: str,
        condition_type: str,
        threshold: float,
        target_kind: str | None = None,
        *,
        action: str | None = None,
        side: str | None = None,
        quantity: int | None = None,
        notional_krw: float | None = None,
        limit_price: float | None = None,
        max_notional_krw: float | None = None,
    ) -> dict[str, object]:
        normalized_market = self._normalize_market(market)
        watch_key = self.validate_watch_inputs(
            market=normalized_market,
            symbol=symbol,
            condition_type=condition_type,
            threshold=threshold,
            target_kind=target_kind,
        )

        normalized_action = (action or "notify_only").strip().lower() or "notify_only"
        policy_payload: dict[str, object] = {"action": normalized_action}
        if normalized_action == "create_order_intent":
            policy_payload["side"] = side
            if quantity is not None:
                policy_payload["quantity"] = quantity
            if notional_krw is not None:
                policy_payload["notional_krw"] = notional_krw
            if limit_price is not None:
                policy_payload["limit_price"] = limit_price
            if max_notional_krw is not None:
                policy_payload["max_notional_krw"] = max_notional_krw
        else:
            for forbidden_name, forbidden_value in (
                ("side", side),
                ("quantity", quantity),
                ("notional_krw", notional_krw),
                ("limit_price", limit_price),
                ("max_notional_krw", max_notional_krw),
            ):
                if forbidden_value is not None:
                    raise ValueError(
                        f"notify_only_must_be_bare: {forbidden_name} not allowed"
                    )

        canonical_payload = {
            "created_at": now_kst().isoformat(),
            **policy_payload,
        }
        try:
            parse_policy(
                market=normalized_market,
                target_kind=watch_key.target_kind,
                condition_type=watch_key.condition_type,
                raw_payload=json.dumps(canonical_payload),
            )
        except WatchPolicyError as exc:
            raise ValueError(str(exc.code)) from exc

        redis_client = await self._get_redis()
        redis_key = self._key_for_market(normalized_market)

        already_exists = await redis_client.hexists(redis_key, watch_key.field)
        existing_field = watch_key.field
        if not already_exists and watch_key.target_kind == "asset":
            already_exists = await redis_client.hexists(
                redis_key,
                watch_key.legacy_field,
            )
            if already_exists:
                existing_field = watch_key.legacy_field
        if already_exists:
            return {
                "market": normalized_market,
                "target_kind": watch_key.target_kind,
                "symbol": watch_key.symbol,
                "condition_type": watch_key.condition_type,
                "threshold": watch_key.threshold,
                "field": existing_field,
                "created": False,
                "already_exists": True,
            }

        await redis_client.hset(
            redis_key, watch_key.field, json.dumps(canonical_payload)
        )

        return {
            "market": normalized_market,
            "target_kind": watch_key.target_kind,
            "symbol": watch_key.symbol,
            "condition_type": watch_key.condition_type,
            "threshold": watch_key.threshold,
            "field": watch_key.field,
            "created": True,
            "already_exists": False,
        }
```

3. In `list_watches`, when building `rows`, also surface the parsed policy. Inside the `for field, raw_payload in payloads.items():` loop, after computing `created_at`, add:

```python
                policy_action = "notify_only"
                policy_side: str | None = None
                policy_quantity: int | None = None
                policy_notional_krw: float | None = None
                policy_limit_price: float | None = None
                policy_max_notional_krw: float | None = None
                try:
                    parsed_policy = parse_policy(
                        market=market_name,
                        target_kind=target_kind,
                        condition_type=normalized_condition,
                        raw_payload=raw_payload,
                    )
                except WatchPolicyError:
                    parsed_policy = NotifyOnlyPolicy()
                if isinstance(parsed_policy, IntentPolicy):
                    policy_action = parsed_policy.action
                    policy_side = parsed_policy.side
                    policy_quantity = parsed_policy.quantity
                    policy_notional_krw = (
                        float(parsed_policy.notional_krw)
                        if parsed_policy.notional_krw is not None
                        else None
                    )
                    policy_limit_price = (
                        float(parsed_policy.limit_price)
                        if parsed_policy.limit_price is not None
                        else None
                    )
                    policy_max_notional_krw = (
                        float(parsed_policy.max_notional_krw)
                        if parsed_policy.max_notional_krw is not None
                        else None
                    )
```

Then add these keys to the appended `rows` dict:

```python
                        "action": policy_action,
                        "side": policy_side,
                        "quantity": policy_quantity,
                        "notional_krw": policy_notional_krw,
                        "limit_price": policy_limit_price,
                        "max_notional_krw": policy_max_notional_krw,
```

- [ ] **Step 6.4: Run all watch alerts tests**

Run: `uv run pytest tests/test_watch_alerts.py -v`
Expected: every existing case still passes plus the three new cases pass. If a pre-existing test asserted exact payload shape (`"created_at"` only), the assertion needs to be relaxed to `_payload_contains` style — fix any such assertion inline.

- [ ] **Step 6.5: Commit**

```bash
git add app/services/watch_alerts.py tests/test_watch_alerts.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): extend watch_alerts payload with action policy

add_watch now accepts notify_only/create_order_intent action with
optional side/quantity/notional_krw/limit_price/max_notional_krw
fields. The Redis payload is JSON of created_at plus the policy
fields; the legacy created_at-only payload still parses as
notify_only. list_watches surfaces the parsed policy on each row.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 7: OpenClaw client — additive `intents` payload field

**Files:**
- Modify: `app/services/openclaw_client.py`
- (No new tests — this surface is exercised end-to-end via the scanner tests in Task 8.)

- [ ] **Step 7.1: Modify `send_watch_alert_to_n8n` signature and payload**

In `app/services/openclaw_client.py`, change the method so it accepts an optional `intents` argument and includes it in the JSON payload (default empty list when omitted):

```python
    async def send_watch_alert_to_n8n(
        self,
        *,
        message: str,
        market: str,
        triggered: list[dict[str, Any]],
        as_of: str,
        correlation_id: str | None = None,
        intents: list[dict[str, Any]] | None = None,
    ) -> WatchAlertDeliveryResult:
        ...
        payload = {
            "alert_type": "watch",
            "correlation_id": correlation_id,
            "as_of": as_of,
            "market": market,
            "triggered": triggered,
            "message": message,
            "intents": intents or [],
        }
        ...
```

Leave the rest of the method (logging, retry, return) unchanged.

- [ ] **Step 7.2: Run a quick smoke test**

Run: `uv run python -c "from app.services.openclaw_client import OpenClawClient; print(OpenClawClient.send_watch_alert_to_n8n.__doc__ or 'ok')"`
Expected: prints something (no AttributeError).

- [ ] **Step 7.3: Commit**

```bash
git add app/services/openclaw_client.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): add intents block to n8n watch alert payload

send_watch_alert_to_n8n now accepts an optional intents list and
emits it as an additive field in the n8n JSON payload. Existing
callers without the kwarg continue to send the same triggered-only
shape.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 8: Watch scanner — branch to intent service

**Files:**
- Modify: `app/jobs/watch_scanner.py`
- Modify: `tests/test_watch_scanner.py`

- [ ] **Step 8.1: Add new failing tests in `tests/test_watch_scanner.py`**

Read the existing file first to find the fixture/helper style. Then add tests that:

1. notify_only watch in `kr` market still produces an n8n alert and deletes the watch (regression).
2. create_order_intent watch produces a `previewed` ledger row, an `intents` block, and deletes the watch.
3. create_order_intent watch failing on cap blocks deletion and ledger row is `failed`.
4. dedupe path: a second scan in the same KST day produces `dedupe_hit` and deletes the watch.

Sketch — adapt to the existing test style (this codebase mocks `OpenClawClient` and `WatchAlertService` in scanner tests):

```python
class TestScannerWithCreateOrderIntent:
    @pytest.mark.asyncio
    async def test_create_order_intent_previewed_branches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ... arrange a single kr watch with action=create_order_intent
        # ... patch market_data.get_quote → trigger price below threshold
        # ... patch WatchOrderIntentService.emit_intent → returns IntentEmissionResult(status="previewed", ...)
        # ... patch OpenClawClient.send_watch_alert_to_n8n → success
        # assert payload included `intents=[...]` with status=previewed
        # assert WatchAlertService.trigger_and_remove was called with the watch field
        ...

    @pytest.mark.asyncio
    async def test_failed_intent_keeps_watch(self) -> None:
        # ... patch service to return status="failed", blocked_by="max_notional_krw_cap"
        # assert trigger_and_remove was NOT called for that field
        ...

    @pytest.mark.asyncio
    async def test_dedupe_hit_still_deletes_watch(self) -> None:
        # ... patch service to return status="dedupe_hit"
        # assert trigger_and_remove was called
        ...
```

Follow the patterns in `tests/test_watch_scanner.py` for monkeypatching `WatchAlertService` and `OpenClawClient`. Use real `parse_policy` so the tests verify the integration with payload parsing.

- [ ] **Step 8.2: Run new tests, expect failure**

Run: `uv run pytest tests/test_watch_scanner.py -v`
Expected: the new tests fail because the scanner does not yet call `WatchOrderIntentService`.

- [ ] **Step 8.3: Modify `app/jobs/watch_scanner.py`**

1. Add imports near the top:

```python
from app.core.db import get_async_session  # whichever helper this codebase uses
from app.core.timezone import now_kst
from app.services.watch_intent_policy import (
    IntentPolicy,
    NotifyOnlyPolicy,
    WatchPolicyError,
    parse_policy,
)
from app.services.watch_order_intent_service import WatchOrderIntentService
```

If `get_async_session` is not the right helper, find it via `grep -n "AsyncSession" app/jobs/`.

2. In `WatchScanner.__init__`, accept an optional intent service factory so tests can inject:

```python
    def __init__(
        self,
        *,
        intent_service_factory=None,
    ) -> None:
        self._watch_service = WatchAlertService()
        self._openclaw = OpenClawClient()
        self._intent_service_factory = intent_service_factory
```

3. In `scan_market`, after the existing `watches` fetch and before the loop, prepare:

```python
        intents: list[dict[str, object]] = []
        kst_date = now_kst().date().isoformat()
```

4. Inside the loop, after computing `current` and confirming the trigger fired, parse policy and branch:

```python
            try:
                policy = parse_policy(
                    market=normalized_market,
                    target_kind=target_kind,
                    condition_type=condition_type,
                    raw_payload=watch.get("raw_payload"),
                )
            except WatchPolicyError as exc:
                logger.warning(
                    "Skipping watch with invalid policy: market=%s field=%s code=%s",
                    normalized_market,
                    field,
                    exc.code,
                )
                continue

            if isinstance(policy, NotifyOnlyPolicy):
                triggered.append({...existing dict...})
                triggered_fields.append(field)
                continue

            assert isinstance(policy, IntentPolicy)
            async with self._intent_session() as (db, factory):
                service = factory(db)
                emission = await service.emit_intent(
                    watch={
                        "market": normalized_market,
                        "target_kind": target_kind,
                        "symbol": symbol,
                        "condition_type": condition_type,
                        "threshold": Decimal(str(threshold)),
                        "threshold_key": str(threshold),
                    },
                    policy=policy,
                    triggered_value=Decimal(str(current)),
                    kst_date=kst_date,
                    correlation_id=uuid4().hex,
                )
            intents.append(emission.to_alert_dict())
            if emission.status in {"previewed", "dedupe_hit"}:
                triggered_fields.append(field)
```

5. Note: `watch.get("raw_payload")` requires a small change in `WatchAlertService.list_watches` to also return the raw payload string. Make that change now (in `app/services/watch_alerts.py`):

```python
                rows.append(
                    {
                        ...
                        "raw_payload": raw_payload,
                    }
                )
```

(Surfacing the raw payload preserves backward compatibility while letting the scanner parse with the same function used at add time.)

6. Add a small helper on the scanner for the DB session:

```python
    def _intent_session(self):
        if self._intent_service_factory is not None:
            return self._intent_service_factory()
        return _default_intent_session()
```

And at the bottom of the module add:

```python
@asynccontextmanager
async def _default_intent_session():
    async with get_async_session() as db:
        yield db, lambda session: WatchOrderIntentService(session)
```

(Adapt `get_async_session` to whatever exists in `app/core/db.py`. If the codebase uses an async session factory you instantiate directly, do that instead.)

7. When building the alert message and calling `_send_alert`, include the `intents` block:

```python
        message = self._build_batched_message(
            normalized_market, triggered, intents=intents
        )
        result = await self._send_alert(
            market=normalized_market,
            triggered=triggered,
            intents=intents,
            message=message,
        )
```

And update `_build_batched_message` and `_send_alert`:

```python
    @staticmethod
    def _build_batched_message(
        market: str,
        triggered: list[dict[str, object]],
        intents: list[dict[str, object]] | None = None,
    ) -> str:
        lines = [f"Watch alerts ({market})"]
        for row in triggered:
            ...
        if intents:
            lines.append("")
            lines.append(f"Order intents ({market}, kis_mock)")
            for intent in intents:
                status = intent["status"]
                if status == "previewed":
                    lines.append(
                        f"- previewed: {intent['symbol']} {intent['side']} "
                        f"qty={intent['quantity']} limit={intent['limit_price']} "
                        f"ledger={intent['ledger_id']}"
                    )
                elif status == "dedupe_hit":
                    lines.append(
                        f"- dedupe_hit: {intent['symbol']} {intent['side']} "
                        f"(already previewed today, ledger={intent['ledger_id']})"
                    )
                else:  # failed
                    lines.append(
                        f"- failed: {intent['symbol']} {intent['side']} "
                        f"qty={intent['quantity']} limit={intent['limit_price']} "
                        f"(blocked_by={intent['blocked_by']}, watch kept)"
                    )
        return "\n".join(lines)
```

```python
    async def _send_alert(
        self,
        *,
        market: str,
        triggered: list[dict[str, object]],
        message: str,
        intents: list[dict[str, object]] | None = None,
    ) -> WatchAlertDeliveryResult:
        correlation_id = str(uuid4())
        as_of = Timestamp.now("UTC").isoformat()
        try:
            return await self._openclaw.send_watch_alert_to_n8n(
                message=message,
                market=market,
                triggered=triggered,
                as_of=as_of,
                correlation_id=correlation_id,
                intents=intents or [],
            )
        except Exception as exc:
            logger.error("Failed to send watch scan alert: %s", exc)
            return WatchAlertDeliveryResult(status="failed", reason="request_failed")
```

8. Add `from contextlib import asynccontextmanager` and `from decimal import Decimal` at the top.

9. The scan should still succeed (return `success`/`skipped`) even when there are zero `triggered` plus zero `intents` — keep the existing skip path. Adjust the early-return condition: previously `if not triggered: ... skipped no_triggered_alerts`. Change to `if not triggered and not intents:` so a scan with only intents (e.g., all watches were create_order_intent and previewed) still sends the alert.

- [ ] **Step 8.4: Run scanner tests**

Run: `uv run pytest tests/test_watch_scanner.py -v`
Expected: existing notify_only tests still pass, new intent tests pass.

- [ ] **Step 8.5: Commit**

```bash
git add app/jobs/watch_scanner.py app/services/watch_alerts.py tests/test_watch_scanner.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): branch watch scanner to intent service

Triggered watches whose Redis payload carries action=create_order_intent
go through the new WatchOrderIntentService and produce a previewed,
failed, or dedupe_hit ledger row plus an entry in the additive
intents block of the n8n alert. notify_only watches keep their
existing alert-and-delete flow. Failed intents preserve the watch so
the operator can adjust the policy.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 9: MCP `manage_watch_alerts` extension

**Files:**
- Modify: `app/mcp_server/tooling/watch_alerts_registration.py`
- Modify: `tests/test_mcp_watch_alerts.py`

- [ ] **Step 9.1: Add failing tests**

Append to `tests/test_mcp_watch_alerts.py`:

```python
@pytest.mark.asyncio
async def test_manage_watch_alerts_add_with_intent_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_add_watch(self, **kwargs: object) -> dict:
        captured.update(kwargs)
        return {
            "market": kwargs["market"],
            "target_kind": kwargs.get("target_kind") or "asset",
            "symbol": kwargs["symbol"],
            "condition_type": kwargs["condition_type"],
            "threshold": kwargs["threshold"],
            "field": "asset:005930:price_below:70000",
            "created": True,
            "already_exists": False,
        }

    from app.mcp_server.tooling import watch_alerts_registration as mod
    monkeypatch.setattr(mod.WatchAlertService, "add_watch", fake_add_watch)

    result = await mod.manage_watch_alerts_impl(
        action="add",
        market="kr",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=70000,
        intent_action="create_order_intent",
        side="buy",
        quantity=1,
        max_notional_krw=1500000,
    )
    assert result["success"] is True
    assert captured["action"] == "create_order_intent"
    assert captured["side"] == "buy"
    assert captured["quantity"] == 1
    assert captured["max_notional_krw"] == 1500000


@pytest.mark.asyncio
async def test_manage_watch_alerts_add_default_remains_notify_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_add_watch(self, **kwargs: object) -> dict:
        captured.update(kwargs)
        return {"created": True, "already_exists": False, "field": "asset:BTC:price_above:1"}

    from app.mcp_server.tooling import watch_alerts_registration as mod
    monkeypatch.setattr(mod.WatchAlertService, "add_watch", fake_add_watch)

    result = await mod.manage_watch_alerts_impl(
        action="add",
        market="crypto",
        symbol="BTC",
        metric="price",
        operator="above",
        threshold=1,
    )
    assert result["success"] is True
    assert "action" not in captured  # no policy fields forwarded
    assert "side" not in captured
```

- [ ] **Step 9.2: Run, expect failure**

Run: `uv run pytest tests/test_mcp_watch_alerts.py -v`
Expected: new tests fail because the tool does not accept `intent_action` etc.

- [ ] **Step 9.3: Modify `app/mcp_server/tooling/watch_alerts_registration.py`**

Extend `manage_watch_alerts_impl`:

```python
async def manage_watch_alerts_impl(
    action: str,
    market: str | None = None,
    target_kind: str | None = None,
    symbol: str | None = None,
    metric: str | None = None,
    operator: str | None = None,
    threshold: float | None = None,
    *,
    intent_action: str | None = None,
    side: str | None = None,
    quantity: int | None = None,
    notional_krw: float | None = None,
    limit_price: float | None = None,
    max_notional_krw: float | None = None,
) -> dict:
    ...
        if normalized_action == "add":
            add_kwargs: dict[str, object] = {
                "market": market,
                "symbol": symbol,
                "condition_type": condition_type,
                "threshold": normalized_threshold,
                "target_kind": target_kind,
            }
            if intent_action is not None:
                add_kwargs["action"] = intent_action
                add_kwargs["side"] = side
                if quantity is not None:
                    add_kwargs["quantity"] = quantity
                if notional_krw is not None:
                    add_kwargs["notional_krw"] = notional_krw
                if limit_price is not None:
                    add_kwargs["limit_price"] = limit_price
                if max_notional_krw is not None:
                    add_kwargs["max_notional_krw"] = max_notional_krw
            try:
                result = await service.add_watch(**add_kwargs)
            except ValueError as exc:
                return {
                    "success": False,
                    "error": str(exc),
                }
            return {
                "success": True,
                "action": "add",
                **result,
                "target_kind": str(target_kind or "asset").strip().lower(),
                "market": normalized_market,
                "symbol": normalized_symbol,
                "condition_type": condition_type,
                "threshold": normalized_threshold,
            }
```

Update the tool registration `description` to mention the optional `intent_action`/`side`/`quantity`/`notional_krw`/`limit_price`/`max_notional_krw` kwargs and that omitting them keeps the legacy notify-only behavior.

- [ ] **Step 9.4: Run all MCP watch alert tests**

Run: `uv run pytest tests/test_mcp_watch_alerts.py -v`
Expected: green.

- [ ] **Step 9.5: Commit**

```bash
git add app/mcp_server/tooling/watch_alerts_registration.py tests/test_mcp_watch_alerts.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): extend manage_watch_alerts with intent policy kwargs

The add action now accepts optional intent_action/side/quantity/
notional_krw/limit_price/max_notional_krw fields. Calls without
them are byte-identical to the previous notify-only flow.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 10: Read-only HTTP router + MCP read tools

**Files:**
- Create: `app/routers/watch_order_intent_ledger.py`
- Create: `app/mcp_server/tooling/watch_order_intent_ledger_read.py`
- Test: `tests/test_watch_order_intent_ledger_router.py`
- Test: `tests/test_mcp_watch_order_intent_ledger.py`

- [ ] **Step 10.1: Create the router**

```python
"""Read-only watch order intent ledger router (ROB-103).

GET endpoints only. No POST/PATCH/DELETE. No broker mutation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.review import WatchOrderIntentLedger
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user

router = APIRouter(prefix="/trading", tags=["watch-order-intent-ledger"])


def _serialize(row: WatchOrderIntentLedger) -> dict:
    return {
        "id": row.id,
        "correlation_id": row.correlation_id,
        "idempotency_key": row.idempotency_key,
        "market": row.market,
        "target_kind": row.target_kind,
        "symbol": row.symbol,
        "condition_type": row.condition_type,
        "threshold": float(row.threshold) if row.threshold is not None else None,
        "action": row.action,
        "side": row.side,
        "account_mode": row.account_mode,
        "execution_source": row.execution_source,
        "lifecycle_state": row.lifecycle_state,
        "quantity": float(row.quantity) if row.quantity is not None else None,
        "limit_price": float(row.limit_price) if row.limit_price is not None else None,
        "notional": float(row.notional) if row.notional is not None else None,
        "currency": row.currency,
        "notional_krw_input": (
            float(row.notional_krw_input) if row.notional_krw_input is not None else None
        ),
        "max_notional_krw": (
            float(row.max_notional_krw) if row.max_notional_krw is not None else None
        ),
        "notional_krw_evaluated": (
            float(row.notional_krw_evaluated)
            if row.notional_krw_evaluated is not None
            else None
        ),
        "fx_usd_krw_used": (
            float(row.fx_usd_krw_used) if row.fx_usd_krw_used is not None else None
        ),
        "approval_required": row.approval_required,
        "execution_allowed": row.execution_allowed,
        "blocking_reasons": row.blocking_reasons,
        "blocked_by": row.blocked_by,
        "detail": row.detail,
        "preview_line": row.preview_line,
        "triggered_value": (
            float(row.triggered_value) if row.triggered_value is not None else None
        ),
        "kst_date": row.kst_date,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/api/watch/order-intent/ledger/recent")
async def list_recent(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    market: str | None = None,
    lifecycle_state: str | None = None,
    kst_date: str | None = None,
    limit: int = 20,
) -> dict:
    capped = max(1, min(limit, 100))
    stmt = select(WatchOrderIntentLedger).order_by(
        WatchOrderIntentLedger.created_at.desc()
    )
    if market is not None:
        stmt = stmt.where(WatchOrderIntentLedger.market == market.strip().lower())
    if lifecycle_state is not None:
        stmt = stmt.where(
            WatchOrderIntentLedger.lifecycle_state == lifecycle_state.strip().lower()
        )
    if kst_date is not None:
        stmt = stmt.where(WatchOrderIntentLedger.kst_date == kst_date.strip())
    stmt = stmt.limit(capped)
    rows = (await db.execute(stmt)).scalars().all()
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


@router.get("/api/watch/order-intent/ledger/{correlation_id}")
async def get_by_correlation(
    correlation_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> dict:
    row = (
        await db.execute(
            select(WatchOrderIntentLedger).where(
                WatchOrderIntentLedger.correlation_id == correlation_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not_found"
        )
    return _serialize(row)
```

- [ ] **Step 10.2: Create the MCP read tools**

```python
"""Read-only watch order intent ledger MCP tools (ROB-103)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.db import get_async_session
from app.models.review import WatchOrderIntentLedger
from app.routers.watch_order_intent_ledger import _serialize

if TYPE_CHECKING:
    from fastmcp import FastMCP

WATCH_ORDER_INTENT_LEDGER_TOOL_NAMES: set[str] = {
    "watch_order_intent_ledger_list_recent",
    "watch_order_intent_ledger_get",
}


async def watch_order_intent_ledger_list_recent_impl(
    market: str | None = None,
    lifecycle_state: str | None = None,
    kst_date: str | None = None,
    limit: int = 20,
) -> dict:
    capped = max(1, min(int(limit), 100))
    async with get_async_session() as db:
        stmt = select(WatchOrderIntentLedger).order_by(
            WatchOrderIntentLedger.created_at.desc()
        )
        if market is not None:
            stmt = stmt.where(WatchOrderIntentLedger.market == market.strip().lower())
        if lifecycle_state is not None:
            stmt = stmt.where(
                WatchOrderIntentLedger.lifecycle_state == lifecycle_state.strip().lower()
            )
        if kst_date is not None:
            stmt = stmt.where(WatchOrderIntentLedger.kst_date == kst_date.strip())
        stmt = stmt.limit(capped)
        rows = (await db.execute(stmt)).scalars().all()
        return {
            "success": True,
            "count": len(rows),
            "items": [_serialize(r) for r in rows],
        }


async def watch_order_intent_ledger_get_impl(correlation_id: str) -> dict:
    async with get_async_session() as db:
        row = (
            await db.execute(
                select(WatchOrderIntentLedger).where(
                    WatchOrderIntentLedger.correlation_id == correlation_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {"success": False, "error": "not_found"}
        return {"success": True, "item": _serialize(row)}


def register_watch_order_intent_ledger_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="watch_order_intent_ledger_list_recent",
        description=(
            "List recent watch_order_intent_ledger rows (read-only). "
            "Optional filters: market, lifecycle_state, kst_date. "
            "limit clamped to 1..100, default 20."
        ),
    )(watch_order_intent_ledger_list_recent_impl)
    mcp.tool(
        name="watch_order_intent_ledger_get",
        description="Fetch a single watch_order_intent_ledger row by correlation_id (read-only).",
    )(watch_order_intent_ledger_get_impl)


__all__ = [
    "WATCH_ORDER_INTENT_LEDGER_TOOL_NAMES",
    "register_watch_order_intent_ledger_tools",
    "watch_order_intent_ledger_get_impl",
    "watch_order_intent_ledger_list_recent_impl",
]
```

If `get_async_session` doesn't exist as named, use whatever async session helper the existing `alpaca_paper_ledger_read.py` MCP tool uses — open that file with `Read` to copy the pattern verbatim.

- [ ] **Step 10.3: Add router test**

Create `tests/test_watch_order_intent_ledger_router.py`. Use the existing `tests/test_alpaca_paper_ledger_router.py` as the template (auth fixture + httpx client). Two cases minimum:

```python
@pytest.mark.asyncio
async def test_list_recent_returns_inserted_row(
    authenticated_client, db_session
) -> None:
    # arrange: insert a previewed row directly
    # act: GET /trading/api/watch/order-intent/ledger/recent
    # assert: 200, count=1, items[0]["lifecycle_state"]=="previewed"
    ...


@pytest.mark.asyncio
async def test_get_by_correlation_404_when_missing(authenticated_client) -> None:
    response = await authenticated_client.get(
        "/trading/api/watch/order-intent/ledger/does-not-exist"
    )
    assert response.status_code == 404
```

- [ ] **Step 10.4: Add MCP tool test**

Create `tests/test_mcp_watch_order_intent_ledger.py`:

```python
import pytest

from app.mcp_server.tooling.watch_order_intent_ledger_read import (
    watch_order_intent_ledger_get_impl,
    watch_order_intent_ledger_list_recent_impl,
)


@pytest.mark.asyncio
async def test_list_recent_returns_dict_shape(db_session) -> None:
    result = await watch_order_intent_ledger_list_recent_impl()
    assert result["success"] is True
    assert "items" in result
    assert "count" in result


@pytest.mark.asyncio
async def test_get_returns_not_found_for_unknown_correlation_id() -> None:
    result = await watch_order_intent_ledger_get_impl("does-not-exist")
    assert result["success"] is False
    assert result["error"] == "not_found"
```

- [ ] **Step 10.5: Run new tests, expect partial fails (no registration yet)**

Run: `uv run pytest tests/test_watch_order_intent_ledger_router.py tests/test_mcp_watch_order_intent_ledger.py -v`
Expected: tool tests pass; router tests may fail until Task 11 wires the router into the FastAPI app.

- [ ] **Step 10.6: Commit**

```bash
git add app/routers/watch_order_intent_ledger.py app/mcp_server/tooling/watch_order_intent_ledger_read.py tests/test_watch_order_intent_ledger_router.py tests/test_mcp_watch_order_intent_ledger.py
git commit -m "$(cat <<'EOF'
feat(ROB-103): add read-only ledger router and MCP tools

GET-only HTTP endpoints under /trading/api/watch/order-intent/ledger
and two MCP tools (list_recent, get) that read review.watch_order_intent_ledger.
No write paths exposed.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 11: Wire router and MCP registration

**Files:**
- Modify: `app/main.py`
- Modify: the existing MCP registration entry-point that wires other tools (locate via `grep`)

- [ ] **Step 11.1: Locate and modify the FastAPI router include**

Run: `grep -n "alpaca_paper_ledger" app/main.py`
Add the new router right next to the alpaca paper ledger import:

```python
from app.routers import (
    ...,
    alpaca_paper_ledger,
    watch_order_intent_ledger,
)
...
app.include_router(watch_order_intent_ledger.router)
```

- [ ] **Step 11.2: Locate the MCP tool registration entry-point**

Run: `grep -rn "register_alpaca_paper_ledger_tools\|register_watch_alert_tools" app/mcp_server/ | head -10`

Add the new registration call in the same place:

```python
from app.mcp_server.tooling.watch_order_intent_ledger_read import (
    register_watch_order_intent_ledger_tools,
)
...
register_watch_order_intent_ledger_tools(mcp)
```

- [ ] **Step 11.3: Run full router + MCP test**

Run: `uv run pytest tests/test_watch_order_intent_ledger_router.py tests/test_mcp_watch_order_intent_ledger.py -v`
Expected: all green.

- [ ] **Step 11.4: Commit**

```bash
git add app/main.py app/mcp_server/  # whichever module was edited
git commit -m "$(cat <<'EOF'
chore(ROB-103): register watch order intent ledger router and MCP tools

Wires the new GET-only router and the two read-only MCP tools into
the FastAPI app and the MCP server.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 12: Operator runbook + final regression sweep

**Files:**
- Create: `docs/runbooks/watch-order-intent-ledger.md`

- [ ] **Step 12.1: Write the runbook**

Mirror `docs/runbooks/alpaca-paper-ledger.md` structure. Required sections:

```markdown
# Watch Order Intent Ledger Runbook (ROB-103)

## What this is
review.watch_order_intent_ledger captures every approval-required
order intent emitted by the watch scanner. It is append-only, written
exclusively by `app.services.watch_order_intent_service.WatchOrderIntentService`,
and never triggers a broker call.

## Adding a watch with policy
Use `manage_watch_alerts add` with the optional intent kwargs:

```
manage_watch_alerts add \
  market=kr symbol=005930 metric=price operator=below threshold=70000 \
  intent_action=create_order_intent side=buy quantity=1 max_notional_krw=1500000
```

Omitting the intent kwargs preserves the legacy notify_only behavior.

## Reading the ledger
- `watch_order_intent_ledger_list_recent(market="kr", lifecycle_state="previewed", limit=20)`
- `watch_order_intent_ledger_get(correlation_id="...")`
- HTTP: `GET /trading/api/watch/order-intent/ledger/recent`
        `GET /trading/api/watch/order-intent/ledger/{correlation_id}`

## Mental model
- previewed → watch was deleted (one previewed row per KST day per
  watch identity + side, enforced by partial unique index)
- failed → watch is **kept**; investigate `blocked_by`:
  - `max_notional_krw_cap` — operator policy too small relative to
    qty * limit_price * fx (US: FX-converted)
  - `fx_unavailable` — USD/KRW quote service down
  - `qty_zero` — `notional_krw / limit_price` floored below 1 share
  - `validation_error` — should not occur at scan time; if it does,
    the watch payload was tampered with
- dedupe_hit → watch deleted; the existing previewed row stays the
  source of truth for the day

## Hard rules
- Direct SQL `INSERT/UPDATE/DELETE` against
  `review.watch_order_intent_ledger` is forbidden.
- This ledger never authorizes a broker submit. ROB-103 explicitly
  excludes broker mutation. `account_mode` is pinned to `kis_mock`
  via a CHECK constraint.
- Live-account intents are not supported in this MVP.
```

- [ ] **Step 12.2: Run the full ROB-103 test sweep**

Run:
```bash
uv run pytest \
  tests/test_watch_intent_policy.py \
  tests/test_watch_order_intent_preview_builder.py \
  tests/test_watch_order_intent_service.py \
  tests/test_watch_alerts.py \
  tests/test_watch_scanner.py \
  tests/test_mcp_watch_alerts.py \
  tests/test_watch_order_intent_ledger_router.py \
  tests/test_mcp_watch_order_intent_ledger.py \
  -v
```
Expected: every test passes.

- [ ] **Step 12.3: Run lint, format, type-check**

Run: `make lint && make typecheck`
Expected: no new errors. If `ty` flags Decimal/float typing in the new code, fix inline (the model uses `float | None` per project convention; service code wraps with `Decimal(str(...))` where needed).

- [ ] **Step 12.4: Smoke import**

Run: `uv run python -c "from app.routers.watch_order_intent_ledger import router; from app.mcp_server.tooling.watch_order_intent_ledger_read import register_watch_order_intent_ledger_tools; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 12.5: Commit + push**

```bash
git add docs/runbooks/watch-order-intent-ledger.md
git commit -m "$(cat <<'EOF'
docs(ROB-103): add watch order intent ledger runbook

Operator-facing runbook covering the ledger schema, the MCP/HTTP
read surfaces, and the previewed/failed/dedupe_hit mental model.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"

git push -u origin HEAD
```

(If a remote does not yet exist for the branch, the engineer can confirm with the user before pushing.)

---

## Acceptance criteria mapping

| Acceptance criterion (issue) | Tasks |
|---|---|
| Existing watch alert-only tests continue to pass | Task 6 (extend) + Task 8 regression case + Task 12 sweep |
| A watch can be configured to produce an approval-required mock order intent preview | Tasks 1, 2, 3, 4, 5, 6, 8, 9 |
| Duplicate trigger / idempotency behavior covered by tests | Task 5 (`test_emit_intent_dedupe_returns_dedupe_hit`) + Task 8 dedupe case |
| Live account actions remain approval-only / non-executing | Task 1 rejects non-mock combos; Task 3/4 CHECK constraints pin `account_mode='kis_mock'`; Task 5 service has no broker submit; Task 12 runbook documents the rule |

## Self-review notes

- Spec sections 1, 2, 3 (scope/allowed combos) → Task 1 + Task 6.
- Spec section 4 (architecture) → file structure table at top of plan.
- Spec section 5 (Redis payload + validation) → Task 1 + Task 6.
- Spec section 6 (scanner flow) → Task 8.
- Spec section 7 (ledger schema) → Tasks 3, 4, 5.
- Spec section 8 (n8n payload) → Tasks 7, 8.
- Spec section 9 (MCP/HTTP surface) → Tasks 9, 10, 11.
- Spec section 10 (testing strategy) → tests in every relevant task.
- Spec section 11 (runbook) → Task 12.
- Spec section 12 (deferred follow-ups) → not in any task; explicitly out of scope.
- All `<DOWN_REV>` and `<SLUG>` placeholders are intentional and must be filled at execution time (Step 0.2 + Step 4.1) — they are runtime values, not unfinished plan content.
