"""CLI mode selection stays explicit and defaults to the non-mutating path."""

from __future__ import annotations

import pytest

from scripts.run_b0x_kr_kiwoom_cycle import _parse_args, _run

pytestmark = pytest.mark.unit


def test_cli_defaults_to_preview_and_preserves_acceptance_form() -> None:
    preview = _parse_args([])
    acceptance = _parse_args(["--confirm"])
    interim = _parse_args(["--interim-ordering", "--confirm"])

    assert preview.confirm is False
    assert preview.interim_ordering is False
    assert acceptance.confirm is True
    assert acceptance.interim_ordering is False
    assert interim.confirm is True
    assert interim.interim_ordering is True


@pytest.mark.asyncio
async def test_cli_rejects_interim_ordering_without_per_call_confirmation() -> None:
    """This returns before any cycle/account work, so it cannot reach a broker."""

    assert await _run(_parse_args(["--interim-ordering"])) == 2


@pytest.mark.asyncio
async def test_cli_rejects_interim_replay_clock_before_any_cycle_work() -> None:
    assert (
        await _run(
            _parse_args(
                [
                    "--interim-ordering",
                    "--confirm",
                    "--now",
                    "2026-08-10T02:00:00+00:00",
                ]
            )
        )
        == 2
    )
