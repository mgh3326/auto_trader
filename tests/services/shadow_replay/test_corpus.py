"""ROB-697 (M1) — replay-corpus selection tests.

The integration test seeds real ORM rows via the shared ``db_session``
fixture (real PostgreSQL, see ``tests/conftest.py``); the unit tests
exercise the pure predicates without touching the DB.

No ``seed_report_item`` factory exists in this repo (checked
``tests/conftest.py`` and ``tests/_investment_reports_helpers.py``), so
rows are constructed inline here, mirroring the payload-builder pattern
used by ``tests/test_investment_reports_model.py``
(``_base_payload`` / ``_base_item_payload``).
"""

from __future__ import annotations

import uuid

import pytest

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services.shadow_replay.corpus import (
    CorpusItem,
    _bundle_items_for_profile,
    _claude_family_extra,
    _claude_family_items,
    _covers_kinds,
    _non_autoemit,
    select_replay_corpus,
)


def _report_payload(**overrides) -> dict:
    payload: dict = {
        "report_uuid": uuid.uuid4(),
        "idempotency_key": f"rob697-report-{uuid.uuid4()}",
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "CLAUDE_ADVISOR",
        "title": "ROB-697 corpus test report",
        "summary": "corpus test",
        "status": "draft",
        "snapshot_bundle_uuid": uuid.uuid4(),
    }
    payload.update(overrides)
    return payload


def _item_payload(report_id: int, **overrides) -> dict:
    payload: dict = {
        "report_id": report_id,
        "item_uuid": uuid.uuid4(),
        "idempotency_key": f"rob697-item-{uuid.uuid4()}",
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "target_kind": "asset",
        "rationale": "ROB-697 corpus test seed row",
        "evidence_snapshot": {},
    }
    payload.update(overrides)
    return payload


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_autoemit_item_excluded(db_session):
    """``_bundle_items_for_profile`` drops auto_emit-sourced items, keeps the human one.

    Task-0 resolution #3: the brief's original test called
    ``select_replay_corpus``, but that requires action+watch coverage
    (``_covers_kinds``), and a single seeded ``action`` item can't satisfy
    that gate — the call would just raise ``CorpusUnavailable`` without
    ever exercising the auto_emit filter. ``_bundle_items_for_profile`` is
    the function that actually applies ``_non_autoemit`` with no coverage
    gate, so it is the correct unit to exercise here.
    """
    human_report = InvestmentReport(**_report_payload())
    db_session.add(human_report)
    await db_session.flush()
    human_item = InvestmentReportItem(**_item_payload(human_report.id))
    db_session.add(human_item)

    autoemit_report = InvestmentReport(**_report_payload())
    db_session.add(autoemit_report)
    await db_session.flush()
    autoemit_item = InvestmentReportItem(
        **_item_payload(
            autoemit_report.id,
            evidence_snapshot={
                "source": "auto_emit",
                "proposer": "auto_emit/buy_from_candidate",
            },
        )
    )
    db_session.add(autoemit_item)
    await db_session.commit()
    await db_session.refresh(human_item)

    items = await _bundle_items_for_profile(db_session, "CLAUDE_ADVISOR", limit=10)

    uuids = {i.item_uuid for i in items}
    assert str(human_item.item_uuid) in uuids
    assert all(
        i.reference_decision.get("proposer") != "auto_emit/buy_from_candidate"
        for i in items
    )


@pytest.mark.unit
def test_non_autoemit_predicate():
    class _I:
        evidence_snapshot = {"source": "auto_emit"}

    assert _non_autoemit(_I()) is False


@pytest.mark.unit
def test_non_autoemit_predicate_proposer_prefix():
    class _I:
        evidence_snapshot = {"proposer": "auto_emit/sell_from_held"}

    assert _non_autoemit(_I()) is False


@pytest.mark.unit
def test_non_autoemit_predicate_human_item_passes():
    class _I:
        evidence_snapshot = {}

    assert _non_autoemit(_I()) is True


def _corpus_item(item_kind: str) -> CorpusItem:
    return CorpusItem(
        snapshot_bundle_uuid=str(uuid.uuid4()),
        report_id=1,
        item_uuid=str(uuid.uuid4()),
        item_kind=item_kind,
        intent="buy_review",
        reference_decision={},
    )


@pytest.mark.unit
def test_covers_kinds_action_and_watch_present():
    items = [_corpus_item("action"), _corpus_item("watch")]
    assert _covers_kinds(items, min_per_kind=1) is True


@pytest.mark.unit
def test_covers_kinds_action_only_is_false():
    items = [_corpus_item("action"), _corpus_item("action")]
    assert _covers_kinds(items, min_per_kind=1) is False


@pytest.mark.unit
def test_claude_family_extra_default_and_env(monkeypatch):
    monkeypatch.delenv("SHADOW_REPLAY_CLAUDE_PROFILES", raising=False)
    assert "파이리" in _claude_family_extra()
    monkeypatch.setenv("SHADOW_REPLAY_CLAUDE_PROFILES", " foo , bar ")
    got = _claude_family_extra()
    assert {"파이리", "foo", "bar"} <= got


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_claude_family_matches_variants_excludes_hermes(db_session):
    """ROB-697 P0 census fix: the literal CLAUDE_ADVISOR is empty; real Claude
    decisions live under names like ``claude_code`` and custom labels like
    ``파이리``. The family matcher must catch those (case-insensitive %claude%
    + the extra label) and must NOT catch HERMES_ADVISOR.
    """
    seeded: dict[str, str] = {}
    for profile, kind in (
        ("claude_code", "action"),
        ("파이리", "watch"),
        ("HERMES_ADVISOR", "action"),
    ):
        report = InvestmentReport(**_report_payload(created_by_profile=profile))
        db_session.add(report)
        await db_session.flush()
        item = InvestmentReportItem(**_item_payload(report.id, item_kind=kind))
        db_session.add(item)
        await db_session.flush()
        seeded[profile] = str(item.item_uuid)
    await db_session.commit()

    items = await _claude_family_items(db_session, limit=200)
    found = {i.item_uuid for i in items}

    assert seeded["claude_code"] in found  # %claude% (lowercase)
    assert seeded["파이리"] in found  # explicit extra label
    assert seeded["HERMES_ADVISOR"] not in found  # hermes excluded


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_select_replay_corpus_prefers_claude_family(db_session):
    """With Claude-family action+watch coverage, the source is ``claude_family``."""
    for profile, kind in (("claude_code", "action"), ("파이리", "watch")):
        report = InvestmentReport(**_report_payload(created_by_profile=profile))
        db_session.add(report)
        await db_session.flush()
        db_session.add(InvestmentReportItem(**_item_payload(report.id, item_kind=kind)))
    await db_session.commit()

    selection = await select_replay_corpus(db_session, min_per_kind=1, limit=200)
    assert selection.source == "claude_family"
