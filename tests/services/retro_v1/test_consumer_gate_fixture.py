"""Consumer-gate fixture for retro_trades reviews and the review-element mutants (hk 3987).

The gate's fake session must return one closed trade with a linked review inside
the queried range, so the Go harness actually checks review elements. The two
review mutants must produce payloads that the closed contract schema rejects,
naming the offending field.
"""

import importlib.util
from pathlib import Path

import jsonschema
import pytest

from app.services.retro_v1.retro_trades import read_retro_trades
from tests.services.retro_v1.support import build_gate_session, validate

pytestmark = pytest.mark.unit

_GATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "ci"
    / "run_retro_consumer_gate.py"
)
_spec = importlib.util.spec_from_file_location("run_retro_consumer_gate", _GATE_PATH)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


async def _gate_trades():
    # The gate queries retro_trades with from=2026-01-01 to=2026-01-10.
    return await read_retro_trades(
        build_gate_session(), from_="2026-01-01", to="2026-01-10"
    )


@pytest.mark.asyncio
async def test_gate_session_returns_one_closed_trade_with_one_review():
    result = await _gate_trades()
    validate(result, "RetroTradesResponse")
    assert len(result["data"]) == 1
    trade = result["data"][0]
    assert len(trade["reviews"]) == 1
    review = trade["reviews"][0]
    assert review["realized_pnl"] == "6000"
    assert review["fill_evidence_available"] is True


@pytest.mark.asyncio
async def test_delete_review_field_mutant_fails_schema_naming_the_field():
    result = gate.drop_review_field(await _gate_trades())
    assert "fill_evidence_available" not in result["data"][0]["reviews"][0]
    with pytest.raises(jsonschema.ValidationError, match="fill_evidence_available"):
        validate(result, "RetroTradesResponse")


@pytest.mark.asyncio
async def test_add_review_field_mutant_fails_schema_naming_the_field():
    result = gate.add_review_field(await _gate_trades())
    assert "unexpected_field" in result["data"][0]["reviews"][0]
    with pytest.raises(jsonschema.ValidationError, match="unexpected_field"):
        validate(result, "RetroTradesResponse")
