"""Public, venue-separated crypto OHLCV corpus builder.

This package is intentionally research-only: it has no application, database,
broker, account, scheduler, credential, or LLM dependencies.
"""

from .constants import CORPUS_ID

__all__ = ["CORPUS_ID"]
