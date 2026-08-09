"""KR CLI — no flag or env var can reach an envelope value (contract §4)."""

from __future__ import annotations

import pytest

from scripts.b0x.envelope import KR_MOCK_ENVELOPE, assert_envelope_locked, load_envelope
from scripts.run_b0x_kr_cycle import _parse_args, _run

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


def test_cli_confirm_is_explicit_and_default_disabled() -> None:
    """The only mutation lever is opt-in and carries no envelope override."""

    assert _parse_args([]).confirm is False
    assert _parse_args(["--confirm"]).confirm is True


@pytest.mark.asyncio
async def test_confirm_rejects_replay_clock_before_any_cycle_work() -> None:
    args = _parse_args(["--confirm", "--now", "2026-08-10T02:00:00+00:00"])
    assert await _run(args) == 2


@pytest.mark.asyncio
async def test_confirm_rejects_derivation_only_before_any_cycle_work() -> None:
    args = _parse_args(["--confirm", "--derivation-only"])
    assert await _run(args) == 2


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
