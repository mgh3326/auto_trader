from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_insight_snapshot import CryptoInsightSnapshot

SENSITIVE_KEY_FRAGMENTS = ("key", "secret", "token", "password", "authorization")
_MAX_RAW_ITEMS = 64
_MAX_RAW_STRING = 500


class CryptoInsightSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    provider: str
    symbol: str | None = None
    value: Decimal | None = None
    unit: str | None = None
    label: str | None = None
    snapshot_at: dt.datetime
    source_url: str | None = None
    freshness_seconds: int | None = None
    raw_payload: dict[str, Any] | None = None

    @field_validator("metric", "provider")
    @classmethod
    def _required_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        return normalized or None

    @field_validator("unit")
    @classmethod
    def _normalize_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None

    @field_validator("label")
    @classmethod
    def _normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


def redact_sensitive_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None

    def _redact(value: Any, *, key: str | None = None) -> Any:
        if key and any(fragment in key.lower() for fragment in SENSITIVE_KEY_FRAGMENTS):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(child_key)[:120]: _redact(child_value, key=str(child_key))
                for child_key, child_value in list(value.items())[:_MAX_RAW_ITEMS]
            }
        if isinstance(value, list):
            return [_redact(item) for item in value[:_MAX_RAW_ITEMS]]
        if isinstance(value, str):
            return value[:_MAX_RAW_STRING]
        if isinstance(value, int | float | bool) or value is None:
            return value
        return str(value)[:_MAX_RAW_STRING]

    return _redact(payload)


def _normal_payload(row: CryptoInsightSnapshotUpsert) -> dict[str, Any]:
    values = row.model_dump()
    values["snapshot_at"] = (
        values["snapshot_at"].astimezone(dt.UTC).replace(microsecond=0)
    )
    values["raw_payload"] = redact_sensitive_payload(values.get("raw_payload"))
    return values


def _matches_identity(values: dict[str, Any]) -> sa.ColumnElement[bool]:
    symbol = values.get("symbol")
    symbol_clause = (
        CryptoInsightSnapshot.symbol.is_(None)
        if symbol is None
        else CryptoInsightSnapshot.symbol == symbol
    )
    return sa.and_(
        CryptoInsightSnapshot.metric == values["metric"],
        CryptoInsightSnapshot.provider == values["provider"],
        symbol_clause,
        CryptoInsightSnapshot.snapshot_at == values["snapshot_at"],
    )


class CryptoInsightSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[CryptoInsightSnapshotUpsert]) -> int:
        count = 0
        for row in rows:
            values = _normal_payload(row)
            existing = (
                await self._session.execute(
                    select(CryptoInsightSnapshot)
                    .where(_matches_identity(values))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is None:
                self._session.add(CryptoInsightSnapshot(**values))
            else:
                for key in (
                    "value",
                    "unit",
                    "label",
                    "source_url",
                    "freshness_seconds",
                    "raw_payload",
                ):
                    setattr(existing, key, values[key])
                existing.updated_at = dt.datetime.now(dt.UTC)
            count += 1
        if count:
            await self._session.flush()
        return count

    async def list_latest(
        self,
        *,
        metrics: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        symbols: Sequence[str | None] | None = None,
        limit_per_metric: int = 1,
    ) -> list[CryptoInsightSnapshot]:
        stmt = select(CryptoInsightSnapshot)
        if metrics:
            stmt = stmt.where(
                CryptoInsightSnapshot.metric.in_([m.strip().lower() for m in metrics])
            )
        if providers:
            stmt = stmt.where(
                CryptoInsightSnapshot.provider.in_(
                    [p.strip().lower() for p in providers]
                )
            )
        if symbols is not None:
            clauses = []
            normalized_symbols = [s.strip().upper() if s else None for s in symbols]
            if None in normalized_symbols:
                clauses.append(CryptoInsightSnapshot.symbol.is_(None))
            concrete = [s for s in normalized_symbols if s]
            if concrete:
                clauses.append(CryptoInsightSnapshot.symbol.in_(concrete))
            stmt = stmt.where(sa.or_(*clauses) if clauses else sa.false())
        stmt = stmt.order_by(
            CryptoInsightSnapshot.metric,
            CryptoInsightSnapshot.provider,
            CryptoInsightSnapshot.symbol.nullsfirst(),
            CryptoInsightSnapshot.snapshot_at.desc(),
            CryptoInsightSnapshot.id.desc(),
        )
        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())
        if limit_per_metric <= 0:
            return []
        seen: dict[tuple[str, str, str | None], int] = {}
        latest: list[CryptoInsightSnapshot] = []
        for row in rows:
            key = (row.metric, row.provider, row.symbol)
            current = seen.get(key, 0)
            if current >= limit_per_metric:
                continue
            latest.append(row)
            seen[key] = current + 1
        return latest

    async def get_latest(
        self,
        metric: str,
        *,
        provider: str | None = None,
        symbol: str | None = None,
    ) -> CryptoInsightSnapshot | None:
        stmt = select(CryptoInsightSnapshot).where(
            CryptoInsightSnapshot.metric == metric.strip().lower()
        )
        if provider:
            stmt = stmt.where(
                CryptoInsightSnapshot.provider == provider.strip().lower()
            )
        if symbol is None:
            stmt = stmt.where(CryptoInsightSnapshot.symbol.is_(None))
        else:
            stmt = stmt.where(CryptoInsightSnapshot.symbol == symbol.strip().upper())
        stmt = stmt.order_by(
            CryptoInsightSnapshot.snapshot_at.desc(), CryptoInsightSnapshot.id.desc()
        ).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def upsert_crypto_insight_snapshots(
    session: AsyncSession, payloads: Iterable[CryptoInsightSnapshotUpsert]
) -> int:
    return await CryptoInsightSnapshotsRepository(session).upsert(payloads)


async def list_latest_crypto_insights(
    session: AsyncSession,
    *,
    metrics: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    symbols: Sequence[str | None] | None = None,
    limit_per_metric: int = 1,
) -> list[CryptoInsightSnapshot]:
    return await CryptoInsightSnapshotsRepository(session).list_latest(
        metrics=metrics,
        providers=providers,
        symbols=symbols,
        limit_per_metric=limit_per_metric,
    )


async def get_latest_crypto_insight(
    session: AsyncSession,
    metric: str,
    *,
    provider: str | None = None,
    symbol: str | None = None,
) -> CryptoInsightSnapshot | None:
    return await CryptoInsightSnapshotsRepository(session).get_latest(
        metric, provider=provider, symbol=symbol
    )
