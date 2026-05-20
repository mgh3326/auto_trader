"""ROB-281 Stage 6 — Discord ops alert unit tests.

Validates the contract: never on success, no-op when webhook unset, never
raises on transport failure, embeds carry slot / market / exception class /
distribution preview / truncated message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.invest_screener_snapshots import alerts as alerts_module
from app.services.invest_screener_snapshots.alerts import (
    send_screener_refresh_alert,
)
from app.services.invest_screener_snapshots.guards import (
    InsufficientRowsError,
    SuspiciousDistributionError,
)

# --- Noop when webhook unset -------------------------------------------------


@pytest.mark.asyncio
async def test_alert_returns_false_when_webhook_unset(mocker) -> None:
    mocker.patch.object(alerts_module.settings, "discord_webhook_alerts", None)
    mock_send = mocker.patch.object(
        alerts_module, "send_discord_embed_single", new=AsyncMock()
    )

    result = await send_screener_refresh_alert(
        slot="krx_preliminary",
        market="kr",
        exception=SuspiciousDistributionError("test"),
    )

    assert result is False
    mock_send.assert_not_awaited()  # transport never called


@pytest.mark.asyncio
async def test_alert_returns_false_when_webhook_empty_string(mocker) -> None:
    """Empty string is falsy and must be treated as unset (no spurious posts)."""
    mocker.patch.object(alerts_module.settings, "discord_webhook_alerts", "")
    mock_send = mocker.patch.object(
        alerts_module, "send_discord_embed_single", new=AsyncMock()
    )

    result = await send_screener_refresh_alert(
        slot="nxt_final", market="kr", exception=RuntimeError("test")
    )

    assert result is False
    mock_send.assert_not_awaited()


# --- Happy path embed shape --------------------------------------------------


@pytest.mark.asyncio
async def test_alert_embed_carries_slot_market_distribution_and_exception(
    mocker,
) -> None:
    mocker.patch.object(
        alerts_module.settings,
        "discord_webhook_alerts",
        "https://discord.com/api/webhooks/x/y",
    )
    mock_send = mocker.patch.object(
        alerts_module,
        "send_discord_embed_single",
        new=AsyncMock(return_value=True),
    )

    exc = SuspiciousDistributionError(
        "no dominant partition: top=2026-05-20",
        distribution={"2026-05-20": 1800, "2026-05-19": 1200},
    )
    result = await send_screener_refresh_alert(
        slot="krx_preliminary",
        market="kr",
        exception=exc,
        distribution=exc.distribution,
        commit_status="skipped",
    )

    assert result is True
    assert mock_send.await_count == 1
    kwargs = mock_send.await_args.kwargs
    assert kwargs["webhook_url"] == "https://discord.com/api/webhooks/x/y"
    embed = kwargs["embed"]
    assert "kr / krx_preliminary" in embed["title"]
    field_values = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_values["slot"] == "krx_preliminary"
    assert field_values["market"] == "kr"
    assert field_values["commit"] == "skipped"
    assert "SuspiciousDistributionError" in field_values["exception"]
    # Top-3 distribution preview includes both dates (sorted desc by count).
    dist_value = field_values["snapshot_date distribution (top 3)"]
    assert "2026-05-20=1800" in dist_value
    assert "2026-05-19=1200" in dist_value
    # 2026-05-20 should appear first because it has the higher count.
    assert dist_value.index("2026-05-20") < dist_value.index("2026-05-19")
    assert "no dominant partition" in field_values["message"]


@pytest.mark.asyncio
async def test_alert_embed_handles_insufficient_rows_without_distribution(
    mocker,
) -> None:
    mocker.patch.object(
        alerts_module.settings,
        "discord_webhook_alerts",
        "https://discord.com/api/webhooks/x/y",
    )
    mock_send = mocker.patch.object(
        alerts_module,
        "send_discord_embed_single",
        new=AsyncMock(return_value=True),
    )

    exc = InsufficientRowsError(
        "kr snapshots_built=50 below floor=2500",
        count=50,
        market="kr",
    )
    result = await send_screener_refresh_alert(
        slot="nxt_final",
        market="kr",
        exception=exc,
        distribution=None,  # caller signals no distribution available
    )

    assert result is True
    embed = mock_send.await_args.kwargs["embed"]
    field_values = {f["name"]: f["value"] for f in embed["fields"]}
    assert "InsufficientRowsError" in field_values["exception"]
    assert field_values["snapshot_date distribution (top 3)"] == "n/a"


# --- Message truncation ------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_message_truncated_when_exception_text_too_long(
    mocker,
) -> None:
    mocker.patch.object(
        alerts_module.settings,
        "discord_webhook_alerts",
        "https://discord.com/api/webhooks/x/y",
    )
    mock_send = mocker.patch.object(
        alerts_module,
        "send_discord_embed_single",
        new=AsyncMock(return_value=True),
    )

    huge_message = "x" * 5000
    exc = RuntimeError(huge_message)
    await send_screener_refresh_alert(slot="post_close", market="us", exception=exc)

    embed = mock_send.await_args.kwargs["embed"]
    message_field = next(f for f in embed["fields"] if f["name"] == "message")
    # 1024 char limit; allow some slack for the ```\n…\n``` wrap.
    assert len(message_field["value"]) <= 1024 + 20
    assert message_field["value"].endswith("…\n```")


# --- Resilience: transport failure must not raise ---------------------------


@pytest.mark.asyncio
async def test_alert_returns_false_on_transport_exception(mocker) -> None:
    mocker.patch.object(
        alerts_module.settings,
        "discord_webhook_alerts",
        "https://discord.com/api/webhooks/x/y",
    )
    mocker.patch.object(
        alerts_module,
        "send_discord_embed_single",
        new=AsyncMock(side_effect=RuntimeError("network broken")),
    )

    # MUST NOT raise — alert delivery failures cannot mask the underlying task
    # failure that triggered the alert.
    result = await send_screener_refresh_alert(
        slot="krx_preliminary",
        market="kr",
        exception=SuspiciousDistributionError("test"),
    )
    assert result is False
