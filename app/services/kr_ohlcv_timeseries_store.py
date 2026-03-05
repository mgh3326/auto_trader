from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.kr_ohlcv_metrics import record_quarantine, record_rows_upserted
from app.services.kr_trading_calendar import route_for_exchange

_KST = ZoneInfo("Asia/Seoul")
_UTC = datetime.UTC
_HOURLY_COLUMNS = [
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
]
_VALID_EXCHANGES = {"KRX", "NXT"}
_VALID_ROUTES = {"J", "NX"}


def _empty_hourly_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_HOURLY_COLUMNS)


def _to_kst_aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_KST)
    return value.astimezone(_KST)


def _normalize_minute_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "value",
            ]
        )

    normalized = frame.copy()
    if "datetime" not in normalized.columns:
        if "date" in normalized.columns and "time" in normalized.columns:
            normalized["datetime"] = pd.to_datetime(
                normalized["date"].astype(str) + " " + normalized["time"].astype(str),
                errors="coerce",
            )
        else:
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

    normalized["datetime"] = pd.to_datetime(normalized["datetime"], errors="coerce")
    normalized = normalized.dropna(subset=["datetime"]).copy()
    if normalized.empty:
        return pd.DataFrame(
            columns=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "value",
            ]
        )

    numeric_defaults: dict[str, float | int] = {
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
        "volume": 0,
        "value": 0,
    }
    for column, default in numeric_defaults.items():
        if column not in normalized.columns:
            normalized[column] = default
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(
            default
        )

    normalized["datetime"] = normalized["datetime"].dt.floor("min")
    normalized = (
        normalized.loc[
            :,
            [
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "value",
            ],
        ]
        .drop_duplicates(subset=["datetime"], keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    return normalized


async def ensure_timescale_ready(*, allow_test_bypass: bool = True) -> None:
    if allow_test_bypass and str(settings.ENVIRONMENT).lower() == "test":
        return

    async with AsyncSessionLocal() as session:
        extension = (
            await session.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'timescaledb'")
            )
        ).scalar_one_or_none()
        if extension != "timescaledb":
            raise RuntimeError("TimescaleDB extension is not installed")

        table_name = (
            await session.execute(
                text("SELECT to_regclass('public.market_candles_1m_kr')")
            )
        ).scalar_one_or_none()
        if table_name is None:
            raise RuntimeError("market_candles_1m_kr table is missing")

        cagg_name = (
            await session.execute(
                text("SELECT to_regclass('public.market_candles_1h_kr')")
            )
        ).scalar_one_or_none()
        if cagg_name is None:
            raise RuntimeError("market_candles_1h_kr continuous aggregate is missing")


async def upsert_market_candles_1m(
    *,
    symbol: str,
    frame: pd.DataFrame,
    exchange: str,
    route: str | None = None,
    source: str = "kis",
) -> dict[str, Any]:
    normalized = _normalize_minute_frame(frame)
    if normalized.empty:
        return {
            "rows": 0,
            "min_ts": None,
            "max_ts": None,
        }

    symbol_value = str(symbol).strip().upper()
    exchange_value = str(exchange or "").strip().upper()
    route_value = str(route or "").strip().upper()
    reason: str | None = None
    if exchange_value not in _VALID_EXCHANGES:
        reason = "invalid_exchange"
    elif route_value and route_value not in _VALID_ROUTES:
        reason = "invalid_route"
    elif route_value and route_for_exchange(exchange_value) != route_value:
        reason = "exchange_route_mismatch"

    params: list[dict[str, Any]] = []
    params_v2: list[dict[str, Any]] = []
    quarantine_params: list[dict[str, Any]] = []
    utc_datetimes: list[datetime.datetime] = []
    fetched_at = datetime.datetime.now(_UTC)
    for row in normalized.itertuples(index=False):
        dt = pd.Timestamp(row.datetime)
        if dt.tzinfo is None:
            dt_kst = dt.tz_localize(_KST)
        else:
            dt_kst = dt.tz_convert(_KST)
        dt_utc = dt_kst.tz_convert(_UTC).to_pydatetime()
        utc_datetimes.append(dt_utc)

        payload = {
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": int(row.volume),
            "value": int(row.value),
            "source": str(source).strip().lower() or "kis",
        }

        if reason is not None:
            quarantine_params.append(
                {
                    "symbol": symbol_value,
                    "route": route_value or None,
                    "exchange_raw": exchange_value,
                    "ts": dt_utc,
                    "payload": json.dumps(payload, ensure_ascii=True),
                    "reason": reason,
                }
            )
            continue

        params.append(
            {
                "exchange": exchange_value,
                "symbol": symbol_value,
                "ts": dt_utc,
                "open": payload["open"],
                "high": payload["high"],
                "low": payload["low"],
                "close": payload["close"],
                "volume": payload["volume"],
                "value": payload["value"],
                "source": payload["source"],
                "fetched_at": fetched_at,
            }
        )
        if settings.KR_OHLCV_V2_DUAL_WRITE_ENABLED:
            params_v2.append(
                {
                    "exchange": exchange_value,
                    "symbol": symbol_value,
                    "ts": dt_utc,
                    "open": int(round(float(payload["open"]))),
                    "high": int(round(float(payload["high"]))),
                    "low": int(round(float(payload["low"]))),
                    "close": int(round(float(payload["close"]))),
                    "volume": payload["volume"],
                    "value": payload["value"],
                    "source": payload["source"],
                    "fetched_at": fetched_at,
                }
            )

    if quarantine_params:
        quarantine_statement = text(
            """
            INSERT INTO market_candles_ingest_quarantine (
                symbol,
                route,
                exchange_raw,
                ts,
                payload,
                reason,
                created_at
            ) VALUES (
                :symbol,
                :route,
                :exchange_raw,
                :ts,
                CAST(:payload AS jsonb),
                :reason,
                now()
            )
            """
        )
        async with AsyncSessionLocal() as session:
            await session.execute(quarantine_statement, quarantine_params)
            await session.commit()
        record_quarantine(len(quarantine_params))
        return {
            "rows": 0,
            "quarantined_rows": len(quarantine_params),
            "min_ts": min(utc_datetimes),
            "max_ts": max(utc_datetimes),
        }

    statement = text(
        """
        INSERT INTO market_candles_1m_kr (
            exchange,
            symbol,
            ts,
            open,
            high,
            low,
            close,
            volume,
            value,
            source,
            fetched_at,
            updated_at
        ) VALUES (
            :exchange,
            :symbol,
            :ts,
            :open,
            :high,
            :low,
            :close,
            :volume,
            :value,
            :source,
            :fetched_at,
            now()
        )
        ON CONFLICT (exchange, symbol, ts) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            value = EXCLUDED.value,
            source = EXCLUDED.source,
            fetched_at = EXCLUDED.fetched_at,
            updated_at = now()
        """
    )

    statement_v2 = text(
        """
        INSERT INTO market_candles_1m_kr_v2 (
            exchange,
            symbol,
            ts,
            open,
            high,
            low,
            close,
            volume,
            value,
            source,
            fetched_at,
            updated_at
        ) VALUES (
            :exchange,
            :symbol,
            :ts,
            :open,
            :high,
            :low,
            :close,
            :volume,
            :value,
            :source,
            :fetched_at,
            now()
        )
        ON CONFLICT (exchange, symbol, ts) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            value = EXCLUDED.value,
            source = EXCLUDED.source,
            fetched_at = EXCLUDED.fetched_at,
            updated_at = now()
        """
    )

    async with AsyncSessionLocal() as session:
        await session.execute(statement, params)
        if settings.KR_OHLCV_V2_DUAL_WRITE_ENABLED and params_v2:
            await session.execute(statement_v2, params_v2)
        await session.commit()

    record_rows_upserted(exchange_value, len(params))

    return {
        "rows": len(params),
        "min_ts": min(utc_datetimes),
        "max_ts": max(utc_datetimes),
    }


async def refresh_market_candles_1h_kr(
    *,
    start_ts: datetime.datetime,
    end_ts: datetime.datetime,
) -> None:
    start_utc = _to_kst_aware(start_ts).astimezone(_UTC)
    end_utc = _to_kst_aware(end_ts).astimezone(_UTC)
    if end_utc <= start_utc:
        return

    statement = text(
        """
        SELECT refresh_continuous_aggregate(
            'market_candles_1h_kr',
            :start_ts,
            :end_ts
        )
        """
    )
    async with AsyncSessionLocal() as session:
        await session.execute(statement, {"start_ts": start_utc, "end_ts": end_utc})
        await session.commit()


async def fetch_market_candles_1h_kr(
    *,
    symbol: str,
    start_bucket: datetime.datetime,
    end_bucket: datetime.datetime,
) -> pd.DataFrame:
    if end_bucket < start_bucket:
        return _empty_hourly_frame()

    start_utc = _to_kst_aware(start_bucket).astimezone(_UTC)
    end_utc = _to_kst_aware(end_bucket).astimezone(_UTC)
    statement = text(
        """
        SELECT
            bucket_start,
            open,
            high,
            low,
            close,
            volume,
            value
        FROM market_candles_1h_kr
        WHERE symbol = :symbol
          AND bucket_start >= :start_bucket
          AND bucket_start <= :end_bucket
        ORDER BY bucket_start ASC
        """
    )
    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    statement,
                    {
                        "symbol": str(symbol).strip().upper(),
                        "start_bucket": start_utc,
                        "end_bucket": end_utc,
                    },
                )
            )
            .mappings()
            .all()
        )

    if not rows:
        return _empty_hourly_frame()

    frame = pd.DataFrame(rows)
    frame["datetime"] = pd.to_datetime(frame["bucket_start"], utc=True).dt.tz_convert(
        _KST
    )
    frame["date"] = frame["datetime"].dt.date
    frame["time"] = frame["datetime"].dt.time
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("volume", "value"):
        frame[column] = (
            pd.to_numeric(frame[column], errors="coerce").fillna(0).astype("int64")
        )
    frame = frame.loc[:, _HOURLY_COLUMNS].sort_values("datetime").reset_index(drop=True)
    return frame


async def fetch_previous_close_before_bucket(
    *,
    symbol: str,
    before_bucket: datetime.datetime,
) -> float | None:
    before_utc = _to_kst_aware(before_bucket).astimezone(_UTC)
    symbol_value = str(symbol).strip().upper()

    cagg_statement = text(
        """
        SELECT close
        FROM market_candles_1h_kr
        WHERE symbol = :symbol
          AND bucket_start < :before_bucket
        ORDER BY bucket_start DESC
        LIMIT 1
        """
    )
    minute_statement = text(
        """
        SELECT close
        FROM market_candles_1m_kr
        WHERE symbol = :symbol
          AND ts < :before_bucket
          AND exchange IN ('KRX', 'NXT')
        ORDER BY
            ts DESC,
            CASE WHEN exchange = 'KRX' THEN 1 ELSE 0 END DESC
        LIMIT 1
        """
    )

    async with AsyncSessionLocal() as session:
        close_value = (
            await session.execute(
                cagg_statement,
                {"symbol": symbol_value, "before_bucket": before_utc},
            )
        ).scalar_one_or_none()
        if close_value is not None:
            return float(close_value)

        minute_close = (
            await session.execute(
                minute_statement,
                {"symbol": symbol_value, "before_bucket": before_utc},
            )
        ).scalar_one_or_none()
        if minute_close is not None:
            return float(minute_close)
    return None


async def fetch_latest_hourly_bucket(
    *,
    symbol: str,
) -> datetime.datetime | None:
    statement = text(
        """
        SELECT bucket_start
        FROM market_candles_1h_kr
        WHERE symbol = :symbol
        ORDER BY bucket_start DESC
        LIMIT 1
        """
    )
    async with AsyncSessionLocal() as session:
        value = (
            await session.execute(statement, {"symbol": str(symbol).strip().upper()})
        ).scalar_one_or_none()

    if value is None:
        return None
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(_UTC)
    else:
        parsed = parsed.tz_convert(_UTC)
    return parsed.tz_convert(_KST).to_pydatetime()


def frame_from_hour_rows(rows: Sequence[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return _empty_hourly_frame()
    frame = pd.DataFrame(rows)
    for column in _HOURLY_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame.loc[:, _HOURLY_COLUMNS].reset_index(drop=True)


__all__ = [
    "ensure_timescale_ready",
    "fetch_latest_hourly_bucket",
    "fetch_market_candles_1h_kr",
    "fetch_previous_close_before_bucket",
    "frame_from_hour_rows",
    "refresh_market_candles_1h_kr",
    "upsert_market_candles_1m",
]
