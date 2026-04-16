"""TC Briefing Discord delivery service.

Sends structured briefing embeds to a Discord channel via the
Discord Channel Messages API (Bot token auth), matching the pattern
used in the Boss Action Queue n8n workflow.
"""

from __future__ import annotations

import logging
import os

import httpx

from app.schemas.n8n.tc_briefing import (
    N8nTcBriefingCategory,
    N8nTcBriefingRequest,
)

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"

# Embed colour per category (decimal)
CATEGORY_COLOURS = {
    "매도": 0xE74C3C,  # red
    "매수": 0x2ECC71,  # green
    "홀드": 0x3498DB,  # blue
    "추가매수": 0xF1C40F,  # yellow
}

CATEGORY_EMOJI = {
    "매도": "\U0001f534",  # red circle
    "매수": "\U0001f7e2",  # green circle
    "홀드": "\U0001f535",  # blue circle
    "추가매수": "\U0001f7e1",  # yellow circle
}


def _build_embeds(request: N8nTcBriefingRequest) -> list[dict]:
    """Build Discord embed objects from briefing categories."""
    embeds: list[dict] = []

    # Title embed
    embeds.append(
        {
            "title": request.title,
            "description": f"`{request.issue_identifier}`",
            "color": 0x9B59B6,  # purple
        }
    )

    for cat in request.briefing_items:
        embed = _build_category_embed(cat, request.paperclip_issue_url)
        if embed:
            embeds.append(embed)

    return embeds


def _build_category_embed(
    cat: N8nTcBriefingCategory,
    paperclip_url: str | None,
) -> dict | None:
    if not cat.items:
        return None

    emoji = CATEGORY_EMOJI.get(cat.category, "\u2022")
    colour = CATEGORY_COLOURS.get(cat.category, 0x95A5A6)

    lines: list[str] = []
    for item in cat.items:
        lines.append(f"**{item.name}** (`{item.symbol}`)")
        lines.append(f"> {item.reason_summary}")
        lines.append("")

    embed: dict = {
        "title": f"{emoji} {cat.category} ({len(cat.items)}건)",
        "description": "\n".join(lines).rstrip(),
        "color": colour,
    }

    if paperclip_url:
        embed["footer"] = {"text": "Paperclip에서 결정하기"}
        embed["url"] = paperclip_url

    return embed


def _build_components(paperclip_url: str | None) -> list[dict] | None:
    """Build Discord message components (button row)."""
    if not paperclip_url:
        return None

    return [
        {
            "type": 1,  # Action Row
            "components": [
                {
                    "type": 2,  # Button
                    "style": 5,  # Link
                    "label": "Paperclip에서 결정하기",
                    "url": paperclip_url,
                }
            ],
        }
    ]


async def send_tc_briefing(
    request: N8nTcBriefingRequest,
) -> dict:
    """Send TC briefing to Discord channel.

    Returns dict with ``message_id`` on success, or ``error`` on failure.
    Uses Discord Channel Messages API with Bot token authentication.
    """
    bot_token = os.getenv("DISCORD_TC_BRIEFING_BOT_TOKEN", "")
    channel_id = os.getenv("DISCORD_TC_BRIEFING_CHANNEL_ID", "")

    if not bot_token or not channel_id:
        missing = []
        if not bot_token:
            missing.append("DISCORD_TC_BRIEFING_BOT_TOKEN")
        if not channel_id:
            missing.append("DISCORD_TC_BRIEFING_CHANNEL_ID")
        logger.warning("TC briefing skipped — missing env: %s", ", ".join(missing))
        return {"message_id": None, "error": f"Missing env: {', '.join(missing)}"}

    embeds = _build_embeds(request)
    components = _build_components(request.paperclip_issue_url)

    # Discord allows max 10 embeds per message
    if len(embeds) > 10:
        embeds = embeds[:10]

    payload: dict = {"embeds": embeds}
    if components:
        payload["components"] = components

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            message_id = data.get("id")
            logger.info("TC briefing sent to Discord — message_id=%s", message_id)
            return {"message_id": message_id}
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response else ""
            logger.error("Discord API error %s: %s", exc.response.status_code, body)
            return {
                "message_id": None,
                "error": f"Discord API {exc.response.status_code}: {body}",
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send TC briefing to Discord: %s", exc)
            return {"message_id": None, "error": str(exc)}
