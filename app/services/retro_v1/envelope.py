"""Shared envelope, snapshot and encoding helpers for the retro/v1 readers.

Contract: ``contracts/retro/v1/openapi.yaml``. Every reader returns the dict
that is the HTTP response body as-is; this module is the only place that knows
the envelope shape, the closed unknown-reason vocabulary, decimal encoding,
opaque refs and cursors.

Read-only by construction: ``read_snapshot`` starts the reader's own
``REPEATABLE READ READ ONLY`` transaction (count and rows share it) with a
statement timeout, and nothing in this package issues anything but SELECT.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

#: The contract's ``timezone`` pattern (``[A-Za-z0-9_.:-]``) cannot carry an IANA
#: name with a slash, so the response says ``KST`` (UTC+9, no DST). All request
#: dates are KST calendar days; all instants are RFC 3339 UTC.
TIMEZONE: Final = "KST"
KST: Final = ZoneInfo("Asia/Seoul")
STATEMENT_TIMEOUT_MS: Final = 8000
DEFAULT_LIMIT: Final = 100
MAX_LIMIT: Final = 500
MAX_SPAN_DAYS: Final = 366
#: A source read that returns this many rows is treated as cut off, so the
#: response cannot claim a total it did not count (``count_unavailable``).
MAX_SOURCE_ROWS: Final = 5000

UNKNOWN_REASONS: Final = frozenset(
    {
        "not_read",
        "unavailable",
        "unsupported",
        "stale",
        "upstream_error",
        "count_unavailable",
        "missing_data",
        "not_applicable",
        "flows_not_confirmed",
        "source_mixed",
        "incomplete_evidence",
    }
)
EVIDENCE_BASES: Final = frozenset({"broker", "inference", "unknown"})

_CODE_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class RetroReadError(Exception):
    """A request the reader cannot answer; ``code`` is the contract Error code.

    The message is always the fixed safe text for the code — never a raw
    exception or broker string (contract ``Error.message`` enum).
    """

    MESSAGES: Final = {
        "validation_error": "Request invalid.",
        "timeout": "Request timed out.",
        "upstream_unavailable": "Upstream unavailable.",
        "schema_mismatch": "Schema mismatch.",
    }

    def __init__(self, code: str) -> None:
        if code not in self.MESSAGES:
            raise ValueError("unknown retro read error code")
        self.code = code
        super().__init__(self.MESSAGES[code])

    @property
    def safe_message(self) -> str:
        return self.MESSAGES[self.code]


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------
def dec(value: Any) -> str | None:
    """Exact decimal string (never a float); ``None`` stays ``None``.

    A ``float`` is rejected on purpose: converting it would invent digits the
    source never had. Callers hand in ``Decimal``/``int`` (DB ``numeric``) or a
    decimal ``str`` (``screener_pick_log.decision_price_text``).
    """
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("retro/v1 decimals must be Decimal, int or str, never float")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    rendered = format(parsed, "f")
    return rendered if _DECIMAL_RE.match(rendered) else None


def iso(value: datetime | None) -> str | None:
    """RFC 3339 UTC instant; a naive datetime is not an instant → unknown."""
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def safe_code(value: Any, *, fallback: str | None = "unclassified") -> str | None:
    """A closed-shape code from a stored string, never free text.

    Stored reasons are sometimes prose; the contract allows codes only
    (``[A-Za-z0-9_.:-]``, 128 max). Anything else collapses to ``fallback``.
    """
    if value is None:
        return None
    candidate = str(value)
    if candidate and len(candidate) <= 64 and _CODE_RE.match(candidate):
        return candidate
    return fallback


def make_ref(kind: str, internal_id: object) -> str:
    """Opaque, stable, non-reversible reference to an internal row.

    Hashes the kind and the internal primary key only. A broker order number,
    account number or other broker-side identifier is never an input, so no ref
    can equal or encode one. The digest is a one-way function; the id space is
    small, so this hides identity from a reader of the response rather than
    from someone who can already enumerate the table.
    """
    digest = hashlib.sha256(f"retro.v1|{kind}|{internal_id}".encode()).hexdigest()
    return f"{kind}-{digest[:20]}"


# --------------------------------------------------------------------------
# request validation
# --------------------------------------------------------------------------
def check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise RetroReadError("validation_error")
    if not 1 <= limit <= MAX_LIMIT:
        raise RetroReadError("validation_error")
    return limit


def parse_date(value: object) -> date:
    """A ``date`` (or its ISO ``YYYY-MM-DD`` text); anything else is invalid."""
    if isinstance(value, datetime):
        raise RetroReadError("validation_error")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise RetroReadError("validation_error")


def resolve_from(from_: object, extra: dict[str, Any]) -> date:
    """The contract query ``from`` is a Python keyword.

    Callers pass it as ``from_=`` or as the literal key ``from`` in ``**params``
    (a route can splat its query dict). Any other extra key is invalid.
    """
    extra = dict(extra)
    literal = extra.pop("from", None)
    if extra or (from_ is not None) == (literal is not None):
        raise RetroReadError("validation_error")
    return parse_date(from_ if from_ is not None else literal)


def check_span(start: date, end: date) -> None:
    if end < start or (end - start).days + 1 > MAX_SPAN_DAYS:
        raise RetroReadError("validation_error")


def check_text(value: str, *, max_len: int = 64) -> str:
    stripped = value.strip() if isinstance(value, str) else ""
    if not stripped or len(stripped) > max_len or not _CODE_RE.match(stripped):
        raise RetroReadError("validation_error")
    return stripped


_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.:/\-]{1,32}$")


def check_symbol(value: str) -> str:
    """A symbol in any broker spelling (``BRK/B``, ``BRK-B``, ``KRW-BTC``)."""
    stripped = value.strip() if isinstance(value, str) else ""
    if not _SYMBOL_RE.match(stripped):
        raise RetroReadError("validation_error")
    return stripped


def kst_window(start: date, end: date) -> tuple[datetime, datetime]:
    """``[start 00:00 KST, end+1 00:00 KST)`` as aware datetimes."""
    lo = datetime.combine(start, datetime.min.time(), tzinfo=KST)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=KST)
    return lo, hi


def params_digest(**params: object) -> str:
    blob = "|".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def decode_cursor(cursor: str | None, digest: str) -> int:
    """Offset from an opaque cursor; a cursor from other params is invalid."""
    if cursor is None:
        return 0
    match = re.fullmatch(r"o(\d{1,9})-([0-9a-f]{8})", cursor)
    if not match or match.group(2) != digest:
        raise RetroReadError("validation_error")
    return int(match.group(1))


def encode_cursor(offset: int, digest: str) -> str:
    return f"o{offset}-{digest}"


# --------------------------------------------------------------------------
# snapshot + guarded sub-reads
# --------------------------------------------------------------------------
def _map_db_error(exc: BaseException) -> RetroReadError:
    # Postgres query_canceled (statement_timeout) is SQLSTATE 57014.
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code == "57014":
        return RetroReadError("timeout")
    return RetroReadError("upstream_unavailable")


@asynccontextmanager
async def read_snapshot(session: AsyncSession) -> AsyncIterator[None]:
    """The reader's own ``REPEATABLE READ READ ONLY`` transaction.

    One snapshot for the count and the rows. The session must be idle: a
    snapshot cannot be started inside a transaction the caller already owns
    (``SET TRANSACTION`` is only valid first), and rolling the caller's
    transaction back would be a side effect of a read. The transaction is READ
    ONLY, so its closing commit cannot write anything.
    """
    if session.in_transaction():
        raise RetroReadError("validation_error")
    try:
        async with session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            await session.execute(
                text("SELECT set_config('statement_timeout', :ms, true)"),
                {"ms": str(STATEMENT_TIMEOUT_MS)},
            )
            yield
    except RetroReadError:
        raise
    except (DBAPIError, OperationalError) as exc:
        raise _map_db_error(exc) from None


class Unknowns:
    """Collects ``unknowns`` entries; reasons are the closed contract set."""

    def __init__(self) -> None:
        self._items: list[dict[str, str]] = []
        self._seen: set[tuple[str, str]] = set()

    def note(self, field: str, reason: str) -> None:
        if reason not in UNKNOWN_REASONS:
            raise ValueError("unknown reason outside the retro/v1 vocabulary")
        safe_field = safe_code(field, fallback="field")
        assert safe_field is not None
        key = (safe_field, reason)
        if key not in self._seen:
            self._seen.add(key)
            self._items.append({"field": safe_field, "reason": reason})

    def has(self, field: str) -> bool:
        return any(item["field"] == field for item in self._items)

    def items(self) -> list[dict[str, str]]:
        return list(self._items)


async def guarded[T](
    session: AsyncSession,
    unknowns: Unknowns,
    field: str,
    read: Callable[[], Awaitable[T]],
    default: T,
) -> tuple[T, bool]:
    """Run one SELECT group in a SAVEPOINT; a failure is an unknown, not zero.

    Without the savepoint one failed statement aborts the whole snapshot
    transaction. Returns ``(value, ok)``; on failure ``default`` is only a
    typed placeholder — callers must not present it as data.
    """
    try:
        async with session.begin_nested():
            return await read(), True
    except (DBAPIError, OperationalError) as exc:
        mapped = _map_db_error(exc)
        unknowns.note(
            field, "upstream_error" if mapped.code != "timeout" else "unavailable"
        )
        return default, False


# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------
_EXPLANATION_FIELDS: Final = frozenset({"reason", "missing_reason"})

Row = tuple[dict[str, Any], dict[str, str]]
"""A response row and, per null field, the reason that explains the null."""


def _null_unknowns(rows: Sequence[Row], unknowns: Unknowns) -> None:
    """Every null row value needs a row reason or an unknown with its path."""
    for index, (row, hints) in enumerate(rows):
        if row.get("reason") or row.get("missing_reason"):
            continue
        for name, value in row.items():
            if value is None:
                default = (
                    "not_applicable" if name in _EXPLANATION_FIELDS else "missing_data"
                )
                unknowns.note(f"data.{index}.{name}", hints.get(name, default))


def build_response(
    *,
    as_of: datetime,
    data_as_of: datetime | None,
    rows: Sequence[Row],
    total: int | None,
    limit: int,
    cursor: str | None,
    digest: str,
    offset: int,
    unknowns: Unknowns,
) -> dict[str, Any]:
    """Page ``rows`` (already ordered, whole population) into the envelope.

    ``total`` is the population size before pagination, or ``None`` when it
    could not be counted — then ``count_unavailable`` is recorded. The page
    length is never reported as the total.
    """
    page = list(rows[offset : offset + limit])
    next_offset = offset + len(page)
    has_more = next_offset < len(rows)
    page_unknowns = Unknowns()
    for item in unknowns.items():
        page_unknowns.note(item["field"], item["reason"])
    if total is None:
        page_unknowns.note("total", "count_unavailable")
    _null_unknowns(page, page_unknowns)
    return {
        "as_of": iso(as_of),
        "data_as_of": iso(data_as_of),
        "timezone": TIMEZONE,
        "total": total,
        "truncated": has_more,
        "limit": limit,
        "cursor": cursor,
        "next_cursor": encode_cursor(next_offset, digest) if has_more else None,
        "unknowns": page_unknowns.items(),
        "data": [row for row, _ in page],
    }


def latest(values: Iterable[datetime | None]) -> datetime | None:
    aware = [v for v in values if v is not None and v.tzinfo is not None]
    return max(aware) if aware else None
