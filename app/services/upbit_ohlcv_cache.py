import asyncio
import json
import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta, timezone

import pandas as pd
import redis.asyncio as redis

from app.core.config import settings
from app.services import upbit as upbit_service

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_EMPTY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]

_REDIS_CLIENT: redis.Redis | None = None
_FALLBACK_COUNT = 0


def get_target_closed_date_kst(now: datetime | None = None) -> date:
    base_now = now or datetime.now(UTC)
    return base_now.astimezone(_KST).date() - timedelta(days=1)


def _epoch_day(value: date) -> int:
    return int(
        datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() // 86400
    )


def _base_key(market: str) -> str:
    return f"upbit:ohlcv:day:v1:{market}"


def _keys(market: str) -> tuple[str, str, str, str]:
    base = _base_key(market)
    return f"{base}:dates", f"{base}:rows", f"{base}:meta", f"{base}:lock"


def _normalize_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _empty_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_EMPTY_COLUMNS)


def _to_json_value(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value


async def _get_redis_client() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = redis.from_url(
            settings.get_redis_url(),
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            decode_responses=True,
        )
    return _REDIS_CLIENT


async def close_ohlcv_cache_redis() -> None:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        await _REDIS_CLIENT.close()
        _REDIS_CLIENT = None


async def _read_cached_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    target_closed_date: date,
    count: int,
) -> pd.DataFrame:
    if count <= 0:
        return _empty_dataframe()

    date_fields = await redis_client.zrevrangebyscore(
        dates_key,
        _epoch_day(target_closed_date),
        "-inf",
        start=0,
        num=count,
    )
    if not date_fields:
        return _empty_dataframe()

    row_payloads = await redis_client.hmget(rows_key, date_fields)
    rows: list[dict[str, object]] = []
    for field, payload in zip(date_fields, row_payloads, strict=False):
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        date_value = parsed.get("date", field)
        try:
            parsed["date"] = date.fromisoformat(str(date_value))
        except ValueError:
            continue
        rows.append(parsed)

    if not rows:
        return _empty_dataframe()

    frame = pd.DataFrame(rows)
    for column in _EMPTY_COLUMNS:
        if column not in frame.columns:
            frame[column] = None

    return frame.loc[:, _EMPTY_COLUMNS].sort_values("date").reset_index(drop=True)


async def _acquire_lock(
    redis_client: redis.Redis,
    lock_key: str,
    ttl_seconds: int,
) -> str | None:
    lock_token = f"{uuid.uuid4()}"
    acquired = await redis_client.set(
        lock_key,
        lock_token,
        nx=True,
        ex=max(int(ttl_seconds), 1),
    )
    if acquired:
        return lock_token
    return None


async def _release_lock(
    redis_client: redis.Redis,
    lock_key: str,
    lock_token: str,
) -> None:
    release_script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    else
        return 0
    end
    """
    try:
        await redis_client.eval(release_script, 1, lock_key, lock_token)
    except Exception:
        return


async def _enforce_retention_limit(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    max_days: int,
) -> int:
    if max_days <= 0:
        return 0

    total_count = int(await redis_client.zcard(dates_key))
    overflow = total_count - max_days
    if overflow <= 0:
        return 0

    stale_dates = await redis_client.zrange(dates_key, 0, overflow - 1)
    if not stale_dates:
        return 0

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.zremrangebyrank(dates_key, 0, overflow - 1)
    pipeline.hdel(rows_key, *stale_dates)
    await pipeline.execute()
    return len(stale_dates)


async def _upsert_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    frame: pd.DataFrame,
) -> int:
    if frame.empty:
        return 0

    zadd_mapping: dict[str, int] = {}
    hset_mapping: dict[str, str] = {}

    for row in frame.itertuples(index=False):
        row_date = getattr(row, "date", None)
        if row_date is None:
            continue
        if not isinstance(row_date, date):
            try:
                row_date = pd.to_datetime(row_date).date()
            except Exception:
                continue

        field = row_date.isoformat()
        zadd_mapping[field] = _epoch_day(row_date)
        payload = {
            "date": field,
            "open": _to_json_value(getattr(row, "open", None)),
            "high": _to_json_value(getattr(row, "high", None)),
            "low": _to_json_value(getattr(row, "low", None)),
            "close": _to_json_value(getattr(row, "close", None)),
            "volume": _to_json_value(getattr(row, "volume", None)),
            "value": _to_json_value(getattr(row, "value", None)),
        }
        hset_mapping[field] = json.dumps(payload)

    if not zadd_mapping or not hset_mapping:
        return 0

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.zadd(dates_key, zadd_mapping)
    pipeline.hset(rows_key, mapping=hset_mapping)
    await pipeline.execute()
    return len(zadd_mapping)


async def _read_oldest_date(redis_client: redis.Redis, dates_key: str) -> date | None:
    oldest_dates = await redis_client.zrange(dates_key, 0, 0)
    if not oldest_dates:
        return None
    try:
        return date.fromisoformat(oldest_dates[0])
    except ValueError:
        return None


async def _read_latest_date(redis_client: redis.Redis, dates_key: str) -> date | None:
    latest_dates = await redis_client.zrevrangebyscore(
        dates_key,
        "+inf",
        "-inf",
        start=0,
        num=1,
    )
    if not latest_dates:
        return None
    try:
        return date.fromisoformat(latest_dates[0])
    except ValueError:
        return None


async def _read_cache_status(
    redis_client: redis.Redis,
    dates_key: str,
    meta_key: str,
    target_closed_date: date,
) -> tuple[int, date | None, bool]:
    cached_count = int(
        await redis_client.zcount(dates_key, "-inf", _epoch_day(target_closed_date))
    )
    latest_cached_date = await _read_latest_date(redis_client, dates_key)
    meta = await redis_client.hgetall(meta_key)
    oldest_confirmed = _normalize_bool(meta.get("oldest_confirmed"))
    return cached_count, latest_cached_date, oldest_confirmed


def _is_cache_sufficient(
    cached_count: int,
    latest_cached_date: date | None,
    oldest_confirmed: bool,
    requested_count: int,
    target_closed_date: date,
) -> bool:
    has_latest_closed = (
        latest_cached_date is not None and latest_cached_date >= target_closed_date
    )
    if not has_latest_closed:
        return False
    if cached_count >= requested_count:
        return True
    return oldest_confirmed


async def _refresh_meta(
    redis_client: redis.Redis,
    dates_key: str,
    meta_key: str,
    target_closed_date: date,
    oldest_confirmed: bool,
) -> None:
    oldest_date = await _read_oldest_date(redis_client, dates_key)
    mapping = {
        "last_closed_date": target_closed_date.isoformat(),
        "oldest_date": oldest_date.isoformat() if oldest_date else "",
        "oldest_confirmed": "true" if oldest_confirmed else "false",
        "last_sync_ts": str(int(datetime.now(UTC).timestamp())),
    }
    await redis_client.hset(meta_key, mapping=mapping)


async def _backfill_until_satisfied(
    redis_client: redis.Redis,
    market: str,
    requested_count: int,
    target_closed_date: date,
    dates_key: str,
    rows_key: str,
    meta_key: str,
    max_days: int,
) -> None:
    meta = await redis_client.hgetall(meta_key)
    oldest_confirmed = _normalize_bool(meta.get("oldest_confirmed"))

    # Stage A: keep cache fresh by filling newest closed candles first.
    while True:
        latest_cached_date = await _read_latest_date(redis_client, dates_key)
        if latest_cached_date is not None and latest_cached_date >= target_closed_date:
            break

        if latest_cached_date is None:
            batch_size = min(max(requested_count, 1), 200)
        else:
            missing_latest_days = (target_closed_date - latest_cached_date).days
            batch_size = min(max(missing_latest_days, 1), 200)

        fetched = await upbit_service.fetch_ohlcv(
            market=market,
            days=batch_size,
            period="day",
            end_date=datetime.combine(target_closed_date, time(23, 59, 59)),
        )
        if fetched.empty or "date" not in fetched.columns:
            break

        fetched = fetched[fetched["date"] <= target_closed_date]
        if fetched.empty:
            break

        inserted_count = await _upsert_rows(redis_client, dates_key, rows_key, fetched)
        logger.info(
            "upbit_ohlcv_cache forward_fill market=%s rows=%d requested=%d",
            market,
            inserted_count,
            batch_size,
        )

        trimmed_count = await _enforce_retention_limit(
            redis_client,
            dates_key,
            rows_key,
            max_days,
        )
        if trimmed_count > 0:
            logger.info(
                "upbit_ohlcv_cache trimmed market=%s removed=%d",
                market,
                trimmed_count,
            )

        latest_after = await _read_latest_date(redis_client, dates_key)
        if latest_cached_date is not None and (
            latest_after is None or latest_after <= latest_cached_date
        ):
            break

        if len(fetched) < batch_size:
            break

    # Stage B: fetch older candles only when depth is insufficient.
    while True:
        cached_count, latest_cached_date, oldest_confirmed = await _read_cache_status(
            redis_client,
            dates_key,
            meta_key,
            target_closed_date,
        )
        if _is_cache_sufficient(
            cached_count,
            latest_cached_date,
            oldest_confirmed,
            requested_count,
            target_closed_date,
        ):
            await _refresh_meta(
                redis_client,
                dates_key,
                meta_key,
                target_closed_date,
                oldest_confirmed,
            )
            return
        if oldest_confirmed:
            break

        earliest_cached_date = await _read_oldest_date(redis_client, dates_key)
        batch_end_date = (
            earliest_cached_date - timedelta(days=1)
            if earliest_cached_date is not None
            else target_closed_date
        )
        if batch_end_date > target_closed_date:
            batch_end_date = target_closed_date

        remaining = requested_count - cached_count
        batch_size = min(max(remaining, 1), 200)

        fetched = await upbit_service.fetch_ohlcv(
            market=market,
            days=batch_size,
            period="day",
            end_date=datetime.combine(batch_end_date, time(23, 59, 59)),
        )
        if fetched.empty or "date" not in fetched.columns:
            if not oldest_confirmed:
                logger.info(
                    "upbit_ohlcv_cache oldest_confirmed enabled market=%s reason=empty_batch",
                    market,
                )
            oldest_confirmed = True
            break

        fetched = fetched[fetched["date"] <= target_closed_date]
        if fetched.empty:
            if not oldest_confirmed:
                logger.info(
                    "upbit_ohlcv_cache oldest_confirmed enabled market=%s reason=no_closed_rows",
                    market,
                )
            oldest_confirmed = True
            break

        inserted_count = await _upsert_rows(redis_client, dates_key, rows_key, fetched)
        logger.info(
            "upbit_ohlcv_cache backfill market=%s rows=%d requested=%d",
            market,
            inserted_count,
            batch_size,
        )

        trimmed_count = await _enforce_retention_limit(
            redis_client,
            dates_key,
            rows_key,
            max_days,
        )
        if trimmed_count > 0:
            logger.info(
                "upbit_ohlcv_cache trimmed market=%s removed=%d",
                market,
                trimmed_count,
            )

        if len(fetched) < batch_size:
            if not oldest_confirmed:
                logger.info(
                    "upbit_ohlcv_cache oldest_confirmed enabled market=%s reason=short_batch returned=%d requested=%d",
                    market,
                    len(fetched),
                    batch_size,
                )
            oldest_confirmed = True
            break

    await _refresh_meta(
        redis_client,
        dates_key,
        meta_key,
        target_closed_date,
        oldest_confirmed,
    )


async def get_closed_daily_candles(market: str, count: int) -> pd.DataFrame | None:
    if not settings.upbit_ohlcv_cache_enabled:
        return None

    normalized_market = str(market or "").strip().upper()
    if not normalized_market:
        return _empty_dataframe()

    requested_count = int(count)
    if requested_count <= 0:
        return _empty_dataframe()

    max_days = max(int(settings.upbit_ohlcv_cache_max_days), 1)
    requested_count = min(requested_count, max_days)

    try:
        redis_client = await _get_redis_client()
        dates_key, rows_key, meta_key, lock_key = _keys(normalized_market)
        target_closed_date = get_target_closed_date_kst()

        trimmed_count = await _enforce_retention_limit(
            redis_client,
            dates_key,
            rows_key,
            max_days,
        )
        if trimmed_count > 0:
            logger.info(
                "upbit_ohlcv_cache trimmed market=%s removed=%d",
                normalized_market,
                trimmed_count,
            )

        cached = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            target_closed_date,
            requested_count,
        )
        (
            cached_count,
            latest_cached_date,
            oldest_confirmed,
        ) = await _read_cache_status(
            redis_client,
            dates_key,
            meta_key,
            target_closed_date,
        )
        if _is_cache_sufficient(
            cached_count,
            latest_cached_date,
            oldest_confirmed,
            requested_count,
            target_closed_date,
        ):
            await _refresh_meta(
                redis_client,
                dates_key,
                meta_key,
                target_closed_date,
                oldest_confirmed,
            )
            logger.info(
                "upbit_ohlcv_cache hit market=%s cached=%d requested=%d",
                normalized_market,
                len(cached),
                requested_count,
            )
            return cached.tail(requested_count).reset_index(drop=True)

        logger.info(
            "upbit_ohlcv_cache miss market=%s cached=%d requested=%d",
            normalized_market,
            len(cached),
            requested_count,
        )

        lock_token = await _acquire_lock(
            redis_client,
            lock_key,
            settings.upbit_ohlcv_cache_lock_ttl_seconds,
        )
        if lock_token is None:
            for _ in range(2):
                await asyncio.sleep(0.1)
                lock_token = await _acquire_lock(
                    redis_client,
                    lock_key,
                    settings.upbit_ohlcv_cache_lock_ttl_seconds,
                )
                if lock_token is not None:
                    break
            if lock_token is None:
                refreshed_cached = await _read_cached_rows(
                    redis_client,
                    dates_key,
                    rows_key,
                    target_closed_date,
                    requested_count,
                )
                (
                    refreshed_count,
                    refreshed_latest_date,
                    refreshed_oldest_confirmed,
                ) = await _read_cache_status(
                    redis_client,
                    dates_key,
                    meta_key,
                    target_closed_date,
                )
                if _is_cache_sufficient(
                    refreshed_count,
                    refreshed_latest_date,
                    refreshed_oldest_confirmed,
                    requested_count,
                    target_closed_date,
                ):
                    return refreshed_cached.tail(requested_count).reset_index(drop=True)
                return None

        try:
            await _backfill_until_satisfied(
                redis_client,
                normalized_market,
                requested_count,
                target_closed_date,
                dates_key,
                rows_key,
                meta_key,
                max_days,
            )
        finally:
            await _release_lock(redis_client, lock_key, lock_token)

        final_rows = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            target_closed_date,
            requested_count,
        )
        return final_rows.tail(requested_count).reset_index(drop=True)

    except Exception as exc:
        global _FALLBACK_COUNT
        _FALLBACK_COUNT += 1
        logger.warning(
            "upbit_ohlcv_cache fallback market=%s fallback_count=%d error=%s",
            normalized_market,
            _FALLBACK_COUNT,
            exc,
        )
        return None


__all__ = [
    "close_ohlcv_cache_redis",
    "get_closed_daily_candles",
    "get_target_closed_date_kst",
]
