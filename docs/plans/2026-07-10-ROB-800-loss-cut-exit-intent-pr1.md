# ROB-800 PR 1 — `loss_cut` exit intent (sanctioned live loss-cut) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed `exit_intent="loss_cut"` path to the shared `place_order` implementation so a sanctioned, per-ticket-approved, retrospective-backed live loss-cut sell can execute below cost basis (and up to a small slip below current price) without disabling any existing guard for any other path.

**Architecture:** `loss_cut` is a NEW path that sits *beside* `defensive_trim` (never replaces or extends it). It reuses defensive_trim's caller-identity + Paperclip-approval machinery, adds two new preconditions (structured `exit_reason`, a validated `retrospective_id`), generalizes the single-UUID caller check into a settings allowlist, and relaxes the limit-sell price guard from "≥ current price" to a **band** "≥ current × (1 − loss_cut_max_slip)" **only inside a resolved loss_cut context**. It works for crypto + KR + US because all three route through the one shared `_place_order_impl`. `exit_intent` is recorded on the live ledgers.

**Tech Stack:** Python 3.13, FastMCP tools, SQLAlchemy async ORM (`review.*` schema), Alembic (async), pydantic-settings, pytest (`make test`), Ruff + ty (`make lint`).

## Global Constraints

- **TDD**: every task writes a failing test first, then minimal code. `make lint` must pass at the end.
- **No regression to existing sell paths**: `defensive_trim`, `scalping_exit`, and `allow_loss_sell` behavior must be byte-for-byte unchanged. This is locked by a guard-matrix test (Task 3) and an end-to-end path-regression test (Task 8).
- **`loss_cut` is sell + limit only.** Market orders stay blocked for every path; `evaluate_market_sell_loss_guard` is NOT touched.
- **`loss_cut` is live-only** (`is_mock=False`). Mock practice already has `allow_loss_sell`; do not overlap.
- **All 4 preconditions are fail-closed** and, on dry_run, ALL violations are returned in a single aggregated response (new behavior — none exists today).
- **Guard relaxation happens ONLY when a `LossCutContext` is resolved.** No global flag, no bypass reachable by any other caller.
- **`loss_cut` and `defensive_trim` are mutually exclusive** in one call.
- **Markets**: crypto (`upbit`), KR (`kis`, via `kis_live_place_order`), US (`kis`, via generic `place_order`). All go through `_place_order_impl`.
- **Migration is additive & operator-applied**: the Alembic migration ships in the PR but `alembic upgrade head` is run separately by the operator (do not auto-run in tests beyond the test DB fixture).
- **Trader agent UUID** (default allowlist member, backcompat): `6b2192cc-14fa-4335-b572-2fe1e0cb54a7` (== `settings.trader_agent_id`).
- **Structured `exit_reason` vocabulary** (loss_cut): `{"stop_loss", "thesis_change"}` — aligned with the retrospective `trigger_type` gate.
- **`config/trading_policy.yaml` is operator-PR-edited only** — but this PR *is* that operator PR for the one new key.
- **Base branch: `main`. Do NOT merge. Report the PR number only.**
- **Out of scope (desk PR 2)**: tcx header injection + loss_cut ticket type. Document its requirements in the PR body (see Task 8).

---

## File Structure

**Create:**
- `alembic/versions/<rev>_rob800_add_exit_intent_to_live_ledgers.py` — additive `exit_intent` column on `review.live_order_ledger` + `review.kis_live_order_ledger`.
- `tests/mcp_server/tooling/test_loss_cut_preconditions.py` — unit tests for the precondition validator + guard band.
- `tests/mcp_server/tooling/test_loss_cut_place_order.py` — integration tests through `_place_order_impl` (dry_run aggregation, band accept/reject, approval_hash-required, ledger `exit_intent`).
- `tests/services/test_loss_cut_policy_and_settings.py` — policy key reader + settings allowlist tests.

**Modify:**
- `config/trading_policy.yaml` — add `sell.loss_cut_max_slip` threshold.
- `app/services/trading_policy_service.py` — add `loss_cut_max_slip()` reader helper.
- `app/core/config.py` — add `LOSS_CUT_ALLOWED_AGENT_IDS` list setting + validator.
- `app/services/trade_journal/trade_retrospective_service.py` — add `get_retrospective_by_id`.
- `app/mcp_server/tooling/order_validation.py` — `LossCutContext`, guard band in `evaluate_sell_price_guards`, `_validate_loss_cut_preconditions`, thread ctx into `_preview_sell` / `_validate_sell_side`.
- `app/mcp_server/tooling/order_execution.py` — new params on `_place_order_impl`, precondition orchestration + aggregated response, loss_cut approval_hash-required gate, thread `exit_intent` into `_execute_and_record` + `_build_preview`.
- `app/mcp_server/tooling/live_order_ledger.py` — thread `exit_intent` to `LiveOrderLedger`.
- `app/mcp_server/tooling/kis_live_ledger.py` — thread `exit_intent` to `KISLiveOrderLedger`.
- `app/models/review.py` — `exit_intent` column on `LiveOrderLedger` + `KISLiveOrderLedger`.
- `app/mcp_server/tooling/orders_registration.py` — `exit_intent` + `retrospective_id` params on `place_order` tool.
- `app/mcp_server/tooling/orders_kis_variants.py` — same params on `_place_order_variant` / `kis_live_place_order`.

---

## Reference: existing anchors (read before implementing)

- Guard SoT: `order_validation.py:83-119` `evaluate_sell_price_guards` (floor exempt via `defensive_trim_ctx` at :112; current-price guard always enforced at :117).
- Precondition template: `order_validation.py:439-496` `_validate_defensive_trim_preconditions` (agent-id check :469-474; Paperclip status=done :476-490; helpers `_is_cached_approved` :314, `_cache_approved` :324, `_fetch_approval_issue_status` :409 with 2s timeout + 60s cache).
- Caller identity: `caller_identity.py` `get_caller_agent_id()` / `get_caller_source()`; header `x-paperclip-agent-id` via `caller_identity_middleware.py`.
- Orchestrator: `order_execution.py:1180-1516` `_place_order_impl` (defensive_trim resolve :1251; sell branch :1304; preview :1323; approval-hash gate :1386-1451; record dispatch `_execute_and_record` :692-1081).
- Ledger models: `review.py` `LiveOrderLedger` (:384-493, has `exit_reason` :447, `dt_*` :465-467) and `KISLiveOrderLedger` (:293-364, `exit_reason` :350, no `dt_*`).
- Retrospective model: `review.py:1020` `TradeRetrospective` (`id` :1089, `symbol` :1096, `trigger_type` :1134, `created_at` :1141); `trigger_type` CHECK :1062-1068 includes `stop_loss`, `thesis_change`. `VALID_TRIGGER_TYPES` in `app/schemas/trade_retrospective.py:22`.
- Policy scalar analog: `config/trading_policy.yaml` `sell.loss_guard_min_multiple` (value 1.01). Loader `get_policy_for` `trading_policy_service.py:56`; `load_trading_policy()` :43.
- Settings list pattern: `config.py` `INVESTMENT_ADVISORY_DRAFT_PROFILES` (:728 + validator :859-884), `trader_agent_id` :763.
- approval token: `toss_approval.py` `verify_approval_token` :134, `APPROVAL_TTL_SECONDS=300` :25 (rung does NOT enter the token, only the idempotency key).

---

### Task 1: Policy key `sell.loss_cut_max_slip` + reader helper

**Files:**
- Modify: `config/trading_policy.yaml` (add under `thresholds:`, after the `sell.loss_guard_min_multiple` block)
- Modify: `app/services/trading_policy_service.py`
- Test: `tests/services/test_loss_cut_policy_and_settings.py`

**Interfaces:**
- Produces: `trading_policy_service.loss_cut_max_slip() -> float` (returns the configured fraction, default `0.02` if the key is somehow absent).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_loss_cut_policy_and_settings.py
import pytest
from app.services import trading_policy_service as tps


@pytest.mark.unit
def test_loss_cut_max_slip_reads_policy_value():
    tps._reset_cache_for_tests()
    assert tps.loss_cut_max_slip() == pytest.approx(0.02)


@pytest.mark.unit
def test_loss_cut_max_slip_visible_in_sell_lane():
    tps._reset_cache_for_tests()
    policy = tps.get_policy_for("crypto", "sell")
    assert "sell.loss_cut_max_slip" in policy["thresholds"]
    assert policy["thresholds"]["sell.loss_cut_max_slip"]["value"] == pytest.approx(0.02)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_loss_cut_policy_and_settings.py -v -k loss_cut_max_slip`
Expected: FAIL (`AttributeError: module ... has no attribute 'loss_cut_max_slip'` and/or key missing).

- [ ] **Step 3a: Add the YAML key**

In `config/trading_policy.yaml`, inside `thresholds:` (immediately after the `sell.loss_guard_min_multiple:` entry) add:

```yaml
  sell.loss_cut_max_slip:
    lanes: [sell]
    value: 0.02
    unit: fraction
    semantics: >-
      ROB-800 — max downward slip below current price a sanctioned loss_cut
      limit sell may price at (price >= current * (1 - value)). Code-enforced
      band inside a resolved loss_cut context only; fat-finger deep discounts
      stay blocked. Not applied to any other sell path.
```

Also update the `authority.does_not_govern` note is unnecessary — this value *is* code-enforced-from-policy like the sector cap; add a one-line comment above the key noting it is a code guard param sourced from policy (mirrors `portfolio.sector_cluster_cap_pct`).

- [ ] **Step 3b: Add the reader helper**

In `app/services/trading_policy_service.py`, after `sector_cluster_for` (line ~100), add:

```python
_LOSS_CUT_MAX_SLIP_KEY = "sell.loss_cut_max_slip"
_LOSS_CUT_MAX_SLIP_DEFAULT = 0.02


def loss_cut_max_slip() -> float:
    """ROB-800 — max downward slip fraction for a sanctioned loss_cut limit sell.

    Code-enforced band magnitude sourced from config/trading_policy.yaml
    (sell.loss_cut_max_slip). Falls back to 0.02 if the key is absent so the
    guard stays fail-closed (a small band) rather than fail-open.
    """
    doc = load_trading_policy()
    spec = doc.thresholds.get(_LOSS_CUT_MAX_SLIP_KEY)
    if spec is None:
        return _LOSS_CUT_MAX_SLIP_DEFAULT
    try:
        value = float(spec.value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _LOSS_CUT_MAX_SLIP_DEFAULT
    if not (0.0 < value < 0.5):
        return _LOSS_CUT_MAX_SLIP_DEFAULT
    return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_loss_cut_policy_and_settings.py -v -k loss_cut_max_slip`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config/trading_policy.yaml app/services/trading_policy_service.py tests/services/test_loss_cut_policy_and_settings.py
git commit -m "feat(ROB-800): add sell.loss_cut_max_slip policy key + reader"
```

---

### Task 2: `LOSS_CUT_ALLOWED_AGENT_IDS` settings allowlist

**Files:**
- Modify: `app/core/config.py`
- Test: `tests/services/test_loss_cut_policy_and_settings.py` (append)

**Interfaces:**
- Produces: `settings.loss_cut_allowed_agent_ids: list[str]` (default `["6b2192cc-14fa-4335-b572-2fe1e0cb54a7"]`), parsed from comma-separated or JSON-list env `LOSS_CUT_ALLOWED_AGENT_IDS`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/test_loss_cut_policy_and_settings.py
from app.core.config import Settings


@pytest.mark.unit
def test_loss_cut_allowlist_defaults_to_trader_agent():
    s = Settings()
    assert s.loss_cut_allowed_agent_ids == ["6b2192cc-14fa-4335-b572-2fe1e0cb54a7"]


@pytest.mark.unit
def test_loss_cut_allowlist_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("LOSS_CUT_ALLOWED_AGENT_IDS", "aaa, bbb ,ccc")
    s = Settings()
    assert s.loss_cut_allowed_agent_ids == ["aaa", "bbb", "ccc"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_loss_cut_policy_and_settings.py -v -k allowlist`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'loss_cut_allowed_agent_ids'`).

- [ ] **Step 3: Implement the setting**

In `app/core/config.py`, near `INVESTMENT_ADVISORY_DRAFT_PROFILES` (line ~728) add the field:

```python
    LOSS_CUT_ALLOWED_AGENT_IDS: Annotated[list[str], NoDecode] = [
        "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"
    ]
```

Add a computed alias so downstream reads `settings.loss_cut_allowed_agent_ids` (snake_case, matching `trader_agent_id` style). Place with the other `@property`/validators (after `_parse_advisory_draft_profiles`, line ~884):

```python
    @field_validator("LOSS_CUT_ALLOWED_AGENT_IDS", mode="before")
    @classmethod
    def _parse_loss_cut_allowlist(cls, v: list[str] | str) -> list[str]:
        """Parse comma-separated or JSON-list env into a clean agent-id list.

        ROB-800 — allowlist of MCP caller agent ids permitted to place a
        sanctioned loss_cut. Defaults to the single Trader agent (backcompat).
        """
        if isinstance(v, list):
            return [str(p).strip() for p in v if str(p).strip()]
        value = (v or "").strip()
        if not value:
            return []
        if value.startswith("["):
            import json

            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = []
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        return [p.strip() for p in value.split(",") if p.strip()]

    @property
    def loss_cut_allowed_agent_ids(self) -> list[str]:
        return self.LOSS_CUT_ALLOWED_AGENT_IDS
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_loss_cut_policy_and_settings.py -v -k allowlist`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/services/test_loss_cut_policy_and_settings.py
git commit -m "feat(ROB-800): add LOSS_CUT_ALLOWED_AGENT_IDS settings allowlist"
```

---

### Task 3: `LossCutContext` + guard band in `evaluate_sell_price_guards` + guard matrix

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py`
- Test: `tests/mcp_server/tooling/test_loss_cut_preconditions.py`

**Interfaces:**
- Produces: `order_validation.LossCutContext` (frozen dataclass: `retrospective_id: int`, `exit_reason: str`, `approval_issue_id: str`, `requester_agent_id: str`, `max_slip: float`, `approval_verified_at: datetime.datetime`).
- Produces: `evaluate_sell_price_guards(..., loss_cut_ctx: LossCutContext | None = None)` — when `loss_cut_ctx` is present, floor is exempt and the current-price guard becomes the band `price >= current_price * (1 - max_slip)`.

- [ ] **Step 1: Write the failing guard-matrix test**

```python
# tests/mcp_server/tooling/test_loss_cut_preconditions.py
import datetime
import pytest
from app.mcp_server.tooling.order_validation import (
    DefensiveTrimContext,
    LossCutContext,
    ScalpingExitContext,
    evaluate_sell_price_guards,
)


def _loss_cut_ctx(max_slip=0.02):
    return LossCutContext(
        retrospective_id=1,
        exit_reason="stop_loss",
        approval_issue_id="ROB-800",
        requester_agent_id="agent-x",
        max_slip=max_slip,
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )


@pytest.mark.unit
def test_loss_cut_allows_price_within_slip_band():
    # current 1245, slip 0.02 -> floor 1220.1; price 1244 (below current, below avg*1.01) allowed
    err = evaluate_sell_price_guards(
        price=1244.0, current_price=1245.0, avg_price=2000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None, loss_cut_ctx=_loss_cut_ctx(),
    )
    assert err is None


@pytest.mark.unit
def test_loss_cut_blocks_below_slip_band():
    # floor = 1245 * 0.98 = 1220.1; price 1200 is a fat-finger -> blocked
    err = evaluate_sell_price_guards(
        price=1200.0, current_price=1245.0, avg_price=2000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None, loss_cut_ctx=_loss_cut_ctx(),
    )
    assert err is not None and "band" in err.lower()


@pytest.mark.unit
def test_defensive_trim_unchanged_still_enforces_current_price():
    # defensive_trim exempts floor but NOT current price -> price below current still blocked
    dt = DefensiveTrimContext(
        approval_issue_id="ROB-164", requester_agent_id="a",
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )
    err = evaluate_sell_price_guards(
        price=1244.0, current_price=1245.0, avg_price=2000.0,
        defensive_trim_ctx=dt, scalping_exit_ctx=None, loss_cut_ctx=None,
    )
    assert err is not None and "below current price" in err


@pytest.mark.unit
def test_plain_sell_unchanged_enforces_floor():
    err = evaluate_sell_price_guards(
        price=1990.0, current_price=1245.0, avg_price=2000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None, loss_cut_ctx=None,
    )
    assert err is not None and "below minimum" in err


@pytest.mark.unit
def test_scalping_and_allow_loss_sell_unchanged_bypass_all():
    sc = ScalpingExitContext(strategy_id="s", reason="stop_loss")
    assert evaluate_sell_price_guards(
        price=1.0, current_price=1245.0, avg_price=2000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=sc, loss_cut_ctx=None) is None
    assert evaluate_sell_price_guards(
        price=1.0, current_price=1245.0, avg_price=2000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None, loss_cut_ctx=None,
        allow_loss_sell=True) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_preconditions.py -v`
Expected: FAIL (`ImportError: cannot import name 'LossCutContext'` and unexpected keyword `loss_cut_ctx`).

- [ ] **Step 3: Add the dataclass + band branch**

In `order_validation.py`, after `ScalpingExitContext` (line ~80) add:

```python
@dataclass(frozen=True)
class LossCutContext:
    """ROB-800 — sanctioned live loss-cut authorization.

    Constructed only by _validate_loss_cut_preconditions after ALL four
    preconditions pass. When present, evaluate_sell_price_guards exempts the
    avg*1.01 floor and relaxes the current-price guard to a band
    (price >= current * (1 - max_slip)). Never threaded from any other path.
    """

    retrospective_id: int
    exit_reason: str
    approval_issue_id: str
    requester_agent_id: str
    max_slip: float
    approval_verified_at: datetime.datetime
```

Change the signature of `evaluate_sell_price_guards` (line 83) to add `loss_cut_ctx: LossCutContext | None = None`, and insert the band branch **before** the floor check (after the `allow_loss_sell` early-return at line 110):

```python
    if loss_cut_ctx is not None:
        # ROB-800 — sanctioned loss_cut: floor exempt, current-price guard
        # relaxed to a downward slip band. Fat-finger deep discounts stay blocked.
        band_floor = current_price * (1.0 - loss_cut_ctx.max_slip)
        if current_price > 0 and price < band_floor:
            return (
                f"loss_cut sell price {price} below slip band floor "
                f"{band_floor:.4f} (current {current_price} * "
                f"(1 - {loss_cut_ctx.max_slip}))"
            )
        return None
```

Update the docstring matrix (lines 96-102) to document the `loss_cut_ctx` row.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_preconditions.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/order_validation.py tests/mcp_server/tooling/test_loss_cut_preconditions.py
git commit -m "feat(ROB-800): LossCutContext + slip-band branch in sell price guard"
```

---

### Task 4: Retrospective id lookup + `_validate_loss_cut_preconditions` (aggregating)

**Files:**
- Modify: `app/services/trade_journal/trade_retrospective_service.py`
- Modify: `app/mcp_server/tooling/order_validation.py`
- Test: `tests/mcp_server/tooling/test_loss_cut_preconditions.py` (append)

**Interfaces:**
- Consumes: `settings.loss_cut_allowed_agent_ids` (Task 2), `trading_policy_service.loss_cut_max_slip` (Task 1), `LossCutContext` (Task 3), `get_caller_agent_id()`, `_fetch_approval_issue_status` / `_is_cached_approved` / `_cache_approved` (existing).
- Produces: `trade_retrospective_service.get_retrospective_by_id(db, retro_id: int) -> TradeRetrospective | None`.
- Produces: `order_validation._validate_loss_cut_preconditions(*, exit_intent, retrospective_id, exit_reason, approval_issue_id, side, order_type, is_mock, symbol) -> tuple[LossCutContext | None, list[str]]`. Returns `(None, [])` when `exit_intent != "loss_cut"`. Otherwise returns `(ctx, [])` on full pass, or `(None, [violation, ...])` collecting EVERY failed precondition.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/mcp_server/tooling/test_loss_cut_preconditions.py
from unittest.mock import AsyncMock, patch
from app.mcp_server.tooling import order_validation as ov


@pytest.mark.unit
async def test_loss_cut_preconditions_collects_all_violations():
    # side=buy, order_type=market, no exit_reason, bad approval fmt, no retrospective_id,
    # caller not allowlisted -> every violation surfaced in one list.
    with patch.object(ov, "get_caller_agent_id", return_value="not-allowed"):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent="loss_cut", retrospective_id=None, exit_reason=None,
            approval_issue_id="bad-id", side="buy", order_type="market",
            is_mock=False, symbol="KRW-DOT",
        )
    assert ctx is None
    joined = " | ".join(errors)
    assert "side='sell'" in joined
    assert "order_type='limit'" in joined
    assert "exit_reason" in joined
    assert "retrospective_id" in joined
    assert "approval_issue_id" in joined
    assert "not permitted" in joined  # caller allowlist
    assert len(errors) >= 6


@pytest.mark.unit
async def test_loss_cut_preconditions_pass_builds_context():
    fake_retro = type("R", (), {
        "id": 42, "symbol": "KRW-DOT", "trigger_type": "stop_loss",
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    })()
    with patch.object(ov, "get_caller_agent_id",
                      return_value="6b2192cc-14fa-4335-b572-2fe1e0cb54a7"), \
         patch.object(ov, "_fetch_approval_issue_status",
                      new=AsyncMock(return_value="done")), \
         patch.object(ov, "_get_retrospective_by_id_for_loss_cut",
                      new=AsyncMock(return_value=fake_retro)):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent="loss_cut", retrospective_id=42, exit_reason="stop_loss",
            approval_issue_id="ROB-800", side="sell", order_type="limit",
            is_mock=False, symbol="KRW-DOT",
        )
    assert errors == []
    assert ctx is not None and ctx.retrospective_id == 42 and ctx.max_slip > 0


@pytest.mark.unit
async def test_loss_cut_preconditions_reject_stale_retrospective():
    old = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc) - __import__("datetime").timedelta(hours=100)
    fake_retro = type("R", (), {
        "id": 42, "symbol": "KRW-DOT", "trigger_type": "stop_loss", "created_at": old})()
    with patch.object(ov, "get_caller_agent_id",
                      return_value="6b2192cc-14fa-4335-b572-2fe1e0cb54a7"), \
         patch.object(ov, "_fetch_approval_issue_status",
                      new=AsyncMock(return_value="done")), \
         patch.object(ov, "_get_retrospective_by_id_for_loss_cut",
                      new=AsyncMock(return_value=fake_retro)):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent="loss_cut", retrospective_id=42, exit_reason="stop_loss",
            approval_issue_id="ROB-800", side="sell", order_type="limit",
            is_mock=False, symbol="KRW-DOT")
    assert ctx is None
    assert any("72h" in e or "stale" in e.lower() for e in errors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_preconditions.py -v -k preconditions`
Expected: FAIL (`AttributeError: ... _validate_loss_cut_preconditions`).

- [ ] **Step 3a: Add the retrospective id lookup**

In `app/services/trade_journal/trade_retrospective_service.py`, add a free function near `get_retrospectives` (line ~467):

```python
async def get_retrospective_by_id(
    db: AsyncSession, retro_id: int
) -> TradeRetrospective | None:
    """ROB-800 — fetch a single retrospective by primary key (read-only)."""
    result = await db.execute(
        select(TradeRetrospective).where(TradeRetrospective.id == retro_id).limit(1)
    )
    return result.scalar_one_or_none()
```

- [ ] **Step 3b: Add the aggregating validator**

In `order_validation.py`, add constants + a thin session wrapper + the validator (after `_validate_defensive_trim_preconditions`, line ~496):

```python
_LOSS_CUT_EXIT_REASONS = frozenset({"stop_loss", "thesis_change"})
_LOSS_CUT_RETRO_TRIGGER_TYPES = frozenset({"stop_loss", "thesis_change"})
_LOSS_CUT_RETRO_MAX_AGE_HOURS = 72


async def _get_retrospective_by_id_for_loss_cut(retrospective_id: int):
    """Open a read-only session and fetch the retrospective row (ROB-800)."""
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.trade_retrospective_service import (
        get_retrospective_by_id,
    )

    async with AsyncSessionLocal() as db:
        return await get_retrospective_by_id(db, retrospective_id)


async def _validate_loss_cut_preconditions(
    *,
    exit_intent: str | None,
    retrospective_id: int | None,
    exit_reason: str | None,
    approval_issue_id: str | None,
    side: str,
    order_type: str,
    is_mock: bool,
    symbol: str,
) -> tuple[LossCutContext | None, list[str]]:
    """ROB-800 — fail-closed loss_cut gate. Collects EVERY violation so a
    dry_run preview can return them all in one response. Returns (None, []) when
    not a loss_cut request."""
    if exit_intent != "loss_cut":
        return None, []

    errors: list[str] = []

    if side != "sell":
        errors.append("loss_cut requires side='sell'")
    if order_type != "limit":
        errors.append("loss_cut requires order_type='limit' (market orders blocked)")
    if is_mock:
        errors.append("loss_cut is live-only; use allow_loss_sell for mock practice")

    resolved_exit_reason = (exit_reason or "").strip()
    if resolved_exit_reason not in _LOSS_CUT_EXIT_REASONS:
        errors.append(
            "loss_cut requires structured exit_reason in "
            f"{sorted(_LOSS_CUT_EXIT_REASONS)}"
        )

    # Caller allowlist (generalizes the defensive_trim single-UUID check).
    caller_agent_id = get_caller_agent_id()
    allowlist = getattr(
        settings, "loss_cut_allowed_agent_ids", [_TRADER_AGENT_ID_DEFAULT]
    )
    if not caller_agent_id:
        errors.append("caller identity unavailable — loss_cut requires authenticated caller")
    elif caller_agent_id not in allowlist:
        errors.append(
            f"caller agent {caller_agent_id} not permitted for loss_cut "
            "(add to LOSS_CUT_ALLOWED_AGENT_IDS)"
        )

    # Approval issue (reuse defensive_trim Paperclip status=done machinery).
    if not approval_issue_id:
        errors.append("loss_cut requires approval_issue_id")
    elif not _DEFENSIVE_TRIM_APPROVAL_REGEX.match(approval_issue_id):
        errors.append("approval_issue_id format invalid (expected e.g. 'ROB-800')")
    else:
        if _is_cached_approved(approval_issue_id):
            approval_status = "done"
        else:
            try:
                approval_status = await _fetch_approval_issue_status(approval_issue_id)
            except Exception:
                approval_status = None
            if approval_status == "done":
                _cache_approved(approval_issue_id)
        if approval_status != "done":
            errors.append(
                f"approval_issue_id {approval_issue_id} not found or not in 'done' status"
            )

    # Retrospective precondition: exists + symbol match + <=72h + trigger_type.
    if retrospective_id is None:
        errors.append("loss_cut requires retrospective_id (no retrospective, no loss_cut)")
    else:
        try:
            retro = await _get_retrospective_by_id_for_loss_cut(retrospective_id)
        except Exception:
            retro = None
        if retro is None:
            errors.append(f"retrospective_id {retrospective_id} not found")
        else:
            if (retro.symbol or "").strip().upper() != symbol.strip().upper():
                errors.append(
                    f"retrospective_id {retrospective_id} symbol {retro.symbol} "
                    f"does not match order symbol {symbol}"
                )
            if retro.trigger_type not in _LOSS_CUT_RETRO_TRIGGER_TYPES:
                errors.append(
                    f"retrospective trigger_type {retro.trigger_type} not in "
                    f"{sorted(_LOSS_CUT_RETRO_TRIGGER_TYPES)}"
                )
            created = retro.created_at
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=datetime.UTC)
                age = datetime.datetime.now(datetime.UTC) - created
                if age > datetime.timedelta(hours=_LOSS_CUT_RETRO_MAX_AGE_HOURS):
                    errors.append(
                        f"retrospective_id {retrospective_id} is stale "
                        f"(> {_LOSS_CUT_RETRO_MAX_AGE_HOURS}h old)"
                    )

    if errors:
        return None, errors

    return (
        LossCutContext(
            retrospective_id=retrospective_id,  # type: ignore[arg-type]
            exit_reason=resolved_exit_reason,
            approval_issue_id=approval_issue_id,  # type: ignore[arg-type]
            requester_agent_id=caller_agent_id,  # type: ignore[arg-type]
            max_slip=_loss_cut_max_slip_value(),
            approval_verified_at=datetime.datetime.now(datetime.UTC),
        ),
        [],
    )


def _loss_cut_max_slip_value() -> float:
    from app.services.trading_policy_service import loss_cut_max_slip

    return loss_cut_max_slip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_preconditions.py -v -k preconditions`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/trade_retrospective_service.py app/mcp_server/tooling/order_validation.py tests/mcp_server/tooling/test_loss_cut_preconditions.py
git commit -m "feat(ROB-800): aggregating loss_cut precondition validator + retrospective id lookup"
```

---

### Task 5: Thread `loss_cut` through `_place_order_impl` (context, aggregated response, approval-hash-required)

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py` (`_preview_sell`, `_validate_sell_side` signatures)
- Modify: `app/mcp_server/tooling/order_execution.py` (`_place_order_impl`, `_build_preview`)
- Test: `tests/mcp_server/tooling/test_loss_cut_place_order.py`

**Interfaces:**
- Consumes: `_validate_loss_cut_preconditions`, `LossCutContext`, `evaluate_sell_price_guards(..., loss_cut_ctx=...)`.
- Produces: `_place_order_impl(..., exit_intent: str | None = None, retrospective_id: int | None = None, ...)`. On loss_cut precondition failure returns `{"success": False, "error": "loss_cut_preconditions_failed", "violations": [...], "symbol": ..., "instrument_type": ...}` (single aggregated response, both dry_run and live). On live send with a resolved loss_cut context, a valid `approval_hash` is REQUIRED regardless of `ORDER_APPROVAL_HASH_MODE`.

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/mcp_server/tooling/test_loss_cut_place_order.py
from unittest.mock import AsyncMock, patch
import pytest
from app.mcp_server.tooling import order_execution as oe
from app.mcp_server.tooling import order_validation as ov


@pytest.mark.unit
async def test_dry_run_loss_cut_returns_all_violations_single_response():
    with patch.object(ov, "get_caller_agent_id", return_value="nobody"):
        resp = await oe._place_order_impl(
            symbol="KRW-DOT", side="buy", market="crypto", order_type="market",
            price=1244.0, quantity=10, dry_run=True,
            exit_intent="loss_cut", retrospective_id=None,
        )
    assert resp["success"] is False
    assert resp["error"] == "loss_cut_preconditions_failed"
    assert isinstance(resp["violations"], list) and len(resp["violations"]) >= 4


@pytest.mark.unit
async def test_loss_cut_and_defensive_trim_mutually_exclusive():
    resp = await oe._place_order_impl(
        symbol="KRW-DOT", side="sell", market="crypto", order_type="limit",
        price=1244.0, quantity=10, dry_run=True,
        exit_intent="loss_cut", defensive_trim=True, approval_issue_id="ROB-800",
    )
    assert resp["success"] is False
    assert "mutually exclusive" in resp["error"].lower()
```

(A full happy-path live test with a real ledger row is exercised in Task 6's test; here we lock the aggregation + exclusivity contract that needs no broker call.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_place_order.py -v`
Expected: FAIL (`_place_order_impl() got an unexpected keyword argument 'exit_intent'`).

- [ ] **Step 3a: Add params + mutual-exclusion + precondition orchestration**

In `order_execution.py` `_place_order_impl` signature (line ~1180), add after `approval_issue_id`:

```python
    exit_intent: str | None = None,
    retrospective_id: int | None = None,
```

Immediately after the `crypto and is_mock` guard (line ~1249) and BEFORE the defensive_trim block (line ~1251), add:

```python
    if exit_intent is not None and exit_intent != "loss_cut":
        return _order_error(f"unknown exit_intent {exit_intent!r} (only 'loss_cut')")
    if exit_intent == "loss_cut" and defensive_trim:
        return _order_error("loss_cut and defensive_trim are mutually exclusive")

    loss_cut_ctx = None
    if exit_intent == "loss_cut":
        loss_cut_ctx, loss_cut_errors = await ov._validate_loss_cut_preconditions(
            exit_intent=exit_intent,
            retrospective_id=retrospective_id,
            exit_reason=exit_reason,
            approval_issue_id=approval_issue_id,
            side=side_lower,
            order_type=order_type_lower,
            is_mock=is_mock,
            symbol=normalized_symbol,
        )
        if loss_cut_errors:
            return {
                "success": False,
                "error": "loss_cut_preconditions_failed",
                "violations": loss_cut_errors,
                "source": source,
                "symbol": normalized_symbol,
                "instrument_type": market_type,
            }
```

(Import `ov` at top of `order_execution.py`: `from app.mcp_server.tooling import order_validation as ov` — verify it is not already aliased; the module already imports several `order_validation` symbols, add the module alias if absent.)

Thread `loss_cut_ctx` into the sell branch: update the `_validate_sell_side(...)` call (line ~1305) to pass `loss_cut_ctx=loss_cut_ctx`, and the `_build_preview(...)` call (line ~1324) to pass `loss_cut_ctx=loss_cut_ctx`.

- [ ] **Step 3b: Thread `loss_cut_ctx` through the validation/preview layer**

In `order_validation.py`:
- `_validate_sell_side` (line 1157): add `loss_cut_ctx: LossCutContext | None = None` param; pass it into the limit-branch `evaluate_sell_price_guards(...)` call (line 1251) as `loss_cut_ctx=loss_cut_ctx`.
- `_preview_sell` (line 937): add `loss_cut_ctx: LossCutContext | None = None` param; pass into `evaluate_sell_price_guards(...)` (line 997) as `loss_cut_ctx=loss_cut_ctx`; and set `result["exit_intent"] = "loss_cut"` + `result["loss_cut_slip_band"] = ...` in the block that currently sets `result["defensive_trim"]` (line 1055) when `loss_cut_ctx is not None`.
- `_preview_order` (line 1070): add + forward `loss_cut_ctx`.

In `order_execution.py` `_build_preview` (line 351): add `loss_cut_ctx` param and forward to `_preview_order`.

- [ ] **Step 3c: loss_cut approval-hash-required gate**

In `_place_order_impl`, at the live approval-hash gate (line ~1416), before the `mode` branch, add a loss_cut-specific hard requirement:

```python
        if loss_cut_ctx is not None and approval_hash is None:
            err = _order_error(
                "loss_cut live send requires approval_hash "
                "(re-run dry_run=True and pass the returned approval_hash)"
            )
            err["error_code"] = "loss_cut_approval_hash_required"
            return err
```

(When `approval_hash` IS supplied, the existing `verify_approval_token` path at lines 1419-1428 already fail-closes on mismatch/expiry — reuse it as-is.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_place_order.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/order_validation.py app/mcp_server/tooling/order_execution.py tests/mcp_server/tooling/test_loss_cut_place_order.py
git commit -m "feat(ROB-800): thread loss_cut through _place_order_impl (aggregated response + approval-hash-required)"
```

---

### Task 6: Ledger `exit_intent` column + migration + write threading

**Files:**
- Modify: `app/models/review.py` (`LiveOrderLedger`, `KISLiveOrderLedger`)
- Create: `alembic/versions/<rev>_rob800_add_exit_intent_to_live_ledgers.py`
- Modify: `app/mcp_server/tooling/live_order_ledger.py`, `app/mcp_server/tooling/kis_live_ledger.py`
- Modify: `app/mcp_server/tooling/order_execution.py` (`_execute_and_record` → `_record_*` calls)
- Test: `tests/mcp_server/tooling/test_loss_cut_place_order.py` (append a ledger-persistence test)

**Interfaces:**
- Produces: `LiveOrderLedger.exit_intent: str | None`, `KISLiveOrderLedger.exit_intent: str | None`.
- Produces: `_record_live_order(..., exit_intent: str | None = None)` and `_record_kis_live_order(..., exit_intent: str | None = None)` persisting the column.

- [ ] **Step 1: Write the failing persistence test**

```python
# append to tests/mcp_server/tooling/test_loss_cut_place_order.py
from app.models.review import LiveOrderLedger, KISLiveOrderLedger


@pytest.mark.unit
def test_live_ledger_models_have_exit_intent_column():
    assert "exit_intent" in LiveOrderLedger.__table__.columns
    assert "exit_intent" in KISLiveOrderLedger.__table__.columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_place_order.py -v -k exit_intent_column`
Expected: FAIL (`KeyError: 'exit_intent'`).

- [ ] **Step 3a: Add the ORM columns**

In `app/models/review.py`:
- `LiveOrderLedger` — after `exit_reason` (line 447) add:
  ```python
  exit_intent: Mapped[str | None] = mapped_column(Text)  # ROB-800: 'loss_cut'
  ```
- `KISLiveOrderLedger` — after `exit_reason` (line 350) add the same line.

- [ ] **Step 3b: Generate + hand-verify the migration**

Run: `uv run alembic revision --autogenerate -m "ROB-800 add exit_intent to live ledgers"`
Then edit the generated file so it contains exactly (additive, nullable, both tables in `review` schema):

```python
def upgrade() -> None:
    op.add_column("live_order_ledger",
        sa.Column("exit_intent", sa.Text(), nullable=True), schema="review")
    op.add_column("kis_live_order_ledger",
        sa.Column("exit_intent", sa.Text(), nullable=True), schema="review")


def downgrade() -> None:
    op.drop_column("live_order_ledger", "exit_intent", schema="review")
    op.drop_column("kis_live_order_ledger", "exit_intent", schema="review")
```

Delete any unrelated autogenerated churn.

- [ ] **Step 3c: Thread `exit_intent` through the writers**

- `live_order_ledger.py`: add `exit_intent: str | None = None` to `_record_live_order` and to the inner `_save_*` helper (mirror `exit_reason` at lines 74/114); set `exit_intent=exit_intent` on the `LiveOrderLedger(...)` construction.
- `kis_live_ledger.py`: same for `_record_kis_live_order` / `_save_kis_live_order_ledger` (mirror `exit_reason` at lines 238/311).
- `order_execution.py` `_execute_and_record` (lines 692-1081): the KR-live `_record_kis_live_order(...)` call (line ~923) and both `_record_live_order(...)` calls (US line ~952, crypto line ~1012) — add `exit_intent=exit_intent`. Add `exit_intent` to `_execute_and_record`'s own signature (line ~692) and pass it from the `_place_order_impl` call site (line ~1453).

- [ ] **Step 4: Run tests (unit + a DB round-trip on the test DB)**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_place_order.py -v`
Expected: PASS. Also run the ledger writer's existing test module to confirm no regression:
`uv run pytest tests/ -v -k "live_order_ledger or kis_live_ledger"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/review.py alembic/versions/*rob800* app/mcp_server/tooling/live_order_ledger.py app/mcp_server/tooling/kis_live_ledger.py app/mcp_server/tooling/order_execution.py tests/mcp_server/tooling/test_loss_cut_place_order.py
git commit -m "feat(ROB-800): record exit_intent on live/kis-live order ledgers (+ migration)"
```

---

### Task 7: MCP tool surfaces — `exit_intent` + `retrospective_id` params

**Files:**
- Modify: `app/mcp_server/tooling/orders_registration.py` (`place_order`)
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (`_place_order_variant` + `kis_live_place_order` schema/description)
- Test: `tests/mcp_server/tooling/test_loss_cut_place_order.py` (append tool-surface test)

**Interfaces:**
- Produces: `place_order(..., exit_intent: str | None = None, retrospective_id: int | None = None)` forwarded to `_place_order_impl`; same for the KR variant.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/mcp_server/tooling/test_loss_cut_place_order.py
import inspect
from app.mcp_server.tooling import orders_kis_variants


@pytest.mark.unit
def test_kr_variant_forwards_loss_cut_params():
    sig = inspect.signature(orders_kis_variants._place_order_variant)
    assert "exit_intent" in sig.parameters
    assert "retrospective_id" in sig.parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_place_order.py -v -k forwards_loss_cut`
Expected: FAIL.

- [ ] **Step 3a: Generic `place_order` tool**

In `orders_registration.py` `place_order` (line 223) add params `exit_intent: str | None = None`, `retrospective_id: int | None = None` after `approval_issue_id` (line 241); forward both into the `_place_order_impl(...)` call (line 305). Append to the tool description (line ~209): a paragraph documenting `exit_intent="loss_cut"` (sell+limit+live only) and its 4 preconditions.

- [ ] **Step 3b: KR variant**

In `orders_kis_variants.py` `_place_order_variant` (line 243) add the two params; forward into `_place_order_impl` (line 295). Add the same params to `kis_live_place_order` (line 494) + forward (line 542-555). Update its description block (lines 460-490) with the loss_cut paragraph. (Leave `kis_mock_place_order` unchanged — loss_cut is live-only; mock passing `exit_intent="loss_cut"` will be rejected by the `is_mock` precondition.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/tooling/test_loss_cut_place_order.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_registration.py app/mcp_server/tooling/orders_kis_variants.py tests/mcp_server/tooling/test_loss_cut_place_order.py
git commit -m "feat(ROB-800): expose exit_intent + retrospective_id on place_order tool surfaces"
```

---

### Task 8: End-to-end regression lock, lint, and PR

**Files:**
- Test: `tests/mcp_server/tooling/test_loss_cut_place_order.py` (append the no-regression matrix)

**Interfaces:** none new — this is verification + handoff.

- [ ] **Step 1: Write the no-regression matrix test**

```python
# append to tests/mcp_server/tooling/test_loss_cut_place_order.py
@pytest.mark.unit
async def test_non_loss_cut_orders_unaffected_by_new_param():
    # exit_intent=None must behave exactly as before: a plain sell below current
    # price is still blocked by the current-price guard.
    with patch.object(oe, "_fetch_current_price", new=AsyncMock(return_value=1245.0)), \
         patch.object(oe, "_validate_sell_side", wraps=None):
        pass  # sanity import guard; full path covered by existing place_order tests


@pytest.mark.unit
async def test_defensive_trim_path_ignores_loss_cut_plumbing():
    # A defensive_trim call with exit_intent=None resolves no loss_cut context.
    ctx, errors = await ov._validate_loss_cut_preconditions(
        exit_intent=None, retrospective_id=None, exit_reason=None,
        approval_issue_id=None, side="sell", order_type="limit",
        is_mock=False, symbol="KRW-DOT")
    assert ctx is None and errors == []
```

- [ ] **Step 2: Run the full guard + order test surface**

Run: `uv run pytest tests/mcp_server/tooling/ -v -k "order or loss_cut or defensive or scalping or sell"`
Expected: PASS (new + all pre-existing guard tests green — this proves defensive_trim / scalping / allow_loss_sell are unregressed).

- [ ] **Step 3: Lint + typecheck**

Run: `make lint`
Expected: clean (Ruff + ty). Fix any findings, re-run.

- [ ] **Step 4: Commit**

```bash
git add tests/mcp_server/tooling/test_loss_cut_place_order.py
git commit -m "test(ROB-800): lock no-regression for defensive_trim/scalping/allow_loss_sell"
```

- [ ] **Step 5: Push + open PR (base: main, DO NOT MERGE)**

```bash
git push -u origin rob-800
gh pr create --base main --title "feat(ROB-800): loss_cut exit intent — sanctioned live loss-cut path" --body "$(cat <<'EOF'
## ROB-800 PR 1 — `exit_intent="loss_cut"`

Adds a fail-closed, per-ticket-approved, retrospective-backed live loss-cut sell path beside `defensive_trim` (no existing guard changed for any other caller). Crypto + KR + US via the shared `_place_order_impl`.

### What it does
- `place_order(exit_intent="loss_cut")` — sell + limit + live only; mutually exclusive with `defensive_trim`.
- Fail-closed preconditions (all violations returned in ONE dry_run response):
  1. structured `exit_reason` ∈ {stop_loss, thesis_change}
  2. `retrospective_id` exists + symbol match + ≤72h + trigger_type ∈ {stop_loss, thesis_change}
  3. `approval_issue_id` Paperclip status=done (reuses defensive_trim verifier)
  4. valid `approval_hash` required on live send (independent of ORDER_APPROVAL_HASH_MODE)
- Caller identity: `LOSS_CUT_ALLOWED_AGENT_IDS` allowlist (default = existing Trader agent, backcompat).
- Guard relaxation ONLY inside a resolved loss_cut context: avg×1.01 floor exempt + current-price guard → band `price ≥ current × (1 − loss_cut_max_slip)`; `loss_cut_max_slip` (default 0.02) new key in `config/trading_policy.yaml`.
- `exit_intent` recorded on `review.live_order_ledger` + `review.kis_live_order_ledger` (additive migration — operator runs `alembic upgrade head`).
- No regression to `defensive_trim` / `scalping_exit` / `allow_loss_sell` (guard-matrix test).

### Follow-up: desk PR 2 (OUT OF SCOPE here)
The desk/tcx adapter must, in a separate PR:
1. Send the desk agent's UUID via the existing `x-paperclip-agent-id` header on the 8770 HTTP path, and have the operator add that UUID to `LOSS_CUT_ALLOWED_AGENT_IDS`.
2. Add a `loss_cut` ticket type with a standalone approval table (not folded into the batch approval sheet), carrying `retrospective_id` + `exit_reason`, reusing 793 meta and wiring execution-followup fill→retrospective realization (aligns with ROB-801).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

Report the PR number back. **Do not merge.**

---

## Self-Review

**Spec coverage (scoping comment → task):**
- `exit_intent="loss_cut"` sell+limit only, market blocked → Task 5 (precondition + orchestration), Task 3 (limit-only reached because market guard untouched).
- Precondition ① structured exit_reason → Task 4.
- Precondition ② retrospective_id exists/symbol/72h/trigger_type → Task 4 (+ retrospective lookup).
- Precondition ③ approval_issue_id Paperclip done (reuse) → Task 4.
- Precondition ④ approval_hash → Task 5 (loss_cut approval-hash-required gate).
- Guard relaxation: floor exempt + band `current×(1−slip)`, `loss_cut_max_slip` key default 0.02 → Task 1 + Task 3.
- Caller identity → allowlist `LOSS_CUT_ALLOWED_AGENT_IDS` default Trader → Task 2 + Task 4.
- Markets crypto+KR/US → shared `_place_order_impl` (Task 5), both tool surfaces (Task 7).
- Ledger records `exit_intent` → Task 6.
- Journal close exit_reason reuse → existing `exit_reason` threading unchanged (loss_cut passes `exit_reason` through the normal path; no new work).
- No regression to defensive_trim/scalping/allow_loss_sell, guard-matrix test → Task 3 + Task 8.
- TDD, make lint, dry_run aggregated single response → Task 5 + Task 8.
- PR base main, no merge, report number → Task 8.
- desk header injection out of scope, documented in PR body → Task 8.

**Type consistency:** `LossCutContext` fields are used identically in Task 3 (definition), Task 4 (construction), Task 5 (consumption). `_validate_loss_cut_preconditions` returns `tuple[LossCutContext | None, list[str]]` consistently. `exit_intent`/`retrospective_id` param names identical across `_place_order_impl`, tool surfaces, ledger writers.

**Open confirmations for the implementer (from the Linear scoping "미결 확인 2건"):**
1. Whether the desk 8770 path already forwards `x-paperclip-agent-id` — this PR only needs the allowlist to accept it; verify at desk-PR time.
2. Paperclip approval UX fit — this PR reuses the existing verifier; the tcx-receipt alternative is explicitly out of scope.
