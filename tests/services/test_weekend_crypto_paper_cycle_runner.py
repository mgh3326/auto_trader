"""Unit tests for ROB-94 weekend crypto Alpaca Paper cycle runner.

All tests are pure and broker-free. Dependencies are async mocks/stubs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.weekend_crypto_paper_cycle_runner import (
    ALLOWED_SIGNAL_TO_EXECUTION_SYMBOL,
    MAX_CANDIDATES,
    MAX_NOTIONAL_USD,
    CryptoCycleCandidate,
    CycleGateError,
    WeekendCryptoPaperCycleRunner,
    _safe_report_dict,
)

_NOW = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)


def _make_candidate(
    *,
    candidate_uuid: str | None = None,
    signal_symbol: str = "KRW-BTC",
    execution_symbol: str = "BTC/USD",
    notional: Decimal = Decimal("10"),
    order_type: str = "limit",
    signal_venue: str = "upbit",
    execution_venue: str = "alpaca_paper",
    execution_asset_class: str = "crypto",
    limit_price: Decimal = Decimal("1.00"),
    time_in_force: str = "gtc",
    lifecycle_correlation_id: str | None = None,
) -> CryptoCycleCandidate:
    uid = candidate_uuid or str(uuid.uuid4())
    return CryptoCycleCandidate(
        candidate_uuid=uid,
        signal_symbol=signal_symbol,
        signal_venue=signal_venue,
        execution_symbol=execution_symbol,
        execution_venue=execution_venue,
        execution_asset_class=execution_asset_class,
        order_type=order_type,
        notional=notional,
        limit_price=limit_price,
        time_in_force=time_in_force,
        lifecycle_correlation_id=lifecycle_correlation_id or f"corr-{uid[:8]}",
    )


def _loader_for(*candidates: CryptoCycleCandidate):
    async def _load(*, symbols, max_candidates):
        filtered = candidates
        if symbols:
            filtered = tuple(c for c in candidates if c.execution_symbol in symbols)
        return list(filtered)[:max_candidates]

    return _load


def _noop_preflight(
    *, expected_signal_symbol=None, expected_execution_symbol=None, now=None
):
    report = MagicMock()
    report.should_block = False
    report.anomalies = []
    return report


def _blocking_preflight(
    *, expected_signal_symbol=None, expected_execution_symbol=None, now=None
):
    from app.services.alpaca_paper_anomaly_checks import (
        PaperExecutionAnomaly,
        PaperExecutionAnomalySeverity,
    )

    anomaly = PaperExecutionAnomaly(
        check_id="unexpected_open_orders",
        severity=PaperExecutionAnomalySeverity.block,
        summary="open orders found",
        details={},
    )
    report = MagicMock()
    report.should_block = True
    report.anomalies = [anomaly]
    return report


async def _noop_freshness(packet, *, now):
    return None


async def _noop_idempotency(packet, *, ledger):
    return None


async def _noop_sell_source(packet, *, ledger):
    return None


async def _noop_preview(**kwargs):
    return {"dry_run": True, "symbol": kwargs.get("symbol"), "side": kwargs.get("side")}


def _make_mock_ledger():
    ledger = MagicMock()
    for name in [
        "record_plan",
        "record_preview",
        "record_validation_attempt",
        "record_submit",
        "record_status",
        "record_position_snapshot",
        "record_sell_validation",
        "record_close",
        "record_final_reconcile",
    ]:
        setattr(ledger, name, AsyncMock(return_value=MagicMock()))
    return ledger


def _make_submit_fn():
    async def _submit(**kwargs):
        return {
            "status": "accepted",
            "id": "broker-order-001",
            "operator_token": "SECRET",
        }

    return _submit


def _make_fill_read_fn(*, filled: bool = True):
    async def _fill(client_order_id: str):
        if not filled:
            return None
        return {
            "order_id": "broker-order-001",
            "filled_qty": "0.001",
            "filled_avg_price": "50000.00",
            "position": {"symbol": "BTC/USD", "qty": "0.001"},
        }

    return _fill


def _make_report_service():
    svc = MagicMock()
    svc.build_report = AsyncMock(
        return_value=MagicMock(
            model_dump=lambda: {
                "lifecycle_correlation_id": "corr-test",
                "status": "complete",
                "api_key": "SECRET",
            }
        )
    )
    return svc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_stops_before_submit():
    submit_fn = AsyncMock()
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate()),
        preview_fn=_noop_preview,
        submit_fn=submit_fn,
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(dry_run=True, now=_NOW)
    submit_fn.assert_not_called()
    assert report.status == "dry_run_ok"
    assert report.traces[0].final_state == "validated"
    assert (
        next(s for s in report.traces[0].stages if s.stage == "execute_gate").status
        == "skipped"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_requires_confirm_and_operator_gate():
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate())
    )
    with pytest.raises(CycleGateError):
        await runner.run_cycle(
            dry_run=False, confirm=False, approval_tokens={}, now=_NOW
        )
    with pytest.raises(CycleGateError):
        await runner.run_cycle(
            dry_run=False, confirm=True, approval_tokens={}, now=_NOW
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_requires_candidate_approval_token():
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate()),
        preview_fn=_noop_preview,
        submit_fn=_make_submit_fn(),
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(
        dry_run=False,
        confirm=True,
        operator_token="operator",
        approval_tokens={},
        now=_NOW,
    )
    assert report.traces[0].final_state == "gate_blocked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_should_block_records_anomaly_and_stops_candidate():
    submit_fn = AsyncMock()
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate()),
        preview_fn=_noop_preview,
        submit_fn=submit_fn,
        preflight_fn=_blocking_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(dry_run=True, now=_NOW)
    submit_fn.assert_not_called()
    assert report.traces[0].final_state == "anomaly"
    assert report.traces[0].anomalies
    assert (
        next(s for s in report.traces[0].stages if s.stage == "preflight").status
        == "blocked"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_blocks_are_candidate_isolated_when_safe():
    calls = 0

    def _sometimes_blocking(**kwargs):
        nonlocal calls
        calls += 1
        return (
            _blocking_preflight(**kwargs) if calls == 1 else _noop_preflight(**kwargs)
        )

    c1 = _make_candidate(candidate_uuid="aaa-" + "0" * 28)
    c2 = _make_candidate(
        candidate_uuid="bbb-" + "0" * 28,
        signal_symbol="KRW-ETH",
        execution_symbol="ETH/USD",
    )
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(c1, c2),
        preview_fn=_noop_preview,
        preflight_fn=_sometimes_blocking,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(dry_run=True, max_candidates=3, now=_NOW)
    assert [t.final_state for t in report.traces] == ["anomaly", "validated"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_packet_idempotency_blocks_duplicate_submit():
    async def _duplicate(packet, *, ledger):
        raise RuntimeError("duplicate client_order_id")

    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate()),
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_duplicate,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(dry_run=True, now=_NOW)
    assert report.traces[0].final_state == "anomaly"
    assert "duplicate" in report.traces[0].anomalies[0]["check_id"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_packet_freshness_blocks_stale_packet():
    async def _stale(packet, *, now):
        raise RuntimeError("stale approval packet")

    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate()),
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_stale,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(dry_run=True, now=_NOW)
    assert report.traces[0].final_state == "anomaly"
    assert report.traces[0].anomalies[0]["check_id"] == "stale_packet"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_source_mismatch_blocks_sell():
    async def _bad_sell_source(packet, *, ledger):
        raise RuntimeError("source mismatch")

    candidate = _make_candidate()
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(candidate),
        preview_fn=_noop_preview,
        submit_fn=_make_submit_fn(),
        fill_read_fn=_make_fill_read_fn(),
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
        sell_source_fn=_bad_sell_source,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(
        dry_run=False,
        confirm=True,
        operator_token="operator",
        approval_tokens={candidate.candidate_uuid: "buy"},
        now=_NOW,
    )
    assert report.traces[0].final_state == "anomaly"
    assert any(
        a["check_id"] == "sell_source_mismatch" for a in report.traces[0].anomalies
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_submit_failure_records_anomaly_instead_of_raising():
    candidate = _make_candidate()
    calls: list[str] = []

    async def _submit(**kwargs):
        calls.append(kwargs["side"])
        if kwargs["side"] == "sell":
            raise RuntimeError("sell broker unavailable")
        return {"status": "accepted", "id": "buy-order-001"}

    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(candidate),
        preview_fn=_noop_preview,
        submit_fn=_submit,
        fill_read_fn=_make_fill_read_fn(),
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
        sell_source_fn=_noop_sell_source,
        ledger_service=_make_mock_ledger(),
    )
    report = await runner.run_cycle(
        dry_run=False,
        confirm=True,
        operator_token="operator",
        approval_tokens={
            candidate.candidate_uuid: "buy",
            f"{candidate.candidate_uuid}:sell": "sell",
        },
        now=_NOW,
    )
    assert calls == ["buy", "sell"]
    assert report.traces[0].final_state == "anomaly"
    assert any(
        a["check_id"] == "sell_submit_or_reconcile_error"
        for a in report.traces[0].anomalies
    )
    assert (
        next(s for s in report.traces[0].stages if s.stage == "sell_submit").status
        == "error"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_candidates_cap_enforced():
    candidates = [_make_candidate(candidate_uuid=f"{i:032d}") for i in range(5)]
    runner = WeekendCryptoPaperCycleRunner(candidate_loader=_loader_for(*candidates))
    report = await runner.run_cycle(dry_run=True, max_candidates=99, now=_NOW)
    assert report.candidates_seen == MAX_CANDIDATES
    assert len(report.traces) == MAX_CANDIDATES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_symbol_allowlist_enforced():
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate(execution_symbol="XRP/USD"))
    )
    report = await runner.run_cycle(dry_run=True, now=_NOW)
    assert report.traces[0].final_state == "cap_blocked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_symbol_allowlist_allows_all_three():
    candidates = [
        _make_candidate(signal_symbol=signal, execution_symbol=sym)
        for signal, sym in sorted(ALLOWED_SIGNAL_TO_EXECUTION_SYMBOL.items())
    ]
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(*candidates),
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
    )
    report = await runner.run_cycle(dry_run=True, max_candidates=3, now=_NOW)
    assert [trace.final_state for trace in report.traces] == ["validated"] * 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notional_cap_enforced():
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(
            _make_candidate(notional=MAX_NOTIONAL_USD + Decimal("0.01"))
        )
    )
    report = await runner.run_cycle(dry_run=True, now=_NOW)
    assert report.traces[0].final_state == "cap_blocked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_only_enforced():
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(_make_candidate(order_type="market"))
    )
    report = await runner.run_cycle(dry_run=True, now=_NOW)
    assert report.traces[0].final_state == "cap_blocked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_roundtrip_report_called_after_final_reconcile():
    candidate = _make_candidate()
    report_service = _make_report_service()
    runner = WeekendCryptoPaperCycleRunner(
        candidate_loader=_loader_for(candidate),
        preview_fn=_noop_preview,
        submit_fn=_make_submit_fn(),
        fill_read_fn=_make_fill_read_fn(),
        preflight_fn=_noop_preflight,
        packet_freshness_fn=_noop_freshness,
        packet_idempotency_fn=_noop_idempotency,
        sell_source_fn=_noop_sell_source,
        ledger_service=_make_mock_ledger(),
        report_service=report_service,
    )
    report = await runner.run_cycle(
        dry_run=False,
        confirm=True,
        operator_token="operator",
        approval_tokens={
            candidate.candidate_uuid: "buy",
            f"{candidate.candidate_uuid}:sell": "sell",
        },
        now=_NOW,
    )
    assert report.traces[0].final_state == "final_reconciled"
    report_service.build_report.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_redacts_or_omits_sensitive_fields():
    safe = _safe_report_dict(
        {
            "status": "ok",
            "api_key": "secret",
            "nested": {"operator_token": "secret", "visible": "ok"},
        }
    )
    assert safe["api_key"] == "[REDACTED]"
    assert safe["nested"]["operator_token"] == "[REDACTED]"
    assert safe["nested"]["visible"] == "ok"
