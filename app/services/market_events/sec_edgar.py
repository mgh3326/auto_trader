"""Read-only SEC EDGAR disclosure collector (hk 6407).

Fetches only official EDGAR endpoints — ``data.sec.gov`` submissions JSON and
``www.sec.gov`` ``company_tickers.json`` — and writes ``market_events`` rows in
the v2 common event form through ``MarketEventsRepository``. There is no IR page
scraping: an IR request returns ``unsupported`` with a closed reason and makes
no network call.

SEC fair access requires a declared ``User-Agent`` on every request and a
bounded request rate. ``SEC_EDGAR_USER_AGENT``
(``settings.sec_edgar_user_agent``) is set by the operator; an empty value
refuses every request before any network call. The value never appears in
logs, errors, or this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.symbol import to_db_symbol
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)

SEC_SOURCE = "sec"
CATEGORY = "disclosure"
MARKET = "us"
COUNTRY = "US"
USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ALLOWED_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})

# SEC fair access allows up to 10 requests/second; this collector stays at or
# below 5 requests/second (one request every 0.2 s).
REQUEST_INTERVAL_SECONDS = 0.2
DEFAULT_TIMEOUT_S = 15.0
_MAX_BODY_BYTES = 32 * 1024 * 1024

_ET = ZoneInfo("America/New_York")

# Disclosure forms stored as ``category='disclosure'``. Amendments carry their
# own accession numbers and belong to the same disclosure family.
SEC_FORMS = frozenset({"8-K", "10-Q", "10-K", "8-K/A", "10-Q/A", "10-K/A"})

# Parallel arrays inside ``filings.recent`` of the submissions JSON.
_RECENT_FIELDS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "act",
    "form",
    "fileNumber",
    "filmNumber",
    "items",
    "core_type",
    "size",
    "isXBRL",
    "isInlineXBRL",
    "isForeignPrivateIssuer",
    "primaryDocument",
    "primaryDocDescription",
)

# Columns whose absence silently corrupts every row (a missing ``form`` or
# ``filingDate`` filters out or mis-dates the whole response, a missing
# ``acceptanceDateTime`` degrades every timestamp to date precision). The
# submissions schema always carries them, so a missing column is a malformed
# response — ``unparseable``, never a successful zero.
_REQUIRED_RECENT_FIELDS = frozenset({"filingDate", "form", "acceptanceDateTime"})

IR_UNSUPPORTED_REASON = (
    "no official API/feed; IR page scraping requires an operator decision"
)


class SecEdgarUserAgentMissing(Exception):
    """``SEC_EDGAR_USER_AGENT`` is empty — no request may be made."""


class SecEdgarError(RuntimeError):
    """A sanitized EDGAR failure carrying a closed ``CollectionFailure`` code.

    ``failure_code`` is one of ``http_4xx``, ``http_5xx``, ``timeout``,
    ``transport``, ``bad_response``, ``unparseable`` or ``quota`` — the only
    text ever recorded in ``last_error``. Messages never carry the User-Agent.
    """

    failure_code: str = "bad_response"

    def __init__(self, message: str = "", *, failure_code: str | None = None) -> None:
        super().__init__(message)
        if failure_code is not None:
            self.failure_code = failure_code


def _http_failure_code(status: int) -> str:
    """Closed failure code for a non-200 HTTP status."""
    if status == 429:
        return "quota"
    if status >= 500:
        return "http_5xx"
    if 400 <= status < 500:
        return "http_4xx"
    # Redirects are refused (follow_redirects=False); anything else unexpected
    # is a response-shape problem, not a client error.
    return "bad_response"


class SecEdgarClient:
    """Paced, allowlisted, read-only EDGAR JSON client.

    The declared User-Agent is required at construction: an empty value raises
    :class:`SecEdgarUserAgentMissing` before any request can be made.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        client_factory: Callable[..., Any] = httpx.AsyncClient,
        sleep: Callable[[float], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        min_interval: float = REQUEST_INTERVAL_SECONDS,
    ) -> None:
        agent = settings.sec_edgar_user_agent if user_agent is None else user_agent
        agent = (agent or "").strip()
        if not agent:
            raise SecEdgarUserAgentMissing(
                f"{USER_AGENT_ENV} is empty; refusing SEC EDGAR requests"
            )
        self._user_agent = agent
        self._client_factory = client_factory
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._min_interval = min_interval
        self._last_request_at: float | None = None

    async def _pace(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = self._monotonic()

    async def _get_json(self, url: str) -> Any:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise SecEdgarError(
                "SEC EDGAR URL is outside the host allowlist",
                failure_code="bad_response",
            )
        await self._pace()
        try:
            async with self._client_factory(
                timeout=DEFAULT_TIMEOUT_S, follow_redirects=False
            ) as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": self._user_agent,
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException:
            raise SecEdgarError(
                "SEC EDGAR request timed out", failure_code="timeout"
            ) from None
        except httpx.HTTPError:
            raise SecEdgarError(
                "SEC EDGAR transport failed", failure_code="transport"
            ) from None

        status = response.status_code
        if status != 200:
            raise SecEdgarError(
                f"SEC EDGAR HTTP {status}", failure_code=_http_failure_code(status)
            )
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise SecEdgarError(
                "SEC EDGAR response is not JSON", failure_code="bad_response"
            )
        body = response.content
        if len(body) > _MAX_BODY_BYTES:
            raise SecEdgarError(
                "SEC EDGAR response exceeds size limit", failure_code="bad_response"
            )
        try:
            return json.loads(body)
        except (ValueError, json.JSONDecodeError):
            raise SecEdgarError(
                "SEC EDGAR response contains invalid JSON",
                failure_code="unparseable",
            ) from None

    async def fetch_company_tickers(self) -> Any:
        """GET the official ticker→CIK map (``www.sec.gov``)."""
        return await self._get_json(COMPANY_TICKERS_URL)

    async def fetch_submissions(self, cik: int) -> Any:
        """GET one company's submissions JSON (``data.sec.gov``)."""
        if type(cik) is not int or cik <= 0:
            raise ValueError("cik must be a positive int")
        return await self._get_json(SUBMISSIONS_URL.format(cik=f"{cik:010d}"))


def _norm_ticker(ticker: str) -> str:
    return to_db_symbol(ticker.strip().upper())


def index_company_tickers(payload: Any) -> dict[str, tuple[int, str]]:
    """Index ``company_tickers.json`` as normalized ticker → (CIK, title)."""
    if not isinstance(payload, dict):
        raise SecEdgarError(
            "SEC EDGAR company_tickers response has an invalid shape",
            failure_code="unparseable",
        )
    index: dict[str, tuple[int, str]] = {}
    for entry in payload.values():
        if not isinstance(entry, dict):
            raise SecEdgarError(
                "SEC EDGAR company_tickers row has an invalid shape",
                failure_code="unparseable",
            )
        cik = entry.get("cik_str")
        ticker = entry.get("ticker")
        title = entry.get("title")
        if type(cik) is not int or cik <= 0 or not isinstance(ticker, str):
            raise SecEdgarError(
                "SEC EDGAR company_tickers row has an invalid shape",
                failure_code="unparseable",
            )
        index[_norm_ticker(ticker)] = (cik, title if isinstance(title, str) else "")
    return index


def _filing_rows(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split ``filings.recent`` parallel arrays into per-filing row dicts.

    Returns ``(rows, meta)`` where ``meta`` carries ``older_history_uncollected``
    (``filings.files`` non-empty → older chunks exist that this collector does
    not fetch) and ``recent_earliest`` (oldest ``filingDate`` present, or None).
    """
    if not isinstance(payload, dict):
        raise SecEdgarError(
            "SEC EDGAR submissions response has an invalid shape",
            failure_code="unparseable",
        )
    filings = payload.get("filings")
    if not isinstance(filings, dict):
        raise SecEdgarError(
            "SEC EDGAR submissions response has an invalid shape",
            failure_code="unparseable",
        )
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        raise SecEdgarError(
            "SEC EDGAR submissions response has an invalid shape",
            failure_code="unparseable",
        )
    accessions = recent.get("accessionNumber")
    older_files = filings.get("files")
    older_uncollected = isinstance(older_files, list) and bool(older_files)
    for field in _REQUIRED_RECENT_FIELDS:
        if not isinstance(recent.get(field), list):
            raise SecEdgarError(
                f"SEC EDGAR submissions recent {field} column is missing",
                failure_code="unparseable",
            )
    if accessions == []:
        return [], {"older_history_uncollected": older_uncollected}
    if not isinstance(accessions, list) or not all(
        isinstance(item, str) and item.strip() for item in accessions
    ):
        raise SecEdgarError(
            "SEC EDGAR submissions recent accessionNumber is invalid",
            failure_code="unparseable",
        )
    count = len(accessions)
    columns: dict[str, list[Any]] = {}
    for field in _RECENT_FIELDS:
        values = recent.get(field)
        if values is None:
            values = [""] * count
        if not isinstance(values, list) or len(values) != count:
            raise SecEdgarError(
                f"SEC EDGAR submissions recent {field} length mismatch",
                failure_code="unparseable",
            )
        columns[field] = values
    rows = [
        {field: columns[field][index] for field in _RECENT_FIELDS}
        for index in range(count)
    ]
    dates = []
    for row in rows:
        raw_date = row.get("filingDate")
        if isinstance(raw_date, str):
            try:
                dates.append(date.fromisoformat(raw_date.strip()))
            except ValueError:
                continue
    meta = {
        "older_history_uncollected": older_uncollected,
        "recent_earliest": min(dates) if dates else None,
    }
    return rows, meta


def _window_complete(
    *,
    older_history_uncollected: bool,
    recent_earliest: date | None,
    from_date: date | None,
    to_date: date | None,
) -> bool:
    """Whether the collected scope provably covers the requested window (§2.4).

    ``filings.recent`` is the complete *recent* listing; ``filings.files``
    points at older chunks this collector never fetches. A run with no explicit
    date bounds collects the recent scope only, which is complete by
    definition. An explicit bound reaching before the oldest recent filing
    date leaves the older part unproven → ``False`` (partition stays pending).
    """
    if from_date is None and to_date is None:
        return True
    if not older_history_uncollected:
        return True
    if recent_earliest is None:
        return False
    lower = from_date or date.min
    return lower >= recent_earliest


_ACCEPTANCE_DIGITS_RE = re.compile(r"^\d{14}$")


def _et_offset_text(local: datetime) -> str:
    offset = local.utcoffset()
    assert offset is not None
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, remainder = divmod(abs(total), 3600)
    return f"{sign}{hours:02d}:{remainder // 60:02d}"


def _parse_acceptance(raw: Any) -> tuple[datetime, str, datetime] | None:
    """Parse EDGAR ``acceptanceDateTime``.

    The submissions JSON returns ISO-8601 UTC (``2026-11-27T18:06:41.000Z`` —
    verified against the filing index page's Eastern "Accepted" time); the
    SGML-header ``YYYYMMDDHHMMSS`` form is US Eastern wall time. Returns
    ``(aware_utc, offset_text, et_local_naive)`` where ``offset_text`` is the
    ``America/New_York`` offset at that instant, or ``None`` when the value
    is missing or malformed — the caller then falls back to date precision
    from ``filingDate``.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    aware_utc: datetime | None = None
    if _ACCEPTANCE_DIGITS_RE.fullmatch(text):
        try:
            aware_utc = (
                datetime.strptime(text, "%Y%m%d%H%M%S")
                .replace(tzinfo=_ET)
                .astimezone(UTC)
            )
        except ValueError:
            return None
    else:
        iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        aware_utc = parsed.astimezone(UTC)
    local = aware_utc.astimezone(_ET)
    return aware_utc, _et_offset_text(local), local.replace(tzinfo=None)


def _safe_primary_document(value: Any) -> str | None:
    """Return a safe relative document path or ``None``."""
    if not isinstance(value, str):
        return None
    doc = value.strip()
    if not doc or doc.startswith("/") or ".." in doc:
        return None
    if any(char.isspace() or ord(char) < 32 for char in doc):
        return None
    return doc


def _filing_url(cik: int, accession: str, primary_document: Any) -> str:
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    doc = _safe_primary_document(primary_document)
    return f"{base}/{doc}" if doc else f"{base}/"


def normalize_sec_filing(
    row: dict[str, Any],
    *,
    cik: int,
    symbol: str | None,
    symbols: list[str] | None,
    company_name: str | None,
    received_at: datetime,
) -> dict[str, Any]:
    """Normalize one ``filings.recent`` row into a ``market_events`` payload.

    ``source_event_id`` is the accession number. ``published_at`` comes from
    ``acceptanceDateTime`` (Eastern → UTC, ``published_precision='second'``);
    a missing/malformed acceptance time degrades to ``published_date`` from
    ``filingDate`` with ``published_precision='date'``.
    """
    accession = row.get("accessionNumber")
    if not isinstance(accession, str) or not accession.strip():
        raise SecEdgarError(
            "SEC filing row missing accessionNumber", failure_code="unparseable"
        )
    accession = accession.strip()
    form = row.get("form")
    if form not in SEC_FORMS:
        raise SecEdgarError(
            f"SEC filing form {form!r} is outside the collected set",
            failure_code="unparseable",
        )

    filing_date_raw = row.get("filingDate")
    event_date: date | None = None
    if isinstance(filing_date_raw, str):
        try:
            event_date = date.fromisoformat(filing_date_raw.strip())
        except ValueError:
            event_date = None

    acceptance = _parse_acceptance(row.get("acceptanceDateTime"))
    if event_date is None and acceptance is not None:
        event_date = acceptance[2].date()
    if event_date is None:
        raise SecEdgarError(
            "SEC filing row has no usable filing date",
            failure_code="unparseable",
        )

    if acceptance is not None:
        published_at, published_offset, published_local = acceptance
        publication = {
            "published_at": published_at,
            "published_date": None,
            "published_offset": published_offset,
            "published_precision": "second",
        }
        release_time_utc = published_at
        release_time_local = published_local
        provider_created_raw = (
            row.get("acceptanceDateTime")
            if isinstance(row.get("acceptanceDateTime"), str)
            else None
        )
    else:
        publication = {
            "published_at": None,
            "published_date": event_date,
            "published_offset": None,
            "published_precision": "date",
        }
        release_time_utc = None
        release_time_local = None
        provider_created_raw = None

    items = row.get("items")
    items_text = items.strip() if isinstance(items, str) else ""
    title = form if not items_text else f"{form} (items {items_text})"

    # ``backfilled`` marks rows collected after their filing day — first-
    # insert-only evidence (hk 6406). Same-day collection is forward-looking;
    # anything older was filled retroactively (§2.5).
    received = (
        received_at
        if received_at.tzinfo is not None
        else received_at.replace(tzinfo=UTC)
    )
    collection_day = received.astimezone(_ET).date()

    return {
        "category": CATEGORY,
        "market": MARKET,
        "country": COUNTRY,
        "symbol": symbol,
        "company_name": company_name or None,
        "title": title,
        "event_date": event_date,
        "release_time_utc": release_time_utc,
        "release_time_local": release_time_local,
        "source_timezone": "America/New_York",
        "time_hint": "unknown",
        "importance": None,
        "status": "released",
        "source": SEC_SOURCE,
        "source_event_id": accession,
        "source_url": _filing_url(cik, accession, row.get("primaryDocument")),
        "fiscal_year": None,
        "fiscal_quarter": None,
        "raw_payload_json": {**row, "edgar_cik": f"{cik:010d}"},
        "provider_created_at": release_time_utc,
        "provider_created_raw": provider_created_raw,
        "provider_updated_at": None,
        "provider_updated_raw": None,
        "first_received_at": received_at,
        "fetched_at": received_at,
        **publication,
        "revised_at": None,
        "effective_at": None,
        "relevance": "direct" if symbol else None,
        "relevance_state": "known" if symbol else "unknown",
        "relevance_basis": "recorded" if symbol else None,
        "instrument_type": "equity_us" if symbol else None,
        "group_key": None,
        "redistributable": True,
        "symbols": symbols,
        "backfilled": event_date < collection_day,
    }


# Bump when ``normalize_sec_filing`` output changes so already-stored rows
# compare unequal once and pick up the correction instead of staying
# ``unchanged`` forever (content_hash covers normalized output, not just the
# raw row).
_NORMALIZER_VERSION = 1

# Fields that describe *this filer's* view of an accession rather than the
# filing itself: co-registrants file one accession under several CIKs and
# their ``filings.recent`` rows differ here (verified shape — fileNumber and
# filmNumber are per registrant, primaryDocument is CIK-scoped). Excluding
# them keeps revision detection on filing content and stops symbol flip-flop.
_DIGEST_EXCLUDED_FIELDS = frozenset({"fileNumber", "filmNumber", "primaryDocument"})


def _content_digest(raw: dict[str, Any]) -> str:
    canonical = {
        key: value for key, value in raw.items() if key not in _DIGEST_EXCLUDED_FIELDS
    }
    return hashlib.sha256(
        json.dumps(
            {"v": _NORMALIZER_VERSION, "row": canonical},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


async def _write_filing(
    repository: MarketEventsRepository,
    event: dict[str, Any],
    raw: dict[str, Any],
    *,
    observed_at: datetime,
) -> str:
    """Compare-and-append one filing, then upsert the current row.

    Returns ``inserted`` / ``revised`` / ``unchanged`` / ``stale``. The
    provenance row is written once per accession via ``append_source``
    (``ON CONFLICT DO NOTHING`` keeps re-collection duplicate-free).

    One accession can be reported by several CIKs (co-registrant joint
    filings). The first writer keeps ``symbol``/``company_name``/
    ``source_url``; later writers only union their tickers into ``symbols``
    and never flip the primary fields back and forth.
    """
    source = event["source"]
    source_event_id = event["source_event_id"]
    digest = _content_digest(raw)
    lock_revision = getattr(repository, "lock_revision_identity", None)
    if lock_revision is not None:
        await lock_revision(
            source=source,
            category=CATEGORY,
            market=MARKET,
            source_event_id=source_event_id,
        )
    latest = await repository.latest_revision(
        source=source,
        category=CATEGORY,
        market=MARKET,
        source_event_id=source_event_id,
    )
    latest_observed_at = getattr(latest, "observed_at", None)
    stale = latest_observed_at is not None and latest_observed_at > observed_at

    existing = None
    if latest is not None:
        getter = getattr(repository, "get_provider_event", None)
        if getter is not None:
            existing = await getter(
                source=source,
                category=CATEGORY,
                market=MARKET,
                source_event_id=source_event_id,
            )
    content_changed = latest is None or latest.content_hash != digest
    symbols_changed = False
    merged_symbols: list[str] | None = None
    if existing is not None:
        prior_symbols = list(getattr(existing, "symbols", None) or [])
        merged = prior_symbols + [
            item for item in (event.get("symbols") or []) if item not in prior_symbols
        ]
        merged_symbols = merged
        symbols_changed = merged != prior_symbols
        event = {
            **event,
            "symbol": getattr(existing, "symbol", None) or event["symbol"],
            "company_name": (
                getattr(existing, "company_name", None) or event["company_name"]
            ),
            "source_url": getattr(existing, "source_url", None) or event["source_url"],
            "symbols": merged or None,
        }
        if not content_changed:
            prior_raw = getattr(existing, "raw_payload_json", None)
            if prior_raw is not None:
                event["raw_payload_json"] = prior_raw

    if stale:
        # A newer observation already owns this accession's content, but a
        # co-registrant read that observed earlier must still union its
        # symbols — upsert with every stored field preserved except symbols.
        if existing is not None and symbols_changed and merged_symbols is not None:
            preserved = {
                key: getattr(existing, key) for key in event if hasattr(existing, key)
            }
            preserved["symbols"] = merged_symbols or None
            await repository.upsert_event_with_values({**event, **preserved}, [])
        return "stale"
    if not content_changed and not symbols_changed:
        return "unchanged"
    if content_changed:
        if not await repository.append_revision(
            source=source,
            category=CATEGORY,
            market=MARKET,
            source_event_id=source_event_id,
            content_hash=digest,
            observed_at=observed_at,
            provider_created_at=event["provider_created_at"],
            provider_updated_at=event["provider_updated_at"],
            raw_payload_json=event["raw_payload_json"],
        ):
            raise RuntimeError("SEC filing revision was not recorded")
    stored = await repository.upsert_event_with_values(event, [])
    await repository.append_source(
        {
            "event_id": stored.id,
            "source": source,
            "source_event_id": source_event_id,
            "source_url": event["source_url"],
            "published_at": event["published_at"],
            "published_date": event["published_date"],
            "published_offset": event["published_offset"],
            "published_precision": event["published_precision"],
            "first_received_at": event["first_received_at"],
            "revised_at": None,
            "raw_hash": digest,
        }
    )
    return "inserted" if latest is None else "revised"


def _issuer_identity(
    payload: dict[str, Any], fallback_symbol: str | None, fallback_name: str | None
) -> tuple[str | None, list[str] | None, str | None]:
    """Resolve (symbol, symbols, company_name) from a submissions payload."""
    tickers_raw = payload.get("tickers")
    tickers: list[str] = []
    if isinstance(tickers_raw, list):
        for item in tickers_raw:
            if isinstance(item, str) and item.strip():
                tickers.append(_norm_ticker(item))
    symbol = fallback_symbol or (tickers[0] if tickers else None)
    symbols = tickers or ([symbol] if symbol else None)
    name = payload.get("name")
    company_name = name if isinstance(name, str) and name.strip() else fallback_name
    return symbol, symbols, company_name


def _scope_key_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(sorted(set(parts))).encode()).hexdigest()


def _request_spec_scope_key(symbols: list[str], ciks: list[int]) -> str:
    """Hash of the requested scope as given — available even when the
    ticker map fails before requests can be resolved."""
    parts = [f"sym:{str(item).strip().upper()}" for item in symbols]
    parts += [f"cik:{cik}" for cik in ciks]
    return _scope_key_hash(*parts)


def _resolved_scope_key(requests: list[dict[str, Any]]) -> str:
    """Hash of the resolved issuer set — ``--symbols NVDA`` and
    ``--ciks 1045810`` produce the same key."""
    return _scope_key_hash(*{f"cik:{request['cik']}" for request in requests})


def _failed_scope_map(raw: Any) -> dict[str, str] | None:
    """Parse per-scope failure state from ``source_request_hash``.

    ``None`` means absent or corrupt — the failing scope is unknown, so no
    scoped run can prove it recovered.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in data.items()}


def _record_scope_failure(raw: Any, key: str, code: str) -> str:
    data = _failed_scope_map(raw) or {}
    data[key] = code
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _in_window(
    row: dict[str, Any], from_date: date | None, to_date: date | None
) -> bool:
    raw_date = row.get("filingDate")
    if not isinstance(raw_date, str):
        # No usable date → cannot prove it is outside the window; keep it.
        return True
    try:
        filing_day = date.fromisoformat(raw_date.strip())
    except ValueError:
        return True
    if from_date is not None and filing_day < from_date:
        return False
    if to_date is not None and filing_day > to_date:
        return False
    return True


async def collect_sec_filings(
    db: AsyncSession | None = None,
    *,
    symbols: Sequence[str] | None = None,
    ciks: Sequence[int | str] | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    commit: bool = False,
    client: SecEdgarClient | None = None,
    repo: MarketEventsRepository | None = None,
    clock: Callable[[], datetime] | None = None,
    run_date: date | None = None,
) -> dict[str, Any]:
    """Collect recent EDGAR filings for a symbol/CIK list.

    ``commit=False`` (default) performs the fetches and normalization only —
    no repository or session is required and nothing is written. ``commit=True``
    writes through ``MarketEventsRepository`` and records one
    ``(sec, disclosure, us, run_date)`` ingestion partition. Every run is
    symbol/CIK-scoped, so it can never prove the source-wide day complete:
    a clean run leaves the partition ``pending`` (the Benzinga ``--symbols``
    precedent), a collection error marks it ``failed`` with a closed
    ``failure_code``, and an already-``succeeded`` partition is restored.
    ``pending`` is never read as "no filings"; ``succeeded``+0 is
    unreachable for this scoped collector.
    """
    symbols = [item for item in (symbols or []) if str(item).strip()]
    parsed_ciks: list[int] = []
    for item in ciks or []:
        try:
            cik = int(str(item).strip())
        except (TypeError, ValueError):
            raise ValueError(f"invalid CIK value: {item!r}") from None
        if cik <= 0:
            raise ValueError(f"invalid CIK value: {item!r}")
        parsed_ciks.append(cik)
    if not symbols and not parsed_ciks:
        raise ValueError("at least one symbol or CIK is required")
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValueError("from_date must be <= to_date")

    # SEC fair access: refuse before any network call when the declared
    # User-Agent is not configured. This check precedes repository use so a
    # misconfigured run never touches the database either.
    user_agent = (settings.sec_edgar_user_agent or "").strip()
    if not user_agent:
        raise SecEdgarUserAgentMissing(
            f"{USER_AGENT_ENV} is empty; refusing SEC EDGAR requests"
        )

    if commit and repo is None:
        raise ValueError("commit requires a repository")
    if repo is None and db is not None:
        repo = MarketEventsRepository(db)
    repository_session = getattr(repo, "db", None)
    if (
        db is not None
        and repository_session is not None
        and repository_session is not db
    ):
        raise ValueError("db must match the repository session")
    session = db if db is not None else repository_session
    if commit and session is None:
        raise ValueError("commit requires a repository session")

    fetcher = client or SecEdgarClient(user_agent=user_agent)
    capture_now = clock or (lambda: datetime.now(UTC))
    now = capture_now()
    partition_day = run_date or now.astimezone(_ET).date()

    report: dict[str, Any] = {
        "source": SEC_SOURCE,
        "category": CATEGORY,
        "market": MARKET,
        "run_date": partition_day.isoformat(),
        "dry_run": not commit,
        "issuers": {},
        "totals": {
            "filings": 0,
            "written": 0,
            "inserted": 0,
            "revised": 0,
            "unchanged": 0,
            "stale": 0,
            "unresolved": 0,
            "failed": 0,
        },
        "partition": None,
    }

    partition = None
    already_complete = False
    complete_event_count = 0
    partition_persisted = False
    persisted_status: str | None = None
    spec_scope_key = _request_spec_scope_key(symbols, parsed_ciks)
    resolved_scope_key: str | None = None
    if commit:
        assert repo is not None
        partition = await repo.get_or_create_partition(
            source=SEC_SOURCE,
            category=CATEGORY,
            market=MARKET,
            partition_date=partition_day,
        )
        already_complete = partition.status == "succeeded"
        complete_event_count = partition.event_count
        # No mark_partition_running: the per-issuer commits below would
        # persist the "running" claim, and a kill mid-run would leave the
        # day partition stuck "running" — coverage exempts only "pending".
        # Benzinga keeps "running" in-transaction only; the terminal mark
        # is the first persisted status of this run.

    first_failure_code: str | None = None

    async def _safe_rollback() -> None:
        if session is None:
            return
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - the session may already be dead
            logger.exception("SEC EDGAR session rollback failed")

    async def _persist_terminal_partition() -> None:
        """Mark the run's terminal partition state and commit it."""
        nonlocal partition_persisted, persisted_status
        assert repo is not None
        if first_failure_code is not None:
            await repo.mark_partition_failed(partition, failure_code=first_failure_code)
            # Per-scope failure state on the shared day row: this run's
            # scope is recorded so another scope's clean run cannot erase
            # it, and this scope's clean re-run is what clears it.
            scope_key = resolved_scope_key or spec_scope_key
            partition.source_request_hash = _record_scope_failure(
                partition.source_request_hash, scope_key, first_failure_code
            )
            persisted_status = "failed"
        elif already_complete:
            # A scoped read cannot complete the day, but it must not downgrade
            # a day an earlier run already proved (Benzinga parity).
            await repo.mark_partition_succeeded(
                partition, event_count=complete_event_count
            )
            partition.source_request_hash = None
            persisted_status = "succeeded"
        else:
            # Clean run: clear only this run's own recorded failures — a
            # different scope's failure on this shared row stays failed.
            failed_scopes = _failed_scope_map(partition.source_request_hash)
            keep_failed = False
            if partition.status == "failed":
                if failed_scopes is None:
                    # Failed before per-scope tracking (or corrupt): the
                    # failing scope is unknown, so no scoped run can prove
                    # it recovered — the failure stays visible.
                    keep_failed = True
                else:
                    for key in (spec_scope_key, resolved_scope_key):
                        if key is not None:
                            failed_scopes.pop(key, None)
                    if failed_scopes:
                        keep_failed = True
                        partition.last_error = next(iter(failed_scopes.values()))
                        partition.source_request_hash = json.dumps(
                            failed_scopes, sort_keys=True, separators=(",", ":")
                        )
            if keep_failed:
                persisted_status = "failed"
            else:
                await repo.mark_partition_pending(partition)
                partition.source_request_hash = None
                persisted_status = "pending"
        if session is not None:
            await session.commit()
        partition_persisted = True

    async def _persist_failure_in_fresh_transaction(failure_code: str) -> None:
        """Record ``failed`` after a run-level crash, in a new transaction.

        The crashed transaction is rolled back first — otherwise the aborted
        Postgres transaction would take the partition write down with it.
        """
        nonlocal partition, partition_persisted, persisted_status
        await _safe_rollback()
        if repo is None:
            return
        try:
            partition = await repo.get_or_create_partition(
                source=SEC_SOURCE,
                category=CATEGORY,
                market=MARKET,
                partition_date=partition_day,
            )
            await repo.mark_partition_failed(partition, failure_code=failure_code)
            scope_key = resolved_scope_key or spec_scope_key
            partition.source_request_hash = _record_scope_failure(
                partition.source_request_hash, scope_key, failure_code
            )
            persisted_status = "failed"
            if session is not None:
                await session.commit()
            partition_persisted = True
        except Exception:  # noqa: BLE001 - last-resort persistence
            logger.exception("SEC EDGAR partition failure was not persisted")

    def _finish_partition() -> None:
        if partition is None:
            return
        if persisted_status is not None:
            status = persisted_status
        elif first_failure_code is not None:
            status = "failed"
        elif already_complete:
            status = "succeeded"
        else:
            status = "pending"
        report["partition"] = {
            "partition_date": partition_day.isoformat(),
            "status": status,
            "event_count": report["totals"]["filings"],
            "persisted": partition_persisted,
        }

    try:
        # Resolve requested symbols through the official ticker map once.
        requests: list[dict[str, Any]] = []
        ticker_index: dict[str, tuple[int, str]] | None = None
        if symbols:
            ticker_index = index_company_tickers(await fetcher.fetch_company_tickers())
        for raw_symbol in symbols:
            label = raw_symbol.strip().upper()
            entry = (ticker_index or {}).get(_norm_ticker(raw_symbol))
            if entry is None:
                report["issuers"][label] = {
                    "status": "unresolved",
                    "reason": "ticker_not_listed",
                }
                report["totals"]["unresolved"] += 1
                continue
            cik, map_title = entry
            requests.append(
                {
                    "label": label,
                    "cik": cik,
                    "fallback_symbol": _norm_ticker(raw_symbol),
                    "fallback_name": map_title or None,
                }
            )
        for cik in parsed_ciks:
            requests.append(
                {
                    "label": f"CIK{cik:010d}",
                    "cik": cik,
                    "fallback_symbol": None,
                    "fallback_name": None,
                }
            )

        if not requests:
            # Nothing resolvable was requested — a typo or empty scope is not
            # a collection failure and proves nothing about the day: pending.
            if commit and partition is not None:
                assert repo is not None
                await repo.mark_partition_pending(partition)
                if session is not None:
                    await session.commit()
                partition_persisted = True
            report["partition"] = {
                "partition_date": partition_day.isoformat(),
                "status": "pending" if commit else "unresolved",
                "event_count": 0,
                "persisted": bool(commit and partition_persisted),
            }
            return report

        resolved_scope_key = _resolved_scope_key(requests)

        for request in requests:
            label = request["label"]
            issuer_failure_code: str | None = None
            try:
                payload = await fetcher.fetch_submissions(request["cik"])
                if not isinstance(payload, dict):
                    raise SecEdgarError(
                        "SEC EDGAR submissions response has an invalid shape",
                        failure_code="unparseable",
                    )
                rows, meta = _filing_rows(payload)
                symbol, symbols_list, company_name = _issuer_identity(
                    payload,
                    request["fallback_symbol"],
                    request["fallback_name"],
                )
                observed_at = capture_now()
                filings: list[tuple[dict[str, Any], dict[str, Any]]] = []
                skipped_unparseable = 0
                for row in rows:
                    form = row.get("form")
                    if not isinstance(form, str) or form not in SEC_FORMS:
                        continue
                    if not _in_window(row, from_date, to_date):
                        continue
                    try:
                        event = normalize_sec_filing(
                            row,
                            cik=request["cik"],
                            symbol=symbol,
                            company_name=company_name,
                            symbols=symbols_list,
                            received_at=observed_at,
                        )
                    except SecEdgarError:
                        skipped_unparseable += 1
                        continue
                    # ``_in_window`` saw only the raw ``filingDate``; when it
                    # was blank normalize fell back to the acceptance-derived
                    # date, so the bound is checked again on the stored date.
                    event_day = event["event_date"]
                    if (from_date is not None and event_day < from_date) or (
                        to_date is not None and event_day > to_date
                    ):
                        continue
                    filings.append((event, row))

                complete = _window_complete(
                    older_history_uncollected=meta["older_history_uncollected"],
                    recent_earliest=meta.get("recent_earliest"),
                    from_date=from_date,
                    to_date=to_date,
                )

                counts = {
                    "inserted": 0,
                    "revised": 0,
                    "unchanged": 0,
                    "stale": 0,
                }
                if commit:
                    assert repo is not None
                    for event, raw in filings:
                        outcome = await _write_filing(
                            repo, event, raw, observed_at=observed_at
                        )
                        counts[outcome] += 1
                    # Per-issuer commit: one issuer's rows survive another
                    # issuer's failure, and advisory xact locks are released
                    # instead of accumulating across the whole run.
                    if session is not None:
                        await session.commit()
                else:
                    counts["inserted"] = len(filings)

                issuer_status = "collected"
                if skipped_unparseable:
                    # Parse failures must never read as "no filings" (§2.4):
                    # the issuer and the run partition fail with a closed
                    # code while the rows that did parse stay stored.
                    issuer_status = "failed"
                    issuer_failure_code = "unparseable"
                    report["totals"]["failed"] += 1
                    if first_failure_code is None:
                        first_failure_code = "unparseable"

                report["issuers"][label] = {
                    "status": issuer_status,
                    **(
                        {"failure_code": issuer_failure_code}
                        if issuer_failure_code is not None
                        else {}
                    ),
                    "cik": f"{request['cik']:010d}",
                    "symbol": symbol,
                    "filings": len(filings),
                    "written": (
                        sum(counts[key] for key in ("inserted", "revised"))
                        if commit
                        else len(filings)
                    ),
                    "inserted": counts["inserted"],
                    "revised": counts["revised"],
                    "unchanged": counts["unchanged"],
                    "stale": counts["stale"],
                    "skipped_unparseable": skipped_unparseable,
                    "older_history_uncollected": meta["older_history_uncollected"],
                    "window_complete": complete,
                }
                report["totals"]["filings"] += len(filings)
                report["totals"]["written"] += report["issuers"][label]["written"]
                for key in ("inserted", "revised", "unchanged", "stale"):
                    report["totals"][key] += counts[key]
            except SecEdgarError as exc:
                await _safe_rollback()
                logger.warning(
                    "SEC EDGAR collection failed for %s: %s (%s)",
                    label,
                    exc.failure_code,
                    exc,
                )
                report["issuers"][label] = {
                    "status": "failed",
                    "failure_code": exc.failure_code,
                    "error": str(exc),
                }
                report["totals"]["failed"] += 1
                if first_failure_code is None:
                    first_failure_code = exc.failure_code
            except Exception:  # noqa: BLE001 - recorded, run continues
                await _safe_rollback()
                logger.exception("SEC EDGAR collection crashed for %s", label)
                report["issuers"][label] = {
                    "status": "failed",
                    "failure_code": "bad_response",
                }
                report["totals"]["failed"] += 1
                if first_failure_code is None:
                    first_failure_code = "bad_response"

        if commit and partition is not None:
            assert repo is not None
            # Per-issuer commits expired the ORM object; re-read so
            # mark_partition_failed sees a fresh retry_count.
            partition = await repo.get_or_create_partition(
                source=SEC_SOURCE,
                category=CATEGORY,
                market=MARKET,
                partition_date=partition_day,
            )
            await _persist_terminal_partition()
        _finish_partition()
    except SecEdgarError as exc:
        # Run-level failure (e.g. the ticker map fetch) — no per-issuer entry.
        report["error"] = {"failure_code": exc.failure_code, "error": str(exc)}
        report["totals"]["failed"] += 1
        first_failure_code = exc.failure_code
        if commit and partition is not None:
            await _persist_failure_in_fresh_transaction(exc.failure_code)
        _finish_partition()
    except Exception:  # noqa: BLE001
        logger.exception("SEC EDGAR collection crashed")
        # Closed code only — raw exception text stays in the log (§2.4).
        report["error"] = {"failure_code": "bad_response"}
        first_failure_code = "bad_response"
        if commit and partition is not None:
            await _persist_failure_in_fresh_transaction("bad_response")
        _finish_partition()

    return report


def collect_ir_requests(symbols: Sequence[str]) -> dict[str, dict[str, str]]:
    """IR feed requests — always ``unsupported``, never scraped.

    Enterprise IR pages have no official API/feed in scope; per the task
    contract the only recorded outcome is the closed ``unsupported`` reason.
    """
    return {
        str(symbol).strip().upper(): {
            "status": "unsupported",
            "reason": IR_UNSUPPORTED_REASON,
        }
        for symbol in symbols
        if str(symbol).strip()
    }
