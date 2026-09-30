"""#1112 — night sweep wiring, schedule gate and 7-D blocking report (no DB).

DB-backed behaviour (A1 sweep, A5 idempotency, the full inference write) lives
in ``test_night_sweep_db.py``; this module pins everything that can be proven
with fakes: the schedule is declared but not attached, the task body is gated,
the night scope is the narrow one, every blocking row names its row id and its
rule (A4), and an inferred expiry is visibly different in rung projections (A3).
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.mcp_server.tooling import order_proposal_tools as opt
from app.services.order_proposals import kr_buy_blocking as kbb
from app.services.order_proposals import service as service_module
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_VOID_REASON,
    classify_leftover_rung,
)
from app.services.order_proposals.kis_leftover_inference_service import (
    KisLeftoverInferenceService,
)
from app.services.order_proposals.night_sweep import (
    NIGHT_SWEEP_CRONS,
    NIGHT_SWEEP_GROUP_STATES,
    NIGHT_SWEEP_RUNG_STATES,
    NIGHT_SWEEP_VOID_REASON,
)
from app.tasks import order_proposal_expiry_tasks as tasks
from tests.services.order_proposals.test_kis_leftover_inference import (
    ISOLATED_BREAKS,
    _facts,
)

pytestmark = pytest.mark.unit

KST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime(2026, 9, 30, 7, 0, tzinfo=KST)


# --- schedule: declared, never attached by default --------------------------


def test_night_sweep_flags_default_off():
    fields = Settings.model_fields
    assert fields["order_proposal_night_sweep_enabled"].default is False
    assert fields["order_proposal_night_sweep_schedule_enabled"].default is False


def test_registered_task_carries_no_schedule_by_default():
    labels = getattr(tasks.order_proposal_night_sweep_task, "labels", {}) or {}
    assert not labels.get("schedule")


def test_declared_crons_are_16_30_and_07_00_kst(monkeypatch):
    assert NIGHT_SWEEP_CRONS == ("30 16 * * 1-5", "0 7 * * 1-5")
    monkeypatch.setattr(
        tasks.settings, "order_proposal_night_sweep_schedule_enabled", False
    )
    assert tasks._night_sweep_schedule_labels() == []
    monkeypatch.setattr(
        tasks.settings, "order_proposal_night_sweep_schedule_enabled", True
    )
    assert tasks._night_sweep_schedule_labels() == [
        {"cron": "30 16 * * 1-5", "cron_offset": "Asia/Seoul"},
        {"cron": "0 7 * * 1-5", "cron_offset": "Asia/Seoul"},
    ]


@pytest.mark.asyncio
async def test_task_body_is_gated_off_by_default(monkeypatch):
    async def _must_not_run(**_: Any) -> dict[str, Any]:
        raise AssertionError("night sweep ran while disabled")

    monkeypatch.setattr(tasks.settings, "order_proposal_night_sweep_enabled", False)
    monkeypatch.setattr(tasks, "run_order_proposal_night_sweep", _must_not_run)
    result = await tasks.order_proposal_night_sweep_task()
    assert result == {"status": "disabled", "swept": 0, "inferred": 0}


@pytest.mark.asyncio
async def test_task_body_reports_counts_when_enabled(monkeypatch):
    async def _fake(**_: Any) -> dict[str, Any]:
        return {
            "expiry": {"swept_count": 2},
            "inference": {"applied": 1, "blocked_rows": [{}, {}, {}]},
        }

    monkeypatch.setattr(tasks.settings, "order_proposal_night_sweep_enabled", True)
    monkeypatch.setattr(tasks, "run_order_proposal_night_sweep", _fake)
    result = await tasks.order_proposal_night_sweep_task()
    assert result == {
        "status": "ok",
        "swept": 2,
        "inferred": 1,
        "inference_blocked": 3,
    }


# --- night scope is the narrow one ------------------------------------------


class _NullSession:
    async def __aenter__(self) -> _NullSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_night_sweep_passes_the_narrow_scope_and_marker(monkeypatch):
    seen: dict[str, Any] = {}

    async def _expire(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"success": True, "swept_count": 0, "swept_proposal_ids": []}

    async def _apply(self: Any, *, now: datetime.datetime) -> dict[str, Any]:
        seen["inference_now"] = now
        return {"applied": 0, "blocked_rows": []}

    monkeypatch.setattr(opt, "run_order_proposal_expire_sweep", _expire)
    monkeypatch.setattr(opt, "AsyncSessionLocal", _NullSession)
    monkeypatch.setattr(KisLeftoverInferenceService, "apply", _apply)

    result = await opt.run_order_proposal_night_sweep(now=NOW)

    assert result["success"] is True
    assert seen["lifecycle_states"] == NIGHT_SWEEP_GROUP_STATES == {"proposed"}
    assert seen["rung_states"] == NIGHT_SWEEP_RUNG_STATES
    assert "approved" not in seen["rung_states"]
    assert "revalidating" not in seen["rung_states"]
    assert seen["void_reason"] == NIGHT_SWEEP_VOID_REASON
    assert seen["inference_now"] == NOW


# --- A4: every blocking row names its row id and its rule -------------------


def _group(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "proposal_id": uuid.uuid4(),
        "symbol": "005880",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "side": "buy",
        "order_type": "limit",
        "lifecycle_state": "proposed",
        "strategy": "underwater_support_net",
        "valid_until": NOW + datetime.timedelta(hours=1),
        "created_at": NOW - datetime.timedelta(days=1),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


_rung_ids = iter(range(1000, 100000))


def _rung(state: str, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": next(_rung_ids),
        "rung_index": 0,
        "side": "buy",
        "state": state,
        "broker_order_id": None,
        "void_reason": None,
        "updated_at": NOW - datetime.timedelta(hours=1),
        "filled_qty": None,
        "quantity": Decimal("1"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeService:
    active: list[tuple[Any, list[Any]]] = []
    cleared: list[tuple[Any, Any]] = []

    def __init__(self, _session: Any) -> None:
        pass

    async def list_active_side_groups(self, **kwargs: Any):
        assert kwargs["market"] == "equity_kr" and kwargs["side"] == "buy"
        return list(self.active)

    async def list_rungs_by_void_reasons(self, **kwargs: Any):
        assert kwargs["void_reasons"] == frozenset(
            {NIGHT_SWEEP_VOID_REASON, EXPIRED_INFERENCE_VOID_REASON}
        )
        return list(self.cleared)


def _install(monkeypatch, *, active, cleared=(), inference_facts=None):
    monkeypatch.setattr(_FakeService, "active", list(active))
    monkeypatch.setattr(_FakeService, "cleared", list(cleared))
    monkeypatch.setattr(service_module, "OrderProposalsService", _FakeService)

    async def _evaluate(self: Any, group: Any, rung: Any, *, now: Any):
        facts = (inference_facts or {})[rung.id]
        return classify_leftover_rung(facts, now=now)

    monkeypatch.setattr(KisLeftoverInferenceService, "evaluate", _evaluate)


def _items(report: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    row = next(r for r in report["symbols"] if r["symbol"] == symbol)
    return row["blocking"]


@pytest.mark.asyncio
async def test_blocking_rows_carry_row_id_and_rule(monkeypatch):
    stale = _group(symbol="005880", valid_until=NOW - datetime.timedelta(days=22))
    stale_rung = _rung("pending_approval")
    stale_mid_approval = _group(
        symbol="000100", valid_until=NOW - datetime.timedelta(days=1)
    )
    mid_rung = _rung("revalidating")
    live = _group(symbol="000200")
    live_rung = _rung("pending_approval")
    approved_late = _group(
        symbol="000300",
        lifecycle_state="approved",
        valid_until=NOW - datetime.timedelta(minutes=5),
    )
    approved_rung = _rung("approved")
    kis_ok = _group(symbol="196170", lifecycle_state="submitted")
    kis_ok_rung = _rung("resting", broker_order_id="0012345678")
    kis_bad = _group(symbol="171090", lifecycle_state="submitted")
    kis_bad_rung = _rung("resting", broker_order_id="0012345678")
    toss = _group(
        symbol="035720", lifecycle_state="submitted", account_mode="toss_live"
    )
    toss_rung = _rung("resting", broker_order_id="T-1")
    mixed = _group(symbol="004020", lifecycle_state="partially_submitted")
    mixed_done = _rung("expired", rung_index=0)
    mixed_live = _rung("acked", rung_index=1, broker_order_id="0000000009")

    _install(
        monkeypatch,
        active=[
            (stale, [stale_rung]),
            (stale_mid_approval, [mid_rung]),
            (live, [live_rung]),
            (approved_late, [approved_rung]),
            (kis_ok, [kis_ok_rung]),
            (kis_bad, [kis_bad_rung]),
            (toss, [toss_rung]),
            (mixed, [mixed_done, mixed_live]),
        ],
        inference_facts={
            kis_ok_rung.id: _facts(),
            kis_bad_rung.id: _facts(**ISOLATED_BREAKS["no_fill_in_execution_ledger"]),
        },
    )

    report = await kbb.build_kr_buy_blocking_report(object(), now=NOW)  # type: ignore[arg-type]

    assert report["state"] == "known"
    assert report["scope"] == "order_proposals_only"
    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    for row in by_symbol.values():
        assert row["blocked"] is True
        for item in row["blocking"]:
            assert item["rule"] in {
                kbb.RULE_NONTERMINAL_PROPOSAL,
                kbb.RULE_STALE_PROPOSAL,
                kbb.RULE_KIS_INFERENCE_NOT_MET,
                kbb.RULE_KIS_INFERENCE_PENDING_SWEEP,
                kbb.RULE_BROKER_LIVE_RUNG,
            }
            assert item["proposal_id"] and isinstance(item["rung_id"], int)

    [item] = _items(report, "005880")
    assert item["rule"] == kbb.RULE_STALE_PROPOSAL
    assert item["proposal_id"] == str(stale.proposal_id)
    assert item["rung_id"] == stale_rung.id
    assert item["sweep_eligible"] is True
    assert item["cleared_by"] == "order_proposal.night_sweep"

    [item] = _items(report, "000100")
    assert item["rule"] == kbb.RULE_STALE_PROPOSAL
    assert item["sweep_eligible"] is False
    assert item["cleared_by"] is None

    [item] = _items(report, "000200")
    assert item["rule"] == kbb.RULE_NONTERMINAL_PROPOSAL
    assert item["past_valid_until"] is False

    [item] = _items(report, "000300")
    assert item["rule"] == kbb.RULE_NONTERMINAL_PROPOSAL
    assert item["past_valid_until"] is True

    [item] = _items(report, "196170")
    assert item["rule"] == kbb.RULE_KIS_INFERENCE_PENDING_SWEEP
    assert item["inference"]["eligible"] is True

    [item] = _items(report, "171090")
    assert item["rule"] == kbb.RULE_KIS_INFERENCE_NOT_MET
    assert item["inference"]["failed_conditions"] == ["no_fill_in_execution_ledger"]

    [item] = _items(report, "035720")
    assert item["rule"] == kbb.RULE_BROKER_LIVE_RUNG
    assert "inference" not in item

    # The already-terminal rung of a partially submitted group is not a blocker.
    items = _items(report, "004020")
    assert [i["rung_id"] for i in items] == [mixed_live.id]
    assert items[0]["rule"] == kbb.RULE_BROKER_LIVE_RUNG

    assert report["blocked_symbol_count"] == 8


@pytest.mark.asyncio
async def test_cleared_rows_name_basis_time_and_caveat(monkeypatch):
    swept_at = NOW - datetime.timedelta(hours=14, minutes=30)
    inferred_at = NOW - datetime.timedelta(minutes=1)
    swept_group = _group(symbol="005880", lifecycle_state="expired")
    swept_rung = _rung(
        "expired", void_reason=NIGHT_SWEEP_VOID_REASON, updated_at=swept_at
    )
    inferred_group = _group(symbol="171090", lifecycle_state="expired")
    inferred_rung = _rung(
        "expired", void_reason=EXPIRED_INFERENCE_VOID_REASON, updated_at=inferred_at
    )
    _install(
        monkeypatch,
        active=[],
        cleared=[(swept_group, swept_rung), (inferred_group, inferred_rung)],
    )

    report = await kbb.build_kr_buy_blocking_report(object(), now=NOW)  # type: ignore[arg-type]

    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    assert by_symbol["005880"]["blocked"] is False
    [swept] = by_symbol["005880"]["cleared"]
    assert swept["basis"] == kbb.CLEARED_BASIS_NIGHT_SWEEP
    assert swept["cleared_at"] == swept_at.isoformat()
    assert swept["rung_id"] == swept_rung.id
    assert swept["caveat"] is None
    [inferred] = by_symbol["171090"]["cleared"]
    assert inferred["basis"] == kbb.CLEARED_BASIS_INFERENCE
    assert inferred["caveat"] == "no_broker_original"
    assert report["blocked_symbol_count"] == 0


@pytest.mark.asyncio
async def test_report_refuses_naive_now():
    with pytest.raises(ValueError):
        await kbb.build_kr_buy_blocking_report(
            object(),  # type: ignore[arg-type]
            now=datetime.datetime(2026, 9, 30, 7),
        )


@pytest.mark.asyncio
async def test_list_tool_degrades_to_unknown_not_empty(monkeypatch):
    async def _boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(opt, "build_kr_buy_blocking_report", _boom)
    monkeypatch.setattr(opt, "AsyncSessionLocal", _NullSession)
    result = await opt._kr_buy_blocking(symbol=None)
    assert result == {"state": "unknown", "error": "kr_buy_blocking_read_failed"}


# --- A3: projections distinguish inferred from broker-confirmed expiry -----


def _projection_rung(void_reason: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        rung_index=0,
        side="buy",
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        notional=Decimal("100"),
        state="expired",
        void_reason=void_reason,
        void_reason_group=None,
        broker_order_id="0012345678",
        correlation_id="c",
    )


def test_rung_projection_marks_inferred_expiry_only():
    inferred = opt._rung_dict(_projection_rung(EXPIRED_INFERENCE_VOID_REASON))
    assert inferred["expiry_basis"] == "inference"
    assert inferred["expiry_caveat"] == "no_broker_original"
    assert inferred["void_reason"] == EXPIRED_INFERENCE_VOID_REASON
    for other in (None, "expired", NIGHT_SWEEP_VOID_REASON):
        projected = opt._rung_dict(_projection_rung(other))
        assert "expiry_basis" not in projected
        assert "expiry_caveat" not in projected


def test_revalidate_terminal_evidence_distinguishes_inferred_expiry():
    from app.mcp_server.tooling.proposal_revalidate import _terminal_evidence

    group = SimpleNamespace(
        lifecycle_state="partially_submitted",
        valid_until=NOW + datetime.timedelta(days=1),
    )

    def _r(index: int, state: str, void_reason: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            rung_index=index, state=state, updated_at=NOW, void_reason=void_reason
        )

    pending = _r(1, "pending_approval", None)
    inferred = _terminal_evidence(
        group, [_r(0, "expired", EXPIRED_INFERENCE_VOID_REASON), pending], NOW
    )
    broker = _terminal_evidence(group, [_r(0, "expired", None), pending], NOW)
    assert inferred is not None and broker is not None
    assert inferred["inferred_expiry_rung_indexes"] == [0]
    assert inferred["expiry_caveat"] == "no_broker_original"
    assert "inferred_expiry_rung_indexes" not in broker
    assert "expiry_caveat" not in broker


@pytest.mark.asyncio
async def test_sweep_eligible_is_false_when_any_rung_is_outside_the_night_scope(
    monkeypatch,
):
    # sweep_expired skips a group holding ANY rung outside its scope, terminal
    # ones included, so the report must not promise the sweep will clear it.
    mixed = _group(symbol="000400", valid_until=NOW - datetime.timedelta(days=2))
    done = _rung("expired", rung_index=0)
    waiting = _rung("pending_approval", rung_index=1)
    _install(monkeypatch, active=[(mixed, [done, waiting])])

    report = await kbb.build_kr_buy_blocking_report(object(), now=NOW)  # type: ignore[arg-type]

    [item] = _items(report, "000400")
    assert item["rung_id"] == waiting.id
    assert item["rule"] == kbb.RULE_STALE_PROPOSAL
    assert item["sweep_eligible"] is False
    assert item["cleared_by"] is None
