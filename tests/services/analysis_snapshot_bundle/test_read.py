from __future__ import annotations

import copy
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from app.schemas.analysis_snapshot_bundle import ANALYSIS_SECTION_NAMES
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.analysis_snapshot_bundle.read import (
    AnalysisBundleIntegrityError,
    AnalysisBundleNotFound,
    AnalysisBundleReadService,
    UnknownAnalysisBundleSection,
)
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

FROZEN_NOW = dt.datetime(2026, 7, 12, 3, 10, tzinfo=dt.UTC)


@dataclass
class SeededBundle:
    bundle_uuid: uuid.UUID
    payload_json: dict[str, Any]
    canonical_payload_hash: str | None


@pytest_asyncio.fixture
async def repo(db_session) -> InvestmentSnapshotsRepository:
    return InvestmentSnapshotsRepository(db_session)


@pytest.fixture
def frozen_clock():
    return lambda: FROZEN_NOW


@pytest_asyncio.fixture
async def service(repo, frozen_clock) -> AnalysisBundleReadService:
    return AnalysisBundleReadService(repo, clock=frozen_clock)


@pytest_asyncio.fixture
async def seed_bundle(repo):
    sequence = 0
    retained_rows = []

    async def seed(
        *,
        purpose: str = "analysis_recheck",
        item_count: int = 1,
        snapshot_kind: str = "llm_input_frozen",
        malformed: bool = False,
        investor_flow_unavailable: bool = False,
        captured_at: dt.datetime = FROZEN_NOW,
    ) -> SeededBundle:
        nonlocal sequence
        sequence += 1
        unique_at = captured_at + dt.timedelta(microseconds=sequence)
        run = await repo.insert_run(
            SnapshotRunCreate(
                purpose="report_generation",
                market="kr",
                account_scope="kis_live",
                requested_by="claude_code",
                policy_version=f"read-test-{sequence}",
            )
        )
        bundle = await repo.insert_bundle(
            BundleCreate(
                purpose=purpose,
                market="kr",
                account_scope="kis_live",
                policy_version=f"read-test-{sequence}",
                as_of=unique_at,
                status="complete",
            )
        )
        payload_json = _frozen_document(
            captured_at=captured_at,
            investor_flow_unavailable=investor_flow_unavailable,
        )
        if malformed:
            del payload_json["sections"]["decision_history"]

        canonical_hash = None
        for item_index in range(item_count):
            item_payload = (
                payload_json
                if item_index == 0
                else _frozen_document(
                    captured_at=captured_at + dt.timedelta(microseconds=item_index + 1)
                )
            )
            if item_index > 0:
                item_payload["sections"]["portfolio"]["data"] = {
                    "stored": "portfolio",
                    "item_index": item_index,
                }
            snapshot = await repo.insert_snapshot(
                SnapshotCreate(
                    run_uuid=run.run_uuid,
                    snapshot_kind=snapshot_kind,
                    market="kr",
                    account_scope="kis_live",
                    source_kind="combined",
                    payload_json=item_payload,
                    as_of=unique_at,
                    freshness_status="fresh",
                )
            )
            await repo.link_bundle_item(
                bundle_uuid=bundle.bundle_uuid,
                item=BundleItemCreate(
                    snapshot_uuid=snapshot.snapshot_uuid,
                    role="required",
                ),
            )
            if item_index == 0:
                payload_json = snapshot.payload_json
                canonical_hash = snapshot.canonical_payload_hash
            retained_rows.append(snapshot)

        return SeededBundle(
            bundle_uuid=bundle.bundle_uuid,
            payload_json=payload_json,
            canonical_payload_hash=canonical_hash,
        )

    return seed


def _frozen_document(
    *,
    captured_at: dt.datetime,
    investor_flow_unavailable: bool = False,
) -> dict[str, Any]:
    timestamp = captured_at.isoformat()
    sections: dict[str, Any] = {}
    for name in ANALYSIS_SECTION_NAMES:
        unavailable = name == "investor_flow" and investor_flow_unavailable
        sections[name] = {
            "status": "unavailable" if unavailable else "ok",
            "collected_at": timestamp,
            "as_of": timestamp,
            "source": {"kind": "persisted", "section": name},
            "soft_ttl_seconds": 60,
            "hard_ttl_seconds": 300,
            "data": None if unavailable else {"stored": name},
            "error": "provider unavailable" if unavailable else None,
        }
    return {
        "schema_version": "analysis-snapshot-bundle.v1",
        "captured_at": timestamp,
        "request": {
            "market": "kr",
            "account_scope": "kis_live",
            "symbols": ["005930"],
            "user_id": 7,
            "market_session": "regular",
            "requested_by": "claude_code",
        },
        "sections": sections,
    }


@pytest.mark.asyncio
async def test_missing_bundle_is_not_found(service):
    with pytest.raises(AnalysisBundleNotFound):
        await service.get(uuid.uuid4())


@pytest.mark.asyncio
async def test_wrong_bundle_purpose_fails_integrity(service, seed_bundle):
    wrong = await seed_bundle(purpose="report_generation")
    with pytest.raises(AnalysisBundleIntegrityError):
        await service.get(wrong.bundle_uuid)


@pytest.mark.asyncio
@pytest.mark.parametrize("item_count", [0, 2])
async def test_wrong_item_count_fails_integrity(service, seed_bundle, item_count):
    stored = await seed_bundle(item_count=item_count)
    with pytest.raises(AnalysisBundleIntegrityError):
        await service.get(stored.bundle_uuid)


@pytest.mark.asyncio
async def test_wrong_snapshot_kind_fails_integrity(service, seed_bundle):
    stored = await seed_bundle(snapshot_kind="portfolio")
    with pytest.raises(AnalysisBundleIntegrityError):
        await service.get(stored.bundle_uuid)


@pytest.mark.asyncio
async def test_tampered_payload_fails_closed(service, seed_bundle):
    stored = await seed_bundle()
    stored.payload_json["sections"]["portfolio"]["data"] = {"tampered": True}
    with pytest.raises(AnalysisBundleIntegrityError):
        await service.get(stored.bundle_uuid)


@pytest.mark.asyncio
async def test_malformed_frozen_document_fails_integrity(service, seed_bundle):
    malformed = await seed_bundle(malformed=True)
    with pytest.raises(AnalysisBundleIntegrityError):
        await service.get(malformed.bundle_uuid)


@pytest.mark.asyncio
async def test_unknown_section_is_rejected(service, seed_bundle):
    stored = await seed_bundle()
    with pytest.raises(UnknownAnalysisBundleSection):
        await service.get(stored.bundle_uuid, sections=["not_a_section"])


@pytest.mark.asyncio
async def test_get_returns_exact_stored_document(service, seed_bundle):
    stored = await seed_bundle()
    response = await service.get(stored.bundle_uuid)
    assert response.document == stored.payload_json
    assert response.content_hash == stored.canonical_payload_hash
    assert response.integrity_verified is True


@pytest.mark.asyncio
async def test_sections_filter_only_projects_stored_values(service, seed_bundle):
    stored = await seed_bundle()
    response = await service.get(stored.bundle_uuid, sections=["portfolio"])
    assert response.document["sections"] == {
        "portfolio": stored.payload_json["sections"]["portfolio"]
    }
    assert response.document["request"] == stored.payload_json["request"]
    assert set(response.section_freshness) == {"portfolio"}


@pytest.mark.asyncio
async def test_unavailable_section_is_not_filled(service, seed_bundle):
    stored = await seed_bundle(investor_flow_unavailable=True)
    response = await service.get(stored.bundle_uuid)
    assert (
        response.document["sections"]["investor_flow"]
        == (stored.payload_json["sections"]["investor_flow"])
    )
    assert response.status == "partial"
    assert response.completeness["unavailable_sections"] == ["investor_flow"]


@pytest.mark.asyncio
async def test_age_and_stale_metadata_do_not_modify_document(
    service, seed_bundle, frozen_clock
):
    stored = await seed_bundle(captured_at=FROZEN_NOW - dt.timedelta(seconds=301))
    before = copy.deepcopy(stored.payload_json)
    response = await service.get(stored.bundle_uuid)
    assert response.read_at == frozen_clock()
    assert response.age_seconds == 301
    assert response.stale_warning is True
    assert response.section_freshness["portfolio"].status == "hard_stale"
    assert response.document == before


@pytest.mark.asyncio
async def test_future_timestamps_clamp_computed_ages_to_zero(service, seed_bundle):
    stored = await seed_bundle(captured_at=FROZEN_NOW + dt.timedelta(seconds=1))
    response = await service.get(stored.bundle_uuid)
    assert response.age_seconds == 0
    assert response.section_freshness["portfolio"].age_seconds == 0
    assert response.stale_warning is False
