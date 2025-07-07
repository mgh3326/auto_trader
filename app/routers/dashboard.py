# app/routers/dashboard.py
from pathlib import Path
from typing import List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# templates 폴더를 프로젝트 루트(api 코드와 같은 레벨)에 둔다고 가정
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    """
    간단한 대시보드 홈. 추후 Jinja → React/Vue SPA 교체 가능.
    """
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "KIS Auto Screener",
        },
    )


# ───────── API 예시: 최근 알림 목록 ──────────
# jobs.screener.DedupCache 같은 in-memory 구조를 가져다 쓰는 예시
try:
    from app.jobs.screener import dedup  # 최근 알림을 보관하는 LRUCache
except ModuleNotFoundError:
    dedup = None


@router.get("/api/alerts", response_model=List[str])
async def latest_alerts(limit: int = 20):
    """
    최근 전송된 Telegram 알림 n개를 JSON으로 반환.
    추후 DB/Redis로 교체해도 라우터는 유지 가능.
    """
    if dedup is None:
        return []
    return list(reversed(dedup.tail(limit)))