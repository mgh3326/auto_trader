"""Unit tests for Research Run service helpers without a database."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.trading import InstrumentType
from app.services.nxt_classifier_service import NxtClassifierItem
from app.services.pending_reconciliation_service import PendingReconciliationItem
from app.services.research_run_service import (
    add_research_run_candidates,
    attach_pending_reconciliations,
    create_research_run,
    get_research_run_by_uuid,
    list_user_research_runs,
    reconciliation_create_from_nxt,
    reconciliation_create_from_recon,
)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self._next_id = 1

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id  # type: ignore[attr-defined]
                self._next_id += 1

    async def refresh(self, obj: object) -> None:
        if getattr(obj, "run_uuid", None) is None and hasattr(obj, "run_uuid"):
            obj.run_uuid = uuid4()  # type: ignore[attr-defined]
        if getattr(obj, "candidate_uuid", None) is None and hasattr(
            obj, "candidate_uuid"
        ):
            obj.candidate_uuid = uuid4()  # type: ignore[attr-defined]


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value

    def scalar_one_or_none(self) -> object:
        return self.value


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows


class _ExecuteSession:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return self.results.pop(0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_research_run_json_safes_metadata_and_advisories() -> None:
    session = _FakeSession()
    run = await create_research_run(
        session,  # type: ignore[arg-type]
        user_id=7,
        market_scope="kr",
        stage="preopen",
        source_profile="hermes",
        market_brief={"score": Decimal("1.25")},
        source_freshness={"quote_age_sec": Decimal("3.5")},
        source_warnings=["missing_orderbook"],
        advisory_links=[
            {
                "provider": "TradingAgents",
                "advisory_only": True,
                "execution_allowed": False,
                "confidence": Decimal("0.70"),
            }
        ],
        generated_at=datetime.now(UTC),
    )

    assert run.id == 1
    assert run.market_brief == {"score": "1.25"}
    assert run.source_freshness == {"quote_age_sec": "3.5"}
    assert run.source_warnings == ["missing_orderbook"]
    assert run.advisory_links[0]["confidence"] == "0.70"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_research_run_rejects_execution_enabled_advisory() -> None:
    session = _FakeSession()
    with pytest.raises(ValueError, match="advisory-only"):
        await create_research_run(
            session,  # type: ignore[arg-type]
            user_id=7,
            market_scope="kr",
            stage="preopen",
            source_profile="hermes",
            advisory_links=[{"advisory_only": True, "execution_allowed": True}],
            generated_at=datetime.now(UTC),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_candidates_json_safes_payloads() -> None:
    session = _FakeSession()
    candidates = await add_research_run_candidates(
        session,  # type: ignore[arg-type]
        research_run_id=11,
        candidates=[
            {
                "symbol": "005930",
                "instrument_type": InstrumentType.equity_kr,
                "candidate_kind": "screener_hit",
                "source_freshness": {"age_sec": Decimal("2")},
                "warnings": ["stale_quote"],
                "payload": {"nested": [Decimal("1.23")]},
            }
        ],
    )

    assert candidates[0].id == 1
    assert candidates[0].research_run_id == 11
    assert candidates[0].source_freshness == {"age_sec": "2"}
    assert candidates[0].payload == {"nested": ["1.23"]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attach_pending_reconciliations_json_safes_decision_support() -> None:
    session = _FakeSession()
    items = await attach_pending_reconciliations(
        session,  # type: ignore[arg-type]
        research_run_id=11,
        items=[
            {
                "order_id": "O1",
                "symbol": "005930",
                "market": "kr",
                "side": "buy",
                "classification": "near_fill",
                "gap_pct": Decimal("0.20"),
                "reasons": ["gap_within_near_fill_pct"],
                "warnings": ["missing_orderbook"],
                "decision_support": {"current_price": Decimal("70140")},
            }
        ],
    )

    assert items[0].id == 1
    assert items[0].gap_pct == pytest.approx(Decimal("0.20"))
    assert items[0].decision_support == {"current_price": "70140"}


@pytest.mark.unit
def test_reconciliation_create_from_recon_preserves_recon_fields() -> None:
    item = PendingReconciliationItem(
        order_id="O42",
        symbol="005930",
        market="kr",
        side="buy",
        classification="near_fill",
        nxt_actionable=True,
        gap_pct=Decimal("0.20"),
        reasons=("gap_within_near_fill_pct",),
        warnings=("missing_orderbook",),
        decision_support={"current_price": Decimal("70140")},
    )

    payload = reconciliation_create_from_recon(item, candidate_id=3, summary="요약")

    assert payload["candidate_id"] == 3
    assert payload["classification"] == "near_fill"
    assert payload["decision_support"] == {"current_price": Decimal("70140")}
    assert payload["summary"] == "요약"


@pytest.mark.unit
def test_reconciliation_create_from_nxt_maps_classifier_fields() -> None:
    item = NxtClassifierItem(
        item_id="O99",
        symbol="005930",
        kind="pending_order",
        side="sell",
        classification="sell_pending_near_resistance",
        nxt_actionable=True,
        summary="NXT 매도 대기 — 저항선 근접",
        reasons=("order_within_near_resistance_pct",),
        warnings=("wide_spread",),
        decision_support={"nearest_resistance_price": Decimal("71000")},
    )

    payload = reconciliation_create_from_nxt(item, candidate_id=5)

    assert payload["candidate_id"] == 5
    assert payload["order_id"] == "O99"
    assert payload["side"] == "sell"
    assert payload["classification"] == "unknown"
    assert payload["nxt_classification"] == "sell_pending_near_resistance"
    assert payload["summary"] == "NXT 매도 대기 — 저항선 근접"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_research_run_by_uuid_returns_owned_run() -> None:
    run = SimpleNamespace(id=1)
    session = _ExecuteSession(_ScalarResult(run))

    result = await get_research_run_by_uuid(
        session,  # type: ignore[arg-type]
        run_uuid=uuid4(),
        user_id=7,
    )

    assert result is run
    assert len(session.statements) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_user_research_runs_applies_filters_and_counts() -> None:
    run = SimpleNamespace(id=1)
    row = SimpleNamespace(ResearchRun=run, candidate_count=2, reconciliation_count=3)
    session = _ExecuteSession(_ScalarResult(1), _RowsResult([row]))

    rows, total = await list_user_research_runs(
        session,  # type: ignore[arg-type]
        user_id=7,
        market_scope="kr",
        stage="preopen",
        status="open",
        limit=10,
        offset=5,
    )

    assert total == 1
    assert rows == [(run, 2, 3)]
    assert len(session.statements) == 2
