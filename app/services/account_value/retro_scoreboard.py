"""retro/v1 ``retro_scoreboard`` read built on the manual account-value report.

Read-only projection of ``load_weekly_report`` into the closed
``RetroScoreboardResponse`` shape (``contracts/retro/v1/openapi.yaml``).

* External-flow completeness is a server-side fact. No argument can declare it;
  operator confirmations determine the value for each account and window.
* Account, market and sleeve identities leave only as opaque ``ref_`` digests.
* Every amount and rate is an exact decimal string (never a float).
* No snapshot or flow writer is imported or called, and nothing is refreshed.
"""

import hashlib
import hmac
import re
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text

from app.models.account_value import AccountValueSnapshot
from app.services.account_value.completeness import load_confirmations
from app.services.account_value.load_report import load_weekly_report
from app.services.account_value.report import ReportRow

MAX_SPAN_DAYS = 366
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
STATEMENT_TIMEOUT_MS = 8000
TIMEZONE = "UTC"

_REF_PREFIX = "ref_"
_REF_HEX_CHARS = 32
_CURSOR_PREFIX = "o"
_REASON_MAX = 128

_BASIS = {
    "modified_dietz_account_krw": "modified_dietz",
    "holdings_only": "holdings_only",
    "sleeve_attributed_pnl_over_budget": "sleeve_budget",
}
_CURRENCY = {"account": "KRW", "kr": "KRW", "us": "USD", "crypto": "KRW"}
_BENCHMARK_SOURCE = {
    "kr": "krx_069500_close",
    "us": "voo_adjusted_close",
    "crypto": "cash_zero",
}
_BENCHMARK_EVIDENCE = {"kr": "broker", "crypto": "inference"}
# Report reason codes that describe the benchmark, never the measured return.
_BENCHMARK_REASONS = {
    "benchmark_missing": "missing_data",
    "benchmark_invalid": "unavailable",
    "benchmark_total_return_unverified": "source_mixed",
    "benchmark_unknown": "unavailable",
    "unknown_market": "unsupported",
    "account_benchmark_unavailable": "not_applicable",
}
_SCOPE_ORDER = {"account": 0, "market": 1, "sleeve": 2}


def _bad(message: str) -> ValueError:
    return ValueError(message)


def _day(name: str, value) -> date:
    if isinstance(value, datetime):
        raise _bad(f"{name} must be a date")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise _bad(f"{name} must be an ISO date")


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        raise _bad("non-finite decimal")
    text_value = format(value, "f")
    if "." in text_value:
        text_value = text_value.rstrip("0").rstrip(".")
    return "0" if text_value in {"", "-0"} else text_value


def _ref(*parts: str) -> str:
    """Keyed digest: the server secret keeps guessable inputs (broker names,
    sleeve keys, ``identity_unknown`` accounts) from being matched offline."""
    from app.core.config import settings

    key = hmac.new(
        settings.SECRET_KEY.encode(), b"retro-v1-subject-ref", hashlib.sha256
    ).digest()
    digest = hmac.new(key, "\0".join(parts).encode(), hashlib.sha256).hexdigest()
    return _REF_PREFIX + digest[:_REF_HEX_CHARS]


def _code(reason: str) -> str:
    """Drop any suffix after ``:`` (it can carry a symbol); keep only the code."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", reason.split(":", 1)[0])


def _unknown_reason(code: str) -> str:
    if code == "flows_not_confirmed":
        return "flows_not_confirmed"
    if code in {"benchmark_total_return_unverified"}:
        return "source_mixed"
    if code.startswith(("missing_", "flow_fx", "valuation_", "invalid_")) or code in {
        "boundary_flow_time_unknown",
        "sleeve_snapshot_missing",
        "sleeve_period_budget_unavailable",
    }:
        return "missing_data"
    return "incomplete_evidence"


def _benchmark(row: ReportRow, scope: str, market: str) -> tuple[dict, str | None]:
    """Return the benchmark object and the unknown reason of its return, if any."""
    if row.benchmark is not None:
        return {
            "return_rate": _decimal_text(row.benchmark),
            "source": _BENCHMARK_SOURCE.get(market),
            "basis": _BENCHMARK_EVIDENCE.get(market, "unknown"),
            "unknown_reason": None,
        }, None
    codes = [_code(code) for code in row.reasons]
    reason = next(
        (_BENCHMARK_REASONS[code] for code in codes if code in _BENCHMARK_REASONS),
        "not_applicable" if scope == "account" else "unavailable",
    )
    return {
        "return_rate": None,
        "source": _BENCHMARK_SOURCE.get(market),
        "basis": "unknown",
        "unknown_reason": reason,
    }, reason


def _score_row(row: ReportRow, flows_complete=False) -> tuple[dict, dict[str, str]]:
    """One ScoreRow plus ``{field: UnknownReason}`` for each null value."""
    scope = (
        "account" if row.market == "account" else "sleeve" if row.sleeve else "market"
    )
    unknown: dict[str, str] = {}
    own_codes = sorted(
        {_code(code) for code in row.reasons} - _BENCHMARK_REASONS.keys()
    )
    benchmark, benchmark_unknown = _benchmark(row, scope, row.market)
    result = {
        "scope": scope,
        "subject_ref": _ref(row.account, row.sleeve or "")
        if scope == "sleeve"
        else _ref(row.account),
        "market": row.market,
        "basis": _BASIS[row.basis],
        "evidence_basis": "unknown"
        if row.return_rate is None
        else "inference"
        if scope == "sleeve"
        else "broker",
        "return_rate": _decimal_text(row.return_rate),
        "unmeasured_reason": None,
        "flows_complete": flows_complete if scope == "account" else None,
        "start_amount": _decimal_text(row.start_value),
        "end_amount": _decimal_text(row.end_value),
        "flow_amount": _decimal_text(row.flows),
        "currency": _CURRENCY[row.market],
        "unit": "currency",
        "benchmark": benchmark,
    }
    if row.return_rate is None:
        # flows_not_confirmed leads so it survives; whole codes are dropped
        # from the tail when the joined text would exceed the field cap.
        ordered = sorted(own_codes, key=lambda code: code != "flows_not_confirmed")
        kept = ordered[:1]
        for code in ordered[1:]:
            if len(".".join([*kept, code])) > _REASON_MAX:
                break
            kept.append(code)
        reason = ".".join(kept) or "missing_data"
        result["unmeasured_reason"] = reason
        primary = (
            "flows_not_confirmed"
            if "flows_not_confirmed" in own_codes
            else own_codes[0]
            if own_codes
            else ""
        )
        unknown["return_rate"] = _unknown_reason(primary)
    else:
        unknown["unmeasured_reason"] = "not_applicable"
    for name, value in (
        ("start_amount", row.start_value),
        ("end_amount", row.end_value),
        ("flow_amount", row.flows),
    ):
        if value is None:
            unknown[name] = "not_applicable" if scope == "sleeve" else "missing_data"
    if scope != "account":
        unknown["flows_complete"] = "not_applicable"
    if benchmark_unknown is not None:
        unknown["benchmark.return_rate"] = benchmark_unknown
    else:
        unknown["benchmark.unknown_reason"] = "not_applicable"
    if benchmark["source"] is None:
        unknown["benchmark.source"] = (
            "not_applicable" if scope == "account" else "unavailable"
        )
    return result, unknown


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if (
        not cursor.startswith(_CURSOR_PREFIX)
        or not cursor[1:].isascii()
        or not cursor[1:].isdigit()
        or len(cursor) > 12
    ):
        raise _bad("cursor is not valid")
    return int(cursor[1:])


async def _begin_read_only(session) -> None:
    # Must be the first statement of the transaction.
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    )
    result = await session.execute(text("SHOW transaction_read_only"))
    if result.scalar_one() != "on":
        raise RuntimeError("transaction_read_only is not on")
    await session.execute(
        text("SELECT set_config('statement_timeout', :value, true)"),
        {"value": str(STATEMENT_TIMEOUT_MS)},
    )


async def read_retro_scoreboard(
    session, *, limit: int = DEFAULT_LIMIT, cursor: str | None = None, **window
) -> dict:
    """Return the ``RetroScoreboardResponse`` for ``from``..``to`` (inclusive).

    ``from`` is a Python keyword, so the two date names arrive as keyword
    arguments (``read_retro_scoreboard(session, **{"from": ..., "to": ...})``).
    Any other keyword, in particular anything about flow completeness, is a
    ``TypeError``: the value is decided on the server.
    Invalid parameters raise ``ValueError``.
    """
    if set(window) != {"from", "to"}:
        raise TypeError("expected exactly the keyword arguments 'from' and 'to'")
    start, end = _day("from", window["from"]), _day("to", window["to"])
    if end < start or (end - start).days + 1 > MAX_SPAN_DAYS:
        raise _bad("invalid date span")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _bad("limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise _bad("limit out of range")
    offset = _offset(cursor)

    await _begin_read_only(session)
    now = datetime.now(UTC)
    confirmations = await load_confirmations(session, start, end)
    rows = await load_weekly_report(session, start, end, confirmations=confirmations)
    data_as_of = (
        await session.execute(
            select(func.max(AccountValueSnapshot.observed_at)).where(
                AccountValueSnapshot.day >= start, AccountValueSnapshot.day <= end
            )
        )
    ).scalar_one_or_none()

    scored = [_score_row(row, row.flows_complete is True) for row in rows]
    scored.sort(
        key=lambda item: (
            _SCOPE_ORDER[item[0]["scope"]],
            item[0]["subject_ref"],
            item[0]["market"],
        )
    )
    page = scored[offset : offset + limit]
    unknowns = [
        {"field": f"data.{index}.{field}", "reason": reason}
        for index, (_, fields) in enumerate(page)
        for field, reason in fields.items()
    ]
    if data_as_of is None:
        unknowns.append({"field": "data_as_of", "reason": "missing_data"})
    more = offset + limit < len(scored)
    return {
        "as_of": _iso(now),
        "data_as_of": _iso(data_as_of) if data_as_of is not None else None,
        "timezone": TIMEZONE,
        "total": len(scored),
        "truncated": more,
        "limit": limit,
        "cursor": cursor,
        "next_cursor": f"{_CURSOR_PREFIX}{offset + limit}" if more else None,
        "unknowns": unknowns,
        "data": [item for item, _ in page],
    }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
