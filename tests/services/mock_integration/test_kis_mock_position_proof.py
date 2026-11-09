"""A claim is released only on terminal order evidence AND exact position proof."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from app.services.kis_mock_recovery import recover_kis_mock_dispatch_claims
from app.services.kis_mock_runner.evidence_store import (
    KISMockEvidenceConflict,
    KISMockEvidenceStore,
    KISMockRecoveryLineage,
    NativeOrderEvidence,
    RediscoveredKISMockClaim,
)
from app.services.kis_mock_runner.position_proof import (
    BalanceSnapshot,
    KISMockPositionReader,
    PositionProof,
    PositionUnproven,
    parse_balance_pages,
    read_complete_domestic_balance,
)
from app.services.mock_integration.coordination import (
    DurableClaim,
    TerminalClaimEvidence,
)
from tests.services.mock_integration.test_coordination import _attempt_envelope
from tests.services.mock_integration.test_kis_coordination_adapter import (
    _coordinate,
    _ready_ports,
    _stack,
)
from tests.services.mock_integration.test_kis_mock_evidence_store import bound_entry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
KST = timezone(timedelta(hours=9))
ODNO = "0007"
SYMBOL = "005930"
ORDERED = datetime(2026, 11, 9, 10, 0, 0, tzinfo=KST)
DURING_SESSION = datetime(2026, 11, 9, 10, 5, 0, tzinfo=KST)
AFTER_DEADLINE = datetime(2026, 11, 9, 21, 0, 0, tzinfo=KST)
OK_TAIL = {"output2": [{"dnca_tot_amt": "1"}], "rt_cd": "0"}


def balance_page(*rows):
    return ({"output1": list(rows), **OK_TAIL}, {"tr_cont": "D"})


def holding(quantity):
    return {"pdno": SYMBOL, "hldg_qty": str(quantity)}


@pytest.fixture
def ctx():
    _, envelope = _attempt_envelope()
    claim = RediscoveredKISMockClaim(
        DurableClaim(7, "scope", envelope.order_attempt.idempotency_key, "buy"),
        envelope.order_attempt.order_attempt_id,
        ODNO,
        True,
    )
    store = KISMockEvidenceStore(MagicMock(), entry=bound_entry())

    @asynccontextmanager
    async def recovery_guard():
        yield SimpleNamespace(session=MagicMock())

    store.recovery_guard = recovery_guard
    store.rediscover_unreleased_claims = AsyncMock(return_value=(claim,))
    store.has_unresolved_legacy_reservations = AsyncMock(return_value=False)
    store.load_recovery_lineage = AsyncMock(
        return_value=KISMockRecoveryLineage(
            envelope, ORDERED - timedelta(minutes=1), ORDERED + timedelta(seconds=1)
        )
    )
    events: list[NativeOrderEvidence] = []

    async def record(_envelope, evidence):
        events.append(evidence)

    async def fills(_claim):
        values = {e.filled_quantity for e in events if e.filled_quantity is not None}
        if len(values) != 1:
            raise PositionUnproven("attributed_fills_unavailable")
        return values.pop()

    store.record_native_evidence = AsyncMock(side_effect=record)
    store.attributed_filled_quantity = AsyncMock(side_effect=fills)
    store.load_position_baseline = AsyncMock(return_value=5)
    store.release_with_recorded_terminal_evidence = AsyncMock(return_value=1)
    row = {
        "odno": ODNO,
        "ord_dt": ORDERED.strftime("%Y%m%d"),
        "ord_tmd": ORDERED.strftime("%H%M%S"),
        "pdno": SYMBOL,
        "sll_buy_dvsn_cd": "02",
        "ord_qty": "1",
        "tot_ccld_qty": "1",
        "rmn_qty": "0",
        "cncl_yn": "N",
        "cncl_cfrm_qty": "0",
        "rjct_qty": "0",
    }
    return SimpleNamespace(
        store=store, row=row, events=events, pages=[balance_page(holding(6))]
    )


async def run(ctx, *, now=DURING_SESSION, reader=None):
    async def default_reader(symbol):
        return parse_balance_pages(ctx.pages, symbol=symbol, observed_at=now)

    result = await recover_kis_mock_dispatch_claims(
        store=ctx.store,
        claims=MagicMock(),
        reader=AsyncMock(return_value=ctx.row),
        dry_run=False,
        confirm=True,
        record_dispatch_evidence=True,
        position_reader=reader or default_reader,
        clock=lambda: now,
    )
    return result, result["claims"][0]


def assert_released(ctx, result, claim, *, readback):
    assert (claim["status"], claim["readback"]) == ("released", readback)
    assert result["account_blocked"] is False
    assert result["status"] == "released"
    ctx.store.release_with_recorded_terminal_evidence.assert_awaited_once()
    proven = ctx.events[-1]
    assert proven.kind == "terminal_reconciliation"
    assert proven.terminal.account_position_reconciled is True
    assert proven.position_proof is not None


def assert_retained(ctx, result, claim):
    assert claim["status"] == "retained"
    assert result["account_blocked"] is True
    ctx.store.release_with_recorded_terminal_evidence.assert_not_awaited()
    assert not any(e.terminal.account_position_reconciled for e in ctx.events)


def cancelled(ctx):
    ctx.row.update(tot_ccld_qty="0", cncl_yn="Y", cncl_cfrm_qty="1")
    ctx.pages = [balance_page(holding(5))]


def rejected(ctx):
    ctx.row.update(tot_ccld_qty="0", rjct_qty="1")
    ctx.pages = [balance_page(holding(5))]


async def test_full_fill_with_matching_position_is_released(ctx):
    result, claim = await run(ctx)
    assert_released(ctx, result, claim, readback="terminal_fill")
    assert ctx.events[-1].position_proof.observed_quantity == 6


async def test_full_fill_with_truncated_position_read_is_retained(ctx):
    ctx.pages = [({"output1": [holding(6)], **OK_TAIL}, {"tr_cont": "M"})]
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_read_truncated"


async def test_position_mismatch_is_retained(ctx):
    ctx.pages = [balance_page(holding(7))]
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_mismatch"


@pytest.mark.parametrize("failure", [None, RuntimeError("balance read failed")])
async def test_missing_position_read_is_retained_not_zero(ctx, failure):
    # A zero baseline would match a zero-defaulted read if a miss became zero.
    ctx.store.load_position_baseline.return_value = 0
    cancelled(ctx)
    reader = AsyncMock(side_effect=failure, return_value=None)
    result, claim = await run(ctx, reader=reader)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] in {
        "position_read_failed",
        "position_read_malformed",
    }


async def test_stale_position_read_is_retained(ctx):
    cancelled(ctx)

    async def stale(symbol):
        return parse_balance_pages(
            ctx.pages, symbol=symbol, observed_at=DURING_SESSION - timedelta(minutes=2)
        )

    result, claim = await run(ctx, reader=stale)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_read_stale"


async def test_duplicated_symbol_row_is_ambiguous(ctx):
    ctx.pages = [balance_page(holding(6), holding(6))]
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_read_ambiguous"


async def test_absent_baseline_is_retained(ctx):
    ctx.store.load_position_baseline.side_effect = PositionUnproven(
        "position_baseline_unavailable"
    )
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_baseline_unavailable"


async def test_another_surviving_claim_prevents_attribution(ctx):
    only = ctx.store.rediscover_unreleased_claims.return_value[0]
    ctx.store.rediscover_unreleased_claims.return_value = (only, only)
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_other_claim_unresolved"


async def test_legacy_reservation_prevents_position_attribution(ctx):
    ctx.store.has_unresolved_legacy_reservations = AsyncMock(return_value=True)
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_other_claim_unresolved"


async def test_released_attempt_is_still_blocked_before_reservation(ctx):
    _, envelope = _attempt_envelope()
    ctx.store.has_unresolved_account_uncertainty = AsyncMock(return_value=False)
    ctx.store.has_consumed_attempt = AsyncMock(return_value=True)
    gate = ctx.store.for_attempt(envelope)
    assert await gate.has_unresolved_account_uncertainty(claim_account_scope="scope")
    ctx.store.has_consumed_attempt.assert_awaited_once_with(envelope)


async def test_coordinator_cannot_resend_a_released_attempt(ctx):
    _, envelope = _attempt_envelope()
    stack = _stack()
    ctx.store.has_unresolved_account_uncertainty = AsyncMock(return_value=False)
    ctx.store.has_consumed_attempt = AsyncMock(return_value=True)
    ports = _ready_ports(
        persistence=stack["persistence"],
        dispatch_evidence=stack["evidence"],
        uncertainty_gate=ctx.store,
    )
    mutation = AsyncMock()
    try:
        await _coordinate(envelope, stack, mutation, ports=ports)
    except Exception as exc:
        assert "durable_claim_conflict" in str(exc)
    assert mutation.await_count == 0
    assert stack["intents"].reserve_calls == []


async def test_unidentifiable_holding_cannot_prove_zero_position(ctx):
    ctx.store.load_position_baseline.return_value = 0
    cancelled(ctx)
    ctx.pages = [balance_page({"pdno": "", "hldg_qty": "1"})]
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["position_reason"] == "position_read_malformed"


@pytest.mark.parametrize(
    "row",
    [
        {"pdno": " ", "hldg_qty": "0"},
        {"pdno": "???", "hldg_qty": "1"},
        {"pdno": "000660", "hldg_qty": "?"},
    ],
)
async def test_any_malformed_holding_row_invalidates_the_read(row):
    with pytest.raises(PositionUnproven, match="position_read_malformed"):
        parse_balance_pages(
            [balance_page(row)], symbol=SYMBOL, observed_at=DURING_SESSION
        )


async def test_recovery_authority_unavailable_keeps_account_blocked(ctx):
    @asynccontextmanager
    async def unavailable():
        raise RuntimeError("lock unavailable")
        yield

    ctx.store.recovery_guard = unavailable
    result = await recover_kis_mock_dispatch_claims(
        store=ctx.store,
        claims=MagicMock(),
        reader=AsyncMock(),
        dry_run=False,
        confirm=True,
        record_dispatch_evidence=True,
    )
    assert result["account_blocked"] is True
    assert result["reason"] == "recovery_authority_unavailable"
    ctx.store.release_with_recorded_terminal_evidence.assert_not_awaited()


async def test_release_of_no_exact_row_is_not_reported_as_released(ctx):
    ctx.store.release_with_recorded_terminal_evidence.return_value = 0
    result, claim = await run(ctx)
    assert claim["status"] == "retained"
    assert claim["position_reason"] == "position_stale_or_claim_changed"
    assert result["account_blocked"] is True


async def test_later_fresh_read_gets_new_proof_key_after_failed_release(ctx):
    ctx.store.release_with_recorded_terminal_evidence.side_effect = [0, 1]
    _, first = await run(ctx)
    assert first["status"] == "retained"
    _, second = await run(ctx, now=DURING_SESSION + timedelta(seconds=1))
    assert second["status"] == "released"
    proofs = [event for event in ctx.events if event.position_proof is not None]
    assert len(proofs) == 2
    assert proofs[0].broker_evidence_key != proofs[1].broker_evidence_key


async def test_confirmed_cancel_with_unchanged_position_is_released(ctx):
    cancelled(ctx)
    result, claim = await run(ctx)
    assert_released(ctx, result, claim, readback="terminal_cancel")
    assert ctx.events[0].kind == "cancel"


@pytest.mark.parametrize(
    "fields",
    [
        {"cncl_yn": "Y", "cncl_cfrm_qty": "0"},
        {"cncl_yn": "Y", "cncl_cfrm_qty": "1", "rmn_qty": "1"},
        {"cncl_yn": "N", "cncl_cfrm_qty": "0"},
        {"cncl_yn": "", "cncl_cfrm_qty": "1"},
    ],
)
async def test_cancel_request_without_confirmation_is_retained(ctx, fields):
    ctx.row.update(tot_ccld_qty="0", **fields)
    ctx.pages = [balance_page(holding(5))]
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["readback"] != "terminal_cancel"


async def test_day_expiry_with_positive_not_working_read_is_released(ctx):
    rejected(ctx)
    result, claim = await run(ctx, now=AFTER_DEADLINE)
    assert_released(ctx, result, claim, readback="terminal_expiry")
    assert ctx.events[0].kind == "expiry"


async def test_expiry_inferred_from_time_only_is_retained(ctx):
    ctx.row.update(tot_ccld_qty="0", rmn_qty="1", rjct_qty="0")
    ctx.pages = [balance_page(holding(5))]
    result, claim = await run(ctx, now=AFTER_DEADLINE)
    assert_retained(ctx, result, claim)
    assert claim["readback"] == "accepted"


async def test_odno_reused_on_another_kst_day_is_not_matched(ctx):
    ctx.row["ord_dt"] = (ORDERED + timedelta(days=1)).strftime("%Y%m%d")
    result, claim = await run(ctx, now=DURING_SESSION + timedelta(days=1))
    assert_retained(ctx, result, claim)
    assert claim["readback"] == "uncertain"
    assert ctx.events == []


async def test_zero_fill_terminal_is_released_with_position_proof(ctx):
    rejected(ctx)
    result, claim = await run(ctx)
    assert_released(ctx, result, claim, readback="zero_fill_terminal")
    assert ctx.events[0].kind == "reject"


async def test_zero_fill_terminal_without_position_proof_is_retained(ctx):
    rejected(ctx)
    ctx.pages = [({"output1": [], "rt_cd": "0", "output2": []}, {"tr_cont": "D"})]
    result, claim = await run(ctx)
    assert_retained(ctx, result, claim)
    assert claim["readback"] == "zero_fill_terminal"
    assert claim["position_reason"] == "position_read_malformed"


async def test_complete_read_omitting_the_symbol_is_zero_for_a_sold_out_position():
    snapshot = parse_balance_pages(
        [balance_page({"pdno": "000660", "hldg_qty": "3"})],
        symbol=SYMBOL,
        observed_at=DURING_SESSION,
    )
    assert snapshot.quantity == 0


@pytest.mark.parametrize(
    ("pages", "reason"),
    [
        ([], "position_read_malformed"),
        (
            [({"rt_cd": "1", "output1": []}, {"tr_cont": "D"})],
            "position_read_malformed",
        ),
        ([({"output1": [holding(1)], **OK_TAIL}, {})], "position_read_truncated"),
        (
            [({"output1": [holding(1)], **OK_TAIL}, {"tr_cont": "F"})],
            "position_read_truncated",
        ),
        ([balance_page({"pdno": SYMBOL, "hldg_qty": ""})], "position_read_malformed"),
        (
            [balance_page({"pdno": SYMBOL, "hldg_qty": "1.5"})],
            "position_read_malformed",
        ),
    ],
)
async def test_incomplete_or_malformed_pages_are_unproven(pages, reason):
    with pytest.raises(PositionUnproven) as caught:
        parse_balance_pages(pages, symbol=SYMBOL, observed_at=DURING_SESSION)
    assert caught.value.reason == reason


def balance_client(responses):
    settings = SimpleNamespace(kis_account_no="12345678-01", kis_access_token="t")
    return SimpleNamespace(
        _ensure_token=AsyncMock(),
        _settings=settings,
        _hdr_base={},
        _kis_url=lambda path: path,
        _request_with_rate_limit_with_headers=AsyncMock(side_effect=responses),
    )


async def test_reader_follows_continuation_to_the_end():
    first = (
        {"ctx_area_fk100": "f", "ctx_area_nk100": "n1", **OK_TAIL, "output1": []},
        {"tr_cont": "F"},
    )
    last = ({"output1": [holding(2)], **OK_TAIL}, {"tr_cont": "D"})
    client = balance_client([first, last])
    pages = await read_complete_domestic_balance(client)
    assert pages == [first, last]
    second = client._request_with_rate_limit_with_headers.await_args_list[1].kwargs
    assert (second["params"]["CTX_AREA_FK100"], second["params"]["CTX_AREA_NK100"]) == (
        "f",
        "n1",
    )
    assert second["headers"]["tr_cont"] == "N"
    assert second["tr_id"] == "VTTC8434R"


async def test_reader_timestamps_the_start_of_the_balance_read(monkeypatch):
    import app.services.kis_mock_runner.position_proof as module

    current = [DURING_SESSION]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]

    async def slow_response(*args, **kwargs):
        current[0] += timedelta(seconds=61)
        return balance_page(holding(6))

    client = balance_client([])
    client._request_with_rate_limit_with_headers.side_effect = slow_response
    reader = KISMockPositionReader(bound_entry())
    monkeypatch.setattr(reader, "_checked_client", lambda: client)
    monkeypatch.setattr(module, "datetime", ClockDateTime)
    snapshot = await reader(SYMBOL)
    assert snapshot.observed_at == DURING_SESSION


async def test_reader_treats_repeated_cursor_and_page_cap_as_truncated():
    page = (
        {"ctx_area_fk100": "f", "ctx_area_nk100": "same", "output1": [], **OK_TAIL},
        {"tr_cont": "M"},
    )
    with pytest.raises(PositionUnproven, match="position_read_truncated"):
        await read_complete_domestic_balance(balance_client([page, page]))
    endless = [
        (
            {
                "ctx_area_fk100": "f",
                "ctx_area_nk100": f"n{i}",
                "output1": [],
                **OK_TAIL,
            },
            {"tr_cont": "M"},
        )
        for i in range(11)
    ]
    with pytest.raises(PositionUnproven, match="position_read_truncated"):
        await read_complete_domestic_balance(balance_client(endless))


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


def store_with_baseline(baseline, committed_fills=(1,)):
    session = MagicMock()
    session.execute = AsyncMock(return_value=_Result(baseline))
    session.scalars = AsyncMock(
        return_value=[
            SimpleNamespace(
                kind="terminal_reconciliation", payload={"filled_quantity": value}
            )
            for value in committed_fills
        ]
    )
    session.commit = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    store = KISMockEvidenceStore(lambda: context, entry=bound_entry())
    store._lineage = AsyncMock(return_value=SimpleNamespace(order_attempt_id="a"))
    store._attributed_odno = AsyncMock(return_value=ODNO)
    store._append = AsyncMock()
    return store


PROVEN = TerminalClaimEvidence(
    lane_native_terminal_evidence=True,
    account_position_reconciled=True,
    remainder_known=True,
)


def evidence(proof, filled=1):
    return NativeOrderEvidence(
        "terminal_reconciliation", "k", ODNO, ODNO, PROVEN, filled, proof
    )


async def test_store_refuses_a_position_bit_without_proof():
    _, envelope = _attempt_envelope()
    store = store_with_baseline(SimpleNamespace(symbol=SYMBOL, quantity=5))
    with pytest.raises(KISMockEvidenceConflict, match="position_proof_required"):
        await store.record_native_evidence(envelope, evidence(None))
    store._append.assert_not_awaited()


@pytest.mark.parametrize(
    "proof",
    [
        PositionProof("buy", 5, 1, 7, "d" * 64, DURING_SESSION),
        PositionProof("buy", 4, 1, 5, "d" * 64, DURING_SESSION),
        PositionProof("sell", 5, 1, 4, "d" * 64, DURING_SESSION),
    ],
)
async def test_store_recomputes_the_proof_against_the_stored_baseline(proof):
    _, envelope = _attempt_envelope()
    store = store_with_baseline(SimpleNamespace(symbol=SYMBOL, quantity=5))
    with pytest.raises(KISMockEvidenceConflict, match="position_proof_mismatch"):
        await store.record_native_evidence(envelope, evidence(proof))
    store._append.assert_not_awaited()


async def test_store_accepts_a_proof_that_recomputes():
    _, envelope = _attempt_envelope()
    store = store_with_baseline(SimpleNamespace(symbol=SYMBOL, quantity=5))
    await store.record_native_evidence(
        envelope, evidence(PositionProof("buy", 5, 1, 6, "d" * 64, DURING_SESSION))
    )
    payload = store._append.await_args.kwargs["payload"]
    assert payload["position_proof"]["observed_quantity"] == 6
    assert payload["filled_quantity"] == 1


def lineage():
    _, envelope = _attempt_envelope()
    return KISMockRecoveryLineage(envelope, ORDERED, ORDERED)


@pytest.mark.parametrize(
    ("baseline", "reason"),
    [
        (None, "position_baseline_unavailable"),
        (
            SimpleNamespace(
                symbol="000660", quantity=1, created_at=ORDERED, observed_at=ORDERED
            ),
            "position_baseline_unavailable",
        ),
        (
            SimpleNamespace(
                symbol=SYMBOL,
                quantity=1,
                created_at=ORDERED + timedelta(seconds=1),
                observed_at=ORDERED,
            ),
            "position_baseline_stale",
        ),
        (
            SimpleNamespace(
                symbol=SYMBOL,
                quantity=1,
                created_at=ORDERED - timedelta(seconds=1),
                observed_at=ORDERED - timedelta(seconds=301),
            ),
            "position_baseline_stale",
        ),
    ],
)
async def test_baseline_must_exactly_precede_the_claim(baseline, reason):
    with pytest.raises(PositionUnproven) as caught:
        await store_with_baseline(baseline).load_position_baseline(lineage())
    assert caught.value.reason == reason


async def test_baseline_committed_just_before_the_claim_is_returned():
    baseline = SimpleNamespace(
        symbol=SYMBOL,
        quantity=4,
        created_at=ORDERED - timedelta(seconds=1),
        observed_at=ORDERED - timedelta(seconds=300),
    )
    assert await store_with_baseline(baseline).load_position_baseline(lineage()) == 4


async def test_baseline_rejects_a_snapshot_for_another_symbol():
    _, envelope = _attempt_envelope()
    store = store_with_baseline(None)
    snapshot = BalanceSnapshot("000660", 1, DURING_SESSION, "d" * 64)
    with pytest.raises(KISMockEvidenceConflict):
        await store.record_position_baseline(envelope, snapshot)


async def test_store_refuses_a_proof_that_contradicts_committed_fills():
    _, envelope = _attempt_envelope()
    store = store_with_baseline(
        SimpleNamespace(symbol=SYMBOL, quantity=5), committed_fills=(5,)
    )
    with pytest.raises(KISMockEvidenceConflict, match="position_proof_mismatch"):
        await store.record_native_evidence(
            envelope, evidence(PositionProof("buy", 5, 1, 6, "d" * 64, DURING_SESSION))
        )
    store._append.assert_not_awaited()


@pytest.mark.parametrize("partial", [{"filled_quantity": 1}, {}])
async def test_terminal_quantity_below_committed_partial_cannot_prove_position(partial):
    _, envelope = _attempt_envelope()
    store = store_with_baseline(SimpleNamespace(symbol=SYMBOL, quantity=5))
    session = store._session_factory().__aenter__.return_value
    session.scalars.return_value = [
        SimpleNamespace(kind="partial_fill", payload=partial),
        SimpleNamespace(kind="terminal_reconciliation", payload={"filled_quantity": 0}),
    ]
    with pytest.raises(KISMockEvidenceConflict, match="position_proof_mismatch"):
        await store.record_native_evidence(
            envelope,
            evidence(PositionProof("buy", 5, 0, 5, "d" * 64, DURING_SESSION), filled=0),
        )
    store._append.assert_not_awaited()


async def test_regressing_partial_observations_remain_unproven():
    _, envelope = _attempt_envelope()
    store = store_with_baseline(SimpleNamespace(symbol=SYMBOL, quantity=5))
    session = store._session_factory().__aenter__.return_value
    session.scalars.return_value = [
        SimpleNamespace(kind="partial_fill", payload={"filled_quantity": 2}),
        SimpleNamespace(kind="partial_fill", payload={"filled_quantity": 1}),
        SimpleNamespace(kind="terminal_reconciliation", payload={"filled_quantity": 2}),
    ]
    with pytest.raises(KISMockEvidenceConflict, match="position_proof_mismatch"):
        await store.record_native_evidence(
            envelope,
            evidence(PositionProof("buy", 5, 2, 7, "d" * 64, DURING_SESSION), filled=2),
        )
    store._append.assert_not_awaited()


async def test_partial_after_terminal_readback_remains_unproven():
    _, envelope = _attempt_envelope()
    store = store_with_baseline(SimpleNamespace(symbol=SYMBOL, quantity=5))
    session = store._session_factory().__aenter__.return_value
    session.scalars.return_value = [
        SimpleNamespace(kind="terminal_reconciliation", payload={"filled_quantity": 2}),
        SimpleNamespace(kind="partial_fill", payload={"filled_quantity": 1}),
    ]
    with pytest.raises(KISMockEvidenceConflict, match="position_proof_mismatch"):
        await store.record_native_evidence(
            envelope,
            evidence(PositionProof("buy", 5, 2, 7, "d" * 64, DURING_SESSION), filled=2),
        )
    store._append.assert_not_awaited()


async def test_repeated_partial_readbacks_keep_their_observation_order(ctx):
    _, envelope = _attempt_envelope(plan_overrides={"quantity": Decimal("3")})
    claim = RediscoveredKISMockClaim(
        DurableClaim(7, "scope", envelope.order_attempt.idempotency_key, "buy"),
        envelope.order_attempt.order_attempt_id,
        ODNO,
        True,
    )
    ctx.store.rediscover_unreleased_claims.return_value = (claim,)
    ctx.store.load_recovery_lineage.return_value = KISMockRecoveryLineage(
        envelope, ORDERED - timedelta(minutes=1), ORDERED + timedelta(seconds=1)
    )
    for filled in (1, 2, 1):
        ctx.row.update(ord_qty="3", tot_ccld_qty=str(filled), rmn_qty=str(3 - filled))
        _, result = await run(ctx)
        assert result["status"] == "retained"
    partials = [event for event in ctx.events if event.kind == "partial_fill"]
    assert [event.filled_quantity for event in partials] == [1, 2, 1]
    assert len({event.broker_evidence_key for event in partials}) == 3


async def test_reader_accepts_an_eight_digit_mock_account():
    last = ({"output1": [], **OK_TAIL}, {"tr_cont": "D"})
    client = balance_client([last])
    client._settings.kis_account_no = "12345678"
    await read_complete_domestic_balance(client)
    params = client._request_with_rate_limit_with_headers.await_args.kwargs["params"]
    assert (params["CANO"], params["ACNT_PRDT_CD"]) == ("12345678", "01")


async def test_reader_close_failure_keeps_the_claim_report(ctx):
    position_reader = MagicMock(close=AsyncMock(side_effect=RuntimeError("close")))
    position_reader.side_effect = None
    ctx.store.position_reader = MagicMock(return_value=position_reader)
    ctx.store.load_position_baseline.side_effect = PositionUnproven(
        "position_baseline_unavailable"
    )
    result = await KISMockLifecycleService(MagicMock()).recover_dispatch_claims(
        store=ctx.store,
        claims=MagicMock(),
        reader=AsyncMock(return_value=ctx.row),
        dry_run=False,
        confirm=True,
        record_dispatch_evidence=True,
    )
    assert result["position_reader_close"] == "failed"
    assert result["claims"][0]["status"] == "retained"
