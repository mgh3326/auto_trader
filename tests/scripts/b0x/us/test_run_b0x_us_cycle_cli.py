"""US CLI cannot expose confirmation or envelope-shaped controls."""

from __future__ import annotations

import pytest

from scripts.b0x.envelope import US_ALPACA_PAPER_LAB_ENVELOPE, assert_envelope_locked
from scripts.run_b0x_us_cycle import _parse_args

pytestmark = pytest.mark.unit


def test_us_cli_has_no_confirm_or_envelope_dial() -> None:
    dests = set(vars(_parse_args([])))
    assert "confirm" not in dests
    envelope_fields = set(US_ALPACA_PAPER_LAB_ENVELOPE.canonical())
    assert not (dests & envelope_fields)
    forbidden = ("notional", "cap", "limit", "envelope", "max_", "loss")
    assert [
        dest for dest in dests if any(token in dest.lower() for token in forbidden)
    ] == []
    assert_envelope_locked(US_ALPACA_PAPER_LAB_ENVELOPE)
