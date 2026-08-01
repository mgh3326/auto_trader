from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from scripts.krb1_p0_liquidity_selector import main, parse_args

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))


def _run_stub_selected(*, as_of_session, target_session, decision_at):
    return {
        "status": "selected",
        "as_of_session": as_of_session.isoformat(),
        "target_session": target_session.isoformat(),
        "decision_at": decision_at.isoformat(),
        "selected_candidates": [{"market": "KOSPI"}, {"market": "KOSDAQ"}],
        "fail_close_reasons": [],
    }


def _run_stub_fail_closed(*, as_of_session, target_session, decision_at):
    return {
        "status": "fail_closed",
        "as_of_session": as_of_session.isoformat(),
        "target_session": target_session.isoformat(),
        "decision_at": decision_at.isoformat(),
        "selected_candidates": [],
        "fail_close_reasons": [
            {"scope": "global", "gate": "test", "reason": "fail_closed"}
        ],
    }


def _run_stub_crash(*, as_of_session, target_session, decision_at):
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_fail_closed_exits_2() -> None:
    with patch(
        "scripts.krb1_p0_liquidity_selector.run", side_effect=_run_stub_fail_closed
    ):
        exit_code = await main(
            [
                "--as-of-session",
                "2026-07-29",
                "--target-session",
                "2026-07-30",
                "--decision-at",
                "2026-07-29T18:00:00+09:00",
            ]
        )
    assert exit_code == 2


@pytest.mark.asyncio
async def test_selected_exits_0() -> None:
    with patch(
        "scripts.krb1_p0_liquidity_selector.run", side_effect=_run_stub_selected
    ):
        exit_code = await main(
            [
                "--as-of-session",
                "2026-07-29",
                "--target-session",
                "2026-07-30",
                "--decision-at",
                "2026-07-29T18:00:00+09:00",
            ]
        )
    assert exit_code == 0


@pytest.mark.parametrize(
    "status",
    ["", "SELECTED", "error", "fail_closed_partial"],
)
@pytest.mark.asyncio
async def test_only_the_exact_selected_status_exits_0(status: str) -> None:
    def run_stub(*, as_of_session, target_session, decision_at):
        return {
            "status": status,
            "selected_candidates": [],
            "fail_close_reasons": [],
        }

    with patch("scripts.krb1_p0_liquidity_selector.run", side_effect=run_stub):
        exit_code = await main(
            [
                "--as-of-session",
                "2026-07-29",
                "--target-session",
                "2026-07-30",
                "--decision-at",
                "2026-07-29T18:00:00+09:00",
            ]
        )
    assert exit_code == 2


@pytest.mark.asyncio
async def test_an_exception_inside_run_still_exits_2() -> None:
    with patch("scripts.krb1_p0_liquidity_selector.run", side_effect=_run_stub_crash):
        exit_code = await main(
            [
                "--as-of-session",
                "2026-07-29",
                "--target-session",
                "2026-07-30",
                "--decision-at",
                "2026-07-29T18:00:00+09:00",
            ]
        )
    assert exit_code == 2


def test_decision_at_is_required_and_must_carry_an_offset() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--as-of-session",
                "2026-07-29",
                "--target-session",
                "2026-07-30",
            ]
        )

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--as-of-session",
                "2026-07-29",
                "--target-session",
                "2026-07-30",
                "--decision-at",
                "2026-07-29T18:00:00",
            ]
        )
