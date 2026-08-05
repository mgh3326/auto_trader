from __future__ import annotations

import argparse

import pytest

from scripts.kis_mock_runner import _parse_args, _run


def test_cli_exposes_no_hard_envelope_override_flags() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--once", "--max-single-notional-krw", "999999999"])
    assert exc_info.value.code == 2


def test_cli_requires_explicit_foreground_mode() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args([])
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_rearm_cli_needs_env_gate_confirmation_and_audit_actor() -> None:
    args = argparse.Namespace(
        rearm=True,
        confirm=False,
        updated_by="operator-a",
        once=False,
        loop=False,
        tag="test",
    )
    assert await _run(args, environment={}) == 2

    args.confirm = True
    args.updated_by = ""
    assert await _run(args, environment={"KIS_MOCK_RUNNER_REARM_ENABLED": "true"}) == 2
