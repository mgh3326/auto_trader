"""Shared, side-effect-free policy for KR support-proximity snapshots."""

from decimal import Decimal

DEFAULT_MIN_MARKET_CAP_KRW = Decimal("300000000000")  # 3천억원
DEFAULT_MIN_TURNOVER_KRW = Decimal("1000000000")  # 10억원
DEFAULT_CANDIDATE_POOL_LIMIT = 30
MAX_CANDIDATE_POOL_LIMIT = 100
DEFAULT_CONCURRENCY = 4
