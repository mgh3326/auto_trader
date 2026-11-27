#!/usr/bin/env python3
"""hk 6395: Benzinga news shadow collector (JSONL, no DB writes).

Manual CLI. Polls Benzinga /api/v2/news per symbol per request parameter
(``primaryTickers`` for coverage, ``tickers`` as evidence — confirmed
2026-11-25 the ``tickers`` filter misses secondary mentions) and Finnhub
company-news for the same symbols in the same pass, appending JSON records
to a JSONL file OUTSIDE the repo: one ``article`` row per story plus one
``poll_status`` row per symbol per parameter per source (ok/empty row
count, or the error class), plus one ``poll_header`` row (planned symbols)
and one ``poll_completion`` row (polled symbols) per pass. A ``summary``
subcommand compares the two streams over a window.

hk 6661: the poll also reads KIS news headlines (``source="kis"``): one
market-wide ``brknews_title`` call per US pass matched to symbols by
``iscd1``..``iscd10``, and per-symbol ``overseas_news_title`` (US) or
``domestic_news_title`` (KR, ``--market kr``). ``--skip-kis`` drops them.
KR polls read KIS only (Benzinga and Finnhub are US sources).

Usage:
    uv run python -m scripts.benzinga_news_shadow poll \\
        --symbols NVDA,AMD --out-dir /tmp/benzinga-shadow [--limit 50]
    uv run python -m scripts.benzinga_news_shadow summary \\
        --input /tmp/benzinga-shadow/<poll>.jsonl [--poll-id <id>]

``--date-from/--date-to`` (``YYYY-MM-DD``, UTC calendar dates) narrow the
Finnhub side client-side by published time; Benzinga receives them as its
``dateFrom``/``dateTo`` request parameters. ``--input`` accepts several
files or a directory (``*.jsonl``) so a window of polls can be compared.

``first_received_at`` is stamped per source after that source's own response
arrives — never one shared stamp for both sides. Sequential fetching
inherently biases the difference toward the second source; the benchmark
reads latency as an ordering signal, not a calibrated race.

Gate: BENZINGA_NEWS_SHADOW_ENABLED must be exactly "true", otherwise the CLI
exits 2 without any network call. The key comes only from env
BENZINGA_API_KEY and never appears in logs, errors, or output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.cli import setup_logging_and_sentry
from app.services import benzinga_news as bz
from app.services import kis_news_shadow as kn
from app.services import saveticker_news_shadow as st

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]

_POLL_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
SAVETICKER_SEEN_STATE = ".saveticker-seen-ids.json"
SAVETICKER_SEEN_STATE_LIMIT = 10_000


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="poll both sources, append JSONL")
    poll.add_argument("--symbols", required=True, help="comma/space separated")
    poll.add_argument("--out-dir", required=True, help="outside the repo")
    poll.add_argument("--limit", type=int, default=bz.DEFAULT_PAGE_SIZE)
    poll.add_argument("--date-from", default=None, help="YYYY-MM-DD UTC, inclusive")
    poll.add_argument("--date-to", default=None, help="YYYY-MM-DD UTC, inclusive")
    poll.add_argument("--poll-id", default=None, help="[A-Za-z0-9._-]+")
    poll.add_argument(
        "--skip-benzinga",
        action="store_true",
        help="Benzinga 소스를 비활성화",
    )
    poll.add_argument(
        "--skip-finnhub",
        action="store_true",
        help="Benzinga only (e.g. Finnhub quota exhausted)",
    )
    poll.add_argument(
        "--skip-kis",
        action="store_true",
        help="no KIS news-headline source (KIS outage or quota)",
    )
    saveticker_group = poll.add_mutually_exclusive_group()
    saveticker_group.add_argument(
        "--saveticker",
        action="store_true",
        help="Save+ 공개 뉴스와 개인 관심도 스냅샷을 포함",
    )
    saveticker_group.add_argument(
        "--skip-saveticker",
        action="store_true",
        help="Save+ 소스를 명시적으로 비활성화",
    )
    poll.add_argument(
        "--saveticker-pages",
        type=int,
        default=3,
        help=f"Save+ 최신 페이지 수 (1..{st.MAX_PAGES})",
    )
    poll.add_argument(
        "--saveticker-page-size",
        type=int,
        default=st.DEFAULT_PAGE_SIZE,
        help="Save+ 페이지 크기 (1..100)",
    )
    poll.add_argument(
        "--market",
        choices=("us", "kr"),
        default="us",
        help="us: Benzinga+Finnhub+KIS; kr: KIS domestic headlines only",
    )

    summary = sub.add_parser("summary", help="compare two streams over JSONL input")
    summary.add_argument(
        "--input", required=True, nargs="+", help="JSONL file(s) or directories"
    )
    summary.add_argument("--poll-id", default=None, help="restrict to one poll")

    return parser.parse_args(argv)


def _parse_symbols(raw: str) -> list[str]:
    seen: list[str] = []
    for token in raw.replace(",", " ").split():
        symbol = token.strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


def _resolve_out_dir(raw: str) -> Path:
    """Refuse any path inside the repo — shadow output stays outside."""
    path = Path(raw).expanduser().resolve()
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return path
    raise ValueError(f"--out-dir must be outside the repo: {raw}")


def _open_kis_reader():  # noqa: ANN202
    """Build the read-only KIS client. Lazy: gate-off and summary never reach it."""
    from app.services.brokers.kis.client import KISClient

    return KISClient(is_mock=False)


def _check_gate() -> int | None:
    """Return exit code if gated off, else None. No network before this."""
    if not bz.shadow_enabled():
        print(
            f"{bz.SHADOW_ENABLED_ENV} is not exactly 'true'; "
            "shadow collection is disabled, no network call made",
            file=sys.stderr,
        )
        return 2
    return None


async def _poll_symbol(
    symbol: str,
    *,
    api_key: str,
    poll_id: str,
    limit: int,
    date_from: str | None,
    date_to: str | None,
    skip_finnhub: bool,
    skip_benzinga: bool = False,
) -> tuple[list[dict], list[str]]:
    """Poll one symbol, stamping each source after its own response.

    Benzinga is queried twice per symbol: ``primaryTickers`` (coverage)
    and ``tickers`` (evidence). Both result sets are recorded with their
    request parameter name in ``query_param`` so the comparison keeps the
    evidence.
    """
    rows: list[dict] = []
    errors: list[str] = []
    for ticker_param in () if skip_benzinga else ("primaryTickers", "tickers"):
        try:
            fetched = await bz.fetch_benzinga_news(
                symbol,
                api_key=api_key,
                poll_id=poll_id,
                now=datetime.now(UTC),
                page_size=limit,
                date_from=date_from,
                date_to=date_to,
                ticker_param=ticker_param,
            )
            seen = datetime.now(UTC)
            for record in fetched:
                record["query_symbol"] = symbol
                record["query_param"] = ticker_param
                record["first_received_at"] = seen.astimezone(UTC).isoformat()
            rows.extend(fetched)
            rows.append(
                _status_row(
                    source="benzinga",
                    symbol=symbol,
                    poll_id=poll_id,
                    status="ok" if fetched else "empty",
                    count=len(fetched),
                    error_class=None,
                    seen_at=seen,
                    query_param=ticker_param,
                )
            )
        except Exception as exc:  # noqa: BLE001 - per-source failure must not kill the pass
            seen = datetime.now(UTC)
            errors.append(f"benzinga {symbol} [{ticker_param}]: {type(exc).__name__}")
            logger.warning(
                "benzinga poll failed for %s [%s]: %s",
                symbol,
                ticker_param,
                bz.redact_token(str(exc)),
            )
            rows.append(
                _status_row(
                    source="benzinga",
                    symbol=symbol,
                    poll_id=poll_id,
                    status="error",
                    count=0,
                    error_class=type(exc).__name__,
                    seen_at=seen,
                    query_param=ticker_param,
                )
            )
    if not skip_finnhub:
        try:
            from app.services.finnhub_news import fetch_news_finnhub

            result = await fetch_news_finnhub(symbol, "us", limit)
            seen = datetime.now(UTC)
            kept = 0
            for item in result.get("news", []):
                record = bz.normalize_finnhub_record(
                    item, poll_id=poll_id, first_received_at=seen
                )
                record["query_symbol"] = symbol
                if not _in_window(record, date_from, date_to):
                    continue
                record["first_received_at"] = seen.astimezone(UTC).isoformat()
                rows.append(record)
                kept += 1
            rows.append(
                _status_row(
                    source="finnhub",
                    symbol=symbol,
                    poll_id=poll_id,
                    status="ok" if kept else "empty",
                    count=kept,
                    error_class=None,
                    seen_at=seen,
                )
            )
        except Exception as exc:  # noqa: BLE001 - same reason
            seen = datetime.now(UTC)
            errors.append(f"finnhub {symbol}: {type(exc).__name__}")
            logger.warning("finnhub poll failed for %s: %s", symbol, type(exc).__name__)
            rows.append(
                _status_row(
                    source="finnhub",
                    symbol=symbol,
                    poll_id=poll_id,
                    status="error",
                    count=0,
                    error_class=type(exc).__name__,
                    seen_at=seen,
                )
            )
    return rows, errors


def _validate_poll_id(raw: str | None) -> str:
    """Restrict ``--poll-id`` to a safe filename pattern (or mint one)."""
    if raw is None:
        return (
            f"poll-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            f"-{uuid.uuid4().hex[:8]}"
        )
    if not _POLL_ID_RE.fullmatch(raw):
        raise ValueError(
            f"--poll-id must match [A-Za-z0-9._-]+, got {raw!r} "
            "(absolute paths and ../ are refused)"
        )
    return raw


def _in_window(record: dict, date_from: str | None, date_to: str | None) -> bool:
    """Keep Finnhub articles whose UTC published date is inside the window."""
    if date_from is None and date_to is None:
        return True
    raw = record.get("created_utc")
    if not isinstance(raw, str):
        return False
    try:
        day = datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return False
    if date_from is not None and day < date_from:
        return False
    return not (date_to is not None and day > date_to)


def _status_row(
    *,
    source: str,
    symbol: str,
    poll_id: str,
    status: str,
    count: int,
    error_class: str | None,
    seen_at: datetime,
    query_param: str | None = None,
    extra: dict | None = None,
) -> dict:
    row = {
        "record_type": "poll_status",
        "source": source,
        "query_symbol": symbol,
        "query_param": query_param,
        "poll_id": poll_id,
        "status": status,
        "count": count,
        "error_class": error_class,
        "first_received_at": seen_at.astimezone(UTC).isoformat(),
    }
    if extra:
        row.update(extra)
    return row


def _load_saveticker_seen_ids(out_dir: Path) -> list[str]:
    """Load a bounded sidecar; never rescan the growing JSONL output history."""
    path = out_dir / SAVETICKER_SEEN_STATE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise st.SaveTickerError("SeenStateInvalid") from None
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise st.SaveTickerError("SeenStateInvalid")
    return list(dict.fromkeys(ids))[-SAVETICKER_SEEN_STATE_LIMIT:]


def _save_saveticker_seen_ids(out_dir: Path, ids: list[str]) -> None:
    """Atomically persist only the most recent bounded provider ID window."""
    bounded = list(dict.fromkeys(ids))[-SAVETICKER_SEEN_STATE_LIMIT:]
    path = out_dir / SAVETICKER_SEEN_STATE
    temp_path = out_dir / f"{SAVETICKER_SEEN_STATE}.{uuid.uuid4().hex}.tmp"
    try:
        temp_path.write_text(
            json.dumps({"ids": bounded}, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temp_path, path)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise st.SaveTickerError("SeenStateWriteFailed") from None


def _saveticker_article_rows(
    items: list[dict],
    *,
    poll_id: str,
    observed_at: datetime,
    polled_symbols: list[str],
    query_param: str,
) -> tuple[list[dict], list[dict]]:
    articles: list[dict] = []
    attention: list[dict] = []
    for item in items:
        record = {
            **item,
            "poll_id": poll_id,
            "query_param": query_param,
            "first_received_at": observed_at.astimezone(UTC).isoformat(),
        }
        symbols = [symbol for symbol in polled_symbols if symbol in item["symbols"]]
        if symbols:
            for symbol in symbols:
                articles.append({**record, "query_symbol": symbol, "symbol": symbol})
        else:
            articles.append({**record, "query_symbol": None, "symbol": None})
        attention.append(
            {
                "record_type": "attention",
                "source": "saveticker",
                "provider_id": item["provider_id"],
                "poll_id": poll_id,
                "observed_at": observed_at.astimezone(UTC).isoformat(),
                "event_time": item["source_created_utc"],
                "publish_time": item["created_utc"],
                "view_count": item["view_count"],
                "comment_count": item["comment_count"],
                "like_stats": item["like_stats"],
                "vote_stats": item["vote_stats"],
                "like_count": item["like_count"],
                "vote_count": item["vote_count"],
                "is_top_story": item["is_top_story"] or query_param == "top_stories",
                "labels": item["labels"],
                "tickers": item["tickers"],
                "title": item["title"],
            }
        )
    return articles, attention


def _saveticker_attention_row(
    item: dict, *, poll_id: str, observed_at: datetime, is_top_story: bool = False
) -> dict:
    return {
        "record_type": "attention",
        "source": "saveticker",
        "provider_id": item["provider_id"],
        "poll_id": poll_id,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "event_time": item["source_created_utc"],
        "publish_time": item["created_utc"],
        "view_count": item["view_count"],
        "comment_count": item["comment_count"],
        "like_stats": item["like_stats"],
        "vote_stats": item["vote_stats"],
        "like_count": item["like_count"],
        "vote_count": item["vote_count"],
        "is_top_story": is_top_story or item["is_top_story"],
        "labels": item["labels"],
        "tickers": item["tickers"],
        "title": item["title"],
    }


async def _saveticker_poll(
    *,
    poll_id: str,
    out_dir: Path,
    polled_symbols: list[str],
    pages: int,
    page_size: int,
) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """Fetch bounded market-wide Save+ pages and top stories, isolating errors."""
    if not 1 <= pages <= st.MAX_PAGES or not 1 <= page_size <= 100:
        raise ValueError(f"Save+ 제한: 페이지 수 1..{st.MAX_PAGES}, 페이지 크기 1..100")
    state_ids = _load_saveticker_seen_ids(out_dir)
    old_ids = set(state_ids)
    # Insertion-ordered recency: the last key is the most recently observed ID.
    recency: dict[str, None] = dict.fromkeys(state_ids)
    articles_by_id: dict[str, list[dict]] = {}
    attention_by_id: dict[str, dict] = {}
    records: list[dict] = []
    errors: list[str] = []

    def observe_item(
        item: dict,
        *,
        seen_at: datetime,
        query_param: str,
        is_top_story: bool,
        attention_priority: bool = False,
    ) -> bool:
        """Record a fresh article once and an attention observation once per run."""
        provider_id = item["provider_id"]
        # Re-observing a known ID refreshes its recency so the bounded state
        # keeps continuously observed items and drops only idle ones.
        recency.pop(provider_id, None)
        recency[provider_id] = None
        previous_attention = attention_by_id.get(provider_id)
        if previous_attention is None or attention_priority:
            attention_by_id[provider_id] = _saveticker_attention_row(
                item, poll_id=poll_id, observed_at=seen_at, is_top_story=is_top_story
            )
            if previous_attention is not None and previous_attention["is_top_story"]:
                attention_by_id[provider_id]["is_top_story"] = True
        elif is_top_story:
            attention_by_id[provider_id]["is_top_story"] = True
        existing = articles_by_id.get(provider_id)
        if existing is not None:
            if is_top_story:
                for row in existing:
                    row["is_top_story"] = True
            return False
        if provider_id in old_ids:
            return False
        article_item = {**item, "is_top_story": item["is_top_story"] or is_top_story}
        article_rows, _ = _saveticker_article_rows(
            [article_item],
            poll_id=poll_id,
            observed_at=seen_at,
            polled_symbols=polled_symbols,
            query_param=query_param,
        )
        articles_by_id[provider_id] = article_rows
        return True

    async with st.SaveTickerClient() as client:
        # One unit per feed; search is one unit per symbol so a failing symbol
        # records its own error and the remaining symbols still run.
        search_units = [("search", sym) for sym in polled_symbols] or [("search", None)]
        units: list[tuple[str, str | None]] = [
            ("news", None),
            *search_units,
            ("top_stories", None),
            ("view_count_desc", None),
        ]
        for source_name, symbol in units:
            label = source_name if symbol is None else f"{source_name}:{symbol}"
            malformed = 0
            fetched_count = 0
            new_count = 0
            truncated_count = 0
            fetch_error: str | None = None
            try:
                if source_name == "news":
                    for page in range(1, pages + 1):
                        payload = await client.news_page(page, page_size)
                        seen_at = datetime.now(UTC)
                        normalized, dropped = st.normalize_envelope(payload)
                        malformed += dropped
                        fetched_count += len(normalized)
                        for item in normalized:
                            new_count += observe_item(
                                item,
                                seen_at=seen_at,
                                query_param=source_name,
                                is_top_story=False,
                            )
                        if not _saveticker_has_next_page(payload, page, page_size):
                            break
                elif source_name == "search" and symbol is not None:
                    for page in range(1, pages + 1):
                        payload = await client.search_page(symbol, page, page_size)
                        seen_at = datetime.now(UTC)
                        normalized, dropped = st.normalize_envelope(payload)
                        malformed += dropped
                        fetched_count += len(normalized)
                        for item in normalized:
                            new_count += observe_item(
                                item,
                                seen_at=seen_at,
                                query_param=source_name,
                                is_top_story=False,
                            )
                        if not _saveticker_has_next_page(
                            payload,
                            page,
                            st.effective_search_page_size(page_size),
                        ):
                            break
                elif source_name == "top_stories":
                    payload = await client.top_stories()
                    seen_at = datetime.now(UTC)
                    normalized, malformed = st.normalize_envelope(payload)
                    fetched_count = len(normalized)
                    for item in normalized:
                        new_count += observe_item(
                            item,
                            seen_at=seen_at,
                            query_param=source_name,
                            is_top_story=True,
                        )
                elif source_name == "view_count_desc":
                    payload = await client.news_page(1, 50, sort="view_count_desc")
                    seen_at = datetime.now(UTC)
                    normalized, malformed = st.normalize_envelope(payload)
                    fetched_count = len(normalized)
                    for item in normalized:
                        new_count += observe_item(
                            item,
                            seen_at=seen_at,
                            query_param=source_name,
                            is_top_story=False,
                            attention_priority=True,
                        )
            except Exception as exc:  # noqa: BLE001 - other feeds continue independently
                seen_at = datetime.now(UTC)
                error_class = _saveticker_error_class(exc)
                errors.append(f"saveticker [{label}]: {error_class}")
                logger.warning("saveticker poll failed [%s]: %s", label, error_class)
                fetch_error = error_class
            malformed_only = (
                malformed > 0 and fetched_count == 0 and fetch_error is None
            )
            if malformed_only:
                fetch_error = "MalformedRows"
                errors.append(f"saveticker [{label}]: MalformedRows")
            status_time = datetime.now(UTC)
            records.append(
                _status_row(
                    source="saveticker",
                    symbol=symbol,
                    poll_id=poll_id,
                    status="error"
                    if fetch_error
                    else ("ok" if fetched_count else "empty"),
                    count=new_count,
                    error_class=fetch_error,
                    seen_at=status_time,
                    query_param=source_name,
                    extra={
                        "malformed_count": malformed,
                        "truncated_count": truncated_count,
                    },
                )
            )
    records[:0] = [row for rows in articles_by_id.values() for row in rows]
    records.extend(attention_by_id.values())
    return records, list(attention_by_id.values()), errors, list(recency)


def _saveticker_error_class(exc: Exception) -> str:
    if isinstance(exc, st.SaveTickerError) and str(exc):
        return str(exc)
    return type(exc).__name__


def _saveticker_has_next_page(payload: dict, page: int, page_size: int) -> bool:
    total_count = payload.get("total_count")
    if type(total_count) is not int or total_count < 0:
        raise st.SaveTickerError("InvalidNewsPagination")
    return page * page_size < total_count


async def _kis_poll(
    *,
    reader: object | None,
    open_error: str | None,
    query_symbol: str | None,
    poll_id: str,
    query_param: str,
    fetch,  # noqa: ANN001 - async callable(reader) -> list[dict]
    normalize,  # noqa: ANN001 - callable(raw, seen) -> list[dict]
) -> tuple[list[dict], list[str]]:
    """One KIS read: a status row for the call, never an exception outward."""
    label = query_symbol or query_param
    if reader is None:
        seen = datetime.now(UTC)
        error_class = open_error or "KISClientUnavailable"
        return [
            _status_row(
                source=kn.SOURCE,
                symbol=query_symbol,
                poll_id=poll_id,
                status="error",
                count=0,
                error_class=error_class,
                seen_at=seen,
                query_param=query_param,
            )
        ], [f"kis {label} [{query_param}]: {error_class}"]
    try:
        raw = await fetch(reader)
        seen = datetime.now(UTC)
        records = normalize(raw, seen)
        return [
            *records,
            _status_row(
                source=kn.SOURCE,
                symbol=query_symbol,
                poll_id=poll_id,
                status="ok" if raw else "empty",
                count=len(raw),
                error_class=None,
                seen_at=seen,
                query_param=query_param,
            ),
        ], []
    except Exception as exc:  # noqa: BLE001 - KIS failure must not stop other sources
        seen = datetime.now(UTC)
        logger.warning(
            "kis poll failed for %s [%s]: %s",
            label,
            query_param,
            bz.redact_token(str(exc)),
        )
        return [
            _status_row(
                source=kn.SOURCE,
                symbol=query_symbol,
                poll_id=poll_id,
                status="error",
                count=0,
                error_class=type(exc).__name__,
                seen_at=seen,
                query_param=query_param,
            )
        ], [f"kis {label} [{query_param}]: {type(exc).__name__}"]


def _kis_symbol_calls(market: str, poll_id: str, symbol: str):  # noqa: ANN202
    """(query_param, fetch, normalize) for one KIS per-symbol read."""
    if market == "us":

        async def fetch_us(reader):  # noqa: ANN001, ANN202
            return await reader.news_titles_overseas(symbol)

        def normalize_us(raw, seen):  # noqa: ANN001, ANN202
            return kn.normalize_overseas_rows(
                raw, poll_id=poll_id, first_received_at=seen, symbol=symbol
            )

        return kn.PARAM_OVERSEAS_NEWS, fetch_us, normalize_us

    async def fetch_kr(reader):  # noqa: ANN001, ANN202
        return await reader.news_titles_domestic(symbol)

    def normalize_kr(raw, seen):  # noqa: ANN001, ANN202
        return kn.normalize_domestic_rows(
            raw, poll_id=poll_id, first_received_at=seen, symbol=symbol
        )

    return kn.PARAM_DOMESTIC_NEWS, fetch_kr, normalize_kr


async def _run_poll(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("no symbols given", file=sys.stderr)
        return 2
    if args.market == "kr" and args.skip_kis:
        print(
            "--market kr polls KIS only; --skip-kis leaves no source", file=sys.stderr
        )
        return 2
    try:
        out_dir = _resolve_out_dir(args.out_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        poll_id = _validate_poll_id(args.poll_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # hk 7092: --poll-id is caller supplied and the collector appends to the
    # same file, so every invocation also carries a distinct attempt id. The
    # summary matches poll_header/poll_completion on (poll_id, attempt_id): a
    # reused poll id can no longer borrow an earlier run's completion record.
    attempt_id = uuid.uuid4().hex
    api_key = None
    if args.market == "us" and not args.skip_benzinga:
        try:
            api_key = bz.read_api_key()
        except bz.BenzingaApiKeyMissing as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.saveticker and (
        not 1 <= args.saveticker_pages <= st.MAX_PAGES
        or not 1 <= args.saveticker_page_size <= 100
    ):
        print(
            f"Save+ 제한: 페이지 수 1..{st.MAX_PAGES}, 페이지 크기 1..100",
            file=sys.stderr,
        )
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (out_dir / f"{poll_id}.jsonl").resolve()
    try:
        out_path.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        print(f"output path must be outside the repo: {out_path}", file=sys.stderr)
        return 2

    kis_on = not args.skip_kis
    kis_reader: object | None = None
    kis_open_error: str | None = None
    if kis_on:
        try:
            kis_reader = _open_kis_reader()
        except Exception as exc:  # noqa: BLE001 - recorded as a KIS error row
            kis_open_error = type(exc).__name__
            logger.warning("kis client unavailable: %s", kis_open_error)

    counts: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    completed: list[str] = []
    try:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "record_type": "poll_header",
                        "poll_id": poll_id,
                        "attempt_id": attempt_id,
                        "symbols": symbols,
                        "started_at": datetime.now(UTC).astimezone(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if args.saveticker:
                try:
                    (
                        saveticker_rows,
                        _attention_rows,
                        saveticker_errors,
                        saveticker_state,
                    ) = await _saveticker_poll(
                        poll_id=poll_id,
                        out_dir=out_dir,
                        polled_symbols=symbols,
                        pages=args.saveticker_pages,
                        page_size=args.saveticker_page_size,
                    )
                    errors.extend(saveticker_errors)
                    for record in saveticker_rows:
                        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    try:
                        _save_saveticker_seen_ids(out_dir, saveticker_state)
                    except Exception as exc:  # noqa: BLE001 - state failures stay source-local
                        error_class = _saveticker_error_class(exc)
                        errors.append(f"saveticker [seen_state]: {error_class}")
                        logger.warning(
                            "saveticker seen-state write failed: %s", error_class
                        )
                        fh.write(
                            json.dumps(
                                _status_row(
                                    source="saveticker",
                                    symbol=None,
                                    poll_id=poll_id,
                                    status="error",
                                    count=0,
                                    error_class=error_class,
                                    seen_at=datetime.now(UTC),
                                    query_param="seen_state",
                                ),
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                except Exception as exc:  # noqa: BLE001 - setup errors are source-local too
                    error_class = type(exc).__name__
                    seen_at = datetime.now(UTC)
                    errors.append(f"saveticker: {error_class}")
                    logger.warning("saveticker client unavailable: %s", error_class)
                    for query_param in ("news", "top_stories"):
                        row = _status_row(
                            source="saveticker",
                            symbol=None,
                            poll_id=poll_id,
                            status="error",
                            count=0,
                            error_class=error_class,
                            seen_at=seen_at,
                            query_param=query_param,
                            extra={"malformed_count": 0},
                        )
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            brk_by_symbol: dict[str, int] = {}
            if kis_on and args.market == "us":
                brk_rows, brk_errs = await _kis_poll(
                    reader=kis_reader,
                    open_error=kis_open_error,
                    query_symbol=None,
                    poll_id=poll_id,
                    query_param=kn.PARAM_BRKNEWS,
                    fetch=lambda r: r.news_titles_overseas_brknews(),
                    normalize=lambda raw, seen: kn.normalize_brknews_rows(
                        raw,
                        poll_id=poll_id,
                        first_received_at=seen,
                        polled_symbols=symbols,
                    ),
                )
                errors.extend(brk_errs)
                for record in brk_rows:
                    if record.get("record_type") == "article":
                        sym = record.get("query_symbol")
                        if sym is not None:
                            brk_by_symbol[sym] = brk_by_symbol.get(sym, 0) + 1
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            for symbol in symbols:
                if args.market == "us":
                    rows, errs = await _poll_symbol(
                        symbol,
                        api_key=api_key,
                        poll_id=poll_id,
                        limit=args.limit,
                        date_from=args.date_from,
                        date_to=args.date_to,
                        skip_finnhub=args.skip_finnhub,
                        skip_benzinga=args.skip_benzinga,
                    )
                else:
                    rows, errs = [], []
                if kis_on:
                    kis_param, kis_fetch, kis_norm = _kis_symbol_calls(
                        args.market, poll_id, symbol
                    )
                    kis_rows, kis_errs = await _kis_poll(
                        reader=kis_reader,
                        open_error=kis_open_error,
                        query_symbol=symbol,
                        poll_id=poll_id,
                        query_param=kis_param,
                        fetch=kis_fetch,
                        normalize=kis_norm,
                    )
                    rows = [*rows, *kis_rows]
                    errs = [*errs, *kis_errs]
                errors.extend(errs)
                for record in rows:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                if args.market == "us":
                    counts[symbol] = {
                        "benzinga": sum(
                            1
                            for r in rows
                            if r.get("record_type") == "article"
                            and r.get("source") == "benzinga"
                            and r.get("query_param") == "primaryTickers"
                        ),
                        "benzinga_tickers": sum(
                            1
                            for r in rows
                            if r.get("record_type") == "article"
                            and r.get("source") == "benzinga"
                            and r.get("query_param") == "tickers"
                        ),
                        "finnhub": sum(
                            1
                            for r in rows
                            if r.get("record_type") == "article"
                            and r.get("source") == "finnhub"
                        ),
                    }
                    if kis_on:
                        counts[symbol]["kis"] = brk_by_symbol.get(symbol, 0) + sum(
                            1
                            for r in rows
                            if r.get("record_type") == "article"
                            and r.get("source") == kn.SOURCE
                            and r.get("query_symbol") == symbol
                            and r.get("query_param") == kn.PARAM_OVERSEAS_NEWS
                        )
                else:
                    counts[symbol] = {
                        "kis": sum(
                            1
                            for r in rows
                            if r.get("record_type") == "article"
                            and r.get("source") == kn.SOURCE
                        )
                    }
                completed.append(symbol)
                # Explicit zeros stay visible: a symbol with no coverage is a
                # gap data point (the tickers=NVDA anomaly), not silence.
                logger.info("poll %s %s: %s", poll_id, symbol, counts[symbol])
            fh.write(
                json.dumps(
                    {
                        "record_type": "poll_completion",
                        "poll_id": poll_id,
                        "attempt_id": attempt_id,
                        "symbols_completed": completed,
                        "finished_at": datetime.now(UTC).astimezone(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    finally:
        if kis_reader is not None:
            close = getattr(kis_reader, "close", None)
            if close is not None:
                await close()

    report = {
        "poll_id": poll_id,
        "attempt_id": attempt_id,
        "out_path": str(out_path),
        "counts": counts,
        "errors": errors,
    }
    print(json.dumps(report, sort_keys=True))
    if errors:
        # A source outage must never look like a clean collection: the
        # shadow could otherwise collect no Benzinga data for days while
        # Dagu logs success.
        print(f"errors: {'; '.join(errors)}", file=sys.stderr)
        return 1
    return 0


def _iter_inputs(raw_inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in raw_inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.glob("*.jsonl")))
        else:
            paths.append(path)
    return paths


def _run_summary(args: argparse.Namespace) -> int:
    paths = _iter_inputs(args.input)
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"input not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not paths:
        print("no JSONL inputs", file=sys.stderr)
        return 2
    ben, fin, frames, kis = [], [], [], []
    saveticker = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if args.poll_id and record.get("poll_id") != args.poll_id:
                    continue
                if record.get("record_type") in ("poll_header", "poll_completion"):
                    frames.append(record)
                elif record.get("source") == "benzinga":
                    ben.append(record)
                elif record.get("source") == "finnhub":
                    fin.append(record)
                elif record.get("source") == kn.SOURCE:
                    kis.append(record)
                elif record.get("source") == "saveticker":
                    saveticker.append(record)
    summary = bz.summarize_shadow(ben, fin, frames)
    summary["kis"] = kn.summarize_kis(kis, ben, fin, frames)
    summary["saveticker"] = summarize_saveticker(saveticker, ben, fin, kis)
    summary["poll_id"] = args.poll_id
    summary["inputs"] = [str(p) for p in paths]
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


def _record_symbols(record: dict) -> set[str]:
    symbols: set[str] = set()
    for field in ("symbols", "stocks", "tickers"):
        values = record.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str):
                symbols.add(value.upper())
            elif isinstance(value, dict):
                symbol = value.get("symbol")
                if isinstance(symbol, str):
                    symbols.add(symbol.upper())
    query_symbol = record.get("query_symbol")
    if isinstance(query_symbol, str):
        symbols.add(query_symbol.upper())
    return symbols


def _record_time(record: dict) -> datetime | None:
    fields = (
        ("source_created_utc",)
        if record.get("source") == "saveticker"
        else ("created_utc", "created_at")
    )
    for field in fields:
        raw = record.get(field)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    return None


def _first_received(record: dict) -> datetime | None:
    raw = record.get("first_received_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def summarize_saveticker(
    saveticker_records: list[dict],
    benzinga_records: list[dict],
    finnhub_records: list[dict],
    kis_records: list[dict],
    *,
    time_window_minutes: int = 10,
) -> dict:
    """Compare Save+ observations without matching translated headlines cross-source.

    Normalized Korean titles are compared only within Save+. Across sources,
    a pair overlaps only when its ticker sets intersect and provider creation
    times are within the fixed ten-minute window.
    """
    articles = [r for r in saveticker_records if r.get("record_type") == "article"]
    attention = [r for r in saveticker_records if r.get("record_type") == "attention"]
    statuses = [r for r in saveticker_records if r.get("record_type") == "poll_status"]

    def dedupe_source(records: list[dict]) -> list[dict]:
        by_id: dict[str, dict] = {}
        unkeyed: list[dict] = []
        for record in records:
            if record.get("record_type") != "article":
                continue
            raw_id = record.get("provider_id")
            if not isinstance(raw_id, str) or not raw_id:
                unkeyed.append({**record, "symbols": sorted(_record_symbols(record))})
                continue
            prior = by_id.get(raw_id)
            if prior is None:
                by_id[raw_id] = {**record, "symbols": sorted(_record_symbols(record))}
                continue
            merged_symbols = _record_symbols(prior) | _record_symbols(record)
            prior_seen, current_seen = _first_received(prior), _first_received(record)
            content_fields = (
                "title",
                "teaser",
                "url",
                "created_raw",
                "created_utc",
                "updated_raw",
                "updated_utc",
                "published_at_raw",
                "published_at",
            )
            changed = any(
                prior.get(name) != record.get(name) for name in content_fields
            )
            if changed:
                prior["revised_at"] = (
                    current_seen.astimezone(UTC).isoformat() if current_seen else None
                )
            if current_seen is not None and (
                prior_seen is None or current_seen < prior_seen
            ):
                revised_at = prior.get("revised_at")
                prior = {**record, "symbols": sorted(merged_symbols)}
                prior["revised_at"] = revised_at
                by_id[raw_id] = prior
            else:
                prior["symbols"] = sorted(merged_symbols)
        return [*by_id.values(), *unkeyed]

    unique_sources = {
        "saveticker": dedupe_source(articles),
        "benzinga": dedupe_source(benzinga_records),
        "finnhub": dedupe_source(finnhub_records),
        "kis": dedupe_source(kis_records),
    }
    source_counts: dict[str, set[str]] = {
        "saveticker": {str(r.get("provider_id")) for r in articles},
        "benzinga": {
            str(r.get("provider_id"))
            for r in benzinga_records
            if r.get("record_type") == "article"
        },
        "finnhub": {
            str(r.get("provider_id"))
            for r in finnhub_records
            if r.get("record_type") == "article"
        },
        "kis": {
            str(r.get("provider_id"))
            for r in kis_records
            if r.get("record_type") == "article"
        },
    }
    ticker_coverage: dict[str, dict[str, set[str]]] = {}
    for source_name, records in unique_sources.items():
        for record in records:
            if record.get("record_type") != "article":
                continue
            provider_id = str(record.get("provider_id") or "")
            for symbol in _record_symbols(record):
                ticker_coverage.setdefault(symbol, {}).setdefault(
                    source_name, set()
                ).add(provider_id)
    title_groups: dict[str, set[str]] = {}
    for record in articles:
        key = st.summary_key_title(record)
        if key:
            title_groups.setdefault(key, set()).add(str(record.get("provider_id")))
    internal_title_overlap = sum(max(0, len(ids) - 1) for ids in title_groups.values())
    pair_matches = {source: set() for source in ("benzinga", "finnhub", "kis")}
    first_seen: list[dict] = []
    all_for_order = {
        **unique_sources,
    }
    for source_name, records in all_for_order.items():
        for record in records:
            if record.get("record_type") != "article":
                continue
            for symbol in sorted(_record_symbols(record)):
                first_seen.append(
                    {
                        "poll_id": record.get("poll_id"),
                        "symbol": symbol,
                        "source": source_name,
                        "provider_id": record.get("provider_id"),
                        "first_received_at": record.get("first_received_at"),
                        "observed_at": record.get("first_received_at"),
                        "event_time": record.get("source_created_utc")
                        if source_name == "saveticker"
                        else record.get("created_utc"),
                        "publish_time": record.get("created_utc")
                        if source_name == "saveticker"
                        else None,
                    }
                )
    other_by_source = {source: unique_sources[source] for source in pair_matches}
    for save_record in unique_sources["saveticker"]:
        save_time = _record_time(save_record)
        save_symbols = _record_symbols(save_record)
        for source_name, records in other_by_source.items():
            for other in records:
                if other.get("record_type") != "article":
                    continue
                other_time = _record_time(other)
                shared = save_symbols & _record_symbols(other)
                if not shared or save_time is None or other_time is None:
                    continue
                if (
                    abs((save_time - other_time).total_seconds())
                    <= time_window_minutes * 60
                ):
                    pair_matches[source_name].add(
                        (
                            str(save_record.get("provider_id")),
                            str(other.get("provider_id")),
                        )
                    )
    pair_counts = {source: len(pairs) for source, pairs in pair_matches.items()}
    first_seen_by_key: dict[tuple[str, str, str], dict] = {}
    for row in first_seen:
        key = (row["source"], str(row.get("provider_id") or ""), row["symbol"])
        prior = first_seen_by_key.get(key)
        current_at, prior_at = _first_received(row), _first_received(prior or {})
        if prior is None or (
            current_at is not None and (prior_at is None or current_at < prior_at)
        ):
            first_seen_by_key[key] = row
    first_seen = list(first_seen_by_key.values())
    first_seen.sort(
        key=lambda r: (
            str(r.get("first_received_at") or ""),
            r["symbol"],
            r["source"],
            str(r["provider_id"]),
        )
    )
    return {
        "coverage": {
            "article_count_by_source": {
                source: len(ids) for source, ids in sorted(source_counts.items())
            },
            "by_ticker": {
                symbol: {source: len(ids) for source, ids in sorted(by_source.items())}
                for symbol, by_source in sorted(ticker_coverage.items())
            },
            "poll_status": [
                {
                    "poll_id": row.get("poll_id"),
                    "query_param": row.get("query_param"),
                    "status": row.get("status"),
                    "count": row.get("count"),
                    "malformed_count": row.get("malformed_count", 0),
                    "truncated_count": row.get("truncated_count", 0),
                    "error_class": row.get("error_class"),
                }
                for row in statuses
            ],
            "attention_rows": len(attention),
            "unmatched_symbol_articles": sum(
                not _record_symbols(row) for row in unique_sources["saveticker"]
            ),
            "ticker_matching_note": (
                "Save+는 미국 기사에만 티커를 제공합니다. 한국 기사는 라벨만 있고 "
                "symbol=null로 미매칭 처리됩니다."
            ),
        },
        "first_seen_ordering": first_seen,
        "headline_overlap": {
            "saveticker_normalized_title_duplicates": internal_title_overlap,
            "cross_source_ticker_time_window_minutes": time_window_minutes,
            "cross_source_pairs_by_source": pair_counts,
            "cross_source_match_rule": "ticker가 겹치고 제공자 created_at 차이가 지정 창 이내; 번역 제목은 비교하지 않음",
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    gated = _check_gate()
    if gated is not None:
        return gated
    setup_logging_and_sentry(service_name="benzinga-news-shadow")
    if args.command == "poll":
        return asyncio.run(_run_poll(args))
    if args.command == "summary":
        return _run_summary(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
