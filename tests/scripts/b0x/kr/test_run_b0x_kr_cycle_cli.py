"""KR CLI — no flag or env var can reach an envelope value (contract §4)."""

from __future__ import annotations

import pytest

from scripts.b0x.envelope import KR_MOCK_ENVELOPE, assert_envelope_locked, load_envelope
from scripts.run_b0x_kr_cycle import _parse_args

pytestmark = pytest.mark.unit


def test_no_cli_flag_can_reach_an_envelope_value() -> None:
    envelope_fields = set(KR_MOCK_ENVELOPE.canonical())
    args = _parse_args([])
    dests = set(vars(args))
    assert not (dests & envelope_fields), (
        f"CLI exposes a dest colliding with an envelope field: {dests & envelope_fields}"
    )
    forbidden_substrings = ("notional", "cap", "limit", "envelope", "max_", "loss")
    offenders = [
        dest
        for dest in dests
        if any(token in dest.lower() for token in forbidden_substrings)
    ]
    assert offenders == [], f"CLI exposes cap-shaped flags: {offenders}"


def test_cli_exposes_no_confirm_flag() -> None:
    """Unlike the crypto sidecar CLI, this one has no ``--confirm`` at all —
    submission is unwired (scripts.b0x.kr.mock), so a flag that implied
    "dispatch this" would be misleading.
    """

    dests = set(vars(_parse_args([])))
    assert "confirm" not in dests


def test_environment_variables_cannot_move_the_kr_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "B0X_KR_PER_ORDER_NOTIONAL",
        "B0X_KR_MAX_CONCURRENT_POSITIONS",
        "B0X_KR_DAILY_LOSS_KILL",
        "B0X_ENVELOPE",
        "PER_ORDER_NOTIONAL",
    ):
        monkeypatch.setenv(name, "99999")
    assert load_envelope("kr") == KR_MOCK_ENVELOPE
    assert_envelope_locked(load_envelope("kr"))
