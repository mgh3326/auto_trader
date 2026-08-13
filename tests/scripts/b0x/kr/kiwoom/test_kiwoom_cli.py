"""CLI mode selection stays explicit and defaults to the non-mutating path."""

from __future__ import annotations

import pytest

from scripts.run_b0x_kr_kiwoom_cycle import _parse_args, _run

pytestmark = pytest.mark.unit


def test_cli_defaults_to_preview_and_preserves_acceptance_form() -> None:
    preview = _parse_args([])
    acceptance = _parse_args(["--confirm"])
    ordering = _parse_args(["--ordering", "--confirm"])

    assert preview.confirm is False
    assert preview.ordering is False
    assert acceptance.confirm is True
    assert acceptance.ordering is False
    assert ordering.confirm is True
    assert ordering.ordering is True
    assert set(vars(ordering)) == {
        "table_dir",
        "out_dir",
        "readiness",
        "confirm",
        "ordering",
        "now",
        "json",
    }


@pytest.mark.asyncio
async def test_cli_rejects_ordering_without_per_call_confirmation() -> None:
    """This returns before any cycle/account work, so it cannot reach a broker."""

    assert await _run(_parse_args(["--ordering"])) == 2


@pytest.mark.asyncio
async def test_cli_rejects_ordering_replay_clock_before_any_cycle_work() -> None:
    assert (
        await _run(
            _parse_args(
                [
                    "--ordering",
                    "--confirm",
                    "--now",
                    "2026-08-10T02:00:00+00:00",
                ]
            )
        )
        == 2
    )
