"""Batch-ingest horizon constants for the daily candle store.

These constants govern how many bars per symbol the daily candle batch
ingest job and the initial backfill CLI are permitted to request from
the external API. They are intentionally separate from the wrapper-level
safety clamp `app.services.brokers.kis.constants.DEFAULT_CANDLES`, which
protects ad-hoc MCP/API display calls (`get_ohlcv(count)` style) from
accidentally requesting huge windows.

Raising these values does not raise the display clamp; the two knobs
remain independent on purpose.
"""

DAILY_CANDLE_BACKFILL_BARS_KR: int = 400
DAILY_CANDLE_BACKFILL_BARS_US: int = 400
DAILY_CANDLE_BACKFILL_BARS_CRYPTO: int = 400

# Daily scheduled syncs are intentionally incremental. Full 400-bar windows are
# for explicit backfill only; running them for every active symbol on every cron
# tick would put unnecessary pressure on provider rate limits.
DAILY_CANDLE_SYNC_BARS_KR: int = 10
DAILY_CANDLE_SYNC_BARS_US: int = 10
DAILY_CANDLE_SYNC_BARS_CRYPTO: int = 10
