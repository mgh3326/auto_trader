"""Market events repository — only place that writes the market event tables (ROB-128).

Idempotency strategy:
* When `source_event_id` is provided, upsert keyed by (source, category, market, source_event_id).
  DART rows (hk 6408) are identified by the receipt number alone: a category change
  moves the one existing row first (`_align_dart_identity`), and two rows for one
  receipt fail closed (`DartIdentityConflict`).
* Otherwise upsert keyed by (source, category, market, symbol, event_date, fiscal_year, fiscal_quarter).
Both keys are enforced by partial unique indexes (see migration).

Values are upserted by (event_id, metric_name, period).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventRevision,
    MarketEventSource,
    MarketEventValue,
)
from app.services.alpaca_paper_ledger_service import _redact_sensitive_keys

PUBLICATION_FIELDS = (
    "published_at",
    "published_date",
    "published_offset",
    "published_precision",
)
# "datetime" records a known instant whose source resolution is unspecified.
PUBLICATION_RANK = {"date": 1, "datetime": 2, "minute": 3, "second": 4}
EVENT_FORMAT_FIELDS = (
    "revised_at",
    "effective_at",
    "relevance",
    "relevance_state",
    "relevance_basis",
    "instrument_type",
    "group_key",
    "redistributable",
    "symbols",
)


class CollectionFailure(StrEnum):
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    BAD_RESPONSE = "bad_response"
    UNPARSEABLE = "unparseable"
    QUOTA = "quota"


def _normalize_publication(payload: dict[str, Any]) -> None:
    """Validate an opt-in publication tuple and normalize instants to UTC."""
    if not any(key in payload for key in PUBLICATION_FIELDS):
        return
    precision = payload.get("published_precision")
    timestamp = payload.get("published_at")
    day = payload.get("published_date")
    if precision == "date":
        if type(day) is not date or timestamp is not None:
            raise ValueError("date precision requires published_date only")
    elif isinstance(precision, str) and precision in PUBLICATION_RANK:
        if (
            day is not None
            or not isinstance(timestamp, datetime)
            or timestamp.utcoffset() is None
        ):
            raise ValueError("timed publication requires an aware published_at")
        payload["published_at"] = timestamp.astimezone(UTC)
    else:
        raise ValueError("published_precision must be date, datetime, minute or second")
    for key in PUBLICATION_FIELDS:
        payload.setdefault(key, None)


def value_keys(values: list[dict[str, Any]]) -> set[tuple[str, str | None]]:
    """``(metric_name, period)`` pairs a provider record currently holds."""
    return {
        (str(value["metric_name"]), value.get("period"))
        for value in values
        if value.get("metric_name")
    }


DART_SOURCE = "dart"


class DartIdentityConflict(RuntimeError):
    """One DART receipt number already maps to more than one market_events row.

    Raised instead of choosing a row: merging or deleting is an operator decision
    (hk 6408). The message carries the receipt number only.
    """


_TIMED_RECEIPT_TEXT = re.compile(r"[T ][0-9]{2}:[0-9]{2}")


def _keep_known_receipt_time(payload: dict[str, Any], stored_raw: Any) -> None:
    """Do not let a date-only DART write erase a receipt time already stored (hk 6408).

    The official list has no time. A write without one (an unenriched run, or a
    scrape that failed) replaces the raw payload, so the time an earlier enriched
    run stored would be lost. When the stored raw payload holds a timed
    ``rcept_dt`` and the incoming one does not, keep the stored time and drop the
    incoming publication tuple, so the timing reader still reports the time.
    """
    incoming_raw = payload.get("raw_payload_json") or {}
    if not isinstance(stored_raw, dict):
        return
    stored_text = str(stored_raw.get("rcept_dt") or "")
    incoming_text = str(incoming_raw.get("rcept_dt") or "")
    if not _TIMED_RECEIPT_TEXT.search(stored_text):
        return
    if _TIMED_RECEIPT_TEXT.search(incoming_text):
        return
    payload["raw_payload_json"] = {**incoming_raw, "rcept_dt": stored_text}
    for key in PUBLICATION_FIELDS:
        payload.pop(key, None)


class MarketEventsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_source_event_ids(
        self, *, source: str, category: str, market: str
    ) -> set[str]:
        """Return stored provider IDs for a bounded collector's stop marker."""
        result = await self.db.execute(
            select(MarketEvent.source_event_id).where(
                MarketEvent.source == source,
                MarketEvent.category == category,
                MarketEvent.market == market,
                MarketEvent.source_event_id.is_not(None),
            )
        )
        return {value for value in result.scalars() if value is not None}

    async def list_dart_null_symbol_rows(
        self, *, from_date: date, to_date: date
    ) -> list[tuple[str, date]]:
        """Return ``(rcept_no, event_date)`` for stored KR DART rows lacking a symbol.

        Read-only. Used by the hk 5623 symbol backfill to choose which days to
        re-collect.
        """
        result = await self.db.execute(
            select(MarketEvent.source_event_id, MarketEvent.event_date).where(
                MarketEvent.source == "dart",
                MarketEvent.market == "kr",
                MarketEvent.symbol.is_(None),
                MarketEvent.source_event_id.is_not(None),
                MarketEvent.event_date >= from_date,
                MarketEvent.event_date <= to_date,
            )
        )
        return [(rcept_no, event_date) for rcept_no, event_date in result.all()]

    async def fill_dart_symbol_if_null(
        self, *, source_event_id: str, symbol: str
    ) -> int:
        """Set ``symbol`` on a stored KR DART row that has none; return rows changed.

        Touches only the ``symbol`` column. ``first_received_at``, the raw payload,
        ``updated_at``, and every other stored value stay as they are (hk 5623).
        A row that already has a symbol is never overwritten, so a repeat call
        changes nothing.

        ``updated_at`` carries ``onupdate=func.now()``, which SQLAlchemy applies to
        any UPDATE that leaves the column out of SET. Assigning the column to
        itself keeps the stored timestamp and suppresses that default.
        """
        result = await self.db.execute(
            update(MarketEvent)
            .where(
                MarketEvent.source == "dart",
                MarketEvent.market == "kr",
                MarketEvent.source_event_id == source_event_id,
                MarketEvent.symbol.is_(None),
            )
            .values(symbol=symbol, updated_at=MarketEvent.updated_at)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    async def lock_revision_identity(
        self, *, source: str, category: str, market: str, source_event_id: str
    ) -> None:
        """Serialize revision compare-and-append for one provider event."""
        identity = "\0".join((source, category, market, source_event_id)).encode()
        lock_id = int.from_bytes(
            hashlib.blake2b(identity, digest_size=8).digest(), "big", signed=True
        )
        await self.db.execute(select(func.pg_advisory_xact_lock(lock_id)))

    async def insert_provider_event(self, event_data: dict[str, Any]) -> bool:
        """Keep the first observation of a provider-keyed event."""
        if not event_data.get("source_event_id"):
            raise ValueError("source_event_id is required")
        payload = dict(event_data)
        _normalize_publication(payload)
        if payload.get("raw_payload_json") is not None:
            payload["raw_payload_json"] = _redact_sensitive_keys(
                payload["raw_payload_json"]
            )
        table = MarketEvent.__table__
        stmt = (
            pg_insert(table)
            .values(**payload)
            .on_conflict_do_nothing(
                index_elements=["source", "category", "market", "source_event_id"],
                index_where=table.c.source_event_id.isnot(None),
            )
            .returning(table.c.id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def _align_dart_identity(self, payload: dict[str, Any]) -> None:
        """Keep one market_events row per DART receipt number (hk 6408).

        The receipt number is the identity; the category is a classification that
        can change when the report name does. The unique index still includes
        category, so a changed category would insert a second row. Before the
        normal upsert, move the one existing row to the new category/market. The
        upsert then updates that row. More than one existing row fails closed.
        """
        receipt = str(payload["source_event_id"])
        identity = "\0".join(("dart-identity", receipt)).encode()
        lock_id = int.from_bytes(
            hashlib.blake2b(identity, digest_size=8).digest(), "big", signed=True
        )
        await self.db.execute(select(func.pg_advisory_xact_lock(lock_id)))
        table = MarketEvent.__table__
        rows = (
            await self.db.execute(
                select(
                    table.c.id,
                    table.c.category,
                    table.c.market,
                    table.c.raw_payload_json,
                )
                .where(
                    table.c.source == DART_SOURCE,
                    table.c.source_event_id == receipt,
                )
                .with_for_update()
            )
        ).all()
        if len(rows) > 1:
            raise DartIdentityConflict(
                f"DART receipt {receipt} has {len(rows)} market_events rows"
            )
        if rows:
            _keep_known_receipt_time(payload, rows[0].raw_payload_json)
        if rows and (rows[0].category, rows[0].market) != (
            payload.get("category"),
            payload.get("market"),
        ):
            await self.db.execute(
                update(table)
                .where(table.c.id == rows[0].id)
                .values(
                    category=payload.get("category"),
                    market=payload.get("market"),
                    updated_at=func.now(),
                )
            )
            await self.db.flush()

    async def upsert_event_with_values(
        self,
        event_data: dict[str, Any],
        values: list[dict[str, Any]],
        *,
        replace_values: bool = False,
        preserve_dart_detail: bool = False,
        clear_notice_symbol: bool = False,
    ) -> MarketEvent:
        payload = dict(event_data)
        _normalize_publication(payload)
        if payload.get("source") == DART_SOURCE and payload.get("source_event_id"):
            await self._align_dart_identity(payload)
        if payload.get("raw_payload_json") is not None:
            payload["raw_payload_json"] = _redact_sensitive_keys(
                payload["raw_payload_json"]
            )

        update_columns = {
            k: payload.get(k)
            for k in (
                "country",
                "currency",
                "company_name",
                "title",
                "release_time_utc",
                "release_time_local",
                "source_timezone",
                "time_hint",
                "importance",
                "status",
                "source_url",
                "raw_payload_json",
                "fetched_at",
                "provider_created_at",
                "provider_created_raw",
                "provider_updated_at",
                "provider_updated_raw",
                *EVENT_FORMAT_FIELDS,
            )
            if k in payload
        }
        # First-observation evidence stays insert-only across re-ingest.
        # This includes first_received_at (hk 6513) and backfilled (hk 6406).
        update_columns["updated_at"] = func.now()

        if payload.get("source_event_id"):
            # Provider-keyed rows are identified by ``source_event_id``, not by
            # the natural key, so a revision may legitimately move the event
            # date (earnings dates, conference-call and ex-dividend dates are
            # revised) or the symbol/fiscal fields. Those columns therefore
            # belong to the update set — otherwise the current row keeps a
            # superseded date while the revision log records the new one.
            # Natural-key rows cannot move them: the natural key *is*
            # (symbol, event_date, fiscal_year, fiscal_quarter).
            #
            # A *None* value never clears a stored one (hk 6513 r2): writers
            # resolve these fields best-effort, e.g. DART falls back to
            # ``symbol=None`` when the corp-code registry read fails
            # (``symbol_resolution_reason='registry_unavailable'``) and the day
            # is re-upserted by the next DAG run. Treating that None as "now
            # unknown, clear it" would erase evidence the earlier run proved.
            for column in ("event_date", "symbol", "fiscal_year", "fiscal_quarter"):
                value = payload.get(column)
                if value is not None:
                    update_columns[column] = value
            if payload.get("source") == DART_SOURCE and not payload.get(
                "published_precision"
            ):
                # A timed DART row keeps its time in raw_payload_json.rcept_dt and
                # no publication tuple. A date-only tuple an earlier write stored
                # must not outrank that time, so clear it (hk 6408).
                update_columns.update(dict.fromkeys(PUBLICATION_FIELDS))
            if clear_notice_symbol:
                if payload.get("source") not in {"upbit_notice", "bithumb_notice"}:
                    raise ValueError("clear_notice_symbol is only valid for notices")
                update_columns["symbol"] = None
            # A DART list row can be replayed after a typed detail lookup fails
            # or by the paced list-only backfill. Keep the existing evidence and
            # corrected receipt date on conflict while still allowing inserts.
            if preserve_dart_detail:
                update_columns.pop("raw_payload_json", None)
                update_columns.pop("event_date", None)
            table = MarketEvent.__table__
            insert = pg_insert(table).values(**payload)
            if precision := payload.get("published_precision"):
                stored_rank = case(
                    PUBLICATION_RANK, value=table.c.published_precision, else_=0
                )
                replace_publication = stored_rank <= PUBLICATION_RANK[precision]
                update_columns.update(
                    {
                        key: case(
                            (replace_publication, insert.excluded[key]),
                            else_=table.c[key],
                        )
                        for key in PUBLICATION_FIELDS
                    }
                )
            stmt = insert.on_conflict_do_update(
                index_elements=["source", "category", "market", "source_event_id"],
                index_where=table.c.source_event_id.isnot(None),
                set_=update_columns,
            ).returning(table.c.id)
            result = await self.db.execute(stmt)
            event_id = result.scalar_one()
        else:
            event = await self._find_event_by_natural_key(payload)
            if event is None:
                event = MarketEvent(**payload)
                self.db.add(event)
                await self.db.flush()
            else:
                precision = payload.get("published_precision")
                if precision and PUBLICATION_RANK[precision] >= PUBLICATION_RANK.get(
                    event.published_precision or "", 0
                ):
                    update_columns.update(
                        {key: payload[key] for key in PUBLICATION_FIELDS}
                    )
                for key, value in update_columns.items():
                    if key != "updated_at":
                        setattr(event, key, value)
                event.updated_at = datetime.now(UTC)
                await self.db.flush()
            event_id = event.id

        if replace_values:
            # The caller holds the provider's complete current record for this
            # event, so a value it no longer reports is superseded, not
            # additive: a revision that moves the fiscal period must not leave
            # the old period's rows beside the new one (hk 6513 r6).
            await self._drop_superseded_values(event_id, values)

        for value in values:
            await self._upsert_value(event_id, value)

        await self.db.flush()
        query = select(MarketEvent).where(MarketEvent.id == event_id)
        if payload.get("source_event_id") and "published_precision" in payload:
            query = query.execution_options(populate_existing=True)
        event = (await self.db.execute(query)).scalar_one()
        return event

    async def _find_event_by_natural_key(
        self, payload: dict[str, Any]
    ) -> MarketEvent | None:
        stmt = select(MarketEvent).where(
            MarketEvent.source == payload.get("source"),
            MarketEvent.category == payload.get("category"),
            MarketEvent.market == payload.get("market"),
            MarketEvent.event_date == payload.get("event_date"),
            MarketEvent.source_event_id.is_(None),
        )
        for attr in ("symbol", "fiscal_year", "fiscal_quarter"):
            column = getattr(MarketEvent, attr)
            value = payload.get(attr)
            stmt = stmt.where(column.is_(None) if value is None else column == value)
        if "published_precision" in payload:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _upsert_value(self, event_id: int, value: dict[str, Any]) -> None:
        payload = {**value, "event_id": event_id}
        update_columns = {
            k: payload.get(k)
            for k in (
                "actual",
                "forecast",
                "previous",
                "revised_previous",
                "unit",
                "surprise",
                "surprise_pct",
                "released_at",
                "value_details",
            )
            if k in payload
        }
        update_columns["updated_at"] = func.now()
        stmt = select(MarketEventValue).where(
            MarketEventValue.event_id == event_id,
            MarketEventValue.metric_name == payload.get("metric_name"),
        )
        period = payload.get("period")
        stmt = stmt.where(
            MarketEventValue.period.is_(None)
            if period is None
            else MarketEventValue.period == period
        )
        existing = (await self.db.execute(stmt)).scalar_one_or_none()
        if existing is None:
            self.db.add(MarketEventValue(**payload))
        else:
            for key, value_ in update_columns.items():
                if key != "updated_at":
                    setattr(existing, key, value_)
            existing.updated_at = datetime.now(UTC)
        await self.db.flush()

    async def _drop_superseded_values(
        self, event_id: int, values: list[dict[str, Any]]
    ) -> None:
        """Delete value rows the incoming record no longer reports (hk 6513 r6).

        Used only for writers whose payload is the provider's complete current
        record (Benzinga calendar rows): a revision that changes the fiscal
        period would otherwise leave the superseded period's values beside the
        new ones, and a reader that takes the first matching metric could serve
        the stale number.
        """
        keep = value_keys(values)
        existing = (
            (
                await self.db.execute(
                    select(MarketEventValue).where(
                        MarketEventValue.event_id == event_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing:
            if (row.metric_name, row.period) not in keep:
                await self.db.delete(row)
        await self.db.flush()

    async def get_provider_event(
        self,
        *,
        source: str,
        category: str,
        market: str,
        source_event_id: str,
    ) -> MarketEvent | None:
        """Return the current row for a provider-keyed event, if any."""
        stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.source == source,
                MarketEvent.category == category,
                MarketEvent.market == market,
                MarketEvent.source_event_id == source_event_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def append_source(self, source_data: dict[str, Any]) -> bool:
        """Store the first provenance observation for a provider ID."""
        payload = dict(source_data)
        if not payload.get("source_event_id"):
            raise ValueError("source_event_id is required")
        _normalize_publication(payload)
        table = MarketEventSource.__table__
        stmt = (
            pg_insert(table)
            .values(**payload)
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(table.c.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    # -- provider revisions (hk 6513) ---------------------------------------

    async def latest_revision(
        self,
        *,
        source: str,
        category: str,
        market: str,
        source_event_id: str,
    ) -> MarketEventRevision | None:
        stmt = (
            select(MarketEventRevision)
            .where(
                MarketEventRevision.source == source,
                MarketEventRevision.category == category,
                MarketEventRevision.market == market,
                MarketEventRevision.source_event_id == source_event_id,
            )
            .order_by(MarketEventRevision.revision_seq.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def append_revision(
        self,
        *,
        source: str,
        category: str,
        market: str,
        source_event_id: str,
        content_hash: str,
        observed_at: datetime,
        provider_created_at: datetime | None = None,
        provider_updated_at: datetime | None = None,
        raw_payload_json: dict[str, Any] | None = None,
        changed_fields: dict[str, Any] | None = None,
        source_revision_id: str | None = None,
    ) -> bool:
        """Append one provider revision; return False when the write lost a race.

        Only the sequence number is unique, so the same content may be appended
        again after an intervening version (A → B → A is three revisions and the
        newest row must describe A). ``ON CONFLICT DO NOTHING`` covers a
        concurrent writer taking the same sequence number; a False result means
        this observation was *not* recorded and the caller must not update the
        current row as if it had been.
        """
        latest = await self.latest_revision(
            source=source,
            category=category,
            market=market,
            source_event_id=source_event_id,
        )
        next_seq = 1 if latest is None else latest.revision_seq + 1
        payload: dict[str, Any] = {
            "source": source,
            "category": category,
            "market": market,
            "source_event_id": source_event_id,
            "revision_seq": next_seq,
            "content_hash": content_hash,
            "provider_created_at": provider_created_at,
            "provider_updated_at": provider_updated_at,
            "observed_at": observed_at,
            "raw_payload_json": (
                _redact_sensitive_keys(raw_payload_json)
                if raw_payload_json is not None
                else None
            ),
        }
        if changed_fields is not None:
            if raw_payload_json is not None:
                raise ValueError(
                    "derived revision fields must be stored without a raw payload"
                )
            payload["changed_fields"] = _redact_sensitive_keys(changed_fields)
        if source_revision_id is not None:
            payload["source_revision_id"] = source_revision_id
        stmt = (
            pg_insert(MarketEventRevision.__table__)
            .values(**payload)
            .on_conflict_do_nothing()
            .returning(MarketEventRevision.__table__.c.id)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.scalar_one_or_none() is not None

    # -- partition state ----------------------------------------------------

    async def get_or_create_partition(
        self,
        *,
        source: str,
        category: str,
        market: str,
        partition_date: date,
    ) -> MarketEventIngestionPartition:
        existing = (
            await self.db.execute(
                select(MarketEventIngestionPartition)
                .where(
                    MarketEventIngestionPartition.source == source,
                    MarketEventIngestionPartition.category == category,
                    MarketEventIngestionPartition.market == market,
                    MarketEventIngestionPartition.partition_date == partition_date,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        row = MarketEventIngestionPartition(
            source=source,
            category=category,
            market=market,
            partition_date=partition_date,
            status="pending",
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def mark_partition_running(
        self, partition: MarketEventIngestionPartition
    ) -> None:
        partition.status = "running"
        partition.started_at = datetime.now(UTC)
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_pending(
        self, partition: MarketEventIngestionPartition
    ) -> None:
        """Return a partition to ``pending``: fetched, but the day is not complete.

        Never counts as succeeded, so no reader treats the day as loaded and a
        later run fetches it again.
        """
        partition.status = "pending"
        partition.event_count = 0
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_succeeded(
        self,
        partition: MarketEventIngestionPartition,
        *,
        event_count: int,
    ) -> None:
        partition.status = "succeeded"
        partition.event_count = event_count
        partition.finished_at = datetime.now(UTC)
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_failed(
        self,
        partition: MarketEventIngestionPartition,
        *,
        error: str | None = None,
        failure_code: CollectionFailure | None = None,
    ) -> None:
        """Record a closed v2 failure code or a legacy caller's error string."""
        if (error is None) == (failure_code is None):
            raise ValueError("provide one of error or failure_code")
        if failure_code is not None:
            error = CollectionFailure(failure_code).value
        assert error is not None
        partition.status = "failed"
        partition.finished_at = datetime.now(UTC)
        partition.last_error = error[:2000]
        partition.retry_count = (partition.retry_count or 0) + 1
        await self.db.flush()

    async def list_succeeded_partitions_in_range(
        self,
        *,
        source: str,
        category: str,
        market: str,
        from_date: date,
        to_date: date,
    ) -> set[date]:
        """Return the set of `partition_date`s with status='succeeded' in the
        inclusive window.

        Used to skip already-succeeded partitions during rolling-window ingestion
        and avoid unnecessarily reconsuming external API quota on a rerun.

        Note: callers MUST NOT use `event_count > 0` as a substitute for
        `status='succeeded'`. A failed partition can still carry a non-zero
        `event_count` from an earlier partial run.
        """
        stmt = select(MarketEventIngestionPartition.partition_date).where(
            MarketEventIngestionPartition.source == source,
            MarketEventIngestionPartition.category == category,
            MarketEventIngestionPartition.market == market,
            MarketEventIngestionPartition.status == "succeeded",
            MarketEventIngestionPartition.partition_date >= from_date,
            MarketEventIngestionPartition.partition_date <= to_date,
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return set(rows)
