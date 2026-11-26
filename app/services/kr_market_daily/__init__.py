"""Read and collect append-only market-wide KR daily facts."""

from app.services.kr_market_daily.reader import read_market_daily_context

__all__ = ["read_market_daily_context"]
