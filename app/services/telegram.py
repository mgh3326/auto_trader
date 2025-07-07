import httpx
from app.core.config import settings

BASE = f"https://api.telegram.org/bot{settings.telegram_token}"

async def send(text: str):
    async with httpx.AsyncClient() as cli:
        await cli.post(f"{BASE}/sendMessage", json={
            "chat_id": cid, "text": text, "parse_mode":"MarkdownV2"
        } for cid in settings.telegram_chat_ids)