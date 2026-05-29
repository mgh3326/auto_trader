"""ROB-369 E12 — deterministic SymbolStage tests.

Per-symbol ``symbol`` snapshots are captured by the collector but no stage
consumed them, so per-symbol context never reached Hermes (orphaned in every
market). SymbolStage surfaces the captured snapshots into stage_inputs.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.symbol import SymbolStage


def _snap(kind, payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(), snapshot_kind=kind, payload_json=payload
    )


def _ctx(symbol_payloads, *, portfolio=None):
    by_kind = {"symbol": [_snap("symbol", p) for p in symbol_payloads]}
    if portfolio is not None:
        by_kind["portfolio"] = [_snap("portfolio", portfolio)]
    return StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind=by_kind,
        bundle_metadata={},
        market="crypto",
    )


def test_symbol_stage_registered_in_default_v1_stages():
    # The capture→synthesis fix is only live once the stage runs in the
    # default Hermes context pipeline.
    from app.services.investment_stages.stages.registry import get_default_v1_stages

    types = {s.stage_type for s in get_default_v1_stages()}
    assert "symbol" in types


@pytest.mark.asyncio
async def test_symbol_stage_unavailable_without_symbol_snapshots():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={}
    )
    with pytest.raises(UnavailableStageError):
        await SymbolStage().run(ctx)


@pytest.mark.asyncio
async def test_symbol_stage_unavailable_when_only_empty_payload():
    # The "no symbols supplied" collector path emits an unavailable snapshot
    # with neither ``symbol`` nor ``missing_symbols`` → nothing to synthesize.
    ctx = _ctx([{}])
    with pytest.raises(UnavailableStageError):
        await SymbolStage().run(ctx)


@pytest.mark.asyncio
async def test_symbol_stage_surfaces_resolved_symbols():
    ctx = _ctx(
        [
            {"symbol": "KRW-BTC", "name": "비트코인", "is_active": True},
            {"symbol": "KRW-ETH", "name": "이더리움", "is_active": True},
        ]
    )
    payload = await SymbolStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert "심볼 2건" in (payload.summary or "")
    assert any("KRW-BTC" in kp for kp in payload.key_points)
    assert any("KRW-ETH" in kp for kp in payload.key_points)
    assert len(payload.cited_snapshots) == 2
    assert all(c.snapshot_kind == "symbol" for c in payload.cited_snapshots)
    assert payload.confidence == 60


@pytest.mark.asyncio
async def test_symbol_stage_marks_held_symbols_with_krw_normalization():
    # Held ticker "BTC" (bare, from the Upbit reader) must match a "KRW-BTC"
    # symbol snapshot via KRW- normalization.
    ctx = _ctx(
        [
            {"symbol": "KRW-BTC", "name": "비트코인"},
            {"symbol": "KRW-SOL", "name": "솔라나"},
        ],
        portfolio={"holdings": [{"ticker": "BTC"}], "reference_holdings": []},
    )
    payload = await SymbolStage().run(ctx)
    btc_kp = next(kp for kp in payload.key_points if "KRW-BTC" in kp)
    sol_kp = next(kp for kp in payload.key_points if "KRW-SOL" in kp)
    assert "보유" in btc_kp
    assert "관심" in sol_kp
    assert "KRW-BTC" in (payload.summary or "")


@pytest.mark.asyncio
async def test_symbol_stage_surfaces_quote_liquidity_when_present():
    ctx = _ctx(
        [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "quote": {"status": "ok", "spread_bps": 4.2},
            },
        ]
    )
    payload = await SymbolStage().run(ctx)
    assert any("스프레드=4.2bps" in kp for kp in payload.key_points)


@pytest.mark.asyncio
async def test_symbol_stage_reports_unresolved_symbols_in_missing_data():
    ctx = _ctx(
        [
            {"symbol": "KRW-BTC", "name": "비트코인"},
            {"missing_symbols": ["DOGECOIN", "FOO"]},
        ]
    )
    payload = await SymbolStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert any("DOGECOIN" in m for m in payload.missing_data)
    assert "심볼 1건" in (payload.summary or "")
