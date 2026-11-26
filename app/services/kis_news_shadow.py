"""hk 6661: KIS news-headline rows for the news shadow (pure, no broker import).

The shadow script (``scripts/benzinga_news_shadow.py``) polls three KIS
headline reads and writes them as ``source="kis"`` JSONL rows in the same
shape as the Benzinga and Finnhub rows:

- ``brknews_title`` (FHKST01011801, US, market-wide, one call per pass):
  rows carry ``iscd1``..``iscd10``. A headline matches a polled symbol when
  one of those codes, normalized to the DB form (``app/core/symbol.py``),
  equals the symbol. A headline matching several polled symbols yields one
  row per symbol (``query_symbol``); a headline matching none is kept once
  with ``query_symbol=None`` so coverage stays visible.
- ``overseas_news_title`` (HHPSTH60100C1, US, per symbol via ``SYMB``).
- ``domestic_news_title`` (FHKST01011800, KR, per symbol via
  ``FID_INPUT_ISCD``).

Time fields (provider time, unverified): the official samples name
``data_dt``/``data_tm`` and do not state a timezone. They are read as
Asia/Seoul and marked ``kis_kst_unverified``. For the overseas news TR the
sample labels them ``조회일자``/``조회시간`` (query date/time), so the basis
is ``kis_label_query_unverified`` there. An unparsable or missing value is
``created_utc=None`` with ``created_reason`` set; it is never zero-filled.

``summarize_kis`` compares KIS against the Benzinga and Finnhub articles of
the same input. KIS rows are excluded from coverage only where KIS's own
``poll_status`` reports an error for that (poll, symbol, query_param). A
market-wide (brknews) error marks every planned symbol of that poll as
unreliable for KIS, without dropping the per-symbol KIS rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.symbol import to_db_symbol
from app.services.benzinga_news import normalize_title

SOURCE = "kis"
KST = ZoneInfo("Asia/Seoul")
KIS_TIME_BASIS = "kis_kst_unverified"
KIS_QUERY_LABEL_BASIS = "kis_label_query_unverified"

TR_BRKNEWS = "FHKST01011801"
TR_OVERSEAS_NEWS = "HHPSTH60100C1"
TR_DOMESTIC_NEWS = "FHKST01011800"

PARAM_BRKNEWS = "brknews_title"
PARAM_OVERSEAS_NEWS = "overseas_news_title"
PARAM_DOMESTIC_NEWS = "domestic_news_title"

_ICODE_FIELDS = tuple(f"iscd{i}" for i in range(1, 11))


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_kis_provider_time(
    date_raw: Any, time_raw: Any, *, basis: str = KIS_TIME_BASIS
) -> tuple[str | None, str | None, str | None]:
    """Return ``(created_raw, created_utc, created_basis)`` for one KIS row.

    ``date_raw`` must be ``YYYYMMDD`` and ``time_raw`` ``HHMMSS`` in Asia/Seoul.
    Missing values give ``created_utc=None`` with the basis ``None``; a value
    that does not parse keeps ``created_raw`` and also gives ``None``. The
    caller reads the reason from ``created_basis``: ``"missing"`` or
    ``"unparsable"`` (a ``None`` basis with a value means parsed).
    """
    d = _text(date_raw)
    t = _text(time_raw)
    raw = " ".join(x for x in (d, t) if x) or None
    if d is None and t is None:
        return None, None, "missing"
    if d is None or t is None or len(d) != 8 or len(t) != 6:
        return raw, None, "unparsable"
    if not (d.isdigit() and t.isdigit()):
        return raw, None, "unparsable"
    try:
        local = datetime(
            int(d[0:4]),
            int(d[4:6]),
            int(d[6:8]),
            int(t[0:2]),
            int(t[2:4]),
            int(t[4:6]),
            tzinfo=KST,
        )
    except ValueError:
        return raw, None, "unparsable"
    return raw, local.astimezone(UTC).isoformat(), basis


def _db_code(raw: Any) -> str | None:
    text = _text(raw)
    if text is None:
        return None
    return to_db_symbol(text.upper())


def _record(
    *,
    tr: str,
    provider_key: Any,
    title: str | None,
    symbols: list[str],
    query_symbol: str | None,
    query_param: str,
    channels: list[str],
    created: tuple[str | None, str | None, str | None],
    symbol_match: str,
    poll_id: str,
    first_received_at: datetime,
) -> dict[str, Any]:
    created_raw, created_utc, created_basis = created
    key = _text(provider_key)
    return {
        "record_type": "article",
        "source": SOURCE,
        "kis_tr": tr,
        "provider_id": f"{tr}:{key}" if key is not None else None,
        "url": None,
        "title": title,
        "teaser": None,
        "symbols": symbols,
        "query_symbol": query_symbol,
        "query_param": query_param,
        "symbol_match": symbol_match,
        "channels": channels,
        "created_raw": created_raw,
        "updated_raw": None,
        "created_basis": created_basis if created_utc else None,
        "created_reason": None if created_utc else created_basis,
        "updated_basis": None,
        "created_utc": created_utc,
        "updated_utc": None,
        "first_received_at": first_received_at.astimezone(UTC).isoformat(),
        "poll_id": poll_id,
    }


def normalize_brknews_rows(
    rows: Iterable[dict[str, Any]],
    *,
    poll_id: str,
    first_received_at: datetime,
    polled_symbols: Iterable[str],
) -> list[dict[str, Any]]:
    """Project market-wide 해외속보(제목) rows onto the shadow record shape.

    Matching rule: a polled symbol matches a headline when one of its
    ``iscd1``..``iscd10`` codes, normalized by ``to_db_symbol``, equals that
    symbol. Matched headlines produce one row per polled symbol, in polled
    order. Unmatched headlines produce one row with ``query_symbol=None``.
    """
    wanted = list(polled_symbols)
    out: list[dict[str, Any]] = []
    for row in rows:
        title = _text(row.get("hts_pbnt_titl_cntt"))
        codes = [c for c in (_db_code(row.get(f)) for f in _ICODE_FIELDS) if c]
        symbols = sorted(set(codes))
        channels = [v for v in (_text(row.get("dorg")),) if v]
        created = parse_kis_provider_time(row.get("data_dt"), row.get("data_tm"))
        code_set = set(codes)
        matched = [s for s in wanted if s in code_set]
        if not matched:
            out.append(
                _record(
                    tr=TR_BRKNEWS,
                    provider_key=row.get("cntt_usiq_srno"),
                    title=title,
                    symbols=symbols,
                    query_symbol=None,
                    query_param=PARAM_BRKNEWS,
                    channels=channels,
                    created=created,
                    symbol_match="unmatched",
                    poll_id=poll_id,
                    first_received_at=first_received_at,
                )
            )
            continue
        for symbol in matched:
            out.append(
                _record(
                    tr=TR_BRKNEWS,
                    provider_key=row.get("cntt_usiq_srno"),
                    title=title,
                    symbols=symbols,
                    query_symbol=symbol,
                    query_param=PARAM_BRKNEWS,
                    channels=channels,
                    created=created,
                    symbol_match="iscd_exact",
                    poll_id=poll_id,
                    first_received_at=first_received_at,
                )
            )
    return out


def normalize_overseas_rows(
    rows: Iterable[dict[str, Any]],
    *,
    poll_id: str,
    first_received_at: datetime,
    symbol: str,
) -> list[dict[str, Any]]:
    """Project per-symbol 해외뉴스종합(제목) rows (``outblock1``)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            _record(
                tr=TR_OVERSEAS_NEWS,
                provider_key=row.get("news_key"),
                title=_text(row.get("title")),
                symbols=[symbol],
                query_symbol=symbol,
                query_param=PARAM_OVERSEAS_NEWS,
                channels=[v for v in (_text(row.get("source")),) if v],
                created=parse_kis_provider_time(
                    row.get("data_dt"), row.get("data_tm"), basis=KIS_QUERY_LABEL_BASIS
                ),
                symbol_match="query_symbol",
                poll_id=poll_id,
                first_received_at=first_received_at,
            )
        )
    return out


def normalize_domestic_rows(
    rows: Iterable[dict[str, Any]],
    *,
    poll_id: str,
    first_received_at: datetime,
    symbol: str,
) -> list[dict[str, Any]]:
    """Project per-symbol 종합 시황/공시(제목) rows (``output``)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            _record(
                tr=TR_DOMESTIC_NEWS,
                provider_key=row.get("cntt_usiq_srno"),
                title=_text(row.get("hts_pbnt_titl_cntt")),
                symbols=[symbol],
                query_symbol=symbol,
                query_param=PARAM_DOMESTIC_NEWS,
                channels=[v for v in (_text(row.get("dorg")),) if v],
                created=parse_kis_provider_time(row.get("data_dt"), row.get("data_tm")),
                symbol_match="query_symbol",
                poll_id=poll_id,
                first_received_at=first_received_at,
            )
        )
    return out


def _planned_and_completed(
    poll_frames: Iterable[dict[str, Any]],
) -> tuple[dict[Any, set[str]], dict[Any, set[str]]]:
    planned: dict[Any, set[str]] = {}
    completed: dict[Any, set[str]] = {}
    for frame in poll_frames:
        poll_id = frame.get("poll_id")
        if poll_id is None:
            continue
        if frame.get("record_type") == "poll_header" and isinstance(
            frame.get("symbols"), list
        ):
            planned.setdefault(poll_id, set()).update(
                s for s in frame["symbols"] if isinstance(s, str)
            )
        elif frame.get("record_type") == "poll_completion" and isinstance(
            frame.get("symbols_completed"), list
        ):
            completed.setdefault(poll_id, set()).update(
                s for s in frame["symbols_completed"] if isinstance(s, str)
            )
    return planned, completed


def summarize_kis(
    kis_records: Iterable[dict[str, Any]],
    benzinga_records: Iterable[dict[str, Any]],
    finnhub_records: Iterable[dict[str, Any]],
    poll_frames: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Compare KIS headlines with Benzinga and Finnhub (pure function).

    Reliability: a ``poll_status`` error for KIS at (poll, symbol, query_param)
    excludes that cell's KIS rows. A market-wide KIS error (``query_symbol``
    null) and an incomplete poll mark the planned symbols of that poll as
    ``unreliable_polls`` for KIS coverage. Benzinga and Finnhub are excluded
    only by their own errors here; their own summary is unchanged.

    Coverage counts distinct ``provider_id`` per symbol, before de-duplication,
    so one headline tagged with several symbols counts once for each of them.
    Headline overlap uses the normalized title only (KIS rows carry no URL).
    First-seen wins compare the earliest ``first_received_at`` per normalized
    title across the three sources, for titles seen in at least two sources.
    """
    kis_all = list(kis_records)
    ben_all = list(benzinga_records)
    fin_all = list(finnhub_records)
    frames = list(poll_frames)
    planned, completed = _planned_and_completed(frames)

    kis_status = [r for r in kis_all if r.get("record_type") == "poll_status"]
    kis_articles = [r for r in kis_all if r.get("record_type") == "article"]

    own_error: set[tuple[Any, Any, Any]] = set()
    market_error_polls: set[Any] = set()
    for status in kis_status:
        if status.get("status") != "error":
            continue
        symbol = status.get("query_symbol")
        if symbol is None:
            market_error_polls.add(status.get("poll_id"))
        else:
            own_error.add((status.get("poll_id"), symbol, status.get("query_param")))

    unreliable_polls: list[dict[str, Any]] = []
    for poll_id in sorted(market_error_polls, key=str):
        for symbol in sorted(planned.get(poll_id, set())):
            unreliable_polls.append(
                {"poll_id": poll_id, "symbol": symbol, "reason": "market_wide_error"}
            )
    for poll_id in sorted(planned, key=str):
        done = completed.get(poll_id)
        if done is None:
            reason = "no_completion_record"
            missing = planned[poll_id]
        else:
            reason = "symbols_unpolled"
            missing = planned[poll_id] - done
        for symbol in sorted(missing):
            unreliable_polls.append(
                {"poll_id": poll_id, "symbol": symbol, "reason": reason}
            )
    unreliable_keys = {(p["poll_id"], p["symbol"]) for p in unreliable_polls}

    kept = [
        r
        for r in kis_articles
        if (r.get("poll_id"), r.get("query_symbol"), r.get("query_param"))
        not in own_error
    ]
    coverage: dict[str, dict[str, int]] = {}
    coverage_ids: dict[str, set[tuple[str, Any]]] = {}

    def _bump(symbol: str, source: str, provider_id: Any) -> None:
        entry = coverage.setdefault(symbol, {"kis": 0, "benzinga": 0, "finnhub": 0})
        ids = coverage_ids.setdefault(symbol, set())
        if (source, provider_id) in ids:
            return
        ids.add((source, provider_id))
        entry[source] += 1

    for record in kept:
        symbol = record.get("query_symbol")
        if symbol is None or record.get("provider_id") is None:
            continue
        _bump(symbol, SOURCE, record["provider_id"])

    own_error_by_source: dict[str, set[tuple[Any, Any]]] = {}
    for source, records in (("benzinga", ben_all), ("finnhub", fin_all)):
        errs: set[tuple[Any, Any]] = set()
        for r in records:
            if r.get("record_type") == "poll_status" and r.get("status") == "error":
                errs.add((r.get("poll_id"), r.get("query_symbol")))
        own_error_by_source[source] = errs
        for r in records:
            if r.get("record_type") != "article":
                continue
            symbol = r.get("query_symbol")
            if symbol is None or r.get("provider_id") is None:
                continue
            if (r.get("poll_id"), symbol) in errs:
                continue
            _bump(symbol, source, r["provider_id"])

    kis_ids = {
        (SOURCE, r["provider_id"]) for r in kept if r.get("provider_id") is not None
    }
    kis_unkeyed = sum(1 for r in kept if r.get("provider_id") is None)

    def _titles(records: Iterable[dict[str, Any]]) -> dict[str, datetime | None]:
        first: dict[str, datetime | None] = {}
        for r in records:
            key = normalize_title(r.get("title"))
            if key is None:
                continue
            seen = _parse_seen(r.get("first_received_at"))
            prior = first.get(key)
            if key not in first or (
                seen is not None and (prior is None or seen < prior)
            ):
                first[key] = seen
        return first

    kis_titles = _titles(kept)
    ben_titles = _titles(
        r
        for r in ben_all
        if r.get("record_type") == "article"
        and (r.get("poll_id"), r.get("query_symbol"))
        not in own_error_by_source["benzinga"]
    )
    fin_titles = _titles(
        r
        for r in fin_all
        if r.get("record_type") == "article"
        and (r.get("poll_id"), r.get("query_symbol"))
        not in own_error_by_source["finnhub"]
    )

    wins = {"kis": 0, "benzinga": 0, "finnhub": 0}
    compared = 0
    for key in set(kis_titles) | set(ben_titles) | set(fin_titles):
        seen_by = {}
        for source, titles in (
            ("kis", kis_titles),
            ("benzinga", ben_titles),
            ("finnhub", fin_titles),
        ):
            if key in titles:
                seen_by[source] = titles[key]
        if len(seen_by) < 2:
            continue
        compared += 1
        dated = {s: t for s, t in seen_by.items() if t is not None}
        if not dated:
            continue
        winner = min(dated, key=lambda s: (dated[s], s))
        wins[winner] += 1

    return {
        "kis_count": len(kis_ids),
        "kis_unkeyed_rows": kis_unkeyed,
        "kis_rows_excluded_unreliable": len(kis_articles) - len(kept),
        "kis_unmatched_headlines": sum(
            1 for r in kept if r.get("query_symbol") is None
        ),
        "kis_provider_time_null": sum(1 for r in kept if r.get("created_utc") is None),
        "provider_time_basis": {
            "kis": KIS_TIME_BASIS,
            "kis_overseas_news_label": KIS_QUERY_LABEL_BASIS,
        },
        "coverage_by_symbol": {k: coverage[k] for k in sorted(coverage)},
        "headline_overlap": {
            "kis_with_benzinga": len(set(kis_titles) & set(ben_titles)),
            "kis_with_finnhub": len(set(kis_titles) & set(fin_titles)),
        },
        "first_seen_titles_compared": compared,
        "first_seen_wins": wins,
        "first_seen_scope": "earliest_sighting_across_input_polls",
        "unreliable_polls": unreliable_polls,
        "unreliable_keys": sorted((str(p), s) for p, s in unreliable_keys),
        "unattributed_market_errors": sorted(
            str(p) for p in market_error_polls if p not in planned
        ),
    }


def _parse_seen(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


__all__ = [
    "PARAM_BRKNEWS",
    "PARAM_DOMESTIC_NEWS",
    "PARAM_OVERSEAS_NEWS",
    "SOURCE",
    "normalize_brknews_rows",
    "normalize_domestic_rows",
    "normalize_overseas_rows",
    "parse_kis_provider_time",
    "summarize_kis",
]
