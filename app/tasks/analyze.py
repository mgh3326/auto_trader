import asyncio
from typing import Optional

from celery import shared_task

from app.analysis.service_analyzers import KISAnalyzer, YahooAnalyzer, UpbitAnalyzer


@shared_task(name="analyze.run_for_stock")
def run_analysis_for_stock(symbol: str, name: str, instrument_type: str) -> dict:
    """Bridge Celery task to run the existing async analyzers.

    This runs the appropriate analyzer based on instrument_type and persists results
    using existing analyzer code paths. Returns a minimal status payload.
    """

    async def _run() -> dict:
        analyzer = None
        try:
            if instrument_type == "equity_kr":
                analyzer = KISAnalyzer()
                await analyzer.analyze_stock_json(name)
            elif instrument_type == "equity_us":
                analyzer = YahooAnalyzer()
                await analyzer.analyze_stock_json(symbol)
            elif instrument_type == "crypto":
                analyzer = UpbitAnalyzer()
                await analyzer.analyze_coin_json(name)
            else:
                return {"status": "ignored", "reason": f"unsupported type: {instrument_type}"}

            return {"status": "ok", "symbol": symbol, "name": name, "instrument_type": instrument_type}
        finally:
            if analyzer and hasattr(analyzer, "close"):
                await analyzer.close()

    # Run the async analyzer in a new event loop isolated from worker's default
    return asyncio.run(_run())



