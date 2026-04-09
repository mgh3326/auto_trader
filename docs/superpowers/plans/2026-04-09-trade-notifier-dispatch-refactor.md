# TradeNotifier Dispatch Pattern Refactor

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a shared `_dispatch` method from 10 copy-pasted `notify_*` methods and remove redundant format wrapper methods that simply delegate to `fmt_discord.*` / `fmt_telegram.*`.

**Architecture:** Add a single `_dispatch(discord_embed, telegram_message, market_type)` method that encapsulates the "Discord first, Telegram fallback" pattern. Each `notify_*` method becomes 3-5 lines: build embed + build telegram message + call `_dispatch`. Format wrapper methods (lines 149-515) are removed; `notify_*` methods call `fmt_discord.*` and `fmt_telegram.*` directly. `notify_openclaw_message` stays unchanged (it uses `_send_to_discord_content_single`, has unique logging, and doesn't fit the embed dispatch pattern).

**Tech Stack:** Python 3.13+, httpx, pytest, pytest-asyncio, ruff, ty

---

## File Structure

```
app/monitoring/trade_notifier/
├── __init__.py              # (no change)
├── types.py                 # (no change)
├── transports.py            # (no change)
├── formatters_discord.py    # (no change)
├── formatters_telegram.py   # (no change)
└── notifier.py              # MODIFY: add _dispatch, simplify notify_*, remove format wrappers
```

Tests:
```
tests/
├── test_trade_notifier.py         # MODIFY: update wrapper-calling tests → call fmt modules directly; add _dispatch tests
└── test_toss_notification.py      # MODIFY: update wrapper-calling tests → call fmt modules directly
```

**Key design decisions:**

- `_dispatch` handles: enabled check, webhook lookup via `_get_webhook_for_market_type`, Discord embed send with Telegram fallback, and exception handling with logging.
- `notify_automation_summary` uses a new `"alerts"` case in `_get_webhook_for_market_type` to route to `self._discord_webhook_alerts`.
- Toss methods (Discord-only, no Telegram fallback) pass `telegram_message=""` to `_dispatch`. Since `_dispatch` checks `if telegram_message:`, an empty string means no Telegram fallback — exactly matching current behavior of returning `False` when webhook is missing.
- `notify_toss_buy/sell_recommendation` retain their `toss_quantity <= 0` early return BEFORE calling `_dispatch`.
- `notify_openclaw_message` is **excluded** from refactoring — its unique logging, `skip_discord` flag, `_send_to_discord_content_single` (not embed), and `_has_telegram_delivery_config` check make it unsuitable for `_dispatch`.
- Format wrapper tests in `test_trade_notifier.py` (lines 85-377) and `test_toss_notification.py` (lines 90-278) that call `trade_notifier._format_*()` are updated to call `fmt_discord.*` / `fmt_telegram.*` directly — these wrappers are pure pass-through and dedicated formatter tests already exist in `test_trade_notifier_formatters_discord.py` / `test_trade_notifier_formatters_telegram.py`.

---

## Task 1: Add `_dispatch` method and `"alerts"` routing

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py:109-142` (routing) and new method after line 556
- Test: `tests/test_trade_notifier.py`

- [ ] **Step 1: Write failing test for `_dispatch` — Discord success**

```python
# Add to tests/test_trade_notifier.py after the existing imports

@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_discord_success(trade_notifier):
    """_dispatch sends embed to Discord and returns True on success."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_crypto="https://discord.com/api/webhooks/crypto",
    )

    with patch.object(
        trade_notifier,
        "_send_to_discord_embed_single",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_discord:
        embed = {"title": "test", "description": "", "color": 0, "fields": []}
        result = await trade_notifier._dispatch(
            discord_embed=embed,
            telegram_message="fallback text",
            market_type="crypto",
        )

        assert result is True
        mock_discord.assert_called_once_with(embed, "https://discord.com/api/webhooks/crypto")
```

- [ ] **Step 2: Write failing test for `_dispatch` — Discord fails, Telegram fallback**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_telegram_fallback(trade_notifier):
    """_dispatch falls back to Telegram when Discord fails."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_crypto="https://discord.com/api/webhooks/crypto",
    )

    with patch.object(
        trade_notifier,
        "_send_to_discord_embed_single",
        new_callable=AsyncMock,
        return_value=False,
    ), patch.object(
        trade_notifier,
        "_send_to_telegram",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_telegram:
        result = await trade_notifier._dispatch(
            discord_embed={"title": "t", "description": "", "color": 0, "fields": []},
            telegram_message="fallback text",
            market_type="crypto",
        )

        assert result is True
        mock_telegram.assert_called_once_with("fallback text")
```

- [ ] **Step 3: Write failing test for `_dispatch` — disabled**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_disabled(trade_notifier):
    """_dispatch returns False when notifier is disabled."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=False,
    )

    result = await trade_notifier._dispatch(
        discord_embed={"title": "t", "description": "", "color": 0, "fields": []},
        telegram_message="text",
        market_type="crypto",
    )

    assert result is False
```

- [ ] **Step 4: Write failing test for `_dispatch` — no webhook, no telegram message (Discord-only mode)**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_no_webhook_no_telegram(trade_notifier):
    """_dispatch returns False when no webhook and telegram_message is empty."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        # no crypto webhook configured
    )

    result = await trade_notifier._dispatch(
        discord_embed={"title": "t", "description": "", "color": 0, "fields": []},
        telegram_message="",
        market_type="crypto",
    )

    assert result is False
```

- [ ] **Step 5: Write failing test for `_dispatch` — exception handling**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_exception_returns_false(trade_notifier):
    """_dispatch catches exceptions and returns False."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_crypto="https://discord.com/api/webhooks/crypto",
    )

    with patch.object(
        trade_notifier,
        "_send_to_discord_embed_single",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result = await trade_notifier._dispatch(
            discord_embed={"title": "t", "description": "", "color": 0, "fields": []},
            telegram_message="text",
            market_type="crypto",
        )

        assert result is False
```

- [ ] **Step 6: Write failing test for `"alerts"` market type routing**

```python
@pytest.mark.unit
def test_market_type_routing_alerts(trade_notifier):
    """_get_webhook_for_market_type returns alerts webhook for 'alerts' market type."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_alerts="https://discord.com/api/webhooks/alerts",
    )

    assert trade_notifier._get_webhook_for_market_type("alerts") == "https://discord.com/api/webhooks/alerts"
```

- [ ] **Step 7: Run tests to verify they all fail**

Run: `uv run pytest tests/test_trade_notifier.py -v -k "test_dispatch or test_market_type_routing_alerts" --timeout=10 -x`
Expected: FAIL — `_dispatch` method does not exist, `"alerts"` not handled

- [ ] **Step 8: Implement `_dispatch` method and `"alerts"` routing**

Add `"alerts"` to `_get_webhook_for_market_type` in `notifier.py`:

```python
    def _get_webhook_for_market_type(self, market_type: str) -> str | None:
        """Get the appropriate Discord webhook URL for a given market type."""
        market_type_normalized = market_type.strip().lower()

        if market_type_normalized in {
            "us",
            "usa",
            "overseas",
            "equity_us",
            "해외주식",
            "nas",
            "nasd",
            "nasdaq",
            "nys",
            "nyse",
            "ams",
            "amex",
        }:
            return self._discord_webhook_us
        elif market_type_normalized in {
            "kr",
            "krx",
            "domestic",
            "equity_kr",
            "국내주식",
        }:
            return self._discord_webhook_kr
        elif market_type_normalized in {"crypto", "cryptocurrency", "coin", "암호화폐"}:
            return self._discord_webhook_crypto
        elif market_type_normalized in {"alerts", "alert"}:
            return self._discord_webhook_alerts
        else:
            logger.warning(f"Unknown market type: {market_type}")
            return None
```

Add `_dispatch` method after the transport wrappers section (after line 555):

```python
    async def _dispatch(
        self,
        discord_embed: DiscordEmbed | None,
        telegram_message: str,
        market_type: str | None = None,
    ) -> bool:
        """Discord-first, Telegram-fallback dispatch.

        Args:
            discord_embed: Discord embed to send. Skipped if None.
            telegram_message: Telegram fallback text. Skipped if empty.
            market_type: Market type for webhook routing. None skips Discord.

        Returns:
            True if notification was delivered via any channel.
        """
        if not self._enabled:
            return False

        try:
            if market_type and discord_embed:
                webhook_url = self._get_webhook_for_market_type(market_type)
                if webhook_url:
                    if await self._send_to_discord_embed_single(discord_embed, webhook_url):
                        return True

            if telegram_message:
                return await self._send_to_telegram(telegram_message)

            return False
        except Exception:
            logger.exception("Notification dispatch failed")
            return False
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `uv run pytest tests/test_trade_notifier.py -v -k "test_dispatch or test_market_type_routing_alerts" --timeout=10 -x`
Expected: PASS — all 6 new tests green

- [ ] **Step 10: Commit**

```bash
git add app/monitoring/trade_notifier/notifier.py tests/test_trade_notifier.py
git commit -m "refactor(trade-notifier): add _dispatch method and 'alerts' market type routing"
```

---

## Task 2: Migrate `notify_buy_order`, `notify_sell_order`, `notify_cancel_orders` to `_dispatch`

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py:559-711`
- Test: `tests/test_trade_notifier.py`

- [ ] **Step 1: Run existing tests to establish baseline**

Run: `uv run pytest tests/test_trade_notifier.py -v -k "notify_buy_order or notify_sell_order or notify_cancel" --timeout=10`
Expected: PASS — all existing tests green

- [ ] **Step 2: Rewrite `notify_buy_order` to use `_dispatch`**

Replace `notify_buy_order` (lines 559-612) with:

```python
    async def notify_buy_order(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_amount: float,
        prices: list[float],
        volumes: list[float],
        market_type: str = "암호화폐",
    ) -> bool:
        """Send buy order notification. Discord first, Telegram fallback."""
        embed = fmt_discord.format_buy_notification(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_amount=total_amount,
            prices=prices,
            volumes=volumes,
            market_type=market_type,
        )
        telegram_msg = fmt_telegram.format_buy_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_amount=total_amount,
            prices=prices,
            volumes=volumes,
            market_type=market_type,
        )
        return await self._dispatch(embed, telegram_msg, market_type)
```

- [ ] **Step 3: Rewrite `notify_sell_order` to use `_dispatch`**

Replace `notify_sell_order` (lines 614-670) with:

```python
    async def notify_sell_order(
        self,
        symbol: str,
        korean_name: str,
        order_count: int,
        total_volume: float,
        prices: list[float],
        volumes: list[float],
        expected_amount: float,
        market_type: str = "암호화폐",
    ) -> bool:
        """Send sell order notification. Discord first, Telegram fallback."""
        embed = fmt_discord.format_sell_notification(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_volume=total_volume,
            prices=prices,
            volumes=volumes,
            expected_amount=expected_amount,
            market_type=market_type,
        )
        telegram_msg = fmt_telegram.format_sell_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            order_count=order_count,
            total_volume=total_volume,
            prices=prices,
            volumes=volumes,
            expected_amount=expected_amount,
            market_type=market_type,
        )
        return await self._dispatch(embed, telegram_msg, market_type)
```

- [ ] **Step 4: Rewrite `notify_cancel_orders` to use `_dispatch`**

Replace `notify_cancel_orders` (lines 672-711) with:

```python
    async def notify_cancel_orders(
        self,
        symbol: str,
        korean_name: str,
        cancel_count: int,
        order_type: str = "전체",
        market_type: str = "암호화폐",
    ) -> bool:
        """Send order cancellation notification. Discord first, Telegram fallback."""
        embed = fmt_discord.format_cancel_notification(
            symbol=symbol,
            korean_name=korean_name,
            cancel_count=cancel_count,
            order_type=order_type,
            market_type=market_type,
        )
        telegram_msg = fmt_telegram.format_cancel_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            cancel_count=cancel_count,
            order_type=order_type,
            market_type=market_type,
        )
        return await self._dispatch(embed, telegram_msg, market_type)
```

- [ ] **Step 5: Run tests to verify no regressions**

Run: `uv run pytest tests/test_trade_notifier.py -v -k "notify_buy_order or notify_sell_order or notify_cancel" --timeout=10`
Expected: PASS — all existing tests still green

- [ ] **Step 6: Commit**

```bash
git add app/monitoring/trade_notifier/notifier.py
git commit -m "refactor(trade-notifier): migrate buy/sell/cancel notify methods to _dispatch"
```

---

## Task 3: Migrate `notify_analysis_complete`, `notify_automation_summary`, `notify_trade_failure` to `_dispatch`

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py:713-832`
- Test: `tests/test_trade_notifier.py`

- [ ] **Step 1: Run existing tests to establish baseline**

Run: `uv run pytest tests/test_trade_notifier.py -v -k "notify_analysis or notify_automation or notify_trade_failure" --timeout=10`
Expected: PASS

- [ ] **Step 2: Rewrite `notify_analysis_complete`**

```python
    async def notify_analysis_complete(
        self,
        symbol: str,
        korean_name: str,
        decision: str,
        confidence: float,
        reasons: list[str],
        market_type: str = "암호화폐",
    ) -> bool:
        """Send AI analysis completion notification. Discord first, Telegram fallback."""
        embed = fmt_discord.format_analysis_notification(
            symbol=symbol,
            korean_name=korean_name,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            market_type=market_type,
        )
        telegram_msg = fmt_telegram.format_analysis_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            market_type=market_type,
        )
        return await self._dispatch(embed, telegram_msg, market_type)
```

- [ ] **Step 3: Rewrite `notify_automation_summary` using `"alerts"` market type**

```python
    async def notify_automation_summary(
        self,
        total_coins: int,
        analyzed: int,
        bought: int,
        sold: int,
        errors: int,
        duration_seconds: float,
    ) -> bool:
        """Send automation execution summary notification."""
        embed = fmt_discord.format_automation_summary(
            total_coins=total_coins,
            analyzed=analyzed,
            bought=bought,
            sold=sold,
            errors=errors,
            duration_seconds=duration_seconds,
        )
        telegram_msg = fmt_telegram.format_automation_summary_telegram(
            total_coins=total_coins,
            analyzed=analyzed,
            bought=bought,
            sold=sold,
            errors=errors,
            duration_seconds=duration_seconds,
        )
        return await self._dispatch(embed, telegram_msg, "alerts")
```

- [ ] **Step 4: Rewrite `notify_trade_failure`**

```python
    async def notify_trade_failure(
        self,
        symbol: str,
        korean_name: str,
        reason: str,
        market_type: str = "암호화폐",
    ) -> bool:
        """Send trade failure notification. Discord first, Telegram fallback."""
        embed = fmt_discord.format_failure_notification(
            symbol=symbol,
            korean_name=korean_name,
            reason=reason,
            market_type=market_type,
        )
        telegram_msg = fmt_telegram.format_failure_notification_telegram(
            symbol=symbol,
            korean_name=korean_name,
            reason=reason,
            market_type=market_type,
        )
        return await self._dispatch(embed, telegram_msg, market_type)
```

- [ ] **Step 5: Run tests to verify no regressions**

Run: `uv run pytest tests/test_trade_notifier.py -v -k "notify_analysis or notify_automation or notify_trade_failure" --timeout=10`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/monitoring/trade_notifier/notifier.py
git commit -m "refactor(trade-notifier): migrate analysis/summary/failure notify methods to _dispatch"
```

---

## Task 4: Migrate `notify_toss_*` methods to `_dispatch`

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py:834-992`
- Test: `tests/test_trade_notifier.py`, `tests/test_toss_notification.py`

- [ ] **Step 1: Run existing tests to establish baseline**

Run: `uv run pytest tests/test_trade_notifier.py tests/test_toss_notification.py -v -k "toss" --timeout=10`
Expected: PASS

- [ ] **Step 2: Rewrite `notify_toss_buy_recommendation` (Discord-only, with `toss_quantity` guard)**

```python
    async def notify_toss_buy_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        kis_quantity: int | None,
        kis_avg_price: float | None,
        recommended_price: float,
        recommended_quantity: int,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> bool:
        """Send Toss manual buy recommendation notification."""
        if not self._enabled:
            return False
        if toss_quantity <= 0:
            logger.debug(
                f"Skipping Toss buy notification for {symbol}: no Toss holdings"
            )
            return False

        embed = fmt_discord.format_toss_buy_recommendation(
            symbol=symbol,
            korean_name=korean_name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            kis_quantity=kis_quantity,
            kis_avg_price=kis_avg_price,
            recommended_price=recommended_price,
            recommended_quantity=recommended_quantity,
            currency=currency,
            market_type=market_type,
            detail_url=detail_url,
        )
        return await self._dispatch(embed, "", market_type)
```

- [ ] **Step 3: Rewrite `notify_toss_sell_recommendation` (Discord-only, with `toss_quantity` guard)**

```python
    async def notify_toss_sell_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        kis_quantity: int | None,
        kis_avg_price: float | None,
        recommended_price: float,
        recommended_quantity: int,
        expected_profit: float,
        profit_percent: float,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> bool:
        """Send Toss manual sell recommendation notification."""
        if not self._enabled:
            return False
        if toss_quantity <= 0:
            logger.debug(
                f"Skipping Toss sell notification for {symbol}: no Toss holdings"
            )
            return False

        embed = fmt_discord.format_toss_sell_recommendation(
            symbol=symbol,
            korean_name=korean_name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            kis_quantity=kis_quantity,
            kis_avg_price=kis_avg_price,
            recommended_price=recommended_price,
            recommended_quantity=recommended_quantity,
            expected_profit=expected_profit,
            profit_percent=profit_percent,
            currency=currency,
            market_type=market_type,
            detail_url=detail_url,
        )
        return await self._dispatch(embed, "", market_type)
```

- [ ] **Step 4: Rewrite `notify_toss_price_recommendation` (Discord-only, with `toss_quantity` guard)**

```python
    async def notify_toss_price_recommendation(
        self,
        symbol: str,
        korean_name: str,
        current_price: float,
        toss_quantity: int,
        toss_avg_price: float,
        decision: str,
        confidence: float,
        reasons: list[str],
        appropriate_buy_min: float | None,
        appropriate_buy_max: float | None,
        appropriate_sell_min: float | None,
        appropriate_sell_max: float | None,
        buy_hope_min: float | None = None,
        buy_hope_max: float | None = None,
        sell_target_min: float | None = None,
        sell_target_max: float | None = None,
        currency: str = "원",
        market_type: str = "국내주식",
        detail_url: str | None = None,
    ) -> bool:
        """Send Toss price recommendation notification with AI analysis."""
        if not self._enabled:
            return False
        if toss_quantity <= 0:
            logger.debug(f"Skipping Toss notification for {symbol}: no Toss holdings")
            return False

        embed = fmt_discord.format_toss_price_recommendation(
            symbol=symbol,
            korean_name=korean_name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            appropriate_buy_min=appropriate_buy_min,
            appropriate_buy_max=appropriate_buy_max,
            appropriate_sell_min=appropriate_sell_min,
            appropriate_sell_max=appropriate_sell_max,
            buy_hope_min=buy_hope_min,
            buy_hope_max=buy_hope_max,
            sell_target_min=sell_target_min,
            sell_target_max=sell_target_max,
            currency=currency,
            market_type=market_type,
            detail_url=detail_url,
        )
        return await self._dispatch(embed, "", market_type)
```

- [ ] **Step 5: Run tests to verify no regressions**

Run: `uv run pytest tests/test_trade_notifier.py tests/test_toss_notification.py -v -k "toss" --timeout=10`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/monitoring/trade_notifier/notifier.py
git commit -m "refactor(trade-notifier): migrate toss notify methods to _dispatch"
```

---

## Task 5: Remove format wrapper methods and update tests

**Files:**
- Modify: `app/monitoring/trade_notifier/notifier.py:147-515` (delete all `_format_*` methods)
- Modify: `tests/test_trade_notifier.py:85-377,577-615,1579-1615` (update to call `fmt_discord.*` / `fmt_telegram.*` directly)
- Modify: `tests/test_toss_notification.py:90-278` (update to call `fmt_discord.*` directly)

- [ ] **Step 1: Remove all `_format_*` wrapper methods from `notifier.py`**

Delete the entire `# ── format wrappers` section (lines 147-515) from `notifier.py`. These methods:

| Wrapper method | Delegates to |
|---|---|
| `_format_buy_notification` | `fmt_discord.format_buy_notification` |
| `_format_sell_notification` | `fmt_discord.format_sell_notification` |
| `_format_cancel_notification` | `fmt_discord.format_cancel_notification` |
| `_format_analysis_notification` | `fmt_discord.format_analysis_notification` |
| `_format_automation_summary` | `fmt_discord.format_automation_summary` |
| `_format_failure_notification` | `fmt_discord.format_failure_notification` |
| `_format_toss_buy_recommendation` | `fmt_discord.format_toss_buy_recommendation` |
| `_format_toss_sell_recommendation` | `fmt_discord.format_toss_sell_recommendation` |
| `_format_toss_price_recommendation_discord_embed` | `fmt_discord.format_toss_price_recommendation` |
| `_format_buy_notification_telegram` | `fmt_telegram.format_buy_notification_telegram` |
| `_format_sell_notification_telegram` | `fmt_telegram.format_sell_notification_telegram` |
| `_format_cancel_notification_telegram` | `fmt_telegram.format_cancel_notification_telegram` |
| `_format_analysis_notification_telegram` | `fmt_telegram.format_analysis_notification_telegram` |
| `_format_automation_summary_telegram` | `fmt_telegram.format_automation_summary_telegram` |
| `_format_failure_notification_telegram` | `fmt_telegram.format_failure_notification_telegram` |
| `_format_toss_price_recommendation_html` | `fmt_telegram.format_toss_price_recommendation_html` |

- [ ] **Step 2: Update `test_trade_notifier.py` — Discord format tests (lines 85-377)**

Replace `trade_notifier._format_buy_notification(...)` calls with `fmt_discord.format_buy_notification(...)`, etc. Add the import at the top of the file:

```python
from app.monitoring.trade_notifier import formatters_discord as fmt_discord
from app.monitoring.trade_notifier import formatters_telegram as fmt_telegram
```

All test functions that call `trade_notifier._format_*()` need updating. Specifically:

- `test_format_buy_notification` (line 85): `trade_notifier._format_buy_notification(...)` → `fmt_discord.format_buy_notification(...)`
- `test_format_buy_notification_without_details` (line 118): same change
- `test_format_sell_notification` (line 144): `trade_notifier._format_sell_notification(...)` → `fmt_discord.format_sell_notification(...)`
- `test_format_sell_notification_without_volumes` (line 178): same change
- `test_format_cancel_notification` (line 212): `trade_notifier._format_cancel_notification(...)` → `fmt_discord.format_cancel_notification(...)`
- `test_format_analysis_notification` (line 237): `trade_notifier._format_analysis_notification(...)` → `fmt_discord.format_analysis_notification(...)`
- `test_format_analysis_notification_hold` (line 273): same change
- `test_format_analysis_notification_sell` (line 298): same change
- `test_format_automation_summary` (line 323): `trade_notifier._format_automation_summary(...)` → `fmt_discord.format_automation_summary(...)`
- `test_format_automation_summary_with_errors` (line 349): same change
- `test_format_failure_notification` (line 577): `trade_notifier._format_failure_notification(...)` → `fmt_discord.format_failure_notification(...)`

These tests no longer need the `trade_notifier` fixture. Remove the parameter. Example transformation:

```python
# Before:
@pytest.mark.unit
def test_format_buy_notification(trade_notifier):
    embed = trade_notifier._format_buy_notification(
        symbol="BTC", korean_name="비트코인", ...
    )

# After:
@pytest.mark.unit
def test_format_buy_notification():
    embed = fmt_discord.format_buy_notification(
        symbol="BTC", korean_name="비트코인", ...
    )
```

- [ ] **Step 3: Update `test_trade_notifier.py` — Telegram format tests (lines 1579-1615)**

- `test_format_automation_summary_telegram` (line 1579): `trade_notifier._format_automation_summary_telegram(...)` → `fmt_telegram.format_automation_summary_telegram(...)`
- `test_format_automation_summary_telegram_with_errors` (line 1601): same change

Remove `trade_notifier` fixture parameter from these tests too.

- [ ] **Step 4: Update `test_toss_notification.py` — format tests (lines 90-278)**

Add import at the top:

```python
from app.monitoring.trade_notifier import formatters_discord as fmt_discord
```

Update all `notifier._format_toss_*()` calls in `TestTradeNotifierFormatting` class:

- `test_format_toss_buy_recommendation_toss_only`: `notifier._format_toss_buy_recommendation(...)` → `fmt_discord.format_toss_buy_recommendation(...)`
- `test_format_toss_buy_recommendation_with_detail_url`: same change
- `test_format_toss_buy_recommendation_with_kis`: same change
- `test_format_toss_sell_recommendation_toss_only`: `notifier._format_toss_sell_recommendation(...)` → `fmt_discord.format_toss_sell_recommendation(...)`
- `test_format_toss_sell_recommendation_with_kis`: same change
- `test_format_toss_buy_recommendation_usd`: same as buy
- `test_format_toss_sell_recommendation_negative_profit`: same as sell

These tests in `TestTradeNotifierFormatting` create their own `TradeNotifier()` instance just to call `_format_*`. After the change, they don't need the notifier at all — they call `fmt_discord.*` directly. Remove `notifier = TradeNotifier()` lines.

- [ ] **Step 5: Run full notification test suite**

Run: `uv run pytest tests/test_trade_notifier.py tests/test_toss_notification.py tests/test_trade_notifier_formatters_discord.py tests/test_trade_notifier_formatters_telegram.py -v --timeout=10`
Expected: PASS — all tests green

- [ ] **Step 6: Commit**

```bash
git add app/monitoring/trade_notifier/notifier.py tests/test_trade_notifier.py tests/test_toss_notification.py
git commit -m "refactor(trade-notifier): remove format wrapper methods, tests call fmt modules directly"
```

---

## Task 6: Final verification

**Files:** (no changes — verification only)

- [ ] **Step 1: Run the full notification test suite**

Run: `uv run pytest tests/ -v -k "notif" --timeout=10 -x`
Expected: PASS

- [ ] **Step 2: Run lint**

Run: `make lint`
Expected: PASS — no lint issues

- [ ] **Step 3: Run typecheck**

Run: `make typecheck`
Expected: PASS — no type errors

- [ ] **Step 4: Verify line count reduction**

Run: `wc -l app/monitoring/trade_notifier/notifier.py`
Expected: ~450-500 lines (down from 1,122)

- [ ] **Step 5: Run broader test suite to catch any callers we missed**

Run: `uv run pytest tests/ -v --timeout=30 -x -m "not integration and not slow"`
Expected: PASS
