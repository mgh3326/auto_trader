from fastapi import APIRouter
from pydantic import BaseModel
from app.jobs.screener import screen_once_async
from app.services.telegram import send

router = APIRouter(prefix="/telegram", tags=["Telegram"])


class Update(BaseModel):
    message: dict


@router.post("/webhook")
async def webhook(update: Update):
    text = update.message.get("text", "")
    if text == "/run":
        await send("⏳ 수동 스크리닝 실행")
        await screen_once_async()
    return {"ok": True}
