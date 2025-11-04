from fastapi import FastAPI

from app.routers import dashboard, health, analysis_json, stock_latest, upbit_trading


def create_app() -> FastAPI:
    app = FastAPI(title="KIS Auto Screener", version="0.1.0")
    # app.include_router(telegram.router)
    app.include_router(dashboard.router)
    app.include_router(health.router)
    app.include_router(analysis_json.router)
    app.include_router(stock_latest.router)
    app.include_router(upbit_trading.router)

    return app


api = create_app()
