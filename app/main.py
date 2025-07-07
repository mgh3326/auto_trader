from fastapi import FastAPI
from app.core import start_scheduler
from app.routers import telegram, dashboard, health

def create_app() -> FastAPI:
    app = FastAPI(title="KIS Auto Screener", version="0.1.0")
    app.include_router(telegram.router)
    app.include_router(dashboard.router)
    app.include_router(health.router)

    @app.on_event("startup")
    async def _start():
        start_scheduler()          # 백그라운드 작업 시작

    return app

api = create_app()