from fastapi import FastAPI

from app.routers import dashboard, health, analysis_json


def create_app() -> FastAPI:
    app = FastAPI(title="KIS Auto Screener", version="0.1.0")
    # app.include_router(telegram.router)
    app.include_router(dashboard.router)
    app.include_router(health.router)
    app.include_router(analysis_json.router)

    return app


api = create_app()
