"""Closed journal trades and their live-account retrospectives."""

import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective
from app.models.trade_journal import TradeJournal
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    MAX_SOURCE_ROWS,
    Unknowns,
    build_response,
    check_limit,
    check_span,
    check_text,
    dec,
    decode_cursor,
    guarded,
    iso,
    kst_window,
    latest,
    make_ref,
    params_digest,
    parse_date,
    read_snapshot,
    resolve_from,
    safe_code,
)
from app.services.retro_v1.market import MARKETS
from app.services.retro_v1.trade_evidence import (
    TradeEvidence,
    load_trade_evidence,
    trade_evidence_fields,
)
from app.services.trade_journal.pnl_status import PNL_STATUS_CLASS, classify_pnl_status

_LIVE_MODES = ("kis_live", "toss_live", "upbit_live")
_FORBIDDEN = (
    "accountno",
    "accountnumber",
    "accountid",
    "brokerorderid",
    "orderno",
    "odno",
    "rawpayload",
    "rawresponse",
    "rawerror",
    "rawexception",
    "errordetail",
    "stacktrace",
    "traceback",
    "exception",
)


def _account_number_like(value: str) -> bool:
    for match in re.finditer(r"(?<!\d)\d{2,}[- ]\d{2,}[- ]\d{2,}(?!\d)", value):
        candidate = match.group()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            try:
                date.fromisoformat(candidate)
                continue
            except ValueError:
                pass
        if "-" in candidate or sum(char.isdigit() for char in candidate) >= 7:
            return True
    return False


def filter_prose(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    tokens = re.findall(r"[A-Za-z0-9_-]+", value.lower())
    normalized = [re.sub(r"[_-]", "", token) for token in tokens]
    if (
        re.search(r"\d{7,}", value)
        or _account_number_like(value)
        or any(
            "".join(normalized[i : i + width]) in _FORBIDDEN
            for i in range(len(normalized))
            for width in (1, 2, 3)
        )
    ):
        return None, True
    return value.strip()[:1000], False


def _amount(row: Any) -> tuple[Decimal | None, str | None]:
    for name, currency in (
        ("total_pnl_krw", "KRW"),
        ("security_pnl_usd", "USD"),
        ("security_pnl_krw", "KRW"),
    ):
        value = getattr(row, name)
        if value is not None:
            return value, currency
    return None, None


async def read_retro_trades(
    session: AsyncSession,
    *,
    to: Any,
    from_: Any = None,
    market: str | None = None,
    strategy: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    check_limit(limit)
    start, end = resolve_from(from_, extra), parse_date(to)
    check_span(start, end)
    if market is not None and market not in MARKETS:
        from app.services.retro_v1.envelope import RetroReadError

        raise RetroReadError("validation_error")
    if strategy is not None:
        strategy = check_text(strategy)
    digest = params_digest(
        op="retro_trades", start=start, end=end, market=market, strategy=strategy
    )
    offset = decode_cursor(cursor, digest)
    lo, hi = kst_window(start, end)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)

    async def journals_read() -> list[Any]:
        stmt = select(TradeJournal).where(
            TradeJournal.status == "closed",
            TradeJournal.account_type == "live",
            TradeJournal.exit_date >= lo,
            TradeJournal.exit_date < hi,
        )
        if market is not None:
            stmt = stmt.where(
                TradeJournal.instrument_type == MARKETS[market].instrument_type
            )
        if strategy is not None:
            stmt = stmt.where(TradeJournal.strategy == strategy)
        stmt = stmt.order_by(TradeJournal.exit_date, TradeJournal.id).limit(
            MAX_SOURCE_ROWS
        )
        return list((await session.execute(stmt)).scalars().all())

    async def reviews_read(ids: list[int]) -> list[Any]:
        stmt = (
            select(TradeRetrospective)
            .where(
                TradeRetrospective.journal_id.in_(ids),
                TradeRetrospective.account_mode.in_(_LIVE_MODES),
            )
            .order_by(TradeRetrospective.created_at, TradeRetrospective.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).scalars().all())

    async with read_snapshot(session):
        journals, journals_ok = await guarded(
            session, unknowns, "source.trade_journals", journals_read, []
        )
        reviews, reviews_ok = ([], True)
        evidence = TradeEvidence()
        if journals:
            reviews, reviews_ok = await guarded(
                session,
                unknowns,
                "source.trade_retrospectives",
                lambda: reviews_read([j.id for j in journals]),
                [],
            )
            evidence = await load_trade_evidence(session, unknowns, journals, reviews)
    journals_capped = len(journals) >= MAX_SOURCE_ROWS
    reviews_capped = len(reviews) >= MAX_SOURCE_ROWS
    if journals_capped:
        unknowns.note("source.trade_journals", "incomplete_evidence")
    if reviews_capped:
        unknowns.note("source.trade_retrospectives", "incomplete_evidence")
    complete = journals_ok and not journals_capped
    reviews_complete = complete and reviews_ok and not reviews_capped
    grouped: dict[int, list[Any]] = defaultdict(list)
    coverage = dict.fromkeys(("recorded", "not_applicable", "unknown", "missing"), 0)
    for review in reviews:
        grouped[review.journal_id].append(review)
        coverage[PNL_STATUS_CLASS[classify_pnl_status(review)]] += 1
    amounts: dict[str, Decimal] = {}
    rows = []
    infos = {info.instrument_type: info for info in MARKETS.values()}
    for journal in journals:
        kind = str(getattr(journal.instrument_type, "value", journal.instrument_type))
        info = infos.get(kind)
        amount, currency = _amount(journal)
        if amount is not None and currency:
            amounts[currency] = amounts.get(currency, Decimal(0)) + amount
        row = {
            "trade_ref": make_ref("trade", journal.id),
            "symbol": journal.symbol,
            "market": info.key if info else None,
            "side": journal.side,
            "strategy": safe_code(journal.strategy),
            "entry_at": iso(journal.created_at),
            "exit_at": iso(journal.exit_date),
            "quantity": dec(journal.quantity),
            "entry_price": dec(journal.entry_price),
            "exit_price": dec(journal.exit_price),
            "currency": info.currency if info else None,
            "return_rate": dec(journal.pnl_pct),
            "realized_amount": dec(amount),
            "realized_currency": currency,
            "exit_reason": safe_code(journal.exit_reason, fallback=None),
            "basis": "inference",
            "reviews": [],
        }
        for review in grouped[journal.id]:
            status = classify_pnl_status(review)
            item = {
                "review_ref": make_ref("review", review.id),
                "outcome": review.outcome,
                "trigger_type": safe_code(review.trigger_type, fallback=None),
                "root_cause_class": safe_code(review.root_cause_class, fallback=None),
                "pnl_status": status,
                "pnl_class": PNL_STATUS_CLASS[status],
                "realized_pnl": dec(review.realized_pnl),
                "realized_pnl_currency": review.realized_pnl_currency,
                "fill_evidence_available": review.fill_evidence_available,
                "created_at": iso(review.created_at),
            }
            for field in ("lesson", "result_summary", "rationale", "next_strategy"):
                item[field], redacted = filter_prose(getattr(review, field))
                if redacted:
                    item.setdefault("_redacted", []).append(field)
            row["reviews"].append(item)
        row.update(trade_evidence_fields(journal, grouped[journal.id], info, evidence))
        rows.append((row, {"market": "unsupported", "currency": "unsupported"}))
    response = build_response(
        as_of=as_of,
        data_as_of=latest(j.exit_date for j in journals),
        rows=rows,
        total=len(rows) if complete else None,
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
    for i, row in enumerate(response["data"]):
        for j, review in enumerate(row["reviews"]):
            redacted = review.pop("_redacted", [])
            for field, value in review.items():
                if value is None:
                    unknowns.note(
                        f"data.{i}.reviews.{j}.{field}",
                        "redacted" if field in redacted else "missing_data",
                    )
        for field, value in row.get("costs", {}).items():
            if isinstance(value, dict):
                for name, inner in value.items():
                    if inner is None:
                        unknowns.note(f"data.{i}.costs.{field}.{name}", "missing_data")
            elif value is None:
                unknowns.note(f"data.{i}.costs.{field}", "missing_data")
    if not amounts or not complete:
        unknowns.note(
            "summary.realized_by_currency",
            "missing_data" if complete else "incomplete_evidence",
        )
    if not complete:
        unknowns.note("summary.trade_count", "incomplete_evidence")
        unknowns.note("summary.return_rate_count", "incomplete_evidence")
    if not reviews_complete:
        unknowns.note("summary.pnl_coverage", "incomplete_evidence")
    response["summary"] = {
        "trade_count": len(rows) if complete else None,
        "return_rate_count": sum(j.pnl_pct is not None for j in journals)
        if complete
        else None,
        "realized_by_currency": {k: dec(v) for k, v in sorted(amounts.items())}
        if amounts and complete
        else None,
        "pnl_coverage": coverage if reviews_complete else None,
    }
    response["unknowns"] += [
        x for x in unknowns.items() if x not in response["unknowns"]
    ]
    return response
