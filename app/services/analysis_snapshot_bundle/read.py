"""DB-only, integrity-verified reads for frozen analysis snapshot bundles."""

from __future__ import annotations

import copy
import datetime as dt
import hmac
import uuid
from collections.abc import Callable
from typing import Literal

from app.schemas.analysis_snapshot_bundle import (
    ANALYSIS_SECTION_NAMES,
    AnalysisBundleGetResponse,
    AnalysisFrozenDocument,
    AnalysisSectionName,
)
from app.services.action_report.common.canonicalize import canonical_payload_hash
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

Clock = Callable[[], dt.datetime]


class AnalysisBundleNotFound(LookupError):
    """Raised when the requested bundle UUID is absent."""


class AnalysisBundleIntegrityError(ValueError):
    """Raised when persisted bundle membership or content is invalid."""


class UnknownAnalysisBundleSection(ValueError):
    """Raised when a projection requests an unsupported section name."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _age_seconds(now: dt.datetime, then: dt.datetime) -> float:
    return max(0.0, (now - then).total_seconds())


def _freshness(
    age_seconds: float, *, soft: int, hard: int
) -> Literal["fresh", "soft_stale", "hard_stale"]:
    if age_seconds > hard:
        return "hard_stale"
    if age_seconds > soft:
        return "soft_stale"
    return "fresh"


class AnalysisBundleReadService:
    def __init__(
        self,
        repository: InvestmentSnapshotsRepository,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repo = repository
        self._clock = clock or _utcnow

    async def get(
        self,
        bundle_id: uuid.UUID,
        sections: list[AnalysisSectionName] | None = None,
    ) -> AnalysisBundleGetResponse:
        bundle = await self._repo.get_bundle_by_uuid(bundle_id)
        if bundle is None:
            raise AnalysisBundleNotFound(str(bundle_id))
        if bundle.purpose != "analysis_recheck":
            raise AnalysisBundleIntegrityError("bundle has unexpected purpose")

        pairs = await self._repo.list_bundle_items_with_snapshots(bundle.id)
        if len(pairs) != 1:
            raise AnalysisBundleIntegrityError(
                "analysis bundle must contain exactly one snapshot"
            )
        snapshot = pairs[0][1]
        if snapshot.snapshot_kind != "llm_input_frozen":
            raise AnalysisBundleIntegrityError("bundle snapshot has unexpected kind")

        actual_hash = canonical_payload_hash(snapshot.payload_json)
        if not hmac.compare_digest(actual_hash, snapshot.canonical_payload_hash):
            raise AnalysisBundleIntegrityError("frozen document hash mismatch")

        stored_document = copy.deepcopy(snapshot.payload_json)
        try:
            validated = AnalysisFrozenDocument.model_validate(stored_document)
        except ValueError as exc:
            raise AnalysisBundleIntegrityError(
                "frozen document failed schema validation"
            ) from exc

        if sections is not None:
            unknown = set(sections) - set(ANALYSIS_SECTION_NAMES)
            if unknown:
                raise UnknownAnalysisBundleSection(
                    f"unknown analysis section: {sorted(unknown)[0]}"
                )

        returned_sections = (
            list(ANALYSIS_SECTION_NAMES) if sections is None else sections
        )
        document = copy.deepcopy(stored_document)
        if sections is not None:
            document["sections"] = {
                name: copy.deepcopy(stored_document["sections"][name])
                for name in returned_sections
            }

        read_at = self._clock()
        bundle_age = _age_seconds(read_at, validated.captured_at)
        section_freshness = {}
        for name in returned_sections:
            section = validated.sections[name]
            section_age = _age_seconds(read_at, section.as_of)
            section_freshness[name] = {
                "as_of": section.as_of,
                "age_seconds": section_age,
                "status": _freshness(
                    section_age,
                    soft=section.soft_ttl_seconds,
                    hard=section.hard_ttl_seconds,
                ),
                "source": copy.deepcopy(section.source),
                "capture_status": section.status,
            }

        unavailable = [
            name
            for name in ANALYSIS_SECTION_NAMES
            if validated.sections[name].status == "unavailable"
        ]
        partial = [
            name
            for name in ANALYSIS_SECTION_NAMES
            if validated.sections[name].status == "partial"
        ]
        return AnalysisBundleGetResponse(
            bundle_id=bundle.bundle_uuid,
            content_hash=snapshot.canonical_payload_hash,
            integrity_verified=True,
            created_at=bundle.created_at,
            captured_at=validated.captured_at,
            read_at=read_at,
            age_seconds=bundle_age,
            status="partial" if unavailable or partial else "complete",
            completeness={
                "unavailable_sections": unavailable,
                "partial_sections": partial,
            },
            stale_warning=bundle_age > 300
            or any(
                metadata["status"] != "fresh" for metadata in section_freshness.values()
            ),
            section_freshness=section_freshness,
            document=document,
        )
