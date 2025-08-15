from fastapi import FastAPI

from app.routers import health, dashboard


def create_app() -> FastAPI:
    app = FastAPI(title="KIS Auto Screener", version="0.1.0")
    # app.include_router(telegram.router)
    app.include_router(dashboard.router)
    app.include_router(health.router)

    return app


api = create_app()
