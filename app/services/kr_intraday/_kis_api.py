from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from datetime import date, time, timedelta
from typing import Any, cast

import pandas as pd

from app.services.brokers.kis.client import KISClient
from app.services.kr_intraday._types import (
    VenueType,
    _KST,
    _MAX_PAGE_CALLS_PER_DAY,
    _MinuteRow,
    _VENUE_CONFIGS,
    _VenueConfig,
)
from app.services.kr_intraday._utils import (
    _convert_kis_datetime_to_utc,
    _ensure_kst_aware,
    _normalize_venues,
    _parse_float,
    _resolve_window_minute_time,
    _store_minute_row,
    _to_float,
    _to_kst_naive,
)

logger = logging.getLogger(__name__)


async def _fetch_kis_minute_frames(
    *,
    symbol: str,
    markets: list[str],
    end_time_kst: datetime.datetime,
    log_context: str,
) -> list[tuple[VenueType, pd.DataFrame]]:
    if not markets:
        return []

    kis = KISClient()
    api_end_date = pd.Timestamp(end_time_kst.date())
    legacy_end_time = end_time_kst.strftime("%H%M%S")

    async def _fetch_one(market: str) -> object:
        minute_chart = cast(Any, getattr(kis, "inquire_minute_chart", None))
        if callable(minute_chart):
            minute_chart_async = cast(
                Callable[..., Awaitable[pd.DataFrame]],
                minute_chart,
            )
            return await minute_chart_async(
                code=symbol,
                market=market,
                time_unit=1,
                n=30,
                end_date=api_end_date,
            )
        return await kis.inquire_time_dailychartprice(
            code=symbol,
            market=market,
            n=30,
            end_date=api_end_date,
            end_time=legacy_end_time,
        )

    frames = await asyncio.gather(
        *[_fetch_one(market) for market in markets],
        return_exceptions=True,
    )

    valid_frames: list[tuple[VenueType, pd.DataFrame]] = []
    for market, frame in zip(markets, frames, strict=False):
        if isinstance(frame, Exception):
            logger.warning(
                "%s KIS API call failed for %s %s: %s",
                log_context,
                symbol,
                market,
                frame,
            )
            continue
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        if "datetime" not in frame.columns:
            continue
        valid_frames.append(("KRX" if market == "J" else "NTX", frame))

    return valid_frames


def _load_api_minute_frame_into_map(
    *,
    frame: pd.DataFrame,
    venue: VenueType,
    start_naive: datetime.datetime,
    end_naive: datetime.datetime,
    minute_by_key: dict[tuple[datetime.datetime, VenueType], _MinuteRow],
    api_minute_rows: list[_MinuteRow],
) -> None:
    dt_series = pd.to_datetime(frame["datetime"], errors="coerce")
    for index, dt_value in enumerate(dt_series.tolist()):
        minute_time = _resolve_window_minute_time(dt_value)
        if minute_time is None:
            continue
        if not (start_naive <= minute_time < end_naive):
            continue
        _store_minute_row(
            minute_by_key,
            minute_time=minute_time,
            venue=venue,
            source=frame.iloc[index],
            api_minute_rows=api_minute_rows,
        )


def _normalize_intraday_rows(
    *,
    frame: pd.DataFrame,
    symbol: str,
    venue_config: _VenueConfig,
    target_day: date,
) -> list[_MinuteRow]:
    """
    Normalize KIS API intraday candle response to _MinuteRow objects.

    Parameters
    ----------
    frame : pd.DataFrame
        KIS API response DataFrame
    symbol : str
        Stock symbol
    venue_config : _VenueConfig
        Venue configuration for session boundaries
    target_day : date
        Target date for filtering

    Returns
    -------
    list[_MinuteRow]
        Normalized minute candle rows, sorted by time
    """
    if frame.empty:
        return []

    rows: list[_MinuteRow] = []
    for item in frame.to_dict("records"):
        raw_datetime = item.get("datetime")
        if raw_datetime is None:
            continue

        parsed = pd.to_datetime(str(raw_datetime), errors="coerce")
        if pd.isna(parsed):
            continue

        parsed_dt = parsed.to_pydatetime()
        local_dt = _ensure_kst_aware(parsed_dt).replace(second=0, microsecond=0)

        if local_dt.date() != target_day:
            continue

        local_clock = time(local_dt.hour, local_dt.minute, local_dt.second)
        if (
            local_clock < venue_config.session_start
            or local_clock > venue_config.session_end
        ):
            continue

        open_value = _parse_float(item.get("open"))
        high_value = _parse_float(item.get("high"))
        low_value = _parse_float(item.get("low"))
        close_value = _parse_float(item.get("close"))
        volume_value = _parse_float(item.get("volume"))
        value_value = _parse_float(item.get("value"))

        if (
            open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
            or volume_value is None
            or value_value is None
        ):
            continue

        rows.append(
            _MinuteRow(
                minute_time=_to_kst_naive(local_dt),
                venue=venue_config.venue,
                open=float(open_value),
                high=float(high_value),
                low=float(low_value),
                close=float(close_value),
                volume=float(volume_value),
                value=float(value_value),
            )
        )

    # Deduplicate by (minute_time, venue)
    deduped: dict[tuple[datetime.datetime, VenueType], _MinuteRow] = {}
    for row in rows:
        deduped[(row.minute_time, row.venue)] = row
    return [deduped[key] for key in sorted(deduped)]


async def _fetch_historical_minutes_via_kis(
    *,
    symbol: str,
    end_date: datetime.date,
    limit: int,
) -> tuple[list[dict[str, object]], list[_MinuteRow]]:
    """
    KIS API를 통해 과거 1분봉 데이터를 조회하여 시간봉으로 집계

    Pagination을 사용하여 inquire_time_dailychartprice API를 호출하고,
    과거 데이터로 walk-back하며 충분한 분봉 데이터를 수집합니다.

    Parameters
    ----------
    symbol : str
        종목코드
    end_date : datetime.date
        조회 종료일
    limit : int
        가져올 시간봉 수

    Returns
    -------
    tuple[list[dict[str, object]], list[_MinuteRow]]
        - 시간봉 데이터 목록 (bucket, open, high, low, close, volume, value, venues)
        - 원본 1분봉 데이터 목록 (DB 저장용)
    """
    kis = KISClient()
    target_day = end_date

    # 목표: limit 시간 = limit * 60 분 데이터 수집
    target_minutes = limit * 60

    # 모든 venue의 분봉 데이터를 저장 (time_utc, venue) -> _MinuteRow
    all_minute_rows: dict[tuple[datetime.datetime, VenueType], _MinuteRow] = {}

    # 초기 end_time: 장 마감 시간 (NTX 20:00, KRX 15:30)
    # 가장 늦은 시장 기준으로 시작 (NTX 20:00)
    end_time = "200000"

    page_calls = 0

    # Pagination loop: 최대 30페이지까지 호출
    for _ in range(_MAX_PAGE_CALLS_PER_DAY):
        # 충분한 데이터를 수집했으면 종료
        if len(all_minute_rows) >= target_minutes:
            logger.info(
                "Collected %d minutes (target: %d), stopping pagination",
                len(all_minute_rows),
                target_minutes,
            )
            break

        page_calls += 1

        # 각 venue별로 API 호출
        for venue_config in _VENUE_CONFIGS.values():
            try:
                frame = await kis.inquire_time_dailychartprice(
                    code=symbol,
                    market=venue_config.market_code,
                    n=200,
                    end_date=pd.Timestamp(target_day),
                    end_time=end_time,
                )

                if frame.empty:
                    continue

                # Normalize and merge rows
                page_rows = _normalize_intraday_rows(
                    frame=frame,
                    symbol=symbol,
                    venue_config=venue_config,
                    target_day=target_day,
                )

                # Add to all_minute_rows (deduplicated by time_utc and venue)
                for row in page_rows:
                    time_utc = _convert_kis_datetime_to_utc(row.minute_time)
                    key = (time_utc, row.venue)
                    all_minute_rows[key] = _MinuteRow(
                        minute_time=row.minute_time,
                        venue=row.venue,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                        value=row.value,
                    )

            except Exception as e:
                # API 호출 실패 시 로그만 남기고 계속 진행
                logger.warning(
                    "KIS API call failed for %s %s at %s %s: %s",
                    symbol,
                    venue_config.venue,
                    target_day,
                    end_time,
                    e,
                )
                continue

        # 데이터를 수집하지 못했으면 종료
        if not all_minute_rows:
            logger.info(
                "No data collected from KIS API for %s on %s", symbol, target_day
            )
            break

        # 가장 이른 시간을 찾아서 다음 커서 계산 (walk backwards)
        earliest_local = min(row.minute_time for row in all_minute_rows.values())
        next_cursor = earliest_local - timedelta(minutes=1)

        # 세션 시작 시간 체크 (가장 이른 세션: NTX 08:00)
        if next_cursor.time() < time(8, 0, 0):
            logger.info(
                "Reached session boundary at %s for %s, stopping pagination",
                next_cursor,
                symbol,
            )
            break

        # 날짜가 바뀌면 종료
        if next_cursor.date() != target_day:
            logger.info(
                "Date boundary reached at %s for %s, stopping pagination",
                next_cursor,
                symbol,
            )
            break

        # 다음 커서 설정
        next_end_time = next_cursor.strftime("%H%M%S")
        if next_end_time == end_time:
            # 커서가 진전하지 않으면 무한 루프 방지
            logger.warning(
                "Cursor not progressing (end_time=%s), stopping pagination",
                end_time,
            )
            break
        end_time = next_end_time

    logger.info(
        "Pagination complete for %s: %d pages, %d minutes collected",
        symbol,
        page_calls,
        len(all_minute_rows),
    )

    if not all_minute_rows:
        return [], []

    # 1분봉을 시간봉으로 집계
    hourly_by_bucket: dict[datetime.datetime, dict[str, Any]] = {}

    for row in all_minute_rows.values():
        bucket_naive = _to_kst_naive(row.minute_time).replace(
            minute=0, second=0, microsecond=0
        )

        if bucket_naive not in hourly_by_bucket:
            hourly_by_bucket[bucket_naive] = {
                "minutes": [],
                "venues": set(),
            }

        hourly_by_bucket[bucket_naive]["minutes"].append(
            {
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
            }
        )
        hourly_by_bucket[bucket_naive]["venues"].add(row.venue)

    # 집계된 시간봉 생성
    hour_rows: list[dict[str, object]] = []

    for bucket_naive in sorted(hourly_by_bucket.keys(), reverse=True)[:limit]:
        data = hourly_by_bucket[bucket_naive]
        minutes = data["minutes"]

        if not minutes:
            continue

        open_ = minutes[0]["open"]
        high_ = max(m["high"] for m in minutes)
        low_ = min(m["low"] for m in minutes)
        close_ = minutes[-1]["close"]
        volume_ = sum(m["volume"] for m in minutes)
        value_ = sum(m["value"] for m in minutes)
        venues = _normalize_venues(list(data["venues"]))

        hour_rows.append(
            {
                "bucket": bucket_naive,
                "open": open_,
                "high": high_,
                "low": low_,
                "close": close_,
                "volume": volume_,
                "value": value_,
                "venues": venues,
            }
        )

    # Return both hourly aggregated data and original minute candles
    minute_rows_list = list(all_minute_rows.values())
    return hour_rows, minute_rows_list
