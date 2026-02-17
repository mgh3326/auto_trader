from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_EMPTY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]

_REDIS_CLIENT: redis.Redis | None = None


def expected_asof_et(now_utc: datetime) -> date:
    base = now_utc
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    et_now = base.astimezone(_ET)

    anchor = (
        et_now.date()
        if et_now.time() >= time(16, 0)
        else et_now.date() - timedelta(days=1)
    )

    while anchor.weekday() >= 5:
        anchor -= timedelta(days=1)
    return anchor


def _epoch_day(value: date) -> int:
    return int(
        datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() // 86400
    )


def _empty_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_EMPTY_COLUMNS)


def _to_json_value(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _keys(
    symbol: str, exchange_code: str, instrument_type: str
) -> tuple[str, str, str, str]:
    base = (
        "kis:ohlcv:day:v1:"
        f"{instrument_type.lower()}:{exchange_code.upper()}:{symbol.upper()}"
    )
    return f"{base}:dates", f"{base}:rows", f"{base}:meta", f"{base}:lock"


def _legacy_keys(
    symbol: str, exchange_code: str, instrument_type: str
) -> tuple[str, str, str, str]:
    base = (
        f"kis:ohlcv:{instrument_type.lower()}:day:v1:"
        f"{exchange_code.upper()}:{symbol.upper()}"
    )
    return f"{base}:dates", f"{base}:rows", f"{base}:meta", f"{base}:lock"


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


async def close_kis_ohlcv_cache_redis() -> None:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        await _REDIS_CLIENT.close()
        _REDIS_CLIENT = None


async def _read_cached_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    target_asof: date,
    count: int,
) -> pd.DataFrame:
    if count <= 0:
        return _empty_dataframe()

    fields = await redis_client.zrevrangebyscore(
        dates_key,
        _epoch_day(target_asof),
        "-inf",
        start=0,
        num=count,
    )
    if not fields:
        return _empty_dataframe()

    payloads = await redis_client.hmget(rows_key, fields)
    rows: list[dict[str, object]] = []
    for field, payload in zip(fields, payloads, strict=False):
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        row_date = parsed.get("date", field)
        try:
            parsed["date"] = date.fromisoformat(str(row_date))
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


async def _read_latest_date(redis_client: redis.Redis, dates_key: str) -> date | None:
    latest = await redis_client.zrevrangebyscore(
        dates_key, "+inf", "-inf", start=0, num=1
    )
    if not latest:
        return None
    try:
        return date.fromisoformat(latest[0])
    except ValueError:
        return None


async def _acquire_lock(
    redis_client: redis.Redis,
    lock_key: str,
    ttl_seconds: int,
) -> str | None:
    token = str(uuid.uuid4())
    acquired = await redis_client.set(
        lock_key, token, nx=True, ex=max(int(ttl_seconds), 1)
    )
    return token if acquired else None


async def _release_lock(redis_client: redis.Redis, lock_key: str, token: str) -> None:
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    else
        return 0
    end
    """
    try:
        await redis_client.eval(script, 1, lock_key, token)
    except Exception:
        return


async def _trim_retention(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    max_days: int,
) -> None:
    if max_days <= 0:
        return
    total = int(await redis_client.zcard(dates_key))
    overflow = total - max_days
    if overflow <= 0:
        return

    stale_dates = await redis_client.zrange(dates_key, 0, overflow - 1)
    if not stale_dates:
        return

    pipe = redis_client.pipeline(transaction=True)
    pipe.zremrangebyrank(dates_key, 0, overflow - 1)
    pipe.hdel(rows_key, *stale_dates)
    await pipe.execute()


def _normalize_daily_frame(frame: pd.DataFrame, target_asof: date) -> pd.DataFrame:
    if frame.empty:
        return _empty_dataframe()

    normalized = frame.copy()
    if "date" not in normalized.columns:
        return _empty_dataframe()

    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce").dt.date
    normalized = normalized.dropna(subset=["date"])  # type: ignore[arg-type]
    if normalized.empty:
        return _empty_dataframe()

    normalized = normalized[normalized["date"] <= target_asof]
    if normalized.empty:
        return _empty_dataframe()

    for column in _EMPTY_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = None

    return (
        normalized.loc[:, _EMPTY_COLUMNS]
        .drop_duplicates(subset=["date"], keep="first")
        .sort_values("date")
        .reset_index(drop=True)
    )


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
        if not isinstance(row_date, date):
            continue

        field = row_date.isoformat()
        zadd_mapping[field] = _epoch_day(row_date)
        hset_mapping[field] = json.dumps(
            {
                "date": field,
                "open": _to_json_value(getattr(row, "open", None)),
                "high": _to_json_value(getattr(row, "high", None)),
                "low": _to_json_value(getattr(row, "low", None)),
                "close": _to_json_value(getattr(row, "close", None)),
                "volume": _to_json_value(getattr(row, "volume", None)),
                "value": _to_json_value(getattr(row, "value", None)),
            }
        )

    if not zadd_mapping:
        return 0

    pipe = redis_client.pipeline(transaction=True)
    pipe.zadd(dates_key, zadd_mapping)
    pipe.hset(rows_key, mapping=hset_mapping)
    await pipe.execute()
    return len(zadd_mapping)


def _cache_hit_sufficient(
    cached: pd.DataFrame,
    latest_cached_date: date | None,
    target_asof: date,
    requested_count: int,
) -> bool:
    if latest_cached_date is None or latest_cached_date < target_asof:
        return False
    return len(cached) >= requested_count


async def _set_probe_meta(
    redis_client: redis.Redis,
    meta_key: str,
    target_asof: date,
    now_utc: datetime,
) -> None:
    retry_seconds = max(int(settings.kis_ohlcv_cache_probe_retry_seconds), 1)
    until_ts = int(now_utc.timestamp()) + retry_seconds
    await redis_client.hset(
        meta_key,
        mapping={
            "last_probe_target_asof": target_asof.isoformat(),
            # Backward compatibility for one release window.
            "last_probe_asof": target_asof.isoformat(),
            "probe_until_ts": str(until_ts),
            "last_probe_ts": str(int(now_utc.timestamp())),
        },
    )
    await redis_client.expire(meta_key, max(int(settings.kis_ohlcv_cache_ttl_seconds), 1))


def _probe_target_from_meta(meta: dict[str, str]) -> str | None:
    return meta.get("last_probe_target_asof") or meta.get("last_probe_asof")


def _is_probe_active(meta: dict[str, str], target_asof: date, now_utc: datetime) -> bool:
    if not meta:
        return False
    if _probe_target_from_meta(meta) != target_asof.isoformat():
        return False
    try:
        return int(meta.get("probe_until_ts", "0")) > int(now_utc.timestamp())
    except ValueError:
        return False


async def _should_skip_probe(
    redis_client: redis.Redis,
    primary_meta_key: str,
    legacy_meta_key: str,
    target_asof: date,
    now_utc: datetime,
) -> bool:
    primary_meta = await redis_client.hgetall(primary_meta_key)
    if _is_probe_active(primary_meta, target_asof, now_utc):
        return True

    legacy_meta = await redis_client.hgetall(legacy_meta_key)
    return _is_probe_active(legacy_meta, target_asof, now_utc)


async def get_closed_daily_candles(
    *,
    symbol: str,
    exchange_code: str,
    count: int,
    instrument_type: str,
    raw_fetcher: Callable[..., Awaitable[pd.DataFrame]],
    now_utc: datetime | None = None,
) -> pd.DataFrame | None:
    if not settings.kis_ohlcv_cache_enabled:
        return None

    normalized_symbol = str(symbol or "").strip().upper()
    normalized_exchange = str(exchange_code or "").strip().upper()
    if not normalized_symbol or not normalized_exchange:
        return _empty_dataframe()

    requested_count = int(count)
    if requested_count <= 0:
        return _empty_dataframe()

    max_days = max(int(settings.kis_ohlcv_cache_max_days), 1)
    requested_count = min(requested_count, max_days)

    base_now_utc = now_utc or datetime.now(UTC)
    if base_now_utc.tzinfo is None:
        base_now_utc = base_now_utc.replace(tzinfo=UTC)

    target_asof = expected_asof_et(base_now_utc)

    try:
        redis_client = await _get_redis_client()
    except Exception as exc:
        logger.warning("kis_ohlcv_cache redis unavailable: %s", exc)
        return None

    try:
        dates_key, rows_key, meta_key, lock_key = _keys(
            normalized_symbol,
            normalized_exchange,
            instrument_type,
        )
        legacy_dates_key, legacy_rows_key, legacy_meta_key, _ = _legacy_keys(
            normalized_symbol,
            normalized_exchange,
            instrument_type,
        )

        await _trim_retention(redis_client, dates_key, rows_key, max_days)
        await _trim_retention(redis_client, legacy_dates_key, legacy_rows_key, max_days)

        cached_primary = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            target_asof,
            requested_count,
        )
        latest_cached_primary = await _read_latest_date(redis_client, dates_key)
        if _cache_hit_sufficient(
            cached_primary,
            latest_cached_primary,
            target_asof,
            requested_count,
        ):
            return cached_primary.tail(requested_count).reset_index(drop=True)

        cached_legacy = await _read_cached_rows(
            redis_client,
            legacy_dates_key,
            legacy_rows_key,
            target_asof,
            requested_count,
        )
        latest_cached_legacy = await _read_latest_date(redis_client, legacy_dates_key)
        if _cache_hit_sufficient(
            cached_legacy,
            latest_cached_legacy,
            target_asof,
            requested_count,
        ):
            return cached_legacy.tail(requested_count).reset_index(drop=True)

        fallback_cached = (
            cached_primary if len(cached_primary) >= len(cached_legacy) else cached_legacy
        )

        if await _should_skip_probe(
            redis_client,
            meta_key,
            legacy_meta_key,
            target_asof,
            base_now_utc,
        ):
            return fallback_cached.tail(requested_count).reset_index(drop=True)

        lock_token = await _acquire_lock(
            redis_client,
            lock_key,
            settings.kis_ohlcv_cache_lock_ttl_seconds,
        )

        if lock_token is None:
            for _ in range(6):
                await asyncio.sleep(0.05)
                refreshed = await _read_cached_rows(
                    redis_client,
                    dates_key,
                    rows_key,
                    target_asof,
                    requested_count,
                )
                refreshed_latest = await _read_latest_date(redis_client, dates_key)
                if _cache_hit_sufficient(
                    refreshed,
                    refreshed_latest,
                    target_asof,
                    requested_count,
                ):
                    return refreshed.tail(requested_count).reset_index(drop=True)
            return fallback_cached.tail(requested_count).reset_index(drop=True)

        try:
            fetched = await raw_fetcher(
                symbol=normalized_symbol,
                exchange_code=normalized_exchange,
                n=requested_count,
                end_date=target_asof,
            )
            normalized = _normalize_daily_frame(fetched, target_asof)

            if normalized.empty:
                await _set_probe_meta(redis_client, meta_key, target_asof, base_now_utc)
            else:
                await _upsert_rows(redis_client, dates_key, rows_key, normalized)
                await _trim_retention(redis_client, dates_key, rows_key, max_days)
                synced_latest = await _read_latest_date(redis_client, dates_key)
                if synced_latest is None or synced_latest < target_asof:
                    await _set_probe_meta(
                        redis_client,
                        meta_key,
                        target_asof,
                        base_now_utc,
                    )
                else:
                    latest_asof = synced_latest.isoformat()
                    await redis_client.hset(
                        meta_key,
                        mapping={
                            "latest_asof": latest_asof,
                            # Backward compatibility for one release window.
                            "last_sync_asof": latest_asof,
                            "last_sync_ts": str(int(base_now_utc.timestamp())),
                            "probe_until_ts": "0",
                            "last_probe_target_asof": "",
                            "last_probe_asof": "",
                        },
                    )
                    await redis_client.expire(
                        meta_key, max(int(settings.kis_ohlcv_cache_ttl_seconds), 1)
                    )
                    await redis_client.expire(
                        dates_key, max(int(settings.kis_ohlcv_cache_ttl_seconds), 1)
                    )
                    await redis_client.expire(
                        rows_key, max(int(settings.kis_ohlcv_cache_ttl_seconds), 1)
                    )
        finally:
            await _release_lock(redis_client, lock_key, lock_token)

        final_rows = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            target_asof,
            requested_count,
        )
        if final_rows.empty and not fallback_cached.empty:
            return fallback_cached.tail(requested_count).reset_index(drop=True)
        return final_rows.tail(requested_count).reset_index(drop=True)
    except Exception as exc:
        logger.warning(
            "kis_ohlcv_cache error symbol=%s error=%s", normalized_symbol, exc
        )
        return None


__all__ = [
    "close_kis_ohlcv_cache_redis",
    "expected_asof_et",
    "get_closed_daily_candles",
]
