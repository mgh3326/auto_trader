# ROB-576 — Toss Fill Discord Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Notify operators on Discord/Telegram when Toss live KR/US fills are booked by reconcile, and optionally add a paused TaskIQ auto-reconcile task so fills are discovered without manual MCP calls.

**Architecture:** PR1 adds a Toss-specific fill normalizer and hooks it into `toss_reconcile_orders_impl` only after a new fill delta is durably booked. MCP startup configures the existing `TradeNotifier` only when `TOSS_FILL_NOTIFY_ENABLED=true`, preserving default-off Toss safety. PR2 mirrors the KIS live paused reconcile task with Toss-specific fail-closed gates.

**Tech Stack:** Python 3.13, FastMCP lifespan, TaskIQ, SQLAlchemy async, pytest/pytest-asyncio, existing `TradeNotifier` Discord/Telegram transports.

**Migration:** 0.

**Execution status (2026-06-15):** Implemented in branch `rob-576` through
PR1 + PR2 scope. The final PR2 implementation reuses the ROB-574 uppercase
auto-reconcile gates (`TOSS_LIVE_AUTO_RECONCILE_*`) already present in the
branch lineage. Focused tests, targeted lint/type checks, and `make test-unit`
passed. Linear label/comment application is still pending because the Linear
session was expired during follow-up verification.

---

## Decision Gate Before Implementation

Code inspection found no automatic Toss reconcile path today. `toss_reconcile_orders_impl` is referenced by the MCP tool and the smoke script, but not by a TaskIQ task or scheduler.

Ask the operator before starting Task 5:

> ROB-576 execution scope: should this issue stop at PR1 (manual reconcile sends fill notifications), or should it also include PR2 (paused Toss auto-reconcile task)? Recommendation: implement PR1 first, then continue PR2 in the same branch only if you want ROB-576 to deliver automatic polling rather than manual-only notifications.

If the operator does not answer, implement Tasks 1-4 only and leave Tasks 5-7 unstarted.

## File Structure

- Modify `app/core/config.py`: add the default-off Toss fill notification gate.
- Create `app/monitoring/trade_notifier/runtime.py`: shared notifier setup/shutdown from `settings`.
- Modify `app/main.py`: use the shared notifier setup/shutdown helper.
- Modify `app/core/taskiq_broker.py`: use the same helper for worker startup.
- Modify `app/mcp_server/lifecycle.py`: configure/shutdown notifier in MCP lifespan only when Toss fill notifications are enabled.
- Modify `app/services/fill_notification.py`: add `normalize_toss_fill`.
- Modify `app/mcp_server/tooling/toss_live_ledger.py`: call `notify_fill` after booked Toss fill deltas.
- Create `app/tasks/toss_live_reconcile_tasks.py`: paused TaskIQ reconcile task, PR2 only.
- Modify `app/tasks/__init__.py`: import Toss reconcile task for registration, PR2 only; do not add it to `TASKIQ_TASK_MODULES`.
- Modify `app/mcp_server/README.md`: document Toss fill notification and auto-reconcile gates.
- Modify `docs/runbooks/toss-live-order-reconcile.md`: add operator workflow for notification and auto-reconcile.
- Test `tests/test_fill_notification.py`: Toss normalizer.
- Test `tests/test_mcp_server_lifecycle.py`: MCP notifier setup/shutdown gate.
- Test `tests/mcp_server/tooling/test_toss_live_ledger.py`: notification hook behavior.
- Test `tests/tasks/test_toss_live_reconcile_tasks.py`: PR2 paused task behavior.
- Test `tests/services/brokers/toss/test_config.py`: default-off gates.

---

### Task 1: Default-Off Gates And Shared Notifier Runtime

**Files:**
- Modify: `app/core/config.py`
- Create: `app/monitoring/trade_notifier/runtime.py`
- Modify: `app/main.py`
- Modify: `app/core/taskiq_broker.py`
- Modify: `app/mcp_server/lifecycle.py`
- Test: `tests/services/brokers/toss/test_config.py`
- Test: `tests/test_mcp_server_lifecycle.py`
- Test: `tests/test_taskiq_broker.py`

- [ ] **Step 1: Write failing config tests**

Append this to `tests/services/brokers/toss/test_config.py`:

```python
def test_toss_fill_notify_gate_defaults_false() -> None:
    configured = Settings(
        kis_app_key="kis-key",
        kis_app_secret="kis-secret",
        opendart_api_key="dart-key",
        upbit_access_key="upbit-key",
        upbit_secret_key="upbit-secret",
        SECRET_KEY="TestSecret123-" + "x" * 32,
    )

    assert configured.toss_fill_notify_enabled is False
```

- [ ] **Step 2: Write failing MCP lifespan tests**

Append imports to `tests/test_mcp_server_lifecycle.py`:

```python
from unittest.mock import AsyncMock
```

Append tests:

```python
@pytest.mark.unit
def test_lifespan_skips_trade_notifier_when_toss_fill_notify_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.mcp_server.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.settings, "toss_fill_notify_enabled", False)
    configure = AsyncMock()
    shutdown = AsyncMock()
    monkeypatch.setattr(lifecycle, "configure_trade_notifier_from_settings", configure)
    monkeypatch.setattr(lifecycle, "shutdown_trade_notifier", shutdown)

    mcp = FastMCP(name="lifecycle-test", lifespan=build_server_lifespan())
    app = mcp.http_app()
    with TestClient(app):
        pass

    configure.assert_not_called()
    shutdown.assert_not_awaited()


@pytest.mark.unit
def test_lifespan_configures_trade_notifier_when_toss_fill_notify_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.mcp_server.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.settings, "toss_fill_notify_enabled", True)
    configure_calls: list[str] = []

    def configure(*, log_context: str) -> bool:
        configure_calls.append(log_context)
        return True

    shutdown = AsyncMock()
    monkeypatch.setattr(lifecycle, "configure_trade_notifier_from_settings", configure)
    monkeypatch.setattr(lifecycle, "shutdown_trade_notifier", shutdown)

    mcp = FastMCP(name="lifecycle-test", lifespan=build_server_lifespan())
    app = mcp.http_app()
    with TestClient(app):
        pass

    assert configure_calls == ["MCP trade notifier"]
    shutdown.assert_awaited_once_with(log_context="MCP trade notifier")
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
uv run pytest \
  tests/services/brokers/toss/test_config.py::test_toss_fill_notify_gate_defaults_false \
  tests/test_mcp_server_lifecycle.py::test_lifespan_skips_trade_notifier_when_toss_fill_notify_disabled \
  tests/test_mcp_server_lifecycle.py::test_lifespan_configures_trade_notifier_when_toss_fill_notify_enabled \
  -v
```

Expected: FAIL because `Settings` and `app.mcp_server.lifecycle` do not expose the new attributes/helpers yet.

- [ ] **Step 4: Add Toss fill notification gate setting**

In `app/core/config.py`, inside the Toss settings block after `toss_live_order_mutations_enabled`, add:

```python
    # ROB-576 — Toss fill notifications are inert until explicitly enabled by
    # the operator. Toss auto-reconcile gates live with the task flags below.
    toss_fill_notify_enabled: bool = False
```

- [ ] **Step 5: Add shared notifier runtime helper**

Create `app/monitoring/trade_notifier/runtime.py`:

```python
"""Shared runtime setup for the TradeNotifier singleton."""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.monitoring.trade_notifier import get_trade_notifier

logger = logging.getLogger(__name__)


def _has_discord(settings_obj: Any) -> bool:
    return any(
        [
            getattr(settings_obj, "discord_webhook_us", None),
            getattr(settings_obj, "discord_webhook_kr", None),
            getattr(settings_obj, "discord_webhook_crypto", None),
            getattr(settings_obj, "discord_webhook_alerts", None),
        ]
    )


def _has_telegram(settings_obj: Any) -> bool:
    return bool(
        getattr(settings_obj, "telegram_token", None)
        and getattr(settings_obj, "telegram_chat_id", None)
    )


def configure_trade_notifier_from_settings(
    *, log_context: str = "Trade notifier", settings_obj: Any = settings
) -> bool:
    """Configure the process-local TradeNotifier from application settings."""
    has_discord = _has_discord(settings_obj)
    has_telegram = _has_telegram(settings_obj)

    if not has_discord and not has_telegram:
        logger.info("%s disabled (no Discord or Telegram configured)", log_context)
        return False

    try:
        trade_notifier = get_trade_notifier()
        bot_token = getattr(settings_obj, "telegram_token", None) or ""
        chat_ids = settings_obj.telegram_chat_ids if has_telegram else []

        trade_notifier.configure(
            bot_token=bot_token,
            chat_ids=chat_ids,
            enabled=True,
            discord_webhook_us=getattr(settings_obj, "discord_webhook_us", None),
            discord_webhook_kr=getattr(settings_obj, "discord_webhook_kr", None),
            discord_webhook_crypto=getattr(settings_obj, "discord_webhook_crypto", None),
            discord_webhook_alerts=getattr(settings_obj, "discord_webhook_alerts", None),
        )

        configured_systems: list[str] = []
        if has_discord:
            webhook_count = sum(
                [
                    bool(getattr(settings_obj, "discord_webhook_us", None)),
                    bool(getattr(settings_obj, "discord_webhook_kr", None)),
                    bool(getattr(settings_obj, "discord_webhook_crypto", None)),
                    bool(getattr(settings_obj, "discord_webhook_alerts", None)),
                ]
            )
            configured_systems.append(f"Discord ({webhook_count} webhook(s))")
        if has_telegram:
            configured_systems.append(
                f"Telegram (chat_id={getattr(settings_obj, 'telegram_chat_id', '')})"
            )

        logger.info("%s initialized: %s", log_context, ", ".join(configured_systems))
        return True
    except Exception as exc:
        logger.error("%s initialization failed: %s", log_context, exc, exc_info=True)
        return False


async def shutdown_trade_notifier(*, log_context: str = "Trade notifier") -> None:
    """Close the process-local TradeNotifier HTTP client."""
    try:
        await get_trade_notifier().shutdown()
        logger.info("%s shutdown complete", log_context)
    except Exception as exc:
        logger.error("%s shutdown failed: %s", log_context, exc, exc_info=True)
```

- [ ] **Step 6: Use helper in API startup/shutdown**

In `app/main.py`, replace:

```python
from app.monitoring.trade_notifier import get_trade_notifier
```

with:

```python
from app.monitoring.trade_notifier.runtime import (
    configure_trade_notifier_from_settings,
    shutdown_trade_notifier,
)
```

Replace the body of `setup_monitoring()` with:

```python
    configure_trade_notifier_from_settings(log_context="Trade notifier")
```

Replace the notifier block in `cleanup_monitoring()` with:

```python
    try:
        await shutdown_trade_notifier(log_context="Trade notifier")
    except Exception as e:
        logger.error(f"Error during trade notifier shutdown: {e}", exc_info=True)
```

- [ ] **Step 7: Use helper in TaskIQ worker startup**

In `app/core/taskiq_broker.py`, replace:

```python
from app.monitoring.trade_notifier import get_trade_notifier
```

with:

```python
from app.monitoring.trade_notifier.runtime import configure_trade_notifier_from_settings
```

Inside `WorkerInitMiddleware.startup`, replace the manual Discord/Telegram block with:

```python
            configure_trade_notifier_from_settings(log_context="Worker trade notifier")
```

Update `tests/test_taskiq_broker.py` to patch `configure_trade_notifier_from_settings` instead of `get_trade_notifier` for worker startup tests:

```python
mock_configure = Mock(return_value=True)
monkeypatch.setattr(
    taskiq_broker,
    "configure_trade_notifier_from_settings",
    mock_configure,
)
```

And assert:

```python
mock_configure.assert_called_once_with(log_context="Worker trade notifier")
```

- [ ] **Step 8: Wire MCP lifespan gate**

In `app/mcp_server/lifecycle.py`, add imports:

```python
from app.core.config import settings
from app.monitoring.trade_notifier.runtime import (
    configure_trade_notifier_from_settings,
    shutdown_trade_notifier,
)
```

Inside `_server_lifespan`, after startup logging and before heartbeat setup, add:

```python
        notifier_configured = False
        if settings.toss_fill_notify_enabled:
            notifier_configured = configure_trade_notifier_from_settings(
                log_context="MCP trade notifier"
            )
```

Inside the `finally` block, before the shutdown log, add:

```python
            if notifier_configured:
                await shutdown_trade_notifier(log_context="MCP trade notifier")
```

- [ ] **Step 9: Run Task 1 tests**

Run:

```bash
uv run pytest \
  tests/services/brokers/toss/test_config.py::test_toss_fill_notify_gate_defaults_false \
  tests/test_mcp_server_lifecycle.py \
  tests/test_taskiq_broker.py \
  -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add \
  app/core/config.py \
  app/monitoring/trade_notifier/runtime.py \
  app/main.py \
  app/core/taskiq_broker.py \
  app/mcp_server/lifecycle.py \
  tests/services/brokers/toss/test_config.py \
  tests/test_mcp_server_lifecycle.py \
  tests/test_taskiq_broker.py
git commit -m "feat(ROB-576): share trade notifier runtime setup"
```

---

### Task 2: Toss Fill Normalizer

**Files:**
- Modify: `app/services/fill_notification.py`
- Test: `tests/test_fill_notification.py`

- [ ] **Step 1: Write failing normalizer tests**

Append imports to `tests/test_fill_notification.py`:

```python
from decimal import Decimal
from types import SimpleNamespace
```

Add `normalize_toss_fill` to the existing import list.

Append tests:

```python
class TestNormalizeTossFill:
    def test_normalize_kr_buy_fill_from_ledger_row(self) -> None:
        row = SimpleNamespace(
            market="kr",
            symbol="005930",
            side="buy",
            currency=None,
            broker_order_id="toss-kr-order-123456",
            order_type="limit",
            price=Decimal("70100"),
        )

        order = normalize_toss_fill(
            row,
            delta=Decimal("2"),
            avg_price=Decimal("70000"),
            fill_status="partial",
        )

        assert order.symbol == "005930"
        assert order.side == "bid"
        assert order.filled_price == 70000
        assert order.filled_qty == 2
        assert order.filled_amount == 140000
        assert order.account == "toss"
        assert order.order_price == 70100
        assert order.order_id == "toss-kr-order-123456"
        assert order.order_type == "limit"
        assert order.fill_status == "partial"
        assert order.market_type == "kr"
        assert order.currency == "KRW"

    def test_normalize_us_sell_fill_preserves_usd(self) -> None:
        row = SimpleNamespace(
            market="us",
            symbol="AAPL",
            side="sell",
            currency="USD",
            broker_order_id="toss-us-order-123456",
            order_type="market",
            price=None,
        )

        order = normalize_toss_fill(
            row,
            delta=Decimal("3"),
            avg_price=Decimal("195.50"),
            fill_status="filled",
        )

        assert order.symbol == "AAPL"
        assert order.side == "ask"
        assert order.filled_price == 195.5
        assert order.filled_qty == 3
        assert order.filled_amount == 586.5
        assert order.market_type == "us"
        assert order.currency == "USD"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
uv run pytest tests/test_fill_notification.py::TestNormalizeTossFill -v
```

Expected: FAIL because `normalize_toss_fill` is not defined.

- [ ] **Step 3: Implement normalizer**

In `app/services/fill_notification.py`, after `normalize_kis_fill`, add:

```python
def _get_fill_source_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def normalize_toss_fill(
    row: Any,
    *,
    delta: Decimal | float | int,
    avg_price: Decimal | float | int,
    fill_status: str | None = None,
    filled_at: Any | None = None,
) -> FillOrder:
    """Normalize a Toss live ledger fill delta into the shared FillOrder shape."""
    symbol = str(_get_fill_source_value(row, "symbol") or "UNKNOWN")
    market_type = _normalize_market_type(_get_fill_source_value(row, "market"))
    currency = _normalize_currency(
        _get_fill_source_value(row, "currency")
    ) or _default_currency_for_market(market_type, account="toss")
    filled_price = _safe_float(avg_price)
    filled_qty = _safe_float(delta)
    filled_amount = filled_price * filled_qty

    return FillOrder(
        symbol=symbol,
        side=_normalize_side(str(_get_fill_source_value(row, "side") or "")),
        filled_price=filled_price,
        filled_qty=filled_qty,
        filled_amount=filled_amount,
        filled_at=_parse_timestamp(filled_at),
        account="toss",
        order_price=_safe_float_or_none(_get_fill_source_value(row, "price")),
        order_id=_safe_text_or_none(_get_fill_source_value(row, "broker_order_id")),
        order_type=_safe_text_or_none(_get_fill_source_value(row, "order_type")),
        fill_status=_normalize_fill_status(fill_status),
        market_type=market_type,
        currency=currency,
    )
```

- [ ] **Step 4: Run normalizer tests**

Run:

```bash
uv run pytest tests/test_fill_notification.py::TestNormalizeTossFill -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/fill_notification.py tests/test_fill_notification.py
git commit -m "feat(ROB-576): normalize Toss fill deltas"
```

---

### Task 3: Reconcile Booking Notification Hook

**Files:**
- Modify: `app/mcp_server/tooling/toss_live_ledger.py`
- Test: `tests/mcp_server/tooling/test_toss_live_ledger.py`

- [ ] **Step 1: Write failing notification tests**

Append imports to `tests/mcp_server/tooling/test_toss_live_ledger.py`:

```python
from types import SimpleNamespace
```

Append tests:

```python
async def test_reconcile_booked_fill_notifies_when_enabled(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is True
    notifier.notify_fill.assert_awaited_once()
    order = notifier.notify_fill.await_args.args[0]
    assert order.account == "toss"
    assert order.market_type == "us"
    assert order.currency == "USD"
    assert order.filled_qty == 2
    assert notifier.notify_fill.await_args.kwargs["enrichment"] is None
    assert notifier.notify_fill.await_args.kwargs["detail_url"].endswith(
        "/invest/stocks/us/AAPL"
    )


async def test_reconcile_booked_fill_skips_notify_when_gate_disabled(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_not_awaited()


async def test_reconcile_booked_fill_skips_notify_below_threshold(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "is_fill_notifiable", return_value=False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_not_awaited()


async def test_reconcile_booked_fill_notification_failure_is_fail_open(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(
        notify_fill=AsyncMock(side_effect=RuntimeError("discord down"))
    )
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_awaited_once()
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
uv run pytest \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_notifies_when_enabled \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_skips_notify_when_gate_disabled \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_skips_notify_below_threshold \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_notification_failure_is_fail_open \
  -v
```

Expected: FAIL because `toss_live_ledger` has no notification hook and no `settings` / `get_trade_notifier` imports.

- [ ] **Step 3: Add notification imports and helper**

In `app/mcp_server/tooling/toss_live_ledger.py`, add imports:

```python
from app.core.config import settings
from app.core.portfolio_links import build_position_detail_url
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.fill_notification import (
    is_fill_notifiable,
    normalize_toss_fill,
)
```

Add helper before `_reconcile_one_toss_row`:

```python
async def _notify_toss_fill(
    row: TossLiveOrderLedger,
    *,
    delta: Decimal,
    avg_price: Decimal,
    fill_status: str | None,
) -> bool:
    if not settings.toss_fill_notify_enabled:
        return False

    order = normalize_toss_fill(
        row,
        delta=delta,
        avg_price=avg_price,
        fill_status=fill_status,
    )
    if not is_fill_notifiable(order):
        logger.info(
            "toss fill notification skipped below threshold ledger_id=%s order_id=%s amount=%s currency=%s",
            row.id,
            row.broker_order_id,
            order.filled_amount,
            order.currency,
        )
        return False

    try:
        return await get_trade_notifier().notify_fill(
            order,
            enrichment=None,
            detail_url=build_position_detail_url(row.symbol, row.market),
        )
    except Exception:
        logger.warning(
            "toss fill notification failed ledger_id=%s order_id=%s",
            row.id,
            row.broker_order_id,
            exc_info=True,
        )
        return False
```

- [ ] **Step 4: Call helper after durable booking**

In `_reconcile_one_toss_row`, after:

```python
    base["action"] = "booked"
    base["trade_id"] = trade_id
    base["journal_id"] = journal_id
```

add:

```python
    base["fill_notified"] = await _notify_toss_fill(
        row,
        delta=delta,
        avg_price=avg_price,
        fill_status="partial" if evidence.verdict == "partial" else "filled",
    )
```

Keep this call after `update_reconcile_outcome(...)` so notification failure cannot roll back ledger booking.

- [ ] **Step 5: Run hook tests**

Run:

```bash
uv run pytest \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_notifies_when_enabled \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_skips_notify_when_gate_disabled \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_skips_notify_below_threshold \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_reconcile_booked_fill_notification_failure_is_fail_open \
  -v
```

Expected: PASS.

- [ ] **Step 6: Run existing Toss reconcile tests**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/toss_live_ledger.py tests/mcp_server/tooling/test_toss_live_ledger.py
git commit -m "feat(ROB-576): notify Toss fills after reconcile booking"
```

---

### Task 4: PR1 Documentation And Verification

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/runbooks/toss-live-order-reconcile.md`

- [ ] **Step 1: Update MCP README Toss safety rules**

In `app/mcp_server/README.md`, under `#### Toss Safety Rules and Gates`, add this bullet after the accepted-only ledger bullet:

```markdown
- **Fill Notifications (ROB-576)**: `toss_reconcile_orders(dry_run=False)` sends a Discord/Telegram fill notification only when `TOSS_FILL_NOTIFY_ENABLED=true`, the reconcile pass books a new fill delta, and the shared `TradeNotifier` has a KR/US webhook or Telegram fallback configured. Notifications reuse the existing fill card format and route by `market='kr'|'us'`; Toss fill enrichment is intentionally disabled (`enrichment=None`) until Toss account PnL/position enrichment exists.
```

- [ ] **Step 2: Update Toss reconcile runbook**

In `docs/runbooks/toss-live-order-reconcile.md`, after the workflow section, add:

```markdown
## Fill Notifications (ROB-576)

When `TOSS_FILL_NOTIFY_ENABLED=true`, `toss_reconcile_orders(dry_run=False)` sends a fill notification after a new fill delta is durably booked. Dry runs never notify. Re-running reconcile for an already-booked quantity does not notify because the existing delta-idempotency guard returns `noop_already_booked`.

Notification routing:

- `market="kr"` → `DISCORD_WEBHOOK_KR`
- `market="us"` → `DISCORD_WEBHOOK_US`
- Telegram fallback uses the existing `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` settings.

Toss fill notifications intentionally use `enrichment=None`. The existing KR/US fill enrichment reads KIS account state and can display the wrong position/PnL for Toss fills if the same symbol is also held in KIS.
```

- [ ] **Step 3: Run PR1 test suite**

Run:

```bash
uv run pytest \
  tests/test_fill_notification.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_mcp_server_lifecycle.py \
  tests/test_taskiq_broker.py \
  tests/services/brokers/toss/test_config.py \
  -v
```

Expected: PASS.

- [ ] **Step 4: Run lint on changed files**

Run:

```bash
uv run ruff check \
  app/core/config.py \
  app/monitoring/trade_notifier/runtime.py \
  app/main.py \
  app/core/taskiq_broker.py \
  app/mcp_server/lifecycle.py \
  app/services/fill_notification.py \
  app/mcp_server/tooling/toss_live_ledger.py \
  tests/services/brokers/toss/test_config.py \
  tests/test_mcp_server_lifecycle.py \
  tests/test_taskiq_broker.py \
  tests/test_fill_notification.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/README.md docs/runbooks/toss-live-order-reconcile.md
git commit -m "docs(ROB-576): document Toss fill notifications"
```

---

### Task 5: Paused Toss Auto-Reconcile Task (PR2, Requires Operator Scope Decision)

**Files:**
- Create: `app/tasks/toss_live_reconcile_tasks.py`
- Modify: `app/tasks/__init__.py`
- Test: `tests/tasks/test_toss_live_reconcile_tasks.py`

- [ ] **Step 1: Write failing task tests**

Create `tests/tasks/test_toss_live_reconcile_tasks.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from app.tasks import toss_live_reconcile_tasks as mod


@pytest.mark.asyncio
async def test_paused_when_flag_disabled():
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", False),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", True
        ),
        patch.object(mod, "toss_reconcile_orders_impl", AsyncMock()) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()

    assert result["status"] == "paused"
    assert "TOSS_LIVE_AUTO_RECONCILE_ENABLED" in result["message"]
    kernel.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_when_safety_review_flag_disabled():
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", False
        ),
        patch.object(mod, "toss_reconcile_orders_impl", AsyncMock()) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()

    assert result["status"] == "paused"
    assert "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED" in result["message"]
    kernel.assert_not_awaited()


@pytest.mark.asyncio
async def test_runs_kernel_when_enabled():
    fake = {"success": True, "counts": {"filled": 1}}
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", True
        ),
        patch.object(
            mod, "toss_reconcile_orders_impl", AsyncMock(return_value=fake)
        ) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()

    kernel.assert_awaited_once_with(dry_run=False)
    assert result == fake
```

- [ ] **Step 2: Run failing task tests**

Run:

```bash
uv run pytest tests/tasks/test_toss_live_reconcile_tasks.py -v
```

Expected: FAIL because `app.tasks.toss_live_reconcile_tasks` does not exist.

- [ ] **Step 3: Add paused TaskIQ task**

Create `app/tasks/toss_live_reconcile_tasks.py`:

```python
"""ROB-576 — paused TaskIQ auto-reconcile for Toss live KR/US orders.

NO schedule: starts paused. An operator adds the cron in robin-prefect-
automations and flips both Toss auto-reconcile gates after review. The task
reuses the existing toss_reconcile_orders_impl kernel; it adds no new live
order mutation path.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl


@broker.task(task_name="toss_live.reconcile_periodic")  # no schedule -> paused
async def toss_live_reconcile_periodic() -> dict:
    if not settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_ENABLED is False",
        }
    if not settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False",
        }
    return await toss_reconcile_orders_impl(dry_run=False)
```

- [ ] **Step 4: Register task module for import side effect**

In `app/tasks/__init__.py`, add `toss_live_reconcile_tasks` to the top import list near `kis_live_reconcile_tasks`:

```python
    kis_live_reconcile_tasks,  # noqa: F401
    toss_live_reconcile_tasks,  # noqa: F401
```

Do not add `toss_live_reconcile_tasks` to `TASKIQ_TASK_MODULES`, matching `kis_live_reconcile_tasks.py`'s paused-task pattern.

- [ ] **Step 5: Run task tests**

Run:

```bash
uv run pytest tests/tasks/test_toss_live_reconcile_tasks.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tasks/toss_live_reconcile_tasks.py app/tasks/__init__.py tests/tasks/test_toss_live_reconcile_tasks.py
git commit -m "feat(ROB-576): add paused Toss auto-reconcile task"
```

---

### Task 6: PR2 Documentation And Review Hold

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/runbooks/toss-live-order-reconcile.md`

- [ ] **Step 1: Document auto-reconcile in MCP README**

In `app/mcp_server/README.md`, extend the ROB-576 fill notification bullet with:

```markdown
The optional paused TaskIQ task `toss_live.reconcile_periodic` calls `toss_reconcile_orders_impl(dry_run=False)` only when both `TOSS_LIVE_AUTO_RECONCILE_ENABLED=true` and `TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`. It has no in-repo schedule; operator automation must register/unpause the cadence externally.
```

- [ ] **Step 2: Document auto-reconcile in runbook**

In `docs/runbooks/toss-live-order-reconcile.md`, after the ROB-576 fill notification section, add:

```markdown
## Auto-Reconcile (ROB-576 PR2)

The optional TaskIQ task `toss_live.reconcile_periodic` is shipped without an in-repo schedule and returns `{"status": "paused"}` until both gates are true:

- `TOSS_LIVE_AUTO_RECONCILE_ENABLED=true`
- `TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`

When enabled, the task calls `toss_reconcile_orders_impl(dry_run=False)`. This still does not place, modify, or cancel live orders; it only books confirmed broker evidence into local trades/journals and triggers fill notifications if `TOSS_FILL_NOTIFY_ENABLED=true`.

Recommended initial external cadence: 1-5 minutes. Start at 5 minutes unless there is an operator need for faster Discord latency, then tighten after watching Toss API rate-limit and OAuth behavior.
```

- [ ] **Step 3: Apply Linear high-risk labels/comment before PR2 merge**

If Linear access is available, add labels to ROB-576:

- `high_risk_change`
- `needs_stronger_model_review`
- `hold_for_final_review`
- `candidate_for_opus`

Add this comment to ROB-576:

```markdown
Applying high_risk_change + needs_stronger_model_review + hold_for_final_review for ROB-576 PR2: this adds an operator-gated TaskIQ path that can unattendedly book Toss live fill evidence into local trades/journals. The task is default-off and requires both TOSS_LIVE_AUTO_RECONCILE_ENABLED and TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED, but no deploy, cron registration, or operational use should proceed until stronger-model/CTO review clears the automation boundary.
```

- [ ] **Step 4: Run PR2 tests**

Run:

```bash
uv run pytest \
  tests/tasks/test_toss_live_reconcile_tasks.py \
  tests/services/brokers/toss/test_config.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/README.md docs/runbooks/toss-live-order-reconcile.md
git commit -m "docs(ROB-576): document Toss auto-reconcile gates"
```

---

### Task 7: Final Verification

**Files:**
- No new files. This verifies the implemented scope.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run pytest \
  tests/test_fill_notification.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_mcp_server_lifecycle.py \
  tests/test_taskiq_broker.py \
  tests/services/brokers/toss/test_config.py \
  tests/tasks/test_toss_live_reconcile_tasks.py \
  -v
```

Expected: PASS. If Task 5 was not implemented, omit `tests/tasks/test_toss_live_reconcile_tasks.py`.

- [ ] **Step 2: Run lint**

Run:

```bash
uv run ruff check \
  app/core/config.py \
  app/monitoring/trade_notifier/runtime.py \
  app/main.py \
  app/core/taskiq_broker.py \
  app/mcp_server/lifecycle.py \
  app/services/fill_notification.py \
  app/mcp_server/tooling/toss_live_ledger.py \
  app/tasks/toss_live_reconcile_tasks.py \
  tests/services/brokers/toss/test_config.py \
  tests/test_mcp_server_lifecycle.py \
  tests/test_taskiq_broker.py \
  tests/test_fill_notification.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/tasks/test_toss_live_reconcile_tasks.py
```

Expected: PASS. If Task 5 was not implemented, remove `app/tasks/toss_live_reconcile_tasks.py` and `tests/tasks/test_toss_live_reconcile_tasks.py` from the command.

- [ ] **Step 3: Run full unit gate if time allows**

Run:

```bash
make test-unit
```

Expected: PASS.

- [ ] **Step 4: Final diff review**

Run:

```bash
git diff --stat
git diff -- app/core/config.py app/monitoring/trade_notifier/runtime.py app/main.py app/core/taskiq_broker.py app/mcp_server/lifecycle.py app/services/fill_notification.py app/mcp_server/tooling/toss_live_ledger.py app/tasks/toss_live_reconcile_tasks.py app/tasks/__init__.py app/mcp_server/README.md docs/runbooks/toss-live-order-reconcile.md
```

Confirm:

- `TOSS_FILL_NOTIFY_ENABLED=false` leaves MCP notifier disabled and no Toss fill notification is sent.
- Notification happens only after `action="booked"`.
- Dry-run, pending, no-fill cancel, and already-booked deltas do not notify.
- `market_type` passed to `TradeNotifier` is `kr` or `us`, never `equity`.
- `enrichment=None` is passed for Toss fills.
- PR2 task has no schedule and requires both Toss auto-reconcile gates.

---

## Self-Review

- Spec coverage: PR1 covers MCP notifier configure, Toss fill normalization, reconcile booking hook, idempotency through existing `delta<=0`, market routing, threshold gate, fail-open notification failure, no enrichment, and docs. PR2 covers paused auto-reconcile with double gate and docs.
- Placeholder scan: this plan intentionally has no unresolved placeholder tokens, no broad "add tests" instruction without code, and no undefined function names.
- Type consistency: settings names are `toss_fill_notify_enabled` and existing uppercase `TOSS_LIVE_AUTO_RECONCILE_*`; task name is `toss_live.reconcile_periodic`; normalizer name is `normalize_toss_fill`; helper name is `_notify_toss_fill`.
