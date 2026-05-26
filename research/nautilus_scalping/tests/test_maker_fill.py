# research/nautilus_scalping/tests/test_maker_fill.py
"""ROB-324 — pure maker/limit-fill scenario builders.

Records in, validated_gate.Trade lists out. No nautilus; fully deterministic."""
from __future__ import annotations

from maker_fill import (
    MAKER_FEE_BPS,
    TAKER_BASELINE_BPS,
    MakerTradeRecord,
)


def test_module_constants_match_demo_fees() -> None:
    assert MAKER_FEE_BPS == 2.0
    assert TAKER_BASELINE_BPS == 4.0


def _rec(net, comm, notional, ts, *, filled=True, tp_hit=True, adverse=0.0) -> MakerTradeRecord:
    return MakerTradeRecord(
        net_at_real_fees=net, commission_real=comm, notional=notional,
        ts_opened=ts, filled=filled, tp_hit=tp_hit, adverse_excursion_bps=adverse,
    )


def test_record_is_frozen_dataclass() -> None:
    r = _rec(1.0, 0.04, 100.0, 0)
    assert r.net_at_real_fees == 1.0 and r.filled is True
