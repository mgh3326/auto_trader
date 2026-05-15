def test_horizon_constants_exist_and_are_independent_of_display_clamp():
    from app.services.brokers.kis.constants import DEFAULT_CANDLES
    from app.services.daily_candles.constants import (
        DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
        DAILY_CANDLE_BACKFILL_BARS_KR,
        DAILY_CANDLE_BACKFILL_BARS_US,
    )

    # All horizons must be strictly larger than the display safety clamp
    # because their entire reason for existing is to permit longer batch
    # backfills than the ad-hoc display path.
    assert DAILY_CANDLE_BACKFILL_BARS_KR > DEFAULT_CANDLES
    assert DAILY_CANDLE_BACKFILL_BARS_US > DEFAULT_CANDLES
    assert DAILY_CANDLE_BACKFILL_BARS_CRYPTO > DEFAULT_CANDLES


def test_horizon_constants_are_ints():
    from app.services.daily_candles.constants import (
        DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
        DAILY_CANDLE_BACKFILL_BARS_KR,
        DAILY_CANDLE_BACKFILL_BARS_US,
    )

    for value in (
        DAILY_CANDLE_BACKFILL_BARS_KR,
        DAILY_CANDLE_BACKFILL_BARS_US,
        DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
    ):
        assert isinstance(value, int)
        assert value > 0


def test_scheduled_sync_horizons_are_smaller_than_backfill_horizons():
    from app.services.daily_candles.constants import (
        DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
        DAILY_CANDLE_BACKFILL_BARS_KR,
        DAILY_CANDLE_BACKFILL_BARS_US,
        DAILY_CANDLE_SYNC_BARS_CRYPTO,
        DAILY_CANDLE_SYNC_BARS_KR,
        DAILY_CANDLE_SYNC_BARS_US,
    )

    assert 0 < DAILY_CANDLE_SYNC_BARS_KR < DAILY_CANDLE_BACKFILL_BARS_KR
    assert 0 < DAILY_CANDLE_SYNC_BARS_US < DAILY_CANDLE_BACKFILL_BARS_US
    assert 0 < DAILY_CANDLE_SYNC_BARS_CRYPTO < DAILY_CANDLE_BACKFILL_BARS_CRYPTO
